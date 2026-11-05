/* Computer role: a USB/IP client presenting the producer's device through
 * TinyUSB (native/yusb presentation core over yoke_usbd). Everything here
 * runs in TinyUSB's task; the network task reaches it through posts. The
 * route follows ADR 0022's generation fence (native/yusb yusb_fence). */
#include <errno.h>
#include <string.h>
#include "bridge.h"
#include "box_gadget.h"
#include "box_media.h"
#include "box_pump.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include "yoke_usbd.h"
#include "yusb_fence.h"
#include "yusb_proxy.h"

static const char *TAG = "yoke-computer";

static struct {
    yusb_policy_t policy;
    yusb_fence_t fence;
    yusb_proxy_t *proxy;
    int fd;                       /* -1 */
    uint32_t socket;              /* fd's generation: readiness posts carry it */
    bool connecting, presented, stopping;
    char rejected[96];            /* this controller cannot present the device */
    /* A route that waits for the running session to stop. */
    bool route_waiting, route_detach;
    uint32_t route_generation;
    yusb_target_t route_target;
    char route_error[64];
    esp_timer_handle_t timer;
    uint64_t armed;               /* the timer's deadline (ms), UINT64_MAX: none */
} c = {.fd = -1, .armed = UINT64_MAX};

static uint64_t now_ms(void) { return (uint64_t)(esp_timer_get_time() / 1000); }

static void service(void);

static void op_log(void *ctx, const char *message)
{
    (void)ctx;
    ESP_LOGI(TAG, "%s", message);
}

