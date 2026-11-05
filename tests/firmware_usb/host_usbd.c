/* Host model of the TinyUSB device backend: the unchanged usbd_core.c,
 * usbd_plan.c and the CDC self-test against a simulated usbd/DWC2 port.
 * The port reproduces what usbd.c does around a class driver (request copy,
 * DATA/ACK stages, deferred calls in FIFO order, aborts that drop
 * completions); it cannot prove DWC2 timing or electrical behaviour. Each
 * mode asserts one property and exits 0. */
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "selftest_cdc.h"
#include "usbd_core.h"

#define S3 ((usbd_limits_t){7, 5, 242, 16, false})
#define P4 ((usbd_limits_t){16, 8, 992, 16, true})

/* ---- simulated port ---- */

typedef struct { void (*fn)(void *); void *arg; } call_t;
static call_t queue[64];
static size_t head, tail;
static uint8_t live[8];                  /* usbd.c's ctrl_xfer.request */
static uint32_t now;
static struct {
    int control_xfers, statuses, stalls, activations, fifo_allocs, ep_stalls;
    uint16_t control_len;
    void *control_data;
    uint8_t armed_address;
    uint8_t *armed_data;
    uint16_t armed_len;
    uint8_t activated[32];
} port_log;

static bool p_control_xfer(void *data, uint16_t len)
{
    port_log.control_xfers++;
    port_log.control_data = data;
    port_log.control_len = len;
    return true;
}
static bool p_control_status(void) { port_log.statuses++; return true; }
static void p_control_stall(void) { port_log.stalls++; }
static bool p_fifo_alloc(uint8_t address, uint16_t packet)
{
    (void)address; (void)packet;
    port_log.fifo_allocs++;
    return true;
}
static bool p_activate(const uint8_t *desc)
{
    port_log.activated[port_log.activations++ % 32] = desc[2];
    return true;
}
static bool p_xfer(uint8_t address, uint8_t *data, uint16_t len)
{
    port_log.armed_address = address;
    port_log.armed_data = data;
    port_log.armed_len = len;
    return true;
}
static void p_stall(uint8_t address) { (void)address; port_log.ep_stalls++; }
static void p_post(void (*fn)(void *), void *arg)
{
    queue[tail++ % 64] = (call_t){fn, arg};
}
static yoke_usbd_speed_t p_speed(void) { return YOKE_USBD_SPEED_FULL; }
static uint32_t p_now(void) { return now; }

static const usbd_port_t port = {p_control_xfer, p_control_status, p_control_stall,
                                 p_fifo_alloc, p_activate, p_xfer, p_stall, p_post,
                                 p_speed, p_now};

static void run_posts(void)
{
    while (head != tail) {
        call_t c = queue[head++ % 64];
        c.fn(c.arg);
    }
}

/* ---- recording sink (for direct API modes) ---- */

static int ep0_results[16], ep0_count, ep_results[16], ep_handles[16], ep_count;
static int events[16], event_count;
static void s_event(void *ctx, yoke_usbd_event_t type, const uint8_t *setup)
{
    (void)ctx; (void)setup;
    events[event_count++ % 16] = type;
    /* Completions queued before an event are delivered before it. */
}
static void s_ep0(void *ctx, int result) { (void)ctx; ep0_results[ep0_count++ % 16] = result; }
static void s_ep(void *ctx, int handle, int result)
{
    (void)ctx;
    ep_handles[ep_count % 16] = handle;
    ep_results[ep_count++ % 16] = result;
}
static const yoke_usbd_sink_t recorder = {s_event, s_ep0, s_ep};

/* ---- self-test port ---- */

static void (*later_fn)(void *);
static void *later_arg;
static uint32_t later_ms;
static void later(void (*fn)(void *), void *arg, uint32_t ms) { later_fn = fn; later_arg = arg; later_ms = ms; }
static void quiet(const char *line) { (void)line; }
static const selftest_cdc_port_t cdc_port = {later, quiet};
static uint8_t buffer[SELFTEST_CDC_BUFFER];

