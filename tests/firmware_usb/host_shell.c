/* The bridge's target glue, unchanged, over host stand-ins: the YWP/1
 * shell (box_ywp.c) with native/ywp, the network task's watch table
 * (box_net.c) and the device role's start (box_device.c, box_host.c) with
 * native/yusb. Sockets are fakes that fail if the network stack is not up;
 * the clock gate, NVS and the USB host backend are test-controlled. HOST
 * MODEL ONLY: no lwIP, multicast, SNTP or task scheduling. */
#include <assert.h>
#include <errno.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "bridge.h"
#include "box_clock.h"
#include "box_ywp.h"
#include "cJSON.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_vfs_eventfd.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "ywp.h"
#include "yoke_usbh.h"

#define UDP_FD 1000
#define TCP_FD 2000

/* ---- the network stack ---- */

static bool network_up, udp_fails, tcp_bind_fails;
static unsigned sockets_made, udp_opens, listens, sends, receives;
static unsigned udp_closes, tcp_closes, shutdowns;
static int last_shutdown_fd = -1;
static uint32_t last_udp_bind_addr;
static uint16_t last_udp_bind_port;
static uint8_t inbound[YWP_MAX_FRAME];
static size_t inbound_len;
static int inbound_left = -1; /* datagrams until EAGAIN; -1: never stops */
static uint32_t inbound_from;

int stub_socket(int domain, int type, int protocol)
{
    (void)domain; (void)protocol;
    assert(network_up); /* lwIP's mailboxes exist only after esp_netif_init */
    if (type == SOCK_DGRAM && udp_fails) {
        errno = ENOMEM;
        return -1;
    }
    sockets_made++;
    if (type == SOCK_DGRAM) {
        udp_opens++;
        return UDP_FD;
    }
    return TCP_FD;
}
int stub_bind(int fd, const struct sockaddr *address, socklen_t size)
{
    assert(size >= sizeof(struct sockaddr_in));
    struct sockaddr_in sin;
    memcpy(&sin, address, sizeof(sin));
    if (fd == TCP_FD && tcp_bind_fails) {
        errno = EADDRINUSE;
        return -1;
    }
    if (fd == UDP_FD) {
        assert(sin.sin_family == AF_INET);
        /* YWP/1 control sockets must bind the multicast group, never INADDR_ANY. */
        assert(sin.sin_addr.s_addr == inet_addr(YWP_CONTROL_GROUP));
        assert(sin.sin_port == htons(YWP_CONTROL_PORT));
        last_udp_bind_addr = sin.sin_addr.s_addr;
        last_udp_bind_port = sin.sin_port;
    }
    return 0;
}
int stub_listen(int fd, int backlog)
{
    (void)backlog;
    assert(fd == TCP_FD);
    listens++;
    return 0;
}
int stub_accept(int fd, struct sockaddr *address, socklen_t *size)
{
    (void)fd; (void)address; (void)size;
    errno = EAGAIN;
    return -1;
}
int stub_setsockopt(int fd, int level, int name, const void *value, socklen_t size)
{
    (void)fd; (void)level; (void)name; (void)value; (void)size;
    return 0;
}
int stub_fcntl(int fd, int command, ...)
{
    (void)fd; (void)command;
    return 0;
}
/* A source that never stops sending inbound[] (when inbound_len). */
ssize_t stub_recvfrom(int fd, void *buffer, size_t size, int flags, struct sockaddr *from,
                      socklen_t *from_size)
{
    (void)flags;
    assert(fd == UDP_FD && *from_size >= sizeof(struct sockaddr_in));
    if (!inbound_len || !inbound_left) {
        errno = EAGAIN;
        return -1;
    }
    if (inbound_left > 0) inbound_left--;
    receives++;
    memcpy(buffer, inbound, inbound_len < size ? inbound_len : size);
    struct sockaddr_in source = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(inbound_from)};
    memcpy(from, &source, sizeof(source));
    return (ssize_t)inbound_len;
}
ssize_t stub_sendto(int fd, const void *buffer, size_t size, int flags,
                    const struct sockaddr *to, socklen_t to_size)
{
    (void)buffer; (void)flags; (void)to; (void)to_size;
    assert(fd == UDP_FD);
    sends++;
    return (ssize_t)size;
}
int stub_shutdown(int fd, int how)
{
    assert((fd == UDP_FD || fd == TCP_FD) && how == SHUT_RDWR);
    shutdowns++;
    last_shutdown_fd = fd;
    return 0;
}
int stub_close(int fd)
{
    assert(fd == UDP_FD || fd == TCP_FD);
    if (fd == UDP_FD) udp_closes++;
    else tcp_closes++;
    return 0;
}