/* DMA-capable internal memory, cache-line aligned on the P4. */
static void *dma_alloc(void *ctx, size_t size, size_t align)
{
    (void)ctx;
    return heap_caps_aligned_calloc(align, 1, size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
}
static void dma_free(void *ctx, void *ptr)
{
    (void)ctx;
    heap_caps_free(ptr);
}
#if CONFIG_CACHE_L1_CACHE_LINE_SIZE
static const yusb_buffers_t buffers = {CONFIG_CACHE_L1_CACHE_LINE_SIZE, dma_alloc, dma_free, NULL};
#else
static const yusb_buffers_t buffers = {4, dma_alloc, dma_free, NULL};
#endif

/* ---- transport ---- */

static size_t io_rx_space(void *core, uint8_t **buf) { return yusb_proxy_rx_space(core, buf); }
static int io_rx_done(void *core, size_t n, uint64_t t) { return yusb_proxy_rx_done(core, n, t); }
static size_t io_tx_iov(const void *core, yusb_iov_t *iov, size_t max)
{
    return yusb_proxy_tx_iov(core, iov, max);
}
static void io_tx_done(void *core, size_t n) { yusb_proxy_tx_done(core, n); }
static const box_core_io_t io = {io_rx_space, io_rx_done, io_tx_iov, io_tx_done};

static void ready(void *arg);

static void close_socket(void)
{
    if (c.fd < 0) return;
    box_net_unwatch(c.fd);
    close(c.fd);
    c.fd = -1;
}

static void present(void)
{
    if (box_gadget_present(c.proxy, c.rejected, sizeof(c.rejected))) {
        c.presented = true;
        return;
    }
    ESP_LOGE(TAG, "device not presentable here: %s", c.rejected);
    yusb_proxy_stop(c.proxy, now_ms());
}

static void finish_route(void);

/* The session is stopped: release everything, then follow the fence. */
static void ended(void)
{
    yusb_proxy_status_t status;
    yusb_proxy_status(c.proxy, &status);
    close_socket(); /* the transport first, then the gadget */
    if (c.presented) box_gadget_withdraw();
    const char *rejected = c.rejected[0] ? c.rejected : status.rejected;
    ESP_LOGW(TAG, "session ended: %s%s%s", status.end_reason, *rejected ? "; " : "", rejected);
    if (status.end == YUSB_END_YX_UNAVAILABLE && c.policy.yx_levers) {
        ESP_LOGW(TAG, "YX codec could not be allocated; plain USB/IP from now on");
        c.policy.yx_levers = 0;
        c.policy.yx_cache_blocks = 0;
    }
    yusb_fence_ended(&c.fence, rejected, status.end_reason, now_ms());
    yusb_proxy_destroy(c.proxy);
    c.proxy = NULL;
    c.presented = c.stopping = false;
    c.rejected[0] = 0;
    if (c.route_waiting) finish_route();
}

static void connect_target(void)
{
    const yusb_target_t *t = &c.fence.target;
    struct sockaddr_in to = {.sin_family = AF_INET, .sin_port = htons(t->port)};
    c.fd = socket(AF_INET, SOCK_STREAM, 0);
    c.socket++;
    if (c.fd < 0 || inet_aton(t->host, &to.sin_addr) == 0 || box_pump_prepare(c.fd) ||
        (connect(c.fd, (struct sockaddr *)&to, sizeof(to)) && errno != EINPROGRESS)) {
        char why[64];
        snprintf(why, sizeof(why), "connect %.15s:%u failed: errno %d", t->host, t->port, errno);
        close_socket();
        yusb_fence_build_failed(&c.fence, YUSB_BUILD_RETRY, why, now_ms());
        return;
    }
    c.connecting = true;
    box_net_watch(c.fd, true, true, yoke_usbd_post, ready, (void *)(uintptr_t)c.socket);
}

static void connected(void)
{
    int error = 0;
    socklen_t size = sizeof(error);
    c.connecting = false;
    if (getsockopt(c.fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) {
        char why[64];
        snprintf(why, sizeof(why), "connect %.15s:%u: errno %d", c.fence.target.host,
                 c.fence.target.port, error);
        close_socket();
        yusb_fence_build_failed(&c.fence, YUSB_BUILD_RETRY, why, now_ms());
        return;
    }
    /* Budget first: a session that cannot fit is refused with a reason,
     * never half created. Endpoint buffers come at SET_CONFIGURATION; if
     * they do not fit, the core refuses the configuration ("UDC could not
     * configure") and the session ends cleanly. */
    size_t need = yusb_proxy_footprint(&c.policy, &buffers).create;
    c.proxy = box_heap_fits(need) ? yusb_proxy_create(&c.policy, box_gadget_ops(),
                                                      c.fence.target.busid, now_ms())
                                  : NULL;
    if (!c.proxy) {
        char why[96];
        snprintf(why, sizeof(why), "RAM: a session needs %u bytes (+%u reserve), %u internal free",
                 (unsigned)need, BOX_HEAP_RESERVE,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        close_socket();
        yusb_fence_build_failed(&c.fence, YUSB_BUILD_REJECTED, why, now_ms());
        return;
    }
    yusb_fence_built(&c.fence);
    ESP_LOGI(TAG, "importing %s from %s:%u", c.fence.target.busid, c.fence.target.host,
             c.fence.target.port);
}

static void pump(void)
{
    int r = box_pump(c.fd, &io, c.proxy, now_ms());
    if (r & (BOX_PUMP_CLOSED | BOX_PUMP_FAILED)) {
        close_socket();
        yusb_proxy_transport_lost(c.proxy, r & BOX_PUMP_CLOSED ? "producer closed" : "socket error",
                                  now_ms());
    } else {
        box_net_watch(c.fd, r & BOX_PUMP_READ, r & BOX_PUMP_WRITE, yoke_usbd_post, ready,
                      (void *)(uintptr_t)c.socket);
    }
}

static void tick(void *arg);

static void arm_timer(uint64_t deadline)
{
    if (deadline == c.armed) return;
    uint64_t t = now_ms();
    c.armed = deadline;
    (void)esp_timer_stop(c.timer);
    if (deadline != UINT64_MAX)
        (void)esp_timer_start_once(c.timer, ((deadline > t ? deadline - t : 0) + 1) * 1000);
}

/* After every input: advance the session's phases until they settle, move
 * bytes, follow the fence, and re-arm the timer for the next deadline. */
static void service(void)
{
    while (c.proxy) {
        yusb_phase_t phase = yusb_proxy_phase(c.proxy);
        if (phase == YUSB_PHASE_STOPPED) {
            ended();
            continue;
        }
        if (phase == YUSB_PHASE_READY && !c.presented && !c.rejected[0]) present();
        else if (phase == YUSB_PHASE_ENDED) yusb_proxy_stop(c.proxy, now_ms());
        else if (phase == YUSB_PHASE_STOPPING && !c.stopping &&
                 yusb_proxy_teardown_overdue(c.proxy, now_ms())) {
            c.stopping = true; /* the gadget stays bound: the fence holds */
            yusb_fence_hold(&c.fence, "gadget teardown unconfirmed");
        }
        if (c.fd >= 0) pump();
        if (yusb_proxy_phase(c.proxy) == phase) break;
    }
    uint64_t deadline = UINT64_MAX;
    if (c.proxy) {
        yusb_proxy_status_t status;
        yusb_proxy_status(c.proxy, &status);
        if (!c.stopping)
            yusb_fence_observe(&c.fence, yusb_wstate_from_name(status.state), status.rejected,
                               status.error);
        deadline = yusb_proxy_poll(c.proxy, now_ms());
    } else if (!c.connecting && c.fence.has_target) {
        if (yusb_fence_due(&c.fence, false, now_ms())) connect_target();
        if (!c.connecting) deadline = c.fence.next_attempt_ms;
    }
    arm_timer(deadline);
}

static void ready(void *arg)
{
    if ((uint32_t)(uintptr_t)arg != c.socket) return; /* a closed socket's late post */
    if (c.connecting) connected();
    service();
}

static void tick(void *arg)
{
    (void)arg;
    service();
}

static void timer_fired(void *arg)
{
    (void)arg;
    yoke_usbd_post(tick, NULL);
}

/* ---- route and health (posted from the network task) ---- */

/* Wire detail is bounded (YWP_DETAIL_MAX); hosts are IPv4 literals here. */
static void state_of(ywp_state_t *state, char *detail, size_t cap)
{
    const yusb_fence_t *f = &c.fence;
    const char *name = yusb_wstate_name(f->state);
    if (!f->has_target) {
        *state = YWP_IDLE;
        snprintf(detail, cap, "%.96s", f->state == YUSB_W_ERROR ? f->detail : "gadget detached");
        if (f->state == YUSB_W_ERROR) *state = YWP_ERROR;
    } else if (f->state == YUSB_W_REJECTED || f->state == YUSB_W_ERROR) {
        *state = YWP_ERROR;
        snprintf(detail, cap, "%s: %.96s", name, f->detail);
    } else if (f->state == YUSB_W_CONFIGURED || f->state == YUSB_W_SUSPENDED) {
        /* The S3 has no VBUS sense: an unplugged computer leaves the bus
         * idle, which reads as suspended (USB-TIER1.md ⚠). */
        *state = YWP_ACTIVE;
        snprintf(detail, cap, "%.31s from %.15s:%u configured at %.9s%s; use unverified",
                 f->target.busid, f->target.host, f->target.port,
                 yusb_speed_name(yoke_usbd_link_speed()),
                 f->state == YUSB_W_SUSPENDED ? ", suspended or unplugged" : "");
    } else {
        *state = YWP_PENDING;
        snprintf(detail, cap, "%s %.15s:%u/%s%s%.64s", name, f->target.host, f->target.port,
                 f->target.busid, f->detail[0] ? ": " : "", f->detail);
    }
}

static void report_route(bool completed)
{
    ywp_state_t state;
    char detail[BOX_DETAIL_MAX + 1];
    state_of(&state, detail, sizeof(detail));
    if (!completed) {
        state = YWP_ERROR;
        snprintf(detail, sizeof(detail), "%s", c.route_error);
    }
    box_net_route_done(completed, state, detail);
}

/* Adopt the waiting route once no older session is running. */
static void finish_route(void)
{
    c.route_waiting = false;
    if (c.route_error[0]) {
        report_route(false);
        return;
    }
    yusb_fence_adopt(&c.fence, c.route_generation, c.route_detach ? NULL : &c.route_target);
    report_route(true);
    service();
}

static void route_work(void *arg)
{
    (void)arg;
    c.route_waiting = true;
    if (c.connecting) { /* an attempt at the old target: abandon it */
        close_socket();
        c.connecting = false;
    }
    if (c.proxy && !c.stopping) {
        yusb_proxy_stop(c.proxy, now_ms()); /* finish_route runs when it has stopped */
        service();
    } else if (c.stopping) {
        snprintf(c.route_error, sizeof(c.route_error), "previous gadget teardown unconfirmed");
        finish_route();
    } else {
        finish_route();
    }
}

static void route(uint32_t generation, bool detach, const char *producer, const char *media,
                  size_t media_len)
{
    (void)producer;
    box_target_t target;
    const char *why = detach ? NULL : box_media_parse(media, media_len, &target);
    c.route_generation = generation;
    c.route_detach = detach;
    snprintf(c.route_error, sizeof(c.route_error), "%s", why ? why : "");
    if (!detach && !why) {
        memset(&c.route_target, 0, sizeof(c.route_target));
        snprintf(c.route_target.host, sizeof(c.route_target.host), "%u.%u.%u.%u",
                 (unsigned)(target.ipv4 >> 24), (unsigned)(target.ipv4 >> 16) & 255u,
                 (unsigned)(target.ipv4 >> 8) & 255u, (unsigned)target.ipv4 & 255u);
        c.route_target.port = target.port;
        snprintf(c.route_target.busid, sizeof(c.route_target.busid), "%s", target.busid);
    }
    /* Read only by route_work: the route queue runs one command at a time. */
    yoke_usbd_post(route_work, NULL);
}

static void poll_work(void *arg)
{
    ywp_state_t state;
    char detail[BOX_DETAIL_MAX + 1];
    state_of(&state, detail, sizeof(detail));
    box_net_poll_done((uint32_t)(uintptr_t)arg, state, detail);
}

static void poll_state(uint32_t tag) { yoke_usbd_post(poll_work, (void *)(uintptr_t)tag); }

static void admission(const uint32_t *ipv4, size_t count)
{
    (void)ipv4;
    (void)count; /* consumers admit nobody: only producers listen */
}

static esp_err_t start(void)
{
    yusb_policy_defaults(&c.policy);
#if CONFIG_IDF_TARGET_ESP32P4
    c.policy.budget = (yusb_budget_t){.max_in = 7, .max_out = 15, .max_number = 15};
    c.policy.max_transfer = 16384;
    /* A producer box serves 8 URBs at a time and stops reading its stream
     * while all of them wait for device data (control requests included):
     * four-deep bulk IN keeps a typical device (one bulk and one interrupt
     * IN) below that. Generic bulk IN moves 4 packets per round trip; BOT
     * has its own 16 KiB slots. */
    c.policy.in_depth_bulk = 4;
    c.policy.bot_in_slots = 4;
    /* Tier 1.5 carries mass storage: write-behind inside a BOT data-out
     * phase only (the CSW still reports the command's outcome). */
    c.policy.bot_out_window = 8;
    /* High-speed bulk outruns the EMAC's 100 Mbit/s, so ADR 0028's zero-block
     * elimination and LZ4 pay off when the producer agrees (+100 KB); the
     * block cache would cost another 131 KB for 16 blocks. */
    c.policy.yx_levers = YUSB_YX_FILL | YUSB_YX_LZ4;
#else
    /* DWC2 on the S3: endpoints 1-6 and four IN FIFOs (⚠ TinyUSB's limits);
     * four OUT pumps and two-deep IN queues keep the session in internal
     * RAM (lane A's measured S3 sizing). Full speed needs no deeper queue
     * for tier 1's classes; bulk IN then moves two packets per round trip. */
    c.policy.budget = (yusb_budget_t){.max_in = 4, .max_out = 4, .max_number = 6};
    c.policy.max_transfer = 4096;
    c.policy.in_depth_bulk = 2;
    c.policy.in_depth_interrupt = 2;
    c.policy.bot_in_slots = 2;
    c.policy.ep0_max = 4096;
    /* yx_levers stays 0: tier 1's transfers are mostly below YX's 512-byte
     * minimum and full speed is far below the link, so FILL (+9 KB) and LZ4
     * (+25 KB) would buy nothing; the cache does not fit. */
#endif
    /* out_window stays 1 on both: a device's OUT STALL reaches the computer
     * on the next transaction, and tier 1's OUT traffic (HID output
     * reports, serial, MIDI) needs no pipelining. */
    /* TinyUSB answers device-recipient GET_DESCRIPTOR from the cache only. */
    c.policy.prefetch_os_string = true;
    /* Below yoke_usbd's 4 s stall of an unanswered deferred request. */
    c.policy.control_timeout_ms = 3500;
    const yusb_fence_cfg_t fence = {.retry_min_ms = 250, .retry_max_ms = 2000,
                                    .retry_rejected_ms = 30000};
    yusb_fence_init(&c.fence, &fence);
    box_gadget_init(&buffers, service, now_ms, op_log);
    yusb_footprint_t need = yusb_proxy_footprint(&c.policy, &buffers);
    ESP_LOGI(TAG, "a session needs %u bytes, its endpoints up to %u more",
             (unsigned)need.create, (unsigned)need.pumps_max);
    const esp_timer_create_args_t timer = {.callback = timer_fired, .name = "usb-session"};
    esp_err_t err = esp_timer_create(&timer, &c.timer);
    return err == ESP_OK ? yoke_usbd_start() : err;
}

static void status(cJSON *usb)
{
    /* Read without the USB task's cooperation: evidence only. */
    cJSON_AddStringToObject(usb, "session", yusb_wstate_name(c.fence.state));
    cJSON_AddStringToObject(usb, "detail", c.fence.detail);
    cJSON_AddStringToObject(usb, "target", c.fence.has_target ? c.fence.target.host : "");
    cJSON_AddNumberToObject(usb, "reconnects", c.fence.reconnects);
}

const box_bridge_t box_computer = {
    .start = start, .route = route, .poll = poll_state, .admission = admission, .status = status,
};
