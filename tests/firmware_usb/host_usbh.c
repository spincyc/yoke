/* The USB Host Library backend (yoke_usbh/usbh_esp.c, unchanged) against a
 * model of ESP-IDF 5.5's library: a transfer counts as in flight until its
 * callback returns and usb_host_client_handle_events moves on, an interface
 * with transfers in flight cannot be released, a device with a claimed
 * interface cannot be closed, flushes complete as cancelled, and DEV_GONE
 * follows the dead transfers in the same pass. The client task runs one
 * loop iteration at a time (the watchdog reset ends it). HOST MODEL ONLY:
 * no DWC2, PHY or device timing. */
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_task_wdt.h"
#include "freertos/task.h"
#include "usb/usb_host.h"
#include "yoke_usbh.h"

/* Interface 0: bulk IN 0x81, bulk OUT 0x02, interrupt IN 0x83. */
static const uint8_t device_desc[18] = {18, 1, 0x00, 0x02, 0, 0, 0, 64, 0x09, 0x12, 0x01, 0x00,
                                        0, 1, 0, 0, 0, 1};
static const uint8_t config_desc[39] = {
    9, 2, 39, 0, 1, 1, 0, 0x80, 50,
    9, 4, 0, 0, 3, 0xff, 0, 0, 0,
    7, 5, 0x81, 2, 64, 0, 0,
    7, 5, 0x02, 2, 64, 0, 0,
    7, 5, 0x83, 3, 8, 0, 1,
};

/* ---- the library model ---- */

static struct {
    void (*event_cb)(const usb_host_client_event_msg_t *msg, void *arg);
    void *event_arg;
    bool open, claimed, in_callback;
    unsigned claims, releases, refused_releases, closes, flushes;
    unsigned inflight[32];               /* per endpoint: until handle_events moves on */
    usb_transfer_t *queued[16];
    size_t nqueued;
    struct {
        usb_transfer_t *t;
        usb_transfer_status_t status;
        int actual;
    } done[16];
    size_t ndone;
    usb_host_client_event_msg_t events[4];
    size_t nevents;
} lib;
static struct stub_usb_device { int unused; } the_device;
static struct stub_usb_client { int unused; } the_client;

static unsigned slot_of(uint8_t ep) { return (ep & 15u) | (ep & 0x80u) >> 3; }

esp_err_t usb_host_install(const usb_host_config_t *config)
{
    (void)config;
    return ESP_OK;
}
esp_err_t usb_host_lib_handle_events(TickType_t timeout, uint32_t *flags)
{
    (void)timeout; (void)flags;
    return ESP_OK;
}
esp_err_t usb_host_client_register(const usb_host_client_config_t *config,
                                   usb_host_client_handle_t *client)
{
    lib.event_cb = config->async.client_event_callback;
    lib.event_arg = config->async.callback_arg;
    *client = &the_client;
    return ESP_OK;
}
esp_err_t usb_host_client_unblock(usb_host_client_handle_t client)
{
    (void)client;
    return ESP_OK;
}

/* The order of usb_host.c: dequeued transfers' callbacks, then in-flight
 * counts drop, then client events. */
esp_err_t usb_host_client_handle_events(usb_host_client_handle_t client, TickType_t timeout)
{
    (void)client; (void)timeout;
    size_t n = lib.ndone;
    unsigned finished[16];
    for (size_t i = 0; i < n; ++i) {
        usb_transfer_t *t = lib.done[i].t;
        finished[i] = slot_of(t->bEndpointAddress);
        t->status = lib.done[i].status;
        t->actual_num_bytes = lib.done[i].actual;
        lib.in_callback = true;
        t->callback(t);
        lib.in_callback = false;
    }
    memmove(lib.done, lib.done + n, (lib.ndone - n) * sizeof(lib.done[0]));
    lib.ndone -= n;
    for (size_t i = 0; i < n; ++i) lib.inflight[finished[i]]--;
    size_t events = lib.nevents;
    lib.nevents = 0;
    for (size_t i = 0; i < events; ++i) lib.event_cb(&lib.events[i], lib.event_arg);
    return ESP_OK;
}

esp_err_t usb_host_device_open(usb_host_client_handle_t client, uint8_t address,
                               usb_device_handle_t *dev)
{
    (void)client; (void)address;
    assert(!lib.open);
    lib.open = true;
    *dev = &the_device;
    return ESP_OK;
}
esp_err_t usb_host_device_close(usb_host_client_handle_t client, usb_device_handle_t dev)
{
    (void)client; (void)dev;
    if (lib.claimed) return ESP_ERR_INVALID_STATE;
    lib.open = false;
    lib.closes++;
    return ESP_OK;
}
esp_err_t usb_host_device_info(usb_device_handle_t dev, usb_device_info_t *info)
{
    (void)dev;
    info->speed = USB_SPEED_FULL;
    return ESP_OK;
}
esp_err_t usb_host_get_device_descriptor(usb_device_handle_t dev, const usb_device_desc_t **desc)
{
    (void)dev;
    *desc = (const usb_device_desc_t *)device_desc;
    return ESP_OK;
}
esp_err_t usb_host_get_active_config_descriptor(usb_device_handle_t dev,
                                                const usb_config_desc_t **desc)
{
    (void)dev;
    *desc = (const usb_config_desc_t *)config_desc;
    return ESP_OK;
}

