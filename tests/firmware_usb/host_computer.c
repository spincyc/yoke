/* Computer-role bridge glue (box_computer.c, unchanged, compiled with the P4
 * policy) over host stand-ins: connect timeout, timer re-arm after tick(),
 * YX->plain RAM fallback, RAM retry backoff, deferred box_net_close(), and
 * overdue gadget teardown completing a waiting route. HOST MODEL ONLY. */
#include <arpa/inet.h>
#include <assert.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include "box_gadget.h"
#include "box_media.h"
#include "box_pump.h"
#include "bridge.h"
#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "yoke_usbd.h"
#include "yusb_export.h"
#include "yusb_proxy.h"

/* ---- heap budget and DMA allocator stand-ins ---- */

static size_t sim_heap_free = 1024u * 1024u;
static size_t sim_heap_largest = 256u * 1024u;

size_t heap_caps_get_free_size(uint32_t caps)
{
    assert(caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    return sim_heap_free;
}
size_t heap_caps_get_largest_free_block(uint32_t caps)
{
    assert(caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    return sim_heap_largest;
}
size_t heap_caps_get_minimum_free_size(uint32_t caps)
{
    assert(caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    return sim_heap_free;
}
bool box_heap_fits(size_t need)
{
    return box_heap_budget_ok(sim_heap_free, sim_heap_largest, need);
}
void *heap_caps_aligned_calloc(size_t alignment, size_t n, size_t size, uint32_t caps)
{
    (void)caps;
    size_t total = n * size;
    if (!total) total = 1;
    if (alignment < sizeof(void *)) alignment = sizeof(void *);
    void *p = NULL;
    if (posix_memalign(&p, alignment, total)) return NULL;
    memset(p, 0, total);
    return p;
}
void heap_caps_free(void *ptr) { free(ptr); }

/* ---- timer stand-in ---- */

struct stub_timer {
    void (*callback)(void *);
    void *arg;
    bool active;
    uint64_t timeout_us;
    unsigned starts;
};
static struct stub_timer sim_timer;

esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *out)
{
    sim_timer = (struct stub_timer){.callback = args->callback, .arg = args->arg};
    *out = &sim_timer;
    return ESP_OK;
}
esp_err_t esp_timer_start_once(esp_timer_handle_t timer, uint64_t timeout_us)
{
    timer->active = true;
    timer->timeout_us = timeout_us;
    timer->starts++;
    return ESP_OK;
}
esp_err_t esp_timer_stop(esp_timer_handle_t timer)
{
    timer->active = false;
    return ESP_OK;
}

/* ---- socket stand-ins (routed from box_computer.c via idf/lwip/sockets.h) ---- */

static bool force_inprogress;
static int sim_so_error;
static unsigned direct_closes;
static int last_sock_fd = -1;

int stub_socket(int domain, int type, int protocol)
{
    int fd = socket(domain, type, protocol);
    last_sock_fd = fd;
    return fd;
}
int stub_connect(int fd, const struct sockaddr *address, socklen_t size)
{
    if (force_inprogress) {
        errno = EINPROGRESS;
        return -1;
    }
    return connect(fd, address, size);
}
int stub_getsockopt(int fd, int level, int name, void *value, socklen_t *size)
{
    if (force_inprogress && level == SOL_SOCKET && name == SO_ERROR) {
        assert(*size >= sizeof(int));
        *(int *)value = sim_so_error;
        return 0;
    }
    return getsockopt(fd, level, name, value, size);
}
int stub_close(int fd)
{
    direct_closes++;
    return close(fd);
}

/* ---- box_net stand-ins ---- */

static int watched_fd = -1;
static void (*watched_ready)(void *);
static void *watched_arg;
static unsigned net_close_calls;
static int last_net_closed_fd = -1;
static unsigned route_done_calls, poll_done_calls;
static bool last_route_completed;
static ywp_state_t last_route_state, last_poll_state;
static char last_route_detail[BOX_DETAIL_MAX + 1], last_poll_detail[BOX_DETAIL_MAX + 1];

void box_net_watch(int fd, bool read, bool write, void (*post)(void (*fn)(void *), void *),
                   void (*ready)(void *arg), void *arg)
{
    (void)post;
    if (read || write) {
        watched_fd = fd;
        watched_ready = ready;
        watched_arg = arg;
    } else if (watched_fd == fd) {
        watched_fd = -1;
    }
}
void box_net_unwatch(int fd)
{
    if (watched_fd == fd) watched_fd = -1;
}
void box_net_close(int fd)
{
    box_net_unwatch(fd);
    net_close_calls++;
    last_net_closed_fd = fd;
    close(fd);
}
void box_net_route_done(bool completed, ywp_state_t state, const char *detail)
{
    route_done_calls++;
    last_route_completed = completed;
    last_route_state = state;
    snprintf(last_route_detail, sizeof(last_route_detail), "%s", detail);
}
void box_net_poll_done(uint32_t tag, ywp_state_t state, const char *detail)
{
    (void)tag;
    poll_done_calls++;
    last_poll_state = state;
    snprintf(last_poll_detail, sizeof(last_poll_detail), "%s", detail);
}

/* ---- yoke_usbd stand-ins ---- */

typedef struct { void (*fn)(void *); void *arg; } post_t;
static post_t usbd_posts[64];
static size_t usbd_head, usbd_tail;
static const yoke_usbd_sink_t *gadget_sink;
static void *gadget_ctx;
static bool gadget_presented;
static unsigned ep_enabled_count, ep_reads, ep_cancels;

esp_err_t yoke_usbd_start(void) { return ESP_OK; }
void yoke_usbd_post(void (*fn)(void *), void *arg)
{
    assert(usbd_tail - usbd_head < 64);
    usbd_posts[usbd_tail++ % 64] = (post_t){fn, arg};
}
static void drain_usbd(void)
{
    while (usbd_head != usbd_tail) {
        post_t p = usbd_posts[usbd_head++ % 64];
        p.fn(p.arg);
    }
}
int yoke_usbd_present(const yoke_usbd_descriptors_t *d, const yoke_usbd_sink_t *sink, void *ctx,
                      char *why, size_t cap)
{
    (void)d; (void)why; (void)cap;
    gadget_sink = sink;
    gadget_ctx = ctx;
    gadget_presented = true;
    return 0;
}
void yoke_usbd_withdraw(void) { gadget_presented = false; }
unsigned yoke_usbd_endpoints(unsigned *in_count)
{
    *in_count = 7;
    return 15;
}
yoke_usbd_speed_t yoke_usbd_link_speed(void) { return YOKE_USBD_SPEED_HIGH; }
void yoke_usbd_ep0_write(const uint8_t *data, size_t len, bool zero)
{
    (void)data; (void)len; (void)zero;
}
void yoke_usbd_ep0_read(uint8_t *data, size_t len) { (void)data; (void)len; }
void yoke_usbd_ep0_stall(void) {}
int yoke_usbd_ep_enable(const uint8_t *desc)
{
    (void)desc;
    return (int)++ep_enabled_count;
}
int yoke_usbd_ep_disable(int handle) { (void)handle; return 0; }
int yoke_usbd_ep_set_halt(int handle) { (void)handle; return 0; }
void yoke_usbd_ep_write(int handle, uint8_t *data, size_t len, bool zero)
{
    (void)handle; (void)data; (void)len; (void)zero;
}
void yoke_usbd_ep_read(int handle, uint8_t *data, size_t len)
{
    (void)handle; (void)data; (void)len;
    ep_reads++;
}
void yoke_usbd_ep_cancel(int handle)
{
    (void)handle;
    /* Deliberately never reports completion: tests overdue teardown. */
    ep_cancels++;
}

cJSON *cJSON_AddStringToObject(cJSON *object, const char *name, const char *value)
{
    (void)object; (void)name; (void)value;
    return NULL;
}
cJSON *cJSON_AddNumberToObject(cJSON *object, const char *name, double value)
{
    (void)object; (void)name; (void)value;
    return NULL;
}

static void fire_timer(void)
{
    assert(sim_timer.active && sim_timer.callback);
    sim_timer.active = false;
    sim_timer.callback(sim_timer.arg);
    drain_usbd();
}

static void route_to(uint32_t gen, const char *host, uint16_t port)
{
    char media[128];
    int n = snprintf(media, sizeof(media),
                     "{\"busids\":[\"1-1\"],\"host\":\"%s\",\"port\":%u}", host, (unsigned)port);
    assert(n > 0);
    box_computer.route(gen, false, "dev-a", media, (size_t)n);
    drain_usbd();
}

/* ---- modes ---- */

/* Nonblocking connect enforces a 5 s timeout, closes via box_net_close(),
 * and tick() resets c.armed so re-arming the same deadline actually fires. */
static int mode_connect_timeout(void)
{
    force_inprogress = true;
    stub_time_us = 1000000; /* t = 1000 ms */
    assert(box_computer.start() == ESP_OK);
    route_to(1, "10.0.0.2", 3240);
    assert(route_done_calls == 1 && last_route_completed);
    int fd1 = last_sock_fd;
    assert(fd1 >= 0 && watched_fd == fd1);
    assert(sim_timer.active && sim_timer.timeout_us == 5001000); /* 5000 ms + 1 ms */
    unsigned starts = sim_timer.starts;
    /* Early tick at t = 2000 ms: same deadline (6000 ms) must still re-arm the timer. */
    stub_time_us = 2000000;
    fire_timer();
    assert(sim_timer.active && sim_timer.starts == starts + 1);
    assert(sim_timer.timeout_us == 4001000 && net_close_calls == 0);
    /* At t = 6000 ms (5 s elapsed): connect times out, closes via box_net_close(). */
    stub_time_us = 6000000;
    fire_timer();
    assert(net_close_calls == 1 && last_net_closed_fd == fd1 && direct_closes == 0);
    assert(sim_timer.active && sim_timer.timeout_us <= 2001000);
    box_computer.poll(1);
    drain_usbd();
    assert(poll_done_calls == 1 && last_poll_state == YWP_PENDING);
    assert(strstr(last_poll_detail, "connect timed out"));
    return 0;
}

/* On P4 (yx_levers != 0), if the YX footprint does not fit internal RAM but
 * plain USB/IP does, box_computer drops YX and creates the plain session. */
static int mode_yx_fallback(void)
{
    force_inprogress = false;
    stub_time_us = 1000000;
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in at = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    socklen_t size = sizeof(at);
    assert(listener >= 0 && !bind(listener, (struct sockaddr *)&at, size) && !listen(listener, 1));
    assert(!getsockname(listener, (struct sockaddr *)&at, &size));
    assert(box_computer.start() == ESP_OK);
    /* P4 YX proxy needs ~274 KB (+32 KB = ~306 KB); plain needs ~174 KB (+32 KB = ~206 KB). */
    sim_heap_free = 235u * 1024u;
    sim_heap_largest = 128u * 1024u;
    route_to(1, "127.0.0.1", ntohs(at.sin_port));
    int server_fd = accept(listener, NULL, NULL);
    close(listener);
    assert(server_fd >= 0);
    assert(watched_fd >= 0 && watched_ready);
    watched_ready(watched_arg);
    drain_usbd();
    /* Session was created (socket kept open, no RAM build failure). */
    assert(net_close_calls == 0 && direct_closes == 0);
    box_computer.poll(1);
    drain_usbd();
    assert(last_poll_state == YWP_PENDING && !strstr(last_poll_detail, "RAM:"));
    /* Cleanly detach to release proxy before exit. */
    box_computer.route(2, true, "", "", 0);
    drain_usbd();
    assert(net_close_calls == 1 && direct_closes == 0);
    close(server_fd);
    return 0;
}

/* When even plain USB/IP does not fit, box_computer retries with fast backoff
 * (YUSB_BUILD_RETRY, <= 2 s) rather than 30 s YUSB_BUILD_REJECTED. */
static int mode_ram_retry(void)
{
    force_inprogress = true;
    sim_so_error = 0;
    sim_heap_free = 16u * 1024u;
    sim_heap_largest = 16u * 1024u;
    stub_time_us = 1000000;
    assert(box_computer.start() == ESP_OK);
    route_to(1, "10.0.0.2", 3240);
    assert(watched_fd >= 0 && watched_ready);
    watched_ready(watched_arg);
    drain_usbd();
    assert(net_close_calls == 1 && direct_closes == 0);
    /* Retry backoff is 250 ms (YUSB_BUILD_RETRY), not 30000 ms (YUSB_BUILD_REJECTED). */
    assert(sim_timer.active && sim_timer.timeout_us <= 2001000);
    box_computer.poll(1);
    drain_usbd();
    assert(last_poll_state == YWP_PENDING && strstr(last_poll_detail, "RAM:"));
    return 0;
}

/* ---- minimal high-speed loopback exporter for teardown-overdue ---- */

static const uint8_t hs_device[18] = {18, 1, 0x00, 0x02, 0xff, 0, 0, 64, 0x09, 0x12, 0x02, 0x00,
                                      0x00, 0x01, 0, 0, 0, 1};
static const uint8_t hs_config[32] = {
    9, 2, 32, 0, 1, 1, 0, 0x80, 50,
    9, 4, 0, 0, 2, 0xff, 0, 0, 0,
    7, 5, 0x81, 2, 0x00, 0x02, 0,
    7, 5, 0x02, 2, 0x00, 0x02, 0,
};
static yusb_export_t *server_export;
static int hc_claim(void *ctx, unsigned dev) { (void)ctx; (void)dev; return 0; }
static void hc_release(void *ctx, unsigned dev) { (void)ctx; (void)dev; }
static int hc_ep_type(void *ctx, unsigned dev, unsigned addr)
{
    (void)ctx; (void)dev;
    return (addr & 0x7fu) == 0 ? 0 : 2;
}
static int hc_submit(void *ctx, unsigned dev, yusb_xurb_t *xurb)
{
    (void)ctx; (void)dev;
    int status = -YUSB_EPIPE;
    uint32_t actual = 0;
    if (xurb->type == YUSB_XFER_CONTROL) {
        if (xurb->setup.request_type == 0x80 && xurb->setup.request == 6) {
            const uint8_t *src = (xurb->setup.value >> 8) == 1 ? hs_device
                                 : (xurb->setup.value >> 8) == 2 ? hs_config
                                                                 : NULL;
            if (src) {
                uint32_t len = (xurb->setup.value >> 8) == 2 ? sizeof(hs_config) : src[0];
                actual = len < xurb->length ? len : xurb->length;
                memcpy(xurb->data, src, actual);
                status = 0;
            }
        } else if (xurb->setup.request_type == 0x00 && xurb->setup.request == 9) {
            status = 0;
        }
    }
    yusb_export_complete(server_export, xurb, status, actual, (uint64_t)(stub_time_us / 1000));
    return 0;
}
static void hc_cancel(void *ctx, unsigned dev, yusb_xurb_t *xurb)
{
    (void)ctx; (void)dev; (void)xurb;
}
static size_t x_rx_space(void *c, uint8_t **b) { return yusb_export_rx_space(c, b); }
static int x_rx_done(void *c, size_t n, uint64_t t) { return yusb_export_rx_done(c, n, t); }
static size_t x_tx_iov(const void *c, yusb_iov_t *v, size_t m) { return yusb_export_tx_iov(c, v, m); }
static void x_tx_done(void *c, size_t n) { yusb_export_tx_done(c, n); }
static const box_core_io_t export_io = {x_rx_space, x_rx_done, x_tx_iov, x_tx_done};

/* When a waiting route stops a live session whose gadget teardown becomes
 * overdue, box_computer completes the waiting route with YWP_ERROR instead
 * of wedging ywp_route forever. */
static int mode_teardown_overdue(void)
{
    force_inprogress = false;
    sim_heap_free = 1024u * 1024u;
    sim_heap_largest = 256u * 1024u;
    stub_time_us = 1000000;
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in at = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    socklen_t size = sizeof(at);
    assert(listener >= 0 && !bind(listener, (struct sockaddr *)&at, size) && !listen(listener, 1));
    assert(!getsockname(listener, (struct sockaddr *)&at, &size));
    yusb_export_dev_t dev = {
        .info = {.path = "/sys/yoke/1-1", .busid = "1-1", .busnum = 1, .devnum = 1, .speed = 3,
                 .vendor = 0x1209, .product = 0x0002, .bcd_device = 0x0100, .device_class = 0xff,
                 .configuration_value = 1, .num_configurations = 1, .num_interfaces = 1},
        .num_ifaces = 1,
        .ifaces = {{0xff, 0, 0}},
    };
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    yusb_hc_ops_t hc = {.claim = hc_claim, .release = hc_release, .endpoint_type = hc_ep_type,
                        .submit = hc_submit, .cancel = hc_cancel};
    server_export = yusb_export_create(&cfg, &hc, &dev, 1);
    assert(server_export);
    assert(box_computer.start() == ESP_OK);
    route_to(1, "127.0.0.1", ntohs(at.sin_port));
    int server_fd = accept(listener, NULL, NULL);
    close(listener);
    assert(server_fd >= 0 && !box_pump_prepare(server_fd));
    /* Pump until the proxy imports, fetches descriptors, and presents. */
    for (int i = 0; i < 500 && !gadget_presented; ++i) {
        if (watched_fd >= 0 && watched_ready) watched_ready(watched_arg);
        drain_usbd();
        (void)box_pump(server_fd, &export_io, server_export, (uint64_t)(stub_time_us / 1000));
        usleep(1000);
    }
    assert(gadget_presented && gadget_sink);
    /* Configure the gadget so bulk OUT starts an ep_read operation. */
    const uint8_t set_cfg[8] = {0x00, 9, 1, 0, 0, 0, 0, 0};
    gadget_sink->event(gadget_ctx, YOKE_USBD_CONTROL_ACKED, set_cfg);
    for (int i = 0; i < 500 && !ep_reads; ++i) {
        if (watched_fd >= 0 && watched_ready) watched_ready(watched_arg);
        drain_usbd();
        (void)box_pump(server_fd, &export_io, server_export, (uint64_t)(stub_time_us / 1000));
        usleep(1000);
    }
    assert(ep_reads > 0);
    /* Detach route waits for teardown; ep_cancel never completes -> STOPPING. */
    assert(route_done_calls == 1);
    box_computer.route(2, true, "", "", 0);
    drain_usbd();
    assert(ep_cancels > 0 && route_done_calls == 1);
    /* Advance past stop_timeout_ms: teardown becomes overdue and unblocks route 2. */
    stub_time_us += 30LL * 1000000;
    fire_timer();
    assert(route_done_calls == 2 && !last_route_completed && last_route_state == YWP_ERROR);
    assert(!strcmp(last_route_detail, "previous gadget teardown unconfirmed"));
    /* Subsequent route while still wedged also fails cleanly without hanging. */
    box_computer.route(3, true, "", "", 0);
    drain_usbd();
    assert(route_done_calls == 3 && !last_route_completed && last_route_state == YWP_ERROR);
    close(server_fd);
    yusb_export_destroy(server_export);
    server_export = NULL;
    return 0;
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "connect-timeout")) return mode_connect_timeout();
    if (!strcmp(argv[1], "yx-fallback")) return mode_yx_fallback();
    if (!strcmp(argv[1], "ram-retry")) return mode_ram_retry();
    if (!strcmp(argv[1], "teardown-overdue")) return mode_teardown_overdue();
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