static const yoke_usbd_descriptors_t *present(const yoke_usbd_sink_t *sink, usbd_limits_t lim)
{
    usbd_core_init(&port, &lim);
    const yoke_usbd_descriptors_t *d = selftest_cdc_init(
        &cdc_port, lim.high_speed ? YOKE_USBD_SPEED_HIGH : YOKE_USBD_SPEED_FULL, "0123ABCD", buffer);
    char why[120] = "";
    assert(usbd_plan_check(d, &lim, why, sizeof(why)) == 0);
    usbd_core_present(d, sink, NULL);
    return d;
}

static void setup(uint8_t type, uint8_t request, uint16_t value, uint16_t index, uint16_t length)
{
    const uint8_t s[8] = {type, request, (uint8_t)value, (uint8_t)(value >> 8), (uint8_t)index,
                          (uint8_t)(index >> 8), (uint8_t)length, (uint8_t)(length >> 8)};
    memcpy(live, s, 8);
    assert(usbd_core_control(USBD_STAGE_SETUP, live));
}

/* usbd.c: SET_CONFIGURATION opens the configuration, then mounts. */
static void configure(const yoke_usbd_descriptors_t *d)
{
    assert(usbd_core_open(d->configs[0] + 9 + 8, 75 - 17) == 75 - 17);
    usbd_core_configured();
    run_posts();
}

static const selftest_cdc_status_t *cdc(void) { return selftest_cdc_status(); }

static int mode_enumerate(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    /* FIFOs for 0x81, 0x02, 0x82; alternate 0 live at once; enabling the
     * same descriptors does not activate (reset DATA0) a second time. */
    assert(port_log.fifo_allocs == 3 && port_log.activations == 3);
    assert(cdc()->configured);
    assert(port_log.armed_address == 0x02 && port_log.armed_len == 64);
    assert(port_log.statuses == 0); /* SET_CONFIGURATION was acknowledged by usbd */
    return 0;
}

static int mode_deferred_in(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    setup(0xa1, 0x21, 0, 0, 7);              /* GET_LINE_CODING */
    run_posts();
    assert(port_log.control_xfers == 0);     /* held off: DWC2 NAKs IN */
    assert(later_fn && later_ms == SELFTEST_CDC_DEFER_MS);
    later_fn(later_arg);
    assert(port_log.control_xfers == 1 && port_log.control_len == 7);
    assert(!memcmp(port_log.control_data, "\x00\xc2\x01\x00\x00\x00\x08", 7));
    assert(usbd_core_control(USBD_STAGE_DATA, live));
    run_posts();
    assert(cdc()->deferred_replies == 1 && cdc()->errors == 0);
    return 0;
}

static int mode_deferred_status(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    setup(0x21, 0x22, 3, 0, 0);              /* SET_CONTROL_LINE_STATE DTR|RTS */
    assert(port_log.statuses == 0);
    later_fn(later_arg);
    assert(port_log.statuses == 1);
    assert(usbd_core_control(USBD_STAGE_ACK, live));
    run_posts();
    assert(cdc()->dtr && cdc()->rts && cdc()->errors == 0);
    return 0;
}

static int mode_out_data(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    setup(0x21, 0x20, 0, 0, 7);              /* SET_LINE_CODING 9600 8N1 */
    assert(port_log.control_xfers == 1 && port_log.control_len == 7);
    memcpy(port_log.control_data, "\x80\x25\x00\x00\x00\x00\x08", 7);
    assert(usbd_core_control(USBD_STAGE_DATA, live)); /* usbd then sends the status */
    run_posts();
    assert(cdc()->baud == 9600);
    assert(port_log.statuses == 0 && port_log.stalls == 0);
    return 0;
}

static int mode_superseded(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    setup(0xa1, 0x21, 0, 0, 7);
    /* The host gave up; usbd answered a newer standard request itself. */
    const uint8_t newer[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};
    memcpy(live, newer, 8);
    later_fn(later_arg);
    run_posts();
    assert(port_log.control_xfers == 0);     /* never answers the wrong request */
    assert(cdc()->errors == 1);              /* ep0_done(INTERRUPTED) */
    return 0;
}