esp_err_t usb_host_interface_claim(usb_host_client_handle_t client, usb_device_handle_t dev,
                                   uint8_t interface, uint8_t alternate)
{
    (void)client; (void)dev; (void)alternate;
    if (interface != 0 || lib.claimed) return ESP_ERR_INVALID_STATE;
    lib.claimed = true;
    lib.claims++; /* fresh pipes: DATA0 */
    return ESP_OK;
}
esp_err_t usb_host_interface_release(usb_host_client_handle_t client, usb_device_handle_t dev,
                                     uint8_t interface)
{
    (void)client; (void)dev;
    if (interface != 0 || !lib.claimed) return ESP_ERR_NOT_FOUND;
    if (lib.inflight[1] || lib.inflight[2] || lib.inflight[16 + 1] || lib.inflight[16 + 3]) {
        lib.refused_releases++;
        return ESP_ERR_INVALID_STATE;
    }
    lib.claimed = false;
    lib.releases++;
    return ESP_OK;
}

static void retire(usb_transfer_t *t, usb_transfer_status_t status, int actual)
{
    for (size_t i = 0; i < lib.nqueued; ++i) {
        if (lib.queued[i] != t) continue;
        memmove(lib.queued + i, lib.queued + i + 1, (--lib.nqueued - i) * sizeof(lib.queued[0]));
        lib.done[lib.ndone].t = t;
        lib.done[lib.ndone].status = status;
        lib.done[lib.ndone++].actual = actual;
        return;
    }
    assert(0);
}

esp_err_t usb_host_endpoint_halt(usb_device_handle_t dev, uint8_t endpoint)
{
    (void)dev; (void)endpoint;
    return ESP_OK;
}
esp_err_t usb_host_endpoint_flush(usb_device_handle_t dev, uint8_t endpoint)
{
    (void)dev;
    lib.flushes++;
    for (size_t i = lib.nqueued; i-- > 0;)
        if (lib.queued[i]->bEndpointAddress == endpoint) retire(lib.queued[i], USB_TRANSFER_STATUS_CANCELED, 0);
    return ESP_OK;
}
esp_err_t usb_host_endpoint_clear(usb_device_handle_t dev, uint8_t endpoint)
{
    (void)dev; (void)endpoint;
    return ESP_OK;
}

esp_err_t usb_host_transfer_alloc(size_t size, int isoc_packets, usb_transfer_t **transfer)
{
    (void)isoc_packets;
    usb_transfer_t *t = calloc(1, sizeof(*t));
    t->data_buffer = calloc(1, size);
    t->data_buffer_size = size;
    *transfer = t;
    return ESP_OK;
}
static esp_err_t submit(usb_transfer_t *t)
{
    assert(lib.open && (size_t)t->num_bytes <= t->data_buffer_size);
    lib.queued[lib.nqueued++] = t;
    lib.inflight[slot_of(t->bEndpointAddress)]++;
    return ESP_OK;
}
esp_err_t usb_host_transfer_submit(usb_transfer_t *t)
{
    assert(lib.claimed);
    return submit(t);
}
esp_err_t usb_host_transfer_submit_control(usb_host_client_handle_t client, usb_transfer_t *t)
{
    (void)client;
    return submit(t);
}

/* The device answers the oldest transfer on endpoint (bytes: data moved). */
static void device_answers(uint8_t endpoint, usb_transfer_status_t status, int bytes)
{
    for (size_t i = 0; i < lib.nqueued; ++i) {
        if (lib.queued[i]->bEndpointAddress != endpoint) continue;
        retire(lib.queued[i], status, endpoint & 0x7fu ? bytes : 8 + bytes);
        return;
    }
    assert(0);
}

/* ---- driving the backend's client task ---- */

static jmp_buf iteration_end;
static void end_iteration(void) { longjmp(iteration_end, 1); }

static void (*client)(void *); /* static: survives the longjmp */

static void run_client_once(void)
{
    for (unsigned i = 0; i < stub_task_count && !client; ++i)
        if (!strcmp(stub_tasks[i].name, "usb-host")) client = stub_tasks[i].fn;
    assert(client);
    stub_wdt_hook = end_iteration;
    if (!setjmp(iteration_end)) client(NULL);
    stub_wdt_hook = NULL;
}

