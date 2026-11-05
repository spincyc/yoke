/* TinyUSB glue for yoke_usbd (target only): the application class driver,
 * descriptor callbacks, the usbd_core port and the USB task watchdog.
 *
 * esp_tinyusb 3.0.0 implements tud_descriptor_{device,configuration,string}_cb
 * for its static model (one configuration, ASCII strings of at most 31
 * characters, one language). A mirror serves the remote bytes verbatim, so
 * this component links TinyUSB's references to the __wrap_ definitions
 * below (CMakeLists.txt; the same technique as the IR panic hook). */
#include <stdio.h>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "device/dcd.h"
#include "device/usbd_pvt.h"
#include "usbd_core.h"

#if TUSB_VERSION_NUMBER != 2100
#error "Review usbd_core.c against this TinyUSB (control stages, ISO alloc/activate, request copy)"
#endif

#define TICK_US 250000   /* deferred-control deadline resolution, watchdog feed */
#define TASK_STACK 6144

static const char *TAG = "yoke-usbd";
#if TUD_OPT_HIGH_SPEED
static const uint8_t rhport = TINYUSB_PORT_HIGH_SPEED_0;
#else
static const uint8_t rhport = TINYUSB_PORT_FULL_SPEED_0;
#endif
static const tusb_control_request_t *live;
static bool installed;
static volatile bool unplugged;
static volatile bool tick_queued;
static esp_timer_handle_t tick_timer;
static esp_task_wdt_user_handle_t wdt_user;

/* dwc2_esp32.h _dwc2_controller[]: Buffer DMA reserves two FIFO words per
 * endpoint for DxEPDMAn. */
#if TUD_OPT_HIGH_SPEED
static const usbd_limits_t limits = {.ep_count = 16, .ep_in_count = 8,
                                     .fifo_words = 1024 - 2 * 16,
                                     .max_interfaces = CFG_TUD_INTERFACE_MAX,
                                     .high_speed = true};
#else
static const usbd_limits_t limits = {.ep_count = 7, .ep_in_count = 5,
                                     .fifo_words = 256 - 2 * 7,
                                     .max_interfaces = CFG_TUD_INTERFACE_MAX,
                                     .high_speed = false};
#endif

/* ---- port ---- */

static bool port_control_xfer(void *data, uint16_t len) { return tud_control_xfer(rhport, live, data, len); }
static bool port_control_status(void) { return tud_control_status(rhport, live); }
static void port_control_stall(void)
{
    usbd_edpt_stall(rhport, 0x00);
    usbd_edpt_stall(rhport, 0x80);
}
static bool port_fifo_alloc(uint8_t address, uint16_t max_packet)
{
    return usbd_edpt_iso_alloc(rhport, address, max_packet);
}
/* Despite the name, DWC2's ISO activate is type-agnostic: disable (abort),
 * then activate from the descriptor with DATA0. */
static bool port_activate(const uint8_t *desc)
{
    return usbd_edpt_iso_activate(rhport, (const tusb_desc_endpoint_t *)desc);
}
static bool port_xfer(uint8_t address, uint8_t *data, uint16_t len)
{
    return usbd_edpt_xfer(rhport, address, data, len, false);
}
static void port_stall(uint8_t address) { usbd_edpt_stall(rhport, address); }
static void port_post(void (*fn)(void *), void *arg) { usbd_defer_func(fn, arg, false); }
static yoke_usbd_speed_t port_speed(void)
{
    if (!tud_connected()) return YOKE_USBD_SPEED_UNKNOWN;
    return tud_speed_get() == TUSB_SPEED_HIGH ? YOKE_USBD_SPEED_HIGH : YOKE_USBD_SPEED_FULL;
}
static uint32_t port_now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

static const usbd_port_t port = {
    .control_xfer = port_control_xfer, .control_status = port_control_status,
    .control_stall = port_control_stall, .fifo_alloc = port_fifo_alloc,
    .activate = port_activate, .xfer = port_xfer, .stall = port_stall, .post = port_post,
    .speed = port_speed, .now_ms = port_now_ms,
};

/* ---- application class driver ---- */

static void driver_init(void) {}
static bool driver_deinit(void) { return true; }
static void driver_reset(uint8_t port_number)
{
    (void)port_number;
    usbd_core_reset();
}
static uint16_t driver_open(uint8_t port_number, const tusb_desc_interface_t *itf, uint16_t max_len)
{
    (void)port_number;
    return usbd_core_open((const uint8_t *)itf, max_len);
}
static bool driver_control(uint8_t port_number, uint8_t stage, const tusb_control_request_t *request)
{
    (void)port_number;
    live = request;
    return usbd_core_control(stage, (const uint8_t *)request);
}
static bool driver_xfer(uint8_t port_number, uint8_t address, xfer_result_t result, uint32_t length)
{
    (void)port_number;
    usbd_core_xfer_done(address, result == XFER_RESULT_SUCCESS, length);
    return true;
}

