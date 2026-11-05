/* ESP-IDF USB Host Library backend (target only; see yoke_usbh.h). Two
 * tasks: the library daemon and one client task that runs every sink call,
 * posted function and transfer completion. Both wake at least once a
 * second to feed the task watchdog.
 *
 * The library counts a transfer in flight until its callback returns and
 * refuses to release an interface (or close a device) meanwhile, so
 * completions are queued in the callback and reach the sink only after
 * usb_host_client_handle_events returns. */
#include <string.h>
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "usb/usb_host.h"
#include "usb_desc.h"
#include "yoke_usbh.h"

#define WAKE_MS 1000
#define DAEMON_STACK 4096
#define CLIENT_STACK 6144   /* runs the export core: ~2.3 KB deep plus logging */
#define TASK_PRIORITY 5
#define POSTS 8
#define MAX_INTERFACES 32

typedef struct {
    usb_transfer_t *xfer;
    yoke_usbh_urb_t *urb;   /* NULL: free */
    uint32_t done;          /* bytes moved by earlier chunks */
    uint32_t chunk;         /* bytes this chunk may move */
    uint16_t packet;
    bool ended;             /* finished in the library, awaiting delivery */
    int status;             /* then its result */
} slot_t;

typedef struct {
    void (*fn)(void *arg);
    void *arg;
} post_t;

static const char *TAG = "yoke-usbh";
static const yoke_usbh_sink_t *sink;
static void *sink_ctx;
static usb_host_client_handle_t client;
static usb_device_handle_t dev;
static const usb_device_desc_t *device_desc;
static const usb_config_desc_t *config_desc;
static unsigned speed, address, outstanding;
static uint32_t claimed;                 /* interface bitmap */
static uint8_t alts[MAX_INTERFACES];     /* current alternate per interface */
static bool gone, stuck;                 /* stuck: a refused release or close, logged */
static uint8_t waiting;                  /* arrived while the previous device was freed */
static slot_t slots[YOKE_USBH_URBS];
static slot_t *finished[YOKE_USBH_URBS]; /* in completion order, awaiting delivery */
static size_t finished_head, finished_count;
static QueueHandle_t posts;

static int wire_status(usb_transfer_status_t status)
{
    switch (status) {
    case USB_TRANSFER_STATUS_COMPLETED: return 0;
    case USB_TRANSFER_STATUS_STALL: return -YOKE_USBH_EPIPE;
    case USB_TRANSFER_STATUS_CANCELED: return -YOKE_USBH_ECONNRESET;
    case USB_TRANSFER_STATUS_NO_DEVICE: return -YOKE_USBH_ENODEV;
    case USB_TRANSFER_STATUS_OVERFLOW: return -YOKE_USBH_EOVERFLOW;
    case USB_TRANSFER_STATUS_TIMED_OUT: return -YOKE_USBH_ETIME;
    default: return -YOKE_USBH_EPROTO;
    }
}

/* The descriptor of endpoint in the claimed alternate settings;
 * *interface is its interface number. */
static const uint8_t *claimed_endpoint(uint8_t endpoint, uint8_t *interface)
{
    usb_walk_t w;
    usb_walk_init(&w, (const uint8_t *)config_desc);
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] != USB_DT_ENDPOINT || p[0] < 7 || p[2] != endpoint || w.interface >= MAX_INTERFACES ||
            !(claimed >> w.interface & 1u) || w.alternate != alts[w.interface])
            continue;
        if (interface) *interface = w.interface;
        return p;
    }
    return NULL;
}

/* Any transfer in flight on an endpoint of the interface. */
static bool interface_busy(uint8_t interface)
{
    for (size_t i = 0; i < YOKE_USBH_URBS; ++i) {
        uint8_t owner;
        if (slots[i].urb && !slots[i].ended && (slots[i].urb->endpoint & 0x7fu) &&
            claimed_endpoint(slots[i].urb->endpoint, &owner) && owner == interface)
            return true;
    }
    return false;
}

static void attach(uint8_t new_address);

/* After DEV_GONE and the last completion; a refused release or close is
 * retried at the next wake, never forgotten. */
static void maybe_detached(void)
{
    if (!gone || outstanding) return;
    yoke_usbh_release();
    if (claimed || usb_host_device_close(client, dev) != ESP_OK) {
        if (!stuck) ESP_LOGE(TAG, "device %u not freed yet; retrying", address);
        stuck = true;
        return;
    }
    stuck = false;
    dev = NULL;
    device_desc = NULL;
    config_desc = NULL;
    gone = false;
    ESP_LOGW(TAG, "device %u detached", address);
    sink->detached(sink_ctx);
    uint8_t next = waiting;
    waiting = 0;
    if (next) attach(next);
}