/* ---- the sink ---- */

static unsigned attached, detached, completions;
static int last_status;
static uint32_t last_actual;
static bool release_on_complete;

static void on_attached(void *ctx)
{
    (void)ctx;
    attached++;
}
static void on_detached(void *ctx)
{
    (void)ctx;
    detached++;
}
static void on_complete(void *ctx, yoke_usbh_urb_t *urb, int status, uint32_t actual)
{
    (void)ctx; (void)urb;
    assert(!lib.in_callback); /* the library still counts the transfer in flight */
    completions++;
    last_status = status;
    last_actual = actual;
    if (release_on_complete) yoke_usbh_release();
}
static const yoke_usbh_sink_t sink = {on_attached, on_detached, on_complete};

static void plug(uint8_t address)
{
    lib.events[lib.nevents].event = USB_HOST_CLIENT_EVENT_NEW_DEV;
    lib.events[lib.nevents++].new_dev.address = address;
}

static void start(void)
{
    assert(yoke_usbh_start(&sink, NULL) == ESP_OK);
    plug(1);
    run_client_once();
    assert(attached == 1 && yoke_usbh_address() == 1);
    assert(yoke_usbh_claim() == 0 && lib.claimed && lib.claims == 1);
}

static uint8_t buffer[64];
static yoke_usbh_urb_t interrupt_in = {.endpoint = 0x83, .data = buffer, .length = 8};
static yoke_usbh_urb_t bulk_in = {.endpoint = 0x81, .data = buffer, .length = 64};

/* An import's last URB completes; its session then releases the device. */
static int mode_release_after_completion(void)
{
    start();
    assert(yoke_usbh_submit(&interrupt_in) == 0);
    device_answers(0x83, USB_TRANSFER_STATUS_COMPLETED, 8);
    release_on_complete = true;
    run_client_once();
    assert(completions == 1 && last_status == 0 && last_actual == 8);
    assert(!lib.claimed && lib.releases == 1 && !lib.refused_releases);
    return 0;
}

/* CLEAR_FEATURE(HALT) leaves fresh pipes (DATA0) by claiming again; with
 * another transfer of the interface in flight only the halt is cleared. */
static int mode_clear_halt(void)
{
    start();
    assert(yoke_usbh_submit(&bulk_in) == 0);
    device_answers(0x81, USB_TRANSFER_STATUS_STALL, 0);
    run_client_once();
    assert(completions == 1 && last_status == -YOKE_USBH_EPIPE);
    yoke_usbh_urb_t clear = {.endpoint = 0x00, .tweak = YOKE_USBH_CLEAR_HALT,
                             .setup = {0x02, 1, 0, 0, 0x81, 0, 0, 0}};
    assert(yoke_usbh_submit(&clear) == 0);
    device_answers(0x00, USB_TRANSFER_STATUS_COMPLETED, 0);
    run_client_once();
    assert(completions == 2 && last_status == 0);
    assert(lib.releases == 1 && lib.claims == 2 && lib.claimed);
    assert(yoke_usbh_submit(&interrupt_in) == 0);
    assert(yoke_usbh_submit(&clear) == 0);
    device_answers(0x00, USB_TRANSFER_STATUS_COMPLETED, 0);
    unsigned flushes = lib.flushes;
    run_client_once();
    assert(completions == 3 && last_status == 0);
    assert(lib.claims == 2 && lib.flushes == flushes + 1); /* the halt only */
    return 0;
}

/* Unplug with a URB in flight, and the device back in the same pass: it is
 * freed after the dead URB completes, and the new one attaches. */
static int mode_unplug_replug(void)
{
    start();
    assert(yoke_usbh_submit(&interrupt_in) == 0);
    device_answers(0x83, USB_TRANSFER_STATUS_NO_DEVICE, 0);
    lib.events[lib.nevents].event = USB_HOST_CLIENT_EVENT_DEV_GONE;
    lib.events[lib.nevents++].dev_gone.dev_hdl = &the_device;
    plug(2);
    run_client_once();
    assert(completions == 1 && last_status == -YOKE_USBH_ENODEV);
    assert(lib.releases == 1 && lib.closes == 1 && detached == 1);
    assert(attached == 2 && yoke_usbh_address() == 2 && lib.open);
    return 0;
}

int main(int argc, char **argv)
{
    static const struct { const char *name; int (*run)(void); } modes[] = {
        {"release-after-completion", mode_release_after_completion},
        {"clear-halt", mode_clear_halt},
        {"unplug-replug", mode_unplug_replug},
    };
    assert(argc == 2);
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i)
        if (!strcmp(argv[1], modes[i].name)) return modes[i].run();
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