/* ---- NVS, hashing, JSON ---- */

static uint8_t nvs_blob[512];
static size_t nvs_size;

esp_err_t nvs_open(const char *name, nvs_open_mode_t mode, nvs_handle_t *handle)
{
    (void)name; (void)mode;
    *handle = 1;
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) { (void)handle; }
esp_err_t nvs_get_blob(nvs_handle_t handle, const char *key, void *out, size_t *size)
{
    (void)handle; (void)key;
    if (!nvs_size) return ESP_ERR_NVS_NOT_FOUND;
    assert(*size >= nvs_size);
    memcpy(out, nvs_blob, nvs_size);
    *size = nvs_size;
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t handle, const char *key, const void *value, size_t size)
{
    (void)handle; (void)key;
    assert(size <= sizeof(nvs_blob));
    memcpy(nvs_blob, value, size);
    nvs_size = size;
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle)
{
    (void)handle;
    return ESP_OK;
}

void mbedtls_sha256_init(mbedtls_sha256_context *ctx) { memset(ctx, 0, sizeof(*ctx)); }
int mbedtls_sha256_starts(mbedtls_sha256_context *ctx, int is224)
{
    assert(!is224);
    ywp_crypto_portable.sha256_start(NULL, ctx->state);
    return 0;
}
int mbedtls_sha256_update(mbedtls_sha256_context *ctx, const unsigned char *data, size_t len)
{
    ywp_crypto_portable.sha256_update(NULL, ctx->state, data, len);
    return 0;
}
int mbedtls_sha256_finish(mbedtls_sha256_context *ctx, unsigned char digest[32])
{
    ywp_crypto_portable.sha256_finish(NULL, ctx->state, digest);
    return 0;
}
void mbedtls_sha256_free(mbedtls_sha256_context *ctx) { (void)ctx; }

static struct {
    char name[32], text[128];
    double number;
} members[64];
static size_t nmembers;
static unsigned state_adds;
static cJSON *record(const char *name, const char *text, double number)
{
    if (!strcmp(name, "state")) state_adds++;
    size_t i = 0;
    while (i < nmembers && strcmp(members[i].name, name)) ++i;
    if (i == nmembers) {
        assert(nmembers < 64);
        snprintf(members[nmembers++].name, sizeof(members[0].name), "%s", name);
    }
    snprintf(members[i].text, sizeof(members[i].text), "%s", text ? text : "");
    members[i].number = number;
    return (cJSON *)&members[i];
}
cJSON *cJSON_AddObjectToObject(cJSON *object, const char *name)
{
    (void)object;
    return record(name, NULL, 0);
}
cJSON *cJSON_AddStringToObject(cJSON *object, const char *name, const char *value)
{
    (void)object;
    return record(name, value, 0);
}
cJSON *cJSON_AddNumberToObject(cJSON *object, const char *name, double value)
{
    (void)object;
    return record(name, NULL, value);
}
cJSON *cJSON_AddBoolToObject(cJSON *object, const char *name, bool value)
{
    (void)object;
    return record(name, NULL, value);
}
const char *stub_json_string(const char *name)
{
    for (size_t i = 0; i < nmembers; ++i)
        if (!strcmp(members[i].name, name)) return members[i].text;
    return NULL;
}
double stub_json_number(const char *name)
{
    for (size_t i = 0; i < nmembers; ++i)
        if (!strcmp(members[i].name, name)) return members[i].number;
    return -1;
}