static esp_err_t start_chunk(slot_t *s)
{
    yoke_usbh_urb_t *urb = s->urb;
    usb_transfer_t *x = s->xfer;
    x->device_handle = dev;
    x->bEndpointAddress = urb->endpoint;
    x->flags = 0;
    if ((urb->endpoint & 0x7fu) == 0) {
        s->chunk = urb->length;
        memcpy(x->data_buffer, urb->setup, 8);
        if (!(urb->setup[0] & 0x80u) && urb->length) memcpy(x->data_buffer + 8, urb->data, urb->length);
        x->num_bytes = 8 + (int)urb->length;
        return usb_host_transfer_submit_control(client, x);
    }
    uint32_t largest = YOKE_USBH_CHUNK - YOKE_USBH_CHUNK % s->packet;
    uint32_t left = urb->length - s->done;
    s->chunk = left < largest ? left : largest;
    if (urb->endpoint & 0x80u) {
        /* The library takes IN transfers in whole packets only. */
        x->num_bytes = (int)((s->chunk + s->packet - 1u) / s->packet * s->packet);
        if (!x->num_bytes) x->num_bytes = s->packet;
    } else {
        memcpy(x->data_buffer, urb->data + s->done, s->chunk);
        x->num_bytes = (int)s->chunk;
        if (urb->zero && s->done + s->chunk == urb->length) x->flags = USB_TRANSFER_FLAG_ZERO_PACK;
    }
    return usb_host_transfer_submit(x);
}

/* Halt, flush (queued transfers complete as cancelled) and resume. */
static void flush(uint8_t endpoint)
{
    if (usb_host_endpoint_halt(dev, endpoint) == ESP_OK &&
        usb_host_endpoint_flush(dev, endpoint) == ESP_OK)
        (void)usb_host_endpoint_clear(dev, endpoint);
}

static bool claim_alternate(uint8_t interface, uint8_t alternate)
{
    if (usb_host_interface_claim(client, dev, interface, alternate) != ESP_OK) {
        ESP_LOGE(TAG, "interface %u alternate %u could not be claimed", interface, alternate);
        return false;
    }
    claimed |= 1u << interface;
    alts[interface] = alternate;
    return true;
}

/* CLEAR_FEATURE(HALT) reset the device's data toggle to DATA0. The
 * library's endpoint clear keeps the pipe's toggle, so the interface is
 * released and claimed again for fresh pipes; with another transfer of the
 * interface in flight that is refused, and only the halt is cleared ⚠. */
static void reset_toggle(uint8_t endpoint)
{
    uint8_t interface;
    if (!claimed_endpoint(endpoint, &interface)) return;
    if (interface_busy(interface) ||
        usb_host_interface_release(client, dev, interface) != ESP_OK) {
        ESP_LOGW(TAG, "endpoint 0x%02x: halt cleared, data toggle kept", endpoint);
        flush(endpoint);
        return;
    }
    claimed &= ~(1u << interface);
    (void)claim_alternate(interface, alts[interface]);
}

/* The library side of SET_INTERFACE (released before the request: claim
 * the new alternate, or the old one again) and CLEAR_FEATURE(HALT). */
static int apply_tweak(const yoke_usbh_urb_t *urb, int status)
{
    uint8_t interface = urb->setup[4];
    if (urb->tweak == YOKE_USBH_SET_INTERFACE) {
        bool ok = claim_alternate(interface, status ? alts[interface] : urb->setup[2]);
        return !ok && !status ? -YOKE_USBH_EPIPE : status;
    }
    if (urb->tweak == YOKE_USBH_CLEAR_HALT && !status) reset_toggle(urb->setup[4]);
    return status;
}

/* Outside the library's callbacks only. */
static void deliver(void)
{
    while (finished_count) {
        slot_t *s = finished[finished_head];
        finished_head = (finished_head + 1) % YOKE_USBH_URBS;
        finished_count--;
        yoke_usbh_urb_t *urb = s->urb;
        int status = apply_tweak(urb, s->status);
        s->urb = NULL;
        outstanding--;
        sink->complete(sink_ctx, urb, status, s->done);
    }
}

