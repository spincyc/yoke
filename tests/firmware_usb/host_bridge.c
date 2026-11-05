/* The whole USB/IP data path of two boxes on the host, in one thread:
 *
 *   modelled device -> yoke_usbh model -> box_host -> export core (native/yusb)
 *     -> TCP loopback (box_pump both ends) -> presentation core (native/yusb)
 *     -> box_gadget -> usbd_core (the TinyUSB backend's state machine)
 *     -> simulated usbd/DWC2 port -> modelled computer
 *
 * Everything but the two USB stacks is the firmware's own code: the cores,
 * box_host.c, box_gadget.c, box_pump.c and usbd_core.c/usbd_plan.c run
 * unchanged. The models stand in for the ESP-IDF USB Host Library (complete
 * at once) and for TinyUSB/DWC2 and the computer (consume at once), so the
 * figures are the software path's own cost, not USB bus throughput. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "box_gadget.h"
#include "box_host.h"
#include "box_pump.h"
#include "usbd_core.h"

/* ---- the modelled device: vendor class, bulk IN/OUT and interrupt IN ---- */

static const uint8_t device_desc[18] = {18, 1, 0x10, 0x01, 0xff, 0, 0, 64, 0x09, 0x12, 0x02, 0x00,
                                        0x00, 0x01, 1, 2, 0, 1};
static const uint8_t config_desc[39] = {
    9, 2, 39, 0, 1, 1, 0, 0x80, 50,
    9, 4, 0, 0, 3, 0xff, 0, 0, 0,
    7, 5, 0x81, 2, 64, 0, 0,
    7, 5, 0x02, 2, 64, 0, 0,
    7, 5, 0x83, 3, 8, 0, 1,
};
static const uint8_t langs[4] = {4, 3, 0x09, 0x04};
static const uint8_t maker[10] = {10, 3, 'Y', 0, 'o', 0, 'k', 0, 'e', 0};
static const uint8_t product[10] = {10, 3, 'L', 0, 'o', 0, 'o', 0, 'p', 0};

typedef struct { void (*fn)(void *); void *arg; } call_t;
static call_t calls[1024];
static size_t call_head, call_tail;
static yoke_usbh_urb_t *pending[YOKE_USBH_URBS];
static size_t pending_count;
static bool claimed, key_down;
static uint64_t device_out_bytes;

static void post(void (*fn)(void *), void *arg)
{
    assert(call_tail - call_head < 1024);
    calls[call_tail++ % 1024] = (call_t){fn, arg};
}
void yoke_usbh_post(void (*fn)(void *arg), void *arg) { post(fn, arg); }
const uint8_t *yoke_usbh_device_descriptor(void) { return device_desc; }
const uint8_t *yoke_usbh_config_descriptor(void) { return config_desc; }
unsigned yoke_usbh_speed(void) { return 2; }
unsigned yoke_usbh_address(void) { return 1; }
int yoke_usbh_claim(void) { claimed = true; return 0; }
void yoke_usbh_release(void) { claimed = false; }
int yoke_usbh_endpoint_type(uint8_t address)
{
    if (!(address & 0x7f)) return 0;
    for (size_t i = 18; i < sizeof(config_desc); i += 7)
        if (config_desc[i + 2] == address) return config_desc[i + 3] & 3;
    return -1;
}
int yoke_usbh_submit(yoke_usbh_urb_t *urb)
{
    assert(pending_count < YOKE_USBH_URBS);
    pending[pending_count++] = urb;
    return 0;
}
void yoke_usbh_cancel(yoke_usbh_urb_t *urb)
{
    for (size_t i = 0; i < pending_count; ++i)
        if (pending[i]->endpoint == urb->endpoint) pending[i]->tweak = 0xff; /* flushed */
}

/* The device answers what it can now, oldest first; interrupt IN waits
 * for a key. */
