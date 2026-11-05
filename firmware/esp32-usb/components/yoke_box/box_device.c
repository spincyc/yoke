/* Device role: a USB/IP server exporting the device on the USB-A socket
 * (native/yusb export core over yoke_usbh, through box_host). Everything
 * here runs in the USB host task; the network task reaches it through
 * posts. TCP 3240 accepts only admitted consumer addresses (ADR 0024, from
 * the YWP/1 ADMISSION effect). */
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include "bridge.h"
#include "box_host.h"
#include "box_media.h"
#include "box_pump.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"

#define SESSIONS 1          /* one export core fits the S3's RAM (USB-TIER1.md) */
#define ADMITTED_MAX 64
#if CONFIG_IDF_TARGET_ESP32P4
#define MAX_URB_BYTES 16384u /* a tier 1.5 consumer's max_transfer (BOT) */
#else
#define MAX_URB_BYTES 4096u /* tier 1 classes (HID, serial, MIDI) ⚠ */
#endif

static const char *TAG = "yoke-device";
static box_export_t sessions[SESSIONS];
static int listener = -1;
static yusb_export_dev_t device;
static bool attached;
static SemaphoreHandle_t admission_lock;
static uint32_t admitted[ADMITTED_MAX];
static size_t admitted_count;
static bool admission_known;   /* the node published a set since start */
static const char *last_refusal = ""; /* evidence for status */

static uint64_t now_ms(void) { return (uint64_t)(esp_timer_get_time() / 1000); }

static void op_log(void *ctx, const char *message)
{
    (void)ctx;
    ESP_LOGI(TAG, "%s", message);
}

static size_t io_rx_space(void *core, uint8_t **buf) { return yusb_export_rx_space(core, buf); }
static int io_rx_done(void *core, size_t n, uint64_t t) { return yusb_export_rx_done(core, n, t); }
static size_t io_tx_iov(const void *core, yusb_iov_t *iov, size_t max)
{
    return yusb_export_tx_iov(core, iov, max);
}
static void io_tx_done(void *core, size_t n) { yusb_export_tx_done(core, n); }
static const box_core_io_t io = {io_rx_space, io_rx_done, io_tx_iov, io_tx_done};

static void session_ready(void *arg);

/* One backend transfer per URB slot. The pool is automatic: room for 8
 * full-size URBs (as many as the backend runs) plus the control buffer,
 * each URB cut to its own length, so parked small reads stay cheap and
 * more of them are held while the stream is still read. One ~34 KiB block
 * on the S3, which its PSRAM takes. */
static void export_cfg(yusb_export_cfg_t *cfg)
{
    yusb_export_defaults(cfg);
    cfg->max_urbs = YOKE_USBH_URBS;
    cfg->max_urb_bytes = MAX_URB_BYTES;
    cfg->pool_bytes = 0;
#if CONFIG_IDF_TARGET_ESP32P4
    /* As the tier 1.5 consumer offers: mass storage crosses 100 Mbit/s
     * slower than high speed moves it (+22 KB); no block cache. */
    cfg->yx.levers = YUSB_YX_FILL | YUSB_YX_LZ4;
#else
    /* Plain: full speed is far below the link and tier 1's transfers are
     * mostly below YX's 512 bytes (FILL+LZ4 would cost +21 KB). */
    cfg->yx.levers = 0;
#endif
}

static void close_transport(box_export_t *s)
{
    box_net_unwatch(s->fd);
    close(s->fd);
    s->fd = -1;
    yusb_export_close(s->x);
}

/* Move bytes; close once the core is finished or the peer is gone; free
 * the session when every URB has come back. */