static const usbd_class_driver_t driver = {
    .name = "yoke-mirror", .init = driver_init, .deinit = driver_deinit,
    .reset = driver_reset, .open = driver_open, .control_xfer_cb = driver_control,
    .xfer_cb = driver_xfer,
};

usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *count)
{
    *count = 1;
    return &driver;
}

/* Device-recipient vendor requests bypass class drivers in usbd.c. */
bool tud_vendor_control_xfer_cb(uint8_t port_number, uint8_t stage,
                                tusb_control_request_t const *request)
{
    return driver_control(port_number, stage, request);
}

/* ---- descriptors ---- */

uint8_t const *__wrap_tud_descriptor_device_cb(void)
{
    const yoke_usbd_descriptors_t *d = usbd_core_descriptors();
    return d ? d->device : NULL;
}

uint8_t const *__wrap_tud_descriptor_configuration_cb(uint8_t index)
{
    const yoke_usbd_descriptors_t *d = usbd_core_descriptors();
    return d && index < d->num_configs ? d->configs[index] : NULL;
}

uint16_t const *__wrap_tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    const yoke_usbd_descriptors_t *d = usbd_core_descriptors();
    /* usbd.c reads the bytes only (bLength, then a copy). */
    return d ? (const uint16_t *)(const void *)usbd_plan_string(d, index, langid) : NULL;
}

uint8_t const *tud_descriptor_bos_cb(void)
{
    const yoke_usbd_descriptors_t *d = usbd_core_descriptors();
    return d ? d->bos : NULL;
}

#if TUD_OPT_HIGH_SPEED
/* On a high-speed port esp_tinyusb's strong qualifier and other-speed
 * callbacks win (and usbd.c calls its own weak defaults, so --wrap cannot
 * reach them). They answer from the qualifier and full-speed configuration
 * esp_tinyusb was installed with: buffers this backend owns and fills from
 * each presentation (usbd_plan_other_speed). */
#define OTHER_SPEED_MAX 512u
static uint8_t qualifier_mirror[10];
/* Its install sizes the other-speed copy from this wTotalLength. */
static uint8_t other_speed_mirror[OTHER_SPEED_MAX] = {9, 2, OTHER_SPEED_MAX & 0xffu,
                                                     OTHER_SPEED_MAX >> 8, 0, 1, 0, 0x80, 50};
#else
/* Overrides usbd.c's weak defaults (esp_tinyusb defines these only for
 * high-speed ports). */
uint8_t const *tud_descriptor_device_qualifier_cb(void)
{
    const yoke_usbd_descriptors_t *d = usbd_core_descriptors();
    return d ? d->qualifier : NULL;
}

uint8_t const *tud_descriptor_other_speed_configuration_cb(uint8_t index)
{
    const yoke_usbd_descriptors_t *d = usbd_core_descriptors();
    return d && index == 0 ? d->other_speed : NULL;
}
#endif

/* ---- bus events ---- */

static void bus_event(void *arg) { usbd_core_bus((yoke_usbd_event_t)(uintptr_t)arg); }

/* Raw DCD events, in the USB ISR: reset and unplug carry no esp_tinyusb
 * event. usbd.c queues the event before calling this hook, so the deferred
 * call runs after its own reset handling (the class driver's reset). */
void tud_event_hook_cb(uint8_t port_number, uint32_t event, bool in_isr)
{
    (void)port_number;
    if (event == DCD_EVENT_BUS_RESET) {
        usbd_defer_func(bus_event, (void *)(uintptr_t)YOKE_USBD_RESET, in_isr);
    } else if (event == DCD_EVENT_UNPLUGGED) {
        unplugged = true;
        usbd_defer_func(bus_event, (void *)(uintptr_t)YOKE_USBD_DISCONNECT, in_isr);
    }
}

static void on_tinyusb(tinyusb_event_t *event, void *arg)
{
    (void)arg;
    switch (event->id) {
    case TINYUSB_EVENT_ATTACHED: /* SET_CONFIGURATION, after open() */
        usbd_core_configured();
        break;
    case TINYUSB_EVENT_DETACHED: /* SET_CONFIGURATION(0), or an unplug */
        if (unplugged) unplugged = false;
        else usbd_core_configured();
        break;
    case TINYUSB_EVENT_SUSPENDED:
        usbd_core_bus(YOKE_USBD_SUSPEND);
        break;
    case TINYUSB_EVENT_RESUMED:
        usbd_core_bus(YOKE_USBD_RESUME);
        break;
    }
}