static int mode_abandoned(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    setup(0xa1, 0x21, 0, 0, 7);
    later_fn(later_arg);
    assert(port_log.control_xfers == 1);     /* the reply is armed */
    /* The host gave up after the reply was armed; usbd answered a newer
     * standard request itself, so no DATA stage reaches the driver. */
    const uint8_t newer[8] = {0x80, 0, 0, 0, 0, 0, 2, 0};
    memcpy(live, newer, 8);
    usbd_core_tick();
    run_posts();
    assert(cdc()->errors == 1);              /* ep0_done(INTERRUPTED), not stranded */
    setup(0x21, 0x22, 1, 0, 0);              /* ep0 serves the next request */
    later_fn(later_arg);
    assert(port_log.statuses == 1);
    assert(usbd_core_control(USBD_STAGE_ACK, live));
    run_posts();
    assert(cdc()->dtr && cdc()->errors == 1);
    return 0;
}

static int mode_timeout(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    setup(0xa1, 0x21, 0, 0, 7);
    now += YOKE_USBD_CONTROL_TIMEOUT_MS - 1;
    usbd_core_tick();
    assert(port_log.stalls == 0);
    now += 1;
    usbd_core_tick();
    assert(port_log.stalls == 1);            /* re-arms SETUP reception on DWC2 */
    later_fn(later_arg);
    run_posts();
    assert(port_log.control_xfers == 0 && cdc()->errors == 1);
    return 0;
}

static int mode_echo(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, S3);
    configure(d);
    memcpy(port_log.armed_data, "hello", 5);
    usbd_core_xfer_done(0x02, true, 5);
    run_posts();
    assert(port_log.armed_address == 0x82 && port_log.armed_len == 5);
    usbd_core_xfer_done(0x82, true, 5);
    run_posts();
    assert(port_log.armed_address == 0x02 && port_log.armed_len == 64);
    assert(cdc()->echoed_bytes == 5);
    return 0;
}

static int mode_reset_order(void)
{
    const yoke_usbd_descriptors_t *d = present(&recorder, S3);
    configure(d);
    int out = yoke_usbd_ep_enable(d->configs[0] + 61);
    assert(out >= 0);
    yoke_usbd_ep_read(out, buffer, 64);
    usbd_core_bus(YOKE_USBD_RESET);
    /* The SHUTDOWN completion is delivered before the RESET event. */
    assert(ep_count == 1 && ep_results[0] == YOKE_USBD_SHUTDOWN && ep_handles[0] == out);
    assert(events[event_count - 1] == YOKE_USBD_RESET);
    usbd_core_xfer_done(0x02, true, 64);     /* a completion from before the reset */
    run_posts();
    assert(ep_count == 1);
    assert(yoke_usbd_ep_enable(d->configs[0] + 61) == -ENODEV); /* unconfigured */
    return 0;
}

static int mode_chunks(void)
{
    const yoke_usbd_descriptors_t *d = present(&recorder, S3);
    configure(d);
    static uint8_t big[20000];
    int in = yoke_usbd_ep_enable(d->configs[0] + 68);
    yoke_usbd_ep_write(in, big, sizeof(big), true);
    assert(port_log.armed_len == 127 * 64);   /* whole packets, 7-bit packet counter */
    usbd_core_xfer_done(0x82, true, 8128);
    assert(port_log.armed_data == big + 8128 && port_log.armed_len == 8128);
    usbd_core_xfer_done(0x82, true, 8128);
    assert(port_log.armed_data == big + 16256 && port_log.armed_len == 20000 - 16256);
    usbd_core_xfer_done(0x82, true, 20000 - 16256);
    run_posts();
    assert(ep_count == 1 && ep_results[0] == 20000); /* short final packet: no ZLP */
    yoke_usbd_ep_write(in, big, 128, true);
    usbd_core_xfer_done(0x82, true, 128);
    assert(port_log.armed_len == 0);          /* ZLP after a packet-aligned write */
    usbd_core_xfer_done(0x82, true, 0);
    run_posts();
    assert(ep_count == 2 && ep_results[1] == 128);
    /* OUT: a full chunk continues; a short packet ends the operation. */
    int out = yoke_usbd_ep_enable(d->configs[0] + 61);
    yoke_usbd_ep_read(out, big, sizeof(big));
    assert(port_log.armed_address == 0x02 && port_log.armed_len == 8128);
    usbd_core_xfer_done(0x02, true, 8128);
    assert(port_log.armed_data == big + 8128 && port_log.armed_len == 8128);
    usbd_core_xfer_done(0x02, true, 100);
    run_posts();
    assert(ep_count == 3 && ep_results[2] == 8228);
    return 0;
}