/* ---- the box around the glue ---- */

static bool clock_synced;
bool box_clock_synced(void) { return clock_synced; }
bool box_heap_fits(size_t need) { return need < 1024u * 1024u; }

static void (*posted_fn[16])(void *);
static void *posted_arg[16];
static size_t posted;
void yoke_usbh_post(void (*fn)(void *), void *arg)
{
    assert(posted < 16);
    posted_fn[posted] = fn;
    posted_arg[posted++] = arg;
}
static void run_usb_task(void)
{
    for (size_t i = 0; i < posted; ++i) posted_fn[i](posted_arg[i]);
    posted = 0;
}
static const yoke_usbh_sink_t *usb_sink;
esp_err_t yoke_usbh_start(const yoke_usbh_sink_t *sink, void *ctx)
{
    (void)ctx;
    usb_sink = sink;
    return ESP_OK;
}
static const uint8_t stub_dev_desc[18] = {18, 1, 0x00, 0x02, 0, 0, 0, 64, 0x09, 0x12, 0x01, 0x00,
                                          0, 1, 0, 0, 0, 1};
static const uint8_t stub_cfg_desc[18] = {9, 2, 18, 0, 1, 1, 0, 0x80, 50,
                                          9, 4, 0, 0, 0, 3, 0, 0, 0};
const uint8_t *yoke_usbh_device_descriptor(void) { return stub_dev_desc; }
const uint8_t *yoke_usbh_config_descriptor(void) { return stub_cfg_desc; }
unsigned yoke_usbh_speed(void) { return 2; }
unsigned yoke_usbh_address(void) { return 1; }
int yoke_usbh_claim(void) { return -YOKE_USBH_ENODEV; }
void yoke_usbh_release(void) {}
int yoke_usbh_endpoint_type(uint8_t endpoint)
{
    (void)endpoint;
    return -1;
}
int yoke_usbh_submit(yoke_usbh_urb_t *urb)
{
    (void)urb;
    return -YOKE_USBH_ENODEV;
}
void yoke_usbh_cancel(yoke_usbh_urb_t *urb) { (void)urb; }

static unsigned polls;
static void role_route(uint32_t generation, bool detach, const char *producer, const char *media,
                       size_t media_len)
{
    (void)generation; (void)detach; (void)producer; (void)media; (void)media_len;
}
static void role_poll(uint32_t tag)
{
    (void)tag;
    polls++;
}
static void role_admission(const uint32_t *ipv4, size_t count)
{
    (void)ipv4; (void)count;
}
static void role_status(cJSON *usb) { (void)usb; }
static const box_bridge_t consumer_role = {.route = role_route, .poll = role_poll,
                                           .admission = role_admission, .status = role_status};

static box_config_t config = {.role = BOX_ROLE_COMPUTER, .node_id = "desk-a",
                              .psk = "0123456789abcdef0123456789abcdef", .psk_len = 32};

static const char *status_text(const char *name)
{
    nmembers = 0;
    state_adds = 0;
    box_ywp_status(NULL);
    assert(state_adds == 1);
    return stub_json_string(name);
}

/* ---- modes ---- */

/* Sockets wait for the box's first address (lwIP is up by then). */
static int mode_start_order(void)
{
    assert(box_net_init() == ESP_OK);
    assert(box_device.start() == ESP_OK && usb_sink);
    run_usb_task();
    assert(!sockets_made); /* nothing listened before the network existed */
    network_up = true;
    box_device.online();
    run_usb_task();
    assert(sockets_made == 1 && listens == 1);
    return 0;
}