static void transfer_done(usb_transfer_t *x)
{
    slot_t *s = x->context;
    yoke_usbh_urb_t *urb = s->urb;
    int status = wire_status(x->status);
    bool control = (urb->endpoint & 0x7fu) == 0;
    uint32_t actual = (uint32_t)x->actual_num_bytes;
    if (control) actual = actual > 8 ? actual - 8 : 0;
    if (actual > s->chunk) { /* the device sent more than the URB asked for */
        actual = s->chunk;
        if (!status) status = -YOKE_USBH_EOVERFLOW;
    }
    bool in = control ? urb->setup[0] & 0x80u : urb->endpoint & 0x80u;
    if (in) memcpy(urb->data + s->done, x->data_buffer + (control ? 8 : 0), actual);
    s->done += actual;
    bool more = !control && !status && actual == s->chunk && s->done < urb->length;
    if (more && start_chunk(s) == ESP_OK) return;
    s->ended = true;
    s->status = more ? -YOKE_USBH_EPROTO : status;
    finished[(finished_head + finished_count++) % YOKE_USBH_URBS] = s; /* a slot at most once */
}

int yoke_usbh_submit(yoke_usbh_urb_t *urb)
{
    if (!dev || gone) return -YOKE_USBH_ENODEV;
    bool control = (urb->endpoint & 0x7fu) == 0;
    const uint8_t *ep = control ? NULL : claimed_endpoint(urb->endpoint, NULL);
    if (!control && !ep) return -YOKE_USBH_EINVAL;
    if (control && urb->length > YOKE_USBH_CHUNK) return -YOKE_USBH_EINVAL;
    if (urb->tweak == YOKE_USBH_SET_INTERFACE) {
        /* The library re-claims an interface only once it is released, and
         * releases it only with nothing in flight (the stub's rule too). */
        uint8_t interface = urb->setup[4];
        if (interface >= MAX_INTERFACES || !(claimed >> interface & 1u)) return -YOKE_USBH_EINVAL;
        if (interface_busy(interface)) return -YOKE_USBH_EBUSY;
        if (usb_host_interface_release(client, dev, interface) != ESP_OK) return -YOKE_USBH_EBUSY;
        claimed &= ~(1u << interface);
    }
    slot_t *s = NULL;
    for (size_t i = 0; i < YOKE_USBH_URBS && !s; ++i)
        if (!slots[i].urb) s = &slots[i];
    if (!s) return -YOKE_USBH_EBUSY;
    *s = (slot_t){.xfer = s->xfer, .urb = urb,
                  .packet = control ? 64u : usb_le16(ep + 4) & 0x7ffu};
    if (start_chunk(s) != ESP_OK) {
        s->urb = NULL;
        (void)apply_tweak(urb, -YOKE_USBH_EPROTO); /* claim the old alternate again */
        return -YOKE_USBH_EPROTO;
    }
    outstanding++;
    return 0;
}

void yoke_usbh_cancel(yoke_usbh_urb_t *urb)
{
    if (!(urb->endpoint & 0x7fu)) return; /* the default pipe cannot be flushed */
    for (size_t i = 0; i < YOKE_USBH_URBS; ++i) {
        if (slots[i].urb != urb) continue;
        if (!slots[i].ended) flush(urb->endpoint); /* else its result is on the way */
        return;
    }
}

int yoke_usbh_claim(void)
{
    if (!dev || gone) return -YOKE_USBH_ENODEV;
    usb_walk_t w;
    usb_walk_init(&w, (const uint8_t *)config_desc);
    for (const uint8_t *p; (p = usb_walk_next(&w));) {
        if (p[1] != USB_DT_INTERFACE || p[0] < 9 || p[3] != 0 || p[2] >= MAX_INTERFACES) continue;
        if (usb_host_interface_claim(client, dev, p[2], 0) != ESP_OK) {
            yoke_usbh_release();
            return -YOKE_USBH_EBUSY;
        }
        claimed |= 1u << p[2];
        alts[p[2]] = 0;
    }
    return 0;
}

void yoke_usbh_release(void)
{
    if (!dev) return;
    for (unsigned i = 0; i < MAX_INTERFACES; ++i) {
        if (!(claimed >> i & 1u)) continue;
        if (usb_host_interface_release(client, dev, (uint8_t)i) == ESP_OK)
            claimed &= ~(1u << i);
        else
            ESP_LOGE(TAG, "interface %u release refused; kept claimed", i);
    }
}

int yoke_usbh_endpoint_type(uint8_t endpoint)
{
    if ((endpoint & 0x7fu) == 0) return dev ? (int)USB_XFER_CONTROL : -1;
    const uint8_t *ep = dev ? claimed_endpoint(endpoint, NULL) : NULL;
    return ep ? ep[3] & 3 : -1;
}