static bool device_step(void)
{
    bool progress = false;
    for (size_t i = 0; i < pending_count;) {
        yoke_usbh_urb_t *u = pending[i];
        int status = 0;
        uint32_t actual = 0;
        if (u->tweak == 0xff) {
            status = -YOKE_USBH_ECONNRESET;
        } else if (!(u->endpoint & 0x7f)) {
            const uint8_t *s = u->setup, *answer = NULL;
            if (s[0] == 0x80 && s[1] == 6) {
                switch (s[3]) {
                case 1: answer = device_desc; break;
                case 2: answer = config_desc; break;
                case 3: answer = s[2] == 0 ? langs : s[2] == 1 ? maker : s[2] == 2 ? product : NULL;
                    break;
                }
            }
            if (!answer) { /* anything else, vendor requests included: STALL */
                status = -YOKE_USBH_EPIPE;
            } else {
                uint32_t len = s[3] == 2 ? sizeof(config_desc) : answer[0];
                actual = len < u->length ? len : u->length;
                memcpy(u->data, answer, actual);
            }
        } else if (u->endpoint == 0x83 && !key_down) {
            ++i;
            continue;
        } else if (u->endpoint & 0x80) {
            actual = u->endpoint == 0x83 ? 8 : u->length;
            memset(u->data, u->endpoint == 0x83 ? 4 : 0x5a, actual);
            if (u->endpoint == 0x83) key_down = false;
        } else {
            actual = u->length;
            device_out_bytes += actual;
        }
        /* Keep submission order: an endpoint completes its URBs in FIFO. */
        memmove(&pending[i], &pending[i + 1], (--pending_count - i) * sizeof(pending[0]));
        box_host_complete(u, status, actual);
        progress = true;
    }
    return progress;
}

/* ---- the simulated usbd/DWC2 port and the modelled computer ---- */

static struct { uint8_t *data; uint16_t len; bool armed; } armed[32];
static uint8_t live[8];
static uint64_t host_in_bytes, host_reports;
static bool host_sends;           /* the computer writes to bulk OUT */
static void *ep0_data;
static uint16_t ep0_len;
static int ep0_busy;              /* 1: data stage, 2: status stage */
static int ep0_stalls;

static unsigned slot(uint8_t a) { return (a & 15u) | (a & 0x80u) >> 3; }
static bool p_control_xfer(void *data, uint16_t len) { ep0_data = data; ep0_len = len; ep0_busy = 1; return true; }
static bool p_control_status(void) { ep0_busy = 2; return true; }
static void p_control_stall(void) { ep0_busy = 0; ep0_stalls++; }
static bool p_fifo_alloc(uint8_t a, uint16_t mp) { (void)a; (void)mp; return true; }
static bool p_activate(const uint8_t *d) { armed[slot(d[2])].armed = false; return true; }
static bool p_xfer(uint8_t a, uint8_t *data, uint16_t len)
{
    armed[slot(a)] = (typeof(armed[0])){data, len, true};
    return true;
}
static void p_stall(uint8_t a) { armed[slot(a)].armed = false; }
static yoke_usbd_speed_t p_speed(void) { return YOKE_USBD_SPEED_FULL; }
static uint32_t p_now(void) { return (uint32_t)(clock() * 1000 / CLOCKS_PER_SEC); }
static const usbd_port_t port = {p_control_xfer, p_control_status, p_control_stall,
                                 p_fifo_alloc, p_activate, p_xfer, p_stall, post, p_speed, p_now};
static const usbd_limits_t s3 = {7, 5, 242, 16, false};

/* The thin TinyUSB glue (usbd_tinyusb.c) is target-only: the same steps. */
int yoke_usbd_present(const yoke_usbd_descriptors_t *d, const yoke_usbd_sink_t *sink, void *ctx,
                      char *why, size_t cap)
{
    if (usbd_plan_check(d, &s3, why, cap)) return -1;
    usbd_core_present(d, sink, ctx);
    return 0;
}
void yoke_usbd_withdraw(void) { usbd_core_withdraw(); }