/* The node waits for a synchronized wall clock, and so does a rebase. */
static int mode_clock_gate(void)
{
    network_up = true;
    stub_time_us = 1000000;
    assert(box_ywp_open(&config, &consumer_role, 0x0a000002u) == ESP_OK);
    assert(box_ywp_running() && box_ywp_fd() == -1 && !sends);
    assert(!strcmp(status_text("state"), "waiting-for-clock"));
    assert(strstr(stub_json_string("error"), "wall clock"));
    assert(box_ywp_rebase() == ESP_ERR_INVALID_STATE && !nvs_size);
    stub_time_us += 1500000;
    box_ywp_timers();
    assert(box_ywp_fd() == -1); /* still waiting */
    clock_synced = true;
    box_ywp_timers();
    assert(box_ywp_fd() == UDP_FD && !strcmp(status_text("error"), ""));
    for (int i = 0; i < 100 && !sends; ++i) {
        stub_time_us += 100000;
        box_ywp_timers();
    }
    assert(sends); /* announcing */
    assert(box_ywp_rebase() == ESP_OK && nvs_size);
    return 0;
}

/* A failed control socket does not stop the node; it is retried. */
static int mode_socket_reopen(void)
{
    network_up = true;
    clock_synced = true;
    udp_fails = true;
    stub_time_us = 1000000;
    assert(box_ywp_open(&config, &consumer_role, 0x0a000002u) == ESP_OK);
    assert(box_ywp_fd() == -1 && strstr(status_text("error"), "retrying"));
    assert(strcmp(stub_json_string("state"), "waiting-for-clock"));
    assert(!box_ywp_receive());
    udp_fails = false;
    stub_time_us += 1100000;
    box_ywp_timers();
    assert(box_ywp_fd() == UDP_FD && !strcmp(status_text("error"), "") && polls == 1);
    return 0;
}

/* Forged frames from one source (spoofable, e.g. a peer's address): a
 * trickle is always parsed, so it cannot mute the peer; a flood is handled
 * one batch per wake, feeding the watchdog, and once a wake filled its
 * batch the failing source is ignored unparsed. */
static int mode_receive_bound(void)
{
    network_up = true;
    clock_synced = true;
    stub_time_us = 1000000;
    assert(box_ywp_open(&config, &consumer_role, 0x0a000002u) == ESP_OK);
    /* A frame signed with another key. */
    ywp_config_t cfg;
    ywp_config_default(&cfg);
    cfg.node_id = "forger";
    cfg.role = YWP_PRODUCER;
    cfg.kind = YWP_USB;
    cfg.psk = (const uint8_t *)"another key, also thirty-two by.";
    cfg.psk_len = 32;
    ywp_saved_t saved = {0};
    ywp_err_t err;
    double wall = (double)time(NULL), t = 1.0;
    ywp_node_t *forger = ywp_node_create(&cfg, &ywp_crypto_portable, &saved, t, wall, &err);
    assert(forger);
    ywp_node_start(forger, t);
    for (int inputs = 10; !inbound_len && inputs--;) {
        const ywp_effect_t *fx;
        size_t count = ywp_node_effects(forger, &fx);
        uint32_t persist = 0;
        for (size_t i = 0; i < count && !inbound_len; ++i) {
            if (fx[i].kind == YWP_FX_SEND) inbound_len = ywp_node_encode(forger, &fx[i], wall, inbound);
            if (fx[i].kind == YWP_FX_PERSIST) persist = fx[i].persist.request_id;
        }
        if (persist) {
            ywp_node_persisted(forger, persist, true, t); /* its start barrier */
        } else {
            t = ywp_node_next_deadline(forger);
            ywp_node_poll(forger, t, wall);
        }
    }
    assert(inbound_len);
    ywp_node_destroy(forger);
    inbound_from = 0x0a000005u;
    for (int wake = 0; wake < 10; ++wake) { /* 2 per wake, far past the burst */
        inbound_left = 2;
        assert(!box_ywp_receive());
    }
    status_text("state");
    assert(stub_json_number("auth") == 20 && stub_json_number("throttled") == 0);
    inbound_left = -1; /* the flood */
    unsigned resets = stub_wdt_resets;
    assert(box_ywp_receive() && receives == 36); /* a full batch, still parsed */
    assert(stub_wdt_resets > resets);
    assert(box_ywp_receive() && receives == 52); /* under load: throttled */
    status_text("state");
    assert(stub_json_number("auth") == 36 && stub_json_number("throttled") == 16);
    inbound_left = 2; /* the flood ends: this wake still throttles, the next does not */
    assert(!box_ywp_receive());
    inbound_left = 2;
    assert(!box_ywp_receive());
    status_text("state");
    assert(stub_json_number("auth") == 38 && stub_json_number("throttled") == 18);
    return 0;
}