static void service(box_export_t *s)
{
    if (!s->x) return;
    if (s->fd >= 0) {
        int r = box_pump(s->fd, &io, s->x, now_ms());
        if (r & (BOX_PUMP_CLOSED | BOX_PUMP_FAILED) ||
            (!(r & BOX_PUMP_WRITE) && yusb_export_finished(s->x)))
            close_transport(s);
        else
            box_net_watch(s->fd, r & BOX_PUMP_READ, r & BOX_PUMP_WRITE, yoke_usbh_post,
                          session_ready, s);
    }
    if (s->fd < 0 && yusb_export_idle(s->x) && !box_host_busy(s)) {
        yusb_export_destroy(s->x);
        s->x = NULL;
        ESP_LOGI(TAG, "USB/IP session closed");
    }
}

static void session_ready(void *arg) { service(arg); }

/* ---- the listener (ADR 0024 admission) ---- */

static bool admitted_peer(uint32_t ipv4)
{
    bool ok = false;
    xSemaphoreTake(admission_lock, portMAX_DELAY);
    for (size_t i = 0; i < admitted_count && !ok; ++i) ok = admitted[i] == ipv4;
    xSemaphoreGive(admission_lock);
    return ok;
}

static void accept_one(int fd, uint32_t peer)
{
    box_export_t *s = NULL;
    for (size_t i = 0; i < SESSIONS && !s; ++i)
        if (!sessions[i].x) s = &sessions[i];
    const char *refused = !admitted_peer(peer) ? "not admitted"
                          : !attached         ? "no device"
                          : !s                ? "busy"
                                              : NULL;
    if (!refused) {
        yusb_export_cfg_t cfg;
        export_cfg(&cfg);
        box_host_bind(s);
        /* Budget first: never a half-created session. */
        if (!box_heap_fits(yusb_export_footprint(&cfg, NULL))) refused = "not enough RAM";
        else if (box_pump_prepare(fd)) refused = "socket setup failed";
        else if (!(s->x = yusb_export_create(&cfg, &s->ops, &device, 1)))
            refused = "export refused the device description, or out of memory";
    }
    if (refused) {
        last_refusal = refused;
        ESP_LOGW(TAG, "connection from %u.%u.%u.%u refused: %s", (unsigned)(peer >> 24),
                 (unsigned)(peer >> 16) & 255u, (unsigned)(peer >> 8) & 255u, (unsigned)peer & 255u,
                 refused);
        close(fd);
        return;
    }
    s->fd = fd;
    service(s);
}

static void listener_ready(void *arg)
{
    (void)arg;
    for (;;) {
        struct sockaddr_in from;
        socklen_t size = sizeof(from);
        int fd = accept(listener, (struct sockaddr *)&from, &size);
        if (fd < 0) break;
        accept_one(fd, ntohl(from.sin_addr.s_addr));
    }
    box_net_watch(listener, true, false, yoke_usbh_post, listener_ready, NULL);
}

static void listen_work(void *arg)
{
    (void)arg;
    int on = 1;
    struct sockaddr_in any = {.sin_family = AF_INET, .sin_port = htons(BOX_USBIP_PORT)};
    listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0 || setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on)) ||
        bind(listener, (struct sockaddr *)&any, sizeof(any)) || listen(listener, 2) ||
        fcntl(listener, F_SETFL, fcntl(listener, F_GETFL, 0) | O_NONBLOCK) < 0) {
        ESP_LOGE(TAG, "USB/IP listener on TCP %u failed: errno %d", BOX_USBIP_PORT, errno);
        return;
    }
    ESP_LOGI(TAG, "USB/IP server on TCP %u (admitted consumers only)", BOX_USBIP_PORT);
    box_net_watch(listener, true, false, yoke_usbh_post, listener_ready, NULL);
}

/* ---- the USB host backend's sink ---- */

static void on_attached(void *ctx)
{
    (void)ctx;
    box_host_describe(&device);
    attached = true;
    ESP_LOGI(TAG, "exporting %04x:%04x as %s", device.info.vendor, device.info.product, BOX_BUSID);
}

