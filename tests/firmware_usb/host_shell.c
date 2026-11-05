/* The bridge's target glue, unchanged, over host stand-ins: the YWP/1
 * shell (box_ywp.c) with native/ywp, the network task's watch table
 * (box_net.c) and the device role's start (box_device.c, box_host.c) with
 * native/yusb. Sockets are fakes that fail if the network stack is not up;
 * the clock gate, NVS and the USB host backend are test-controlled. HOST
 * MODEL ONLY: no lwIP, multicast, SNTP or task scheduling. */
#include <assert.h>
#include <errno.h>
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
#include "lwip/sockets.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "ywp.h"
#include "yoke_usbh.h"

#define UDP_FD 1000
#define TCP_FD 2000

/* ---- the network stack ---- */

static bool network_up, udp_fails;
static unsigned sockets_made, listens, sends, receives;
static uint8_t inbound[YWP_MAX_FRAME];
static size_t inbound_len;
static int inbound_left = -1; /* datagrams until EAGAIN; -1: never stops */
static uint32_t inbound_from;

int stub_socket(int domain, int type, int protocol)
{
    (void)domain; (void)protocol;
    assert(network_up); /* lwIP's mailboxes exist only after esp_netif_init */
    if (type == SOCK_DGRAM && udp_fails) return -1;
    sockets_made++;
    return type == SOCK_DGRAM ? UDP_FD : TCP_FD;
}
int stub_bind(int fd, const struct sockaddr *address, socklen_t size)
{
    (void)fd; (void)address; (void)size;
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
int stub_close(int fd)
{
    assert(fd == UDP_FD || fd == TCP_FD);
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
static cJSON *record(const char *name, const char *text, double number)
{
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
const uint8_t *yoke_usbh_device_descriptor(void) { return NULL; }
const uint8_t *yoke_usbh_config_descriptor(void) { return NULL; }
unsigned yoke_usbh_speed(void) { return 0; }
unsigned yoke_usbh_address(void) { return 0; }
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

static unsigned polls, routes;
static void role_route(uint32_t generation, bool detach, const char *producer, const char *media,
                       size_t media_len)
{
    (void)generation; (void)detach; (void)producer; (void)media; (void)media_len;
    routes++;
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
    box_ywp_status(NULL);
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

int main(int argc, char **argv)
{
    static const struct { const char *name; int (*run)(void); } modes[] = {
        {"start-order", mode_start_order},       {"clock-gate", mode_clock_gate},
        {"socket-reopen", mode_socket_reopen},   {"receive-bound", mode_receive_bound},
        {"watch-wakes", mode_watch_wakes},
    };
    assert(argc == 2);
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i)
        if (!strcmp(argv[1], modes[i].name)) return modes[i].run();
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 2;
}