/* The network task is woken only when a watch changes. */
static long drain_wakes(void)
{
    uint64_t count = 0;
    return read(stub_eventfd_fd, &count, sizeof(count)) == sizeof(count) ? (long)count : 0;
}
static void ready(void *arg) { (void)arg; }
static int mode_watch_wakes(void)
{
    assert(box_net_init() == ESP_OK && stub_eventfd_fd >= 0);
    box_net_watch(7, true, false, yoke_usbh_post, ready, NULL);
    assert(drain_wakes() == 1);
    box_net_watch(7, true, false, yoke_usbh_post, ready, NULL);
    assert(drain_wakes() == 0); /* unchanged: no context switch */
    box_net_watch(7, true, true, yoke_usbh_post, ready, NULL);
    assert(drain_wakes() == 1);
    box_net_unwatch(7);
    assert(drain_wakes() == 1);
    box_net_unwatch(7);
    assert(drain_wakes() == 0);
    return 0;
}

/* Drive box_net's task() for one select() iteration via the WDT hook. */
static jmp_buf net_done;
static unsigned net_wdt_limit, net_wdt_seen;
static void (*net_wdt_between)(unsigned seen);
static void stop_net_task(void)
{
    ++net_wdt_seen;
    if (net_wdt_between) net_wdt_between(net_wdt_seen);
    if (net_wdt_seen >= net_wdt_limit) longjmp(net_done, 1);
}
static void (*net_task_fn)(void *); /* static: survives longjmp */
static void run_net_task(unsigned iterations, void (*between)(unsigned seen))
{
    net_task_fn = NULL;
    for (unsigned i = 0; i < stub_task_count && !net_task_fn; ++i)
        if (!strcmp(stub_tasks[i].name, "yoke-net")) net_task_fn = stub_tasks[i].fn;
    assert(net_task_fn);
    net_wdt_seen = 0;
    net_wdt_limit = iterations + 1;
    net_wdt_between = between;
    stub_wdt_hook = stop_net_task;
    if (!setjmp(net_done)) net_task_fn(NULL);
    stub_wdt_hook = NULL;
    net_wdt_between = NULL;
}

/* Cross-core box_net_close(): direct before task start; after start it
 * unwatches, shuts down, wakes yoke-net, and closes only on yoke-net. */
static int mode_deferred_close(void)
{
    network_up = true;
    stub_time_us = 1000000;
    assert(box_net_init() == ESP_OK);
    /* Before box_net_start(): closes directly. */
    box_net_watch(TCP_FD, true, false, yoke_usbh_post, ready, NULL);
    box_net_close(TCP_FD);
    assert(tcp_closes == 1 && shutdowns == 0);
    (void)drain_wakes();
    /* After box_net_start(): unwatched, shutdown + wake, deferred close on yoke-net. */
    assert(box_net_start(&config, &consumer_role, 0x0a000002u) == ESP_OK);
    box_net_watch(TCP_FD, true, false, yoke_usbh_post, ready, NULL);
    (void)drain_wakes();
    box_net_close(TCP_FD);
    assert(tcp_closes == 1 && shutdowns == 1 && last_shutdown_fd == TCP_FD);
    /* Duplicate close of the same queued fd is ignored. */
    box_net_close(TCP_FD);
    assert(tcp_closes == 1 && shutdowns == 1);
    /* yoke-net wakes from real eventfd select() (normalized timeval) and closes TCP_FD. */
    run_net_task(1, NULL);
    assert(tcp_closes == 2);
    return 0;
}