static bool host_step(void)
{
    bool progress = false;
    if (ep0_busy) { /* the computer finishes whatever control stage is armed */
        int stage = ep0_busy == 1 ? USBD_STAGE_DATA : USBD_STAGE_ACK;
        ep0_busy = 0;
        assert(usbd_core_control((uint8_t)stage, live));
        progress = true;
    }
    for (unsigned i = 1; i < 32; ++i) {
        if (!armed[i].armed) continue;
        uint8_t address = (uint8_t)((i & 15u) | (i & 16u) << 3);
        if (!(address & 0x80) && !host_sends) continue;
        armed[i].armed = false;
        if (address & 0x80) {
            host_in_bytes += armed[i].len;
            host_reports += address == 0x83;
        } else {
            memset(armed[i].data, 0xa5, armed[i].len);
        }
        usbd_core_xfer_done(address, true, armed[i].len);
        progress = true;
    }
    return progress;
}

/* ---- the two boxes' glue, as box_computer.c and box_device.c run it ---- */

static box_export_t exporter = {.fd = -1};
static yusb_proxy_t *proxy;
static int client_fd = -1;
static bool presented;

static uint64_t now_ms(void) { return (uint64_t)clock() * 1000 / CLOCKS_PER_SEC; }
static void serviced(box_export_t *e) { (void)e; }
static void proxy_serviced(void) {}

static size_t x_rx_space(void *c, uint8_t **b) { return yusb_export_rx_space(c, b); }
static int x_rx_done(void *c, size_t n, uint64_t t) { return yusb_export_rx_done(c, n, t); }
static size_t x_tx_iov(const void *c, yusb_iov_t *v, size_t m) { return yusb_export_tx_iov(c, v, m); }
static void x_tx_done(void *c, size_t n) { yusb_export_tx_done(c, n); }
static const box_core_io_t export_io = {x_rx_space, x_rx_done, x_tx_iov, x_tx_done};
static size_t p_rx_space(void *c, uint8_t **b) { return yusb_proxy_rx_space(c, b); }
static int p_rx_done(void *c, size_t n, uint64_t t) { return yusb_proxy_rx_done(c, n, t); }
static size_t p_tx_iov(const void *c, yusb_iov_t *v, size_t m) { return yusb_proxy_tx_iov(c, v, m); }
static void p_tx_done(void *c, size_t n) { yusb_proxy_tx_done(c, n); }
static const box_core_io_t proxy_io = {p_rx_space, p_rx_done, p_tx_iov, p_tx_done};

static void step(void)
{
    int a = box_pump(exporter.fd, &export_io, exporter.x, now_ms());
    int b = box_pump(client_fd, &proxy_io, proxy, now_ms());
    assert(!((a | b) & (BOX_PUMP_CLOSED | BOX_PUMP_FAILED)));
    while (call_head != call_tail) {
        call_t c = calls[call_head++ % 1024];
        c.fn(c.arg);
    }
    device_step();
    host_step();
    if (yusb_proxy_phase(proxy) == YUSB_PHASE_READY && !presented) {
        char why[96];
        assert(box_gadget_present(proxy, why, sizeof(why)));
        presented = true;
    }
    (void)yusb_proxy_poll(proxy, now_ms());
}

static void setup_request(uint8_t type, uint8_t request, uint16_t value, uint16_t index, uint16_t len)
{
    const uint8_t s[8] = {type, request, (uint8_t)value, (uint8_t)(value >> 8), (uint8_t)index,
                          (uint8_t)(index >> 8), (uint8_t)len, (uint8_t)(len >> 8)};
    memcpy(live, s, 8);
    assert(usbd_core_control(USBD_STAGE_SETUP, live));
}

