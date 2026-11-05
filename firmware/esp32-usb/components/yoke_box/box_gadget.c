#include "box_gadget.h"

_Static_assert((int)YOKE_USBD_CONTROL == (int)YUSB_EV_CONTROL &&
                   (int)YOKE_USBD_DISCONNECT == (int)YUSB_EV_DISCONNECT &&
                   (int)YOKE_USBD_CONTROL_ACKED == (int)YUSB_EV_CONTROL_ACKED,
               "yoke_usbd events are Raw Gadget's, as the core's");
_Static_assert((int)YOKE_USBD_INTERRUPTED == (int)YUSB_GADGET_INTERRUPTED &&
                   (int)YOKE_USBD_HALT_CLEARED == (int)YUSB_GADGET_HALT_CLEARED &&
                   (int)YOKE_USBD_SHUTDOWN == (int)YUSB_GADGET_SHUTDOWN &&
                   (int)YOKE_USBD_ERROR == (int)YUSB_GADGET_ERROR,
               "yoke_usbd results are the core's gadget results");

static yusb_proxy_t *session;
static void (*after)(void);
static uint64_t (*clock_ms)(void);
static yoke_usbd_descriptors_t descriptors;
static yoke_usbd_string_t strings[YUSB_MAX_STRINGS];

static void op_ep0_write(void *ctx, uint8_t *data, size_t len, bool zero)
{
    (void)ctx;
    yoke_usbd_ep0_write(data, len, zero);
}
static void op_ep0_read(void *ctx, uint8_t *data, size_t len)
{
    (void)ctx;
    yoke_usbd_ep0_read(data, len);
}
static void op_ep0_stall(void *ctx)
{
    (void)ctx;
    yoke_usbd_ep0_stall();
}
static int op_ep_enable(void *ctx, const yusb_endpoint_t *ep)
{
    (void)ctx;
    return yoke_usbd_ep_enable(ep->raw);
}
static int op_ep_disable(void *ctx, int handle)
{
    (void)ctx;
    return yoke_usbd_ep_disable(handle);
}
static int op_ep_set_halt(void *ctx, int handle)
{
    (void)ctx;
    return yoke_usbd_ep_set_halt(handle);
}
/* TinyUSB configured the controller at SET_CONFIGURATION; DWC2 has no
 * VBUS draw control (bMaxPower is in the presented descriptor). */
static int op_configure(void *ctx)
{
    (void)ctx;
    return 0;
}
static int op_vbus_draw(void *ctx, unsigned milliamps)
{
    (void)ctx;
    (void)milliamps;
    return 0;
}
static yusb_speed_t op_link_speed(void *ctx)
{
    (void)ctx;
    return (yusb_speed_t)yoke_usbd_link_speed();
}
static void op_ep_write(void *ctx, int handle, uint8_t *data, size_t len, bool zero)
{
    (void)ctx;
    yoke_usbd_ep_write(handle, data, len, zero);
}
static void op_ep_read(void *ctx, int handle, uint8_t *data, size_t len)
{
    (void)ctx;
    yoke_usbd_ep_read(handle, data, len);
}
static void op_ep_cancel(void *ctx, int handle)
{
    (void)ctx;
    yoke_usbd_ep_cancel(handle);
}

static yusb_gadget_ops_t ops = {
    .ep0_write = op_ep0_write, .ep0_read = op_ep0_read, .ep0_stall = op_ep0_stall,
    .ep_enable = op_ep_enable, .ep_disable = op_ep_disable, .ep_set_halt = op_ep_set_halt,
    .configure = op_configure, .vbus_draw = op_vbus_draw, .link_speed = op_link_speed,
    .ep_write = op_ep_write, .ep_read = op_ep_read, .ep_cancel = op_ep_cancel,
};

static void on_event(void *ctx, yoke_usbd_event_t type, const uint8_t *setup)
{
    (void)ctx;
    if (session) yusb_proxy_event(session, (yusb_event_t)type, setup, clock_ms());
    after();
}
static void on_ep0_done(void *ctx, int result)
{
    (void)ctx;
    if (session) yusb_proxy_ep0_done(session, result, clock_ms());
    after();
}
static void on_ep_done(void *ctx, int handle, int result)
{
    (void)ctx;
    if (session) yusb_proxy_ep_done(session, handle, result, clock_ms());
    after();
}
static const yoke_usbd_sink_t sink = {.event = on_event, .ep0_done = on_ep0_done,
                                      .ep_done = on_ep_done};

void box_gadget_init(const yusb_buffers_t *buffers, void (*service)(void),
                     uint64_t (*now_ms)(void), yusb_log_fn log)
{
    ops.buffers = buffers;
    ops.log = log;
    after = service;
    clock_ms = now_ms;
}

const yusb_gadget_ops_t *box_gadget_ops(void) { return &ops; }

/* The descriptors the computer sees: the core's import-time cache. */
static const yoke_usbd_descriptors_t *cache(const yusb_presentation_t *p)
{
    descriptors = (yoke_usbd_descriptors_t){
        .speed = p->speed == YUSB_SPEED_HIGH ? YOKE_USBD_SPEED_HIGH : YOKE_USBD_SPEED_FULL,
        .device = yusb_blob(p, p->device_raw),
        .num_configs = p->num_configs < YOKE_USBD_MAX_CONFIGS ? p->num_configs
                                                              : YOKE_USBD_MAX_CONFIGS,
        .qualifier = yusb_blob(p, p->qualifier),
        .other_speed = yusb_blob(p, p->other_speed),
        .bos = yusb_blob(p, p->bos),
        .strings = strings,
        .num_strings = p->num_strings,
    };
    for (unsigned i = 0; i < descriptors.num_configs; ++i)
        descriptors.configs[i] = yusb_blob(p, p->config_raw[i]);
    for (unsigned i = 0; i < p->num_strings; ++i)
        strings[i] = (yoke_usbd_string_t){p->strings[i].index, p->strings[i].lang,
                                          yusb_blob(p, p->strings[i].data)};
    return &descriptors;
}

bool box_gadget_present(yusb_proxy_t *proxy, char *why, size_t cap)
{
    const yusb_presentation_t *p = yusb_proxy_presentation(proxy);
    if (yoke_usbd_present(cache(p), &sink, NULL, why, cap)) return false;
    session = proxy;
    /* As Raw Gadget's EPS_INFO: bulk or interrupt, IN on the numbers with
     * a TX FIFO only, so the core refuses what it cannot place. */
    yusb_udc_ep_t eps[15];
    unsigned ins, count = yoke_usbd_endpoints(&ins);
    uint16_t packet = descriptors.speed == YOKE_USBD_SPEED_HIGH ? 1024 : 64;
    for (unsigned i = 0; i < count; ++i)
        eps[i] = (yusb_udc_ep_t){.number = (uint8_t)(i + 1), .bulk = true, .interrupt = true,
                                 .dir_in = i + 1 <= ins, .dir_out = true,
                                 .maxpacket_limit = packet};
    yusb_proxy_gadget_started(proxy, eps, count, clock_ms());
    return true;
}

void box_gadget_withdraw(void)
{
    session = NULL;
    yoke_usbd_withdraw();
}