/* Multicast group bind, link-bounce rejoin on identical IPv4, and media
 * update only when IPv4 actually changes (producer role). */
static int mode_address_rejoin(void)
{
    network_up = true;
    clock_synced = true;
    stub_time_us = 1000000;
    box_config_t prod = config;
    prod.role = BOX_ROLE_DEVICE;
    assert(box_ywp_open(&prod, &consumer_role, 0x0a000002u) == ESP_OK);
    assert(udp_opens == 1 && udp_closes == 0);
    assert(last_udp_bind_addr == inet_addr(YWP_CONTROL_GROUP));
    assert(last_udp_bind_port == htons(YWP_CONTROL_PORT));
    assert(!strcmp(status_text("error"), ""));
    /* Same IP (DHCP renewal after Ethernet link bounce): reopens/rejoins multicast. */
    unsigned sends_before = sends;
    box_ywp_address(0x0a000002u);
    assert(udp_closes == 1 && udp_opens == 2);
    assert(last_udp_bind_addr == inet_addr(YWP_CONTROL_GROUP));
    assert(!strcmp(status_text("error"), ""));
    /* Producer media did not change, so no extra media-change announcement burst. */
    assert(sends == sends_before);
    /* New IP: reopens/rejoins multicast and updates producer media without error. */
    box_ywp_address(0x0a000003u);
    assert(udp_closes == 2 && udp_opens == 3);
    assert(!strcmp(status_text("error"), ""));
    return 0;
}

static void listener_retry_step(unsigned seen)
{
    if (seen == 2) {
        /* Iteration 1 dispatched the failing poll -> state is "error". */
        assert(!strcmp(status_text("state"), "error"));
        /* Allow bind() to succeed; next poll retries listen_work() and transitions to active. */
        tcp_bind_fails = false;
        box_device.poll(0);
        run_usb_task();
        assert(listens == 1 && tcp_closes == 2);
        /* Unwatch fake fd 2000 (> Linux FD_SETSIZE) so select() runs on wake_fd. */
        box_net_unwatch(TCP_FD);
    }
}

/* Failed TCP listener bind closes the leaked fd, reports YWP_ERROR on poll,
 * and retries listen_work() on subsequent polls until it succeeds. */
static int mode_listener_retry(void)
{
    network_up = true;
    clock_synced = true;
    udp_fails = true; /* keep box_ywp_fd() == -1 so select() runs only on wake_fd */
    tcp_bind_fails = true;
    stub_time_us = 1000000;
    box_config_t prod = config;
    prod.role = BOX_ROLE_DEVICE;
    assert(box_net_init() == ESP_OK);
    assert(box_device.start() == ESP_OK && usb_sink);
    usb_sink->attached(NULL);
    box_device.online();
    run_usb_task();
    /* bind() failed after socket(): listener fd was closed and reset, not leaked. */
    assert(tcp_closes == 1 && listens == 0);
    assert(box_net_start(&prod, &box_device, 0x0a000002u) == ESP_OK);
    /* First poll while bind still fails: retries listen, closes again, reports error. */
    box_device.poll(0);
    run_usb_task();
    assert(tcp_closes == 2 && listens == 0);
    run_net_task(2, listener_retry_step);
    assert(!strcmp(status_text("state"), "active"));
    return 0;
}

int main(int argc, char **argv)
{
    static const struct { const char *name; int (*run)(void); } modes[] = {
        {"start-order", mode_start_order},       {"clock-gate", mode_clock_gate},
        {"socket-reopen", mode_socket_reopen},   {"receive-bound", mode_receive_bound},
        {"watch-wakes", mode_watch_wakes},       {"deferred-close", mode_deferred_close},
        {"address-rejoin", mode_address_rejoin}, {"listener-retry", mode_listener_retry},
    };
    assert(argc == 2);
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i)
        if (!strcmp(argv[1], modes[i].name)) return modes[i].run();
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