static void connect_boxes(void)
{
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in at = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    socklen_t size = sizeof(at);
    assert(listener >= 0 && !bind(listener, (struct sockaddr *)&at, size) && !listen(listener, 1));
    assert(!getsockname(listener, (struct sockaddr *)&at, &size));
    client_fd = socket(AF_INET, SOCK_STREAM, 0);
    assert(!connect(client_fd, (struct sockaddr *)&at, size));
    exporter.fd = accept(listener, NULL, NULL);
    close(listener);
    assert(!box_pump_prepare(client_fd) && !box_pump_prepare(exporter.fd));
}

/* The proxy's data buffer allocator, with a byte budget: a box whose RAM
 * cannot hold the endpoints' buffers at SET_CONFIGURATION. */
static size_t budget = SIZE_MAX, allocated;
static void *budget_alloc(void *ctx, size_t size, size_t align)
{
    (void)ctx;
    if (allocated + size > budget) return NULL;
    allocated += size;
    void *p = aligned_alloc(align, size);
    if (p) memset(p, 0, size);
    return p;
}
static void budget_free(void *ctx, void *p)
{
    (void)ctx;
    free(p);
}
static const yusb_buffers_t buffers = {4, budget_alloc, budget_free, NULL};

/* The ESP32-S3 policy of box_computer.c. */
static yusb_policy_t s3_policy(void)
{
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    policy.budget = (yusb_budget_t){.max_in = 4, .max_out = 4, .max_number = 6};
    policy.max_transfer = 4096;
    policy.in_depth_bulk = 2;
    policy.in_depth_interrupt = 2;
    policy.bot_in_slots = 2;
    policy.ep0_max = 4096;
    policy.prefetch_os_string = true;
    policy.control_timeout_ms = 3500;
    return policy;
}

/* Import, fetch and present; then the computer sends SET_CONFIGURATION
 * (usbd handles it, the sink relays it already acknowledged). race: the
 * computer's next class request (SET_IDLE) follows before the remote
 * device answered it. */
static void configure(bool starve, bool race)
{
    usbd_core_init(&port, &s3);
    box_host_init(serviced, now_ms, NULL);
    box_gadget_init(&buffers, proxy_serviced, now_ms, NULL);
    yusb_export_dev_t device;
    box_host_describe(&device);
    assert(!strcmp(device.info.busid, "1-1") && device.info.vendor == 0x1209 &&
           device.num_ifaces == 1 && device.ifaces[0].cls == 0xff);
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = YOKE_USBH_URBS;
    cfg.max_urb_bytes = 4096;
    box_host_bind(&exporter);
    exporter.x = yusb_export_create(&cfg, &exporter.ops, &device, 1);
    yusb_policy_t policy = s3_policy();
    proxy = yusb_proxy_create(&policy, box_gadget_ops(), "1-1", now_ms());
    assert(exporter.x && proxy);
    connect_boxes();
    for (int i = 0; i < 2000 && !presented; ++i) step();
    assert(presented && usbd_core_descriptors());
    if (starve) budget = allocated; /* nothing more for the endpoints' buffers */
    const uint8_t *cfgd = usbd_core_descriptors()->configs[0];
    assert(usbd_core_open(cfgd + 9, (uint16_t)(cfgd[2] - 9)) == cfgd[2] - 9);
    usbd_core_configured();
    if (race) {
        step();
        setup_request(0x21, 0x0a, 0, 0, 0);
    }
    for (int i = 0; i < 2000; ++i) step();
}

static void expect_configured(void)
{
    yusb_proxy_status_t st;
    yusb_proxy_status(proxy, &st);
    if (strcmp(st.state, "configured")) {
        fprintf(stderr, "state %s error %s end %s\n", st.state, st.error, st.end_reason);
        assert(0);
    }
    assert(claimed);
}

static void start(void)
{
    configure(false, false);
    expect_configured();
}

static double seconds(void) { return (double)clock() / CLOCKS_PER_SEC; }

static int mode_enumerate(void)
{
    start();
    /* A vendor IN request is relayed to the device and its STALL comes back. */
    setup_request(0xc0, 1, 0, 0, 4);
    for (int i = 0; i < 200; ++i) step();
    assert(ep0_stalls == 1 && ep0_busy == 0);
    printf("{\"configured\": true}\n");
    return 0;
}