/* esp_tinyusb's task blocks in tud_task() forever, so it cannot feed the
 * task watchdog itself: a periodic tick queued to it does (at most one
 * outstanding), and also expires unanswered deferred control requests. */
static void tick(void *arg)
{
    (void)arg;
    tick_queued = false;
    (void)esp_task_wdt_reset_user(wdt_user);
    usbd_core_tick();
}

static void queue_tick(void *arg)
{
    (void)arg;
    if (tick_queued) return;
    tick_queued = true;
    usbd_defer_func(tick, NULL, false);
}

static void connected(void *arg)
{
    (void)arg;
    usbd_core_bus(YOKE_USBD_CONNECT);
}

/* Only to satisfy esp_tinyusb's install checks: the wrapped callbacks serve
 * the presentation instead. */
static const uint8_t placeholder_device[18] = {18, 1, 0x00, 0x02, 0, 0, 0, 64, 0, 0, 0, 0,
                                               0, 0, 0, 0, 0, 1};
static const uint8_t placeholder_config[9] = {9, 2, 9, 0, 0, 1, 0, 0x80, 50};

esp_err_t yoke_usbd_start(void)
{
    if (installed) return ESP_ERR_INVALID_STATE;
    usbd_core_init(&port, &limits);
#if TUD_OPT_HIGH_SPEED
    tinyusb_config_t cfg = TINYUSB_CONFIG_HIGH_SPEED(on_tinyusb, NULL);
    cfg.descriptor.high_speed_config = placeholder_config;
    cfg.descriptor.qualifier = (const tusb_desc_device_qualifier_t *)qualifier_mirror;
    cfg.descriptor.full_speed_config = other_speed_mirror;
#else
    tinyusb_config_t cfg = TINYUSB_CONFIG_FULL_SPEED(on_tinyusb, NULL);
    cfg.descriptor.full_speed_config = placeholder_config;
#endif
    cfg.descriptor.device = (const tusb_desc_device_t *)placeholder_device;
    cfg.task.size = TASK_STACK;
    esp_err_t err = tinyusb_driver_install(&cfg);
    /* dcd_init ends by connecting: drop the pull-up again. esp_tinyusb logs
     * before returning, so the computer sees an attach pulse of a few
     * milliseconds, below its 100 ms connect debounce (⚠ unmeasured). */
    if (err == ESP_OK) tud_disconnect();
    const esp_timer_create_args_t timer = {.callback = queue_tick, .name = "usbd-tick"};
    if (err == ESP_OK) err = esp_timer_create(&timer, &tick_timer);
    if (err == ESP_OK) err = esp_timer_start_periodic(tick_timer, TICK_US);
    /* Watched only once the tick that feeds it runs. */
    if (err == ESP_OK) err = esp_task_wdt_add_user("usb-device", &wdt_user);
    installed = err == ESP_OK;
    return err;
}

int yoke_usbd_present(const yoke_usbd_descriptors_t *d, const yoke_usbd_sink_t *sink,
                      void *ctx, char *why, size_t cap)
{
    if (!installed || usbd_core_descriptors()) {
        snprintf(why, cap, installed ? "a device is already presented" : "not started");
        return -1;
    }
    if (usbd_plan_check(d, &limits, why, cap)) return -1;
#if TUD_OPT_HIGH_SPEED
    usbd_plan_other_speed(d, qualifier_mirror, other_speed_mirror, sizeof(other_speed_mirror));
#endif
    usbd_core_present(d, sink, ctx);
    usbd_defer_func(connected, NULL, false); /* ahead of any bus event */
    tud_connect();
    ESP_LOGI(TAG, "presenting %04x:%04x at %s speed", d->device[8] | d->device[9] << 8,
             d->device[10] | d->device[11] << 8,
             d->speed == YOKE_USBD_SPEED_HIGH ? "high" : "full");
    return 0;
}

void yoke_usbd_withdraw(void)
{
    usbd_core_withdraw();
    tud_disconnect();
}

uint8_t *yoke_usbd_alloc(size_t size)
{
#if CONFIG_CACHE_L1_CACHE_LINE_SIZE
    /* dcd_dwc2 cleans/invalidates whole cache lines around every buffer. */
    const size_t align = CONFIG_CACHE_L1_CACHE_LINE_SIZE;
#else
    const size_t align = 4;
#endif
    size = (size + align - 1) / align * align;
    return heap_caps_aligned_calloc(align, 1, size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
}