static void on_detached(void *ctx)
{
    (void)ctx;
    attached = false;
    for (size_t i = 0; i < SESSIONS; ++i) {
        if (!sessions[i].x || sessions[i].fd < 0) continue;
        close_transport(&sessions[i]); /* the device left: so does its import */
        service(&sessions[i]);
    }
}

static void on_complete(void *ctx, yoke_usbh_urb_t *urb, int status, uint32_t actual)
{
    (void)ctx;
    box_host_complete(urb, status, actual);
}

static const yoke_usbh_sink_t sink = {
    .attached = on_attached, .detached = on_detached, .complete = on_complete,
};

/* ---- route, health and admission (from the network task) ---- */

static void route(uint32_t generation, bool detach, const char *producer, const char *media,
                  size_t media_len)
{
    (void)generation; (void)detach; (void)producer; (void)media; (void)media_len;
    /* Producers never receive routes from their node. */
    box_net_route_done(true, YWP_IDLE, "producer: no route");
}

static void poll_work(void *arg)
{
    char detail[BOX_DETAIL_MAX + 1];
    ywp_state_t state;
    bool imported = false;
    for (size_t i = 0; i < SESSIONS; ++i)
        imported |= sessions[i].x && yusb_export_device(sessions[i].x) >= 0;
    if (!admission_known) {
        state = YWP_PENDING;
        snprintf(detail, sizeof(detail), "admission allowlist not yet requested");
    } else if (!attached) {
        state = YWP_ERROR;
        snprintf(detail, sizeof(detail), "no USB device on the USB-A port");
    } else {
        state = YWP_ACTIVE;
        snprintf(detail, sizeof(detail), "1 device(s) exported (%04x:%04x%s)", device.info.vendor,
                 device.info.product, imported ? ", imported" : "");
    }
    box_net_poll_done((uint32_t)(uintptr_t)arg, state, detail);
}

static void poll_state(uint32_t tag) { yoke_usbh_post(poll_work, (void *)(uintptr_t)tag); }

static void admission(const uint32_t *ipv4, size_t count)
{
    xSemaphoreTake(admission_lock, portMAX_DELAY);
    admitted_count = count < ADMITTED_MAX ? count : ADMITTED_MAX;
    memcpy(admitted, ipv4, admitted_count * sizeof(*ipv4));
    admission_known = true;
    xSemaphoreGive(admission_lock);
    ESP_LOGI(TAG, "admission: %u consumer address(es)", (unsigned)admitted_count);
}

static esp_err_t start(void)
{
    for (size_t i = 0; i < SESSIONS; ++i) sessions[i].fd = -1;
    admission_lock = xSemaphoreCreateMutex();
    if (!admission_lock) return ESP_ERR_NO_MEM;
    box_host_init(service, now_ms, op_log);
    yusb_export_cfg_t cfg;
    export_cfg(&cfg);
    ESP_LOGI(TAG, "an import needs %u bytes", (unsigned)yusb_export_footprint(&cfg, NULL));
    return yoke_usbh_start(&sink, NULL);
}

static void online(void) { yoke_usbh_post(listen_work, NULL); }

static void status(cJSON *usb)
{
    /* Read without the USB task's cooperation: evidence only. */
    cJSON_AddBoolToObject(usb, "attached", attached);
    cJSON_AddNumberToObject(usb, "vendor", attached ? device.info.vendor : 0);
    cJSON_AddNumberToObject(usb, "product", attached ? device.info.product : 0);
    unsigned open = 0;
    for (size_t i = 0; i < SESSIONS; ++i) open += sessions[i].x != NULL;
    cJSON_AddNumberToObject(usb, "sessions", open);
    cJSON_AddNumberToObject(usb, "admitted", (double)admitted_count);
    cJSON_AddStringToObject(usb, "last_refusal", last_refusal);
}

const box_bridge_t box_device = {
    .start = start, .online = online, .route = route, .poll = poll_state,
    .admission = admission, .status = status,
};