static uint64_t mebibytes = 16;

static int mode_bulk_in(void)
{
    start();
    double t0 = seconds();
    while (host_in_bytes < mebibytes << 20) step();
    double dt = seconds() - t0;
    printf("{\"bulk_in_mb_per_s\": %.1f}\n", host_in_bytes / dt / 1e6);
    return 0;
}

static int mode_bulk_out(void)
{
    start();
    host_sends = true;
    double t0 = seconds();
    while (device_out_bytes < mebibytes << 18) step(); /* a quarter: one packet per round trip */
    double dt = seconds() - t0;
    printf("{\"bulk_out_mb_per_s\": %.1f}\n", device_out_bytes / dt / 1e6);
    return 0;
}

/* The endpoints' buffers do not fit: the configuration is refused with a
 * reason and nothing is half enabled; the data path stays silent. */
/* ---- a cdc-acm-like consumer speaking USB/IP to the export core ---- */

static void export_step(void)
{
    int r = box_pump(exporter.fd, &export_io, exporter.x, now_ms());
    assert(!(r & (BOX_PUMP_CLOSED | BOX_PUMP_FAILED)));
    while (call_head != call_tail) {
        call_t c = calls[call_head++ % 1024];
        c.fn(c.arg);
    }
    device_step();
}

static void client_send(const void *data, size_t len)
{
    const uint8_t *p = data;
    for (int spins = 0; len; ++spins) {
        assert(spins < 100000);
        ssize_t n = send(client_fd, p, len, 0);
        if (n > 0) {
            p += n;
            len -= (size_t)n;
        } else {
            assert(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
            export_step();
        }
    }
}

/* Exactly len bytes from the export, moving it meanwhile; a stream that
 * stopped fails the spin bound instead of hanging. */
static void client_read(void *data, size_t len)
{
    uint8_t *p = data;
    for (int spins = 0; len; ++spins) {
        assert(spins < 100000);
        ssize_t n = recv(client_fd, p, len, 0);
        if (n > 0) {
            p += n;
            len -= (size_t)n;
        } else {
            assert(n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
            export_step();
        }
    }
}

static void client_header(const yusb_usbip_hdr_t *h)
{
    uint8_t raw[YUSB_USBIP_HEADER_SIZE];
    yusb_usbip_encode(h, raw);
    client_send(raw, sizeof(raw));
}

/* Linux cdc-acm parks 16 reads; here on the model's quiet interrupt IN, more
 * than the backend's 8 transfers. A control request and unlinks behind them
 * (of a read still held by the core, and of one at the backend) must still
 * be served. */
static int mode_parked_reads(void)
{
    box_host_init(serviced, now_ms, NULL);
    yusb_export_dev_t device;
    box_host_describe(&device);
    yusb_export_cfg_t cfg;
    yusb_export_defaults(&cfg);
    cfg.max_urbs = YOKE_USBH_URBS;
    cfg.max_urb_bytes = 4096;
    box_host_bind(&exporter);
    exporter.x = yusb_export_create(&cfg, &exporter.ops, &device, 1);
    assert(exporter.x);
    connect_boxes();
    uint8_t req[YUSB_REQ_IMPORT_SIZE], rep[YUSB_REP_IMPORT_SIZE];
    assert(yusb_req_import_encode("1-1", req) == 0);
    client_send(req, sizeof(req));
    client_read(rep, sizeof(rep));
    uint16_t code;
    uint32_t status;
    yusb_device_info_t info;
    assert(!yusb_op_decode(rep, &code, &status) && code == YUSB_OP_REP_IMPORT && !status);
    assert(!yusb_device_info_decode(rep + YUSB_OP_HEADER_SIZE, &info));
    uint32_t devid = yusb_devid(&info);
    static const uint8_t no_setup[8];
    for (uint32_t seq = 1; seq <= 16; ++seq) {
        yusb_usbip_hdr_t read = yusb_cmd_submit(seq, devid, YUSB_USBIP_DIR_IN, 3, 0, 8, 1, no_setup);
        client_header(&read);
    }
    const uint8_t get_device[8] = {0x80, 6, 0, 1, 0, 0, 18, 0};
    yusb_usbip_hdr_t h = yusb_cmd_submit(17, devid, YUSB_USBIP_DIR_IN, 0, 0, 18, 0, get_device);
    client_header(&h);
    h = yusb_cmd_unlink(18, devid, 16); /* held by the core */
    client_header(&h);
    h = yusb_cmd_unlink(19, devid, 1);  /* at the backend: flushed */
    client_header(&h);
    bool control = false, unlinked[2] = {false, false};
    while (!control || !unlinked[0] || !unlinked[1]) {
        uint8_t raw[YUSB_USBIP_HEADER_SIZE];
        client_read(raw, sizeof(raw));
        assert(!yusb_usbip_decode(raw, &h));
        if (h.command == YUSB_RET_SUBMIT) {
            assert(h.seqnum == 17 && h.status == 0 && h.actual_length == 18 && !control);
            uint8_t desc[18];
            client_read(desc, sizeof(desc));
            assert(!memcmp(desc, device_desc, sizeof(desc)));
            control = true;
        } else {
            assert(h.command == YUSB_RET_UNLINK && (h.seqnum == 18 || h.seqnum == 19));
            assert(h.status == -YUSB_ECONNRESET);
            unlinked[h.seqnum - 18] = true;
        }
    }
    printf("{\"served\": 3}\n");
    return 0;
}

static int mode_no_ram(void)
{
    configure(true, false);
    yusb_proxy_status_t st;
    yusb_proxy_status(proxy, &st);
    assert(strcmp(st.state, "configured") && strstr(st.error, "UDC could not configure"));
    assert(host_in_bytes == 0);
    printf("{\"refused\": true}\n");
    return 0;
}

/* SET_CONFIGURATION is acknowledged by TinyUSB at once; a class request
 * right behind it must not abandon its remote completion, and is answered
 * (the model's device stalls SET_IDLE). */
static int mode_set_config_race(void)
{
    configure(false, true);
    expect_configured();
    assert(ep0_stalls == 1 && !ep0_busy);
    printf("{\"configured\": 1}\n");
    return 0;
}

static int mode_report(void)
{
    start();
    /* Reports keep their order and count; the bulk pump runs alongside. */
    for (int k = 0; k < 100; ++k) {
        uint64_t before = host_reports;
        key_down = true;
        for (int i = 0; i < 400 && host_reports == before; ++i) step();
        assert(host_reports == before + 1);
    }
    double t0 = seconds();
    for (uint64_t k = 0; k < mebibytes * 64; ++k) {
        uint64_t before = host_reports;
        key_down = true;
        while (host_reports == before) step();
    }
    printf("{\"report_path_us\": %.1f}\n", (seconds() - t0) * 1e6 / (double)(mebibytes * 64));
    return 0;
}

/* mode [MiB]: the bulk volumes scale with MiB (default 16). */
int main(int argc, char **argv)
{
    assert(argc == 2 || argc == 3);
    if (argc == 3) mebibytes = strtoull(argv[2], NULL, 10);
    if (!strcmp(argv[1], "enumerate")) return mode_enumerate();
    if (!strcmp(argv[1], "bulk-in")) return mode_bulk_in();
    if (!strcmp(argv[1], "bulk-out")) return mode_bulk_out();
    if (!strcmp(argv[1], "report")) return mode_report();
    if (!strcmp(argv[1], "no-ram")) return mode_no_ram();
    if (!strcmp(argv[1], "set-config-race")) return mode_set_config_race();
    if (!strcmp(argv[1], "parked-reads")) return mode_parked_reads();
    return 2;
}