static void complete_in(void *arg) { usbd_core_xfer_done(0x82, true, (uint32_t)(uintptr_t)arg); }

static int mode_cancel(void)
{
    const yoke_usbd_descriptors_t *d = present(&recorder, S3);
    configure(d);
    int out = yoke_usbd_ep_enable(d->configs[0] + 61);
    int before = port_log.activations;
    yoke_usbd_ep_read(out, buffer, 64);
    yoke_usbd_ep_cancel(out);
    assert(port_log.activations == before + 1); /* DWC2 abort */
    run_posts();
    assert(ep_count == 1 && ep_results[0] == YOKE_USBD_INTERRUPTED);
    usbd_core_xfer_done(0x02, true, 64);       /* stale: dropped */
    run_posts();
    assert(ep_count == 1);
    /* A completion the controller queued before the abort wins. */
    yoke_usbd_ep_read(out, buffer, 64);
    yoke_usbd_ep_cancel(out);
    usbd_core_xfer_done(0x02, true, 10);
    run_posts();
    assert(ep_count == 2 && ep_results[1] == 10);
    /* Two aborts, one fence post at a time; the second endpoint's real
     * completion, queued between the aborts, still wins over its fence. */
    const uint8_t *in_desc = d->configs[0] + 68;
    assert(in_desc[2] == 0x82);
    int in = yoke_usbd_ep_enable(in_desc);
    yoke_usbd_ep_read(out, buffer, 64);
    yoke_usbd_ep_write(in, buffer + 64, 64, false);
    yoke_usbd_ep_cancel(out);
    p_post(complete_in, (void *)(uintptr_t)64);
    yoke_usbd_ep_cancel(in);
    assert(tail - head == 2);                  /* the fence and the IN completion */
    run_posts();
    assert(ep_count == 4 && ep_handles[2] == out && ep_results[2] == YOKE_USBD_INTERRUPTED);
    assert(ep_handles[3] == in && ep_results[3] == 64);
    return 0;
}

static int mode_halt(void)
{
    const yoke_usbd_descriptors_t *d = present(&recorder, S3);
    configure(d);
    int in = yoke_usbd_ep_enable(d->configs[0] + 68);
    assert(yoke_usbd_ep_set_halt(in) == 0 && port_log.ep_stalls == 1);
    yoke_usbd_ep_write(in, buffer, 8, false);
    assert(port_log.armed_address != 0x82);    /* waits for the host's clear */
    setup(0x02, 1, 0, 0x82, 0);                /* CLEAR_FEATURE(ENDPOINT_HALT) */
    run_posts();
    assert(ep_count == 1 && ep_results[0] == YOKE_USBD_HALT_CLEARED);
    /* A clear while a transfer is armed ends it the same way (dwc3 -EPIPE). */
    yoke_usbd_ep_write(in, buffer, 8, false);
    assert(port_log.armed_address == 0x82);
    setup(0x02, 1, 0, 0x82, 0);
    run_posts();
    assert(ep_count == 2 && ep_results[1] == YOKE_USBD_HALT_CLEARED);
    assert(ep0_count == 0);                    /* usbd answered the clear itself */
    return 0;
}

/* SET_CONFIGURATION reaches the sink as CONTROL_ACKED and is owed nothing;
 * the next request is held off and answered as usual; a stall owed nothing
 * (the core's after a bus reset) completes without bus effect. */