const uint8_t *yoke_usbh_device_descriptor(void) { return (const uint8_t *)device_desc; }
const uint8_t *yoke_usbh_config_descriptor(void) { return (const uint8_t *)config_desc; }
unsigned yoke_usbh_speed(void) { return dev ? speed : 0; }
unsigned yoke_usbh_address(void) { return dev ? address : 0; }

static void attach(uint8_t new_address)
{
    usb_device_info_t info;
    if (dev && gone) {
        waiting = new_address; /* the replugged device, once its predecessor is freed */
        return;
    }
    if (dev) {
        ESP_LOGW(TAG, "device %u ignored: one device per box", new_address);
        return;
    }
    if (usb_host_device_open(client, new_address, &dev) != ESP_OK) {
        ESP_LOGE(TAG, "device %u could not be opened", new_address);
        dev = NULL;
        return;
    }
    if (usb_host_device_info(dev, &info) != ESP_OK ||
        usb_host_get_device_descriptor(dev, &device_desc) != ESP_OK ||
        usb_host_get_active_config_descriptor(dev, &config_desc) != ESP_OK) {
        ESP_LOGE(TAG, "device %u has no readable descriptors", new_address);
        (void)usb_host_device_close(client, dev);
        dev = NULL;
        return;
    }
    /* usb_speed_t LOW/FULL/HIGH to Linux usb_device_speed 1/2/3. */
    speed = info.speed == USB_SPEED_HIGH ? 3u : info.speed == USB_SPEED_FULL ? 2u : 1u;
    address = new_address;
    sink->attached(sink_ctx);
}

static void on_client_event(const usb_host_client_event_msg_t *msg, void *arg)
{
    (void)arg;
    if (msg->event == USB_HOST_CLIENT_EVENT_NEW_DEV) {
        attach(msg->new_dev.address);
    } else if (msg->event == USB_HOST_CLIENT_EVENT_DEV_GONE && dev) {
        gone = true;
        for (unsigned i = 0; i < 32; ++i) {
            uint8_t endpoint = (uint8_t)((i & 15u) | (i & 16u) << 3);
            if (i & 15u && claimed_endpoint(endpoint, NULL)) flush(endpoint);
        }
    }
}

static void daemon_task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    for (;;) {
        uint32_t flags = 0;
        (void)usb_host_lib_handle_events(pdMS_TO_TICKS(WAKE_MS), &flags);
        esp_task_wdt_reset();
    }
}

static void client_task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    for (;;) {
        (void)usb_host_client_handle_events(client, pdMS_TO_TICKS(WAKE_MS));
        deliver();
        maybe_detached();
        for (post_t p; xQueueReceive(posts, &p, 0) == pdTRUE;) p.fn(p.arg);
        esp_task_wdt_reset();
    }
}

void yoke_usbh_post(void (*fn)(void *arg), void *arg)
{
    post_t p = {fn, arg};
    (void)xQueueSend(posts, &p, portMAX_DELAY);
    (void)usb_host_client_unblock(client);
}

esp_err_t yoke_usbh_start(const yoke_usbh_sink_t *s, void *ctx)
{
    sink = s;
    sink_ctx = ctx;
    posts = xQueueCreate(POSTS, sizeof(post_t));
    if (!posts) return ESP_ERR_NO_MEM;
    for (size_t i = 0; i < YOKE_USBH_URBS; ++i) {
        esp_err_t err = usb_host_transfer_alloc(8 + YOKE_USBH_CHUNK, 0, &slots[i].xfer);
        if (err != ESP_OK) return err;
        slots[i].xfer->callback = transfer_done;
        slots[i].xfer->context = &slots[i];
    }
    const usb_host_config_t host = {.intr_flags = ESP_INTR_FLAG_LEVEL1};
    esp_err_t err = usb_host_install(&host);
    if (err != ESP_OK) return err;
    const usb_host_client_config_t config = {
        .max_num_event_msg = 4,
        .async = {.client_event_callback = on_client_event},
    };
    err = usb_host_client_register(&config, &client);
    if (err != ESP_OK) return err;
    if (xTaskCreatePinnedToCore(daemon_task, "usb-host-lib", DAEMON_STACK, NULL,
                                TASK_PRIORITY + 1, NULL, 1) != pdPASS ||
        xTaskCreatePinnedToCore(client_task, "usb-host", CLIENT_STACK, NULL, TASK_PRIORITY, NULL,
                                1) != pdPASS)
        return ESP_ERR_NO_MEM;
    ESP_LOGI(TAG, "USB host running; root port powered");
    return ESP_OK;
}