static int mode_control_acked(void)
{
    present(&recorder, S3);
    configure(usbd_core_descriptors());
    assert(event_count && events[(event_count - 1) % 16] == YOKE_USBD_CONTROL_ACKED);
    assert(!ep0_count && !port_log.statuses && !port_log.stalls);
    setup(0x21, 0x0a, 0, 0, 0);               /* SET_IDLE right behind it */
    assert(events[(event_count - 1) % 16] == YOKE_USBD_CONTROL);
    yoke_usbd_ep0_stall();                    /* the remote refused it */
    run_posts();
    assert(port_log.stalls == 1 && ep0_count == 1 && ep0_results[0] == 0);
    usbd_core_bus(YOKE_USBD_RESET);
    yoke_usbd_ep0_stall();
    run_posts();
    assert(port_log.stalls == 1 && ep0_count == 2 && ep0_results[1] == 0);
    return 0;
}

static int mode_contract(void)
{
    const yoke_usbd_descriptors_t *d = present(&recorder, S3);
    yoke_usbd_ep0_write(buffer, 1, false);     /* no request outstanding */
    assert(yoke_usbd_ep_enable(d->configs[0] + 61) == -ENODEV);
    configure(d);
    run_posts();
    assert(ep0_count == 1 && ep0_results[0] == YOKE_USBD_INTERRUPTED); /* delivered before the event */
    uint8_t foreign[7] = {7, 5, 0x83, 2, 64, 0, 0};
    assert(yoke_usbd_ep_enable(foreign) == -EINVAL);
    int out = yoke_usbd_ep_enable(d->configs[0] + 61);
    assert(yoke_usbd_ep_enable(d->configs[0] + 61) == -EBUSY);
    yoke_usbd_ep_read(out, buffer, 64);
    assert(yoke_usbd_ep_disable(out) == -EBUSY && yoke_usbd_ep_set_halt(out) == -EBUSY);
    yoke_usbd_ep_write(out, buffer, 1, false); /* wrong direction while busy: ignored */
    usbd_core_xfer_done(0x02, true, 3);
    run_posts();
    assert(ep_results[ep_count - 1] == 3 && yoke_usbd_ep_disable(out) == 0);
    /* ep0 write for an OUT request is a contract error, not a reply. */
    setup(0x21, 0x20, 0, 0, 7);
    yoke_usbd_ep0_write(buffer, 7, false);
    run_posts();
    assert(ep0_results[ep0_count - 1] == YOKE_USBD_ERROR && port_log.control_xfers == 0);
    yoke_usbd_ep0_stall();
    run_posts();
    assert(port_log.stalls == 1 && ep0_results[ep0_count - 1] == 0);
    /* Without a sink a SETUP stalls. */
    usbd_core_withdraw();
    const usbd_limits_t s3 = S3;
    usbd_core_init(&port, &s3);
    memcpy(live, "\xc0\x01\x00\x00\x00\x00\x00\x00", 8);
    assert(!usbd_core_control(USBD_STAGE_SETUP, live));
    return 0;
}

static int mode_high_speed(void)
{
    const yoke_usbd_descriptors_t *d = present(&selftest_cdc_sink, P4);
    configure(d);
    assert(port_log.armed_address == 0x02 && port_log.armed_len == 512);
    return 0;
}

int main(int argc, char **argv)
{
    static const struct { const char *name; int (*run)(void); } modes[] = {
        {"enumerate", mode_enumerate}, {"deferred-in", mode_deferred_in},
        {"deferred-status", mode_deferred_status}, {"out-data", mode_out_data},
        {"superseded", mode_superseded}, {"abandoned", mode_abandoned},
        {"timeout", mode_timeout}, {"echo", mode_echo},
        {"reset-order", mode_reset_order}, {"chunks", mode_chunks}, {"cancel", mode_cancel},
        {"halt", mode_halt}, {"control-acked", mode_control_acked}, {"contract", mode_contract},
        {"high-speed", mode_high_speed},
    };
    assert(argc == 2);
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i)
        if (!strcmp(argv[1], modes[i].name)) return modes[i].run();
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
