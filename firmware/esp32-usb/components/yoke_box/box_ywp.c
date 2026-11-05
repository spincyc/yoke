#include "box_ywp.h"
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <string.h>
#include <sys/time.h>
#include "box_clock.h"
#include "box_limit.h"
#include "box_media.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "mbedtls/sha256.h"
#include "nvs.h"
#include "ywp.h"
#include "ywp_route.h"
#include "ywp_state.h"

#define NVS_NAMESPACE "yoke_usb"
#define STATE_KEY "ywp_state"
#define HEALTH_S 1.0          /* driver health poll, as the reference runner */
#define ADMITTED_MAX 64
#define RECEIVE_BATCH 16      /* datagrams per wake: timers and the watchdog get turns */

_Static_assert(sizeof(mbedtls_sha256_context) <= YWP_SHA256_STATE_MAX, "hash state fits");

static const char *TAG = "yoke-ywp";
static const box_config_t *config;
static const box_bridge_t *role;
static ywp_saved_t saved;
static ywp_node_t *node;
static ywp_route_t route;
static box_limit_t limit;
static int sock = -1;
static uint32_t own_ipv4;
static bool owned;            /* the saved state loaded: a node owns it */
static bool waiting;          /* for the wall clock, before the node exists */
static bool producer, health_outstanding;
static bool flooded;          /* the last wake filled its batch */
static double health_due;
static SemaphoreHandle_t snapshot_lock;
static struct {
    ywp_status_t status;
    size_t peers, footprint, stack_free;
    uint32_t datagrams, polls, datagram_us_max, poll_us_max;
    uint64_t datagram_us, poll_us, throttled;
    char error[96];
} snapshot;

static double now(void) { return (double)esp_timer_get_time() / 1e6; }

static double wall(void)
{
    struct timeval tv;
    return gettimeofday(&tv, NULL) ? NAN : (double)tv.tv_sec + (double)tv.tv_usec / 1e6;
}

/* mbedTLS hashing (the SHA engine on ESP32); the core composes HMAC. */
static void sha_start(void *ctx, void *state)
{
    (void)ctx;
    mbedtls_sha256_init(state);
    (void)mbedtls_sha256_starts(state, 0);
}

static void sha_update(void *ctx, void *state, const uint8_t *data, size_t len)
{
    (void)ctx;
    (void)mbedtls_sha256_update(state, data, len);
}

static void sha_finish(void *ctx, void *state, uint8_t digest[YWP_SHA256_LEN])
{
    (void)ctx;
    (void)mbedtls_sha256_finish(state, digest);
    mbedtls_sha256_free(state);
}

static const ywp_crypto_ops_t crypto = {.sha256_start = sha_start, .sha256_update = sha_update,
                                        .sha256_finish = sha_finish};

static void set_error(const char *error)
{
    xSemaphoreTake(snapshot_lock, portMAX_DELAY);
    snprintf(snapshot.error, sizeof(snapshot.error), "%s", error);
    xSemaphoreGive(snapshot_lock);
}

static void fail(const char *error)
{
    set_error(error);
    ESP_LOGE(TAG, "%s", error);
}

static void take_snapshot(void)
{
    xSemaphoreTake(snapshot_lock, portMAX_DELAY);
    ywp_node_status(node, &snapshot.status);
    snapshot.peers = ywp_node_peer_count(node);
    snapshot.stack_free = uxTaskGetStackHighWaterMark(NULL); /* bytes on ESP-IDF */
    xSemaphoreGive(snapshot_lock);
}

/* The node's own cost per input (effects excluded), for the bench. */
static void timed(bool datagram, int64_t started)
{
    uint32_t us = (uint32_t)(esp_timer_get_time() - started);
    xSemaphoreTake(snapshot_lock, portMAX_DELAY);
    if (datagram) {
        snapshot.datagrams++;
        snapshot.datagram_us += us;
        if (us > snapshot.datagram_us_max) snapshot.datagram_us_max = us;
    } else {
        snapshot.polls++;
        snapshot.poll_us += us;
        if (us > snapshot.poll_us_max) snapshot.poll_us_max = us;
    }
    xSemaphoreGive(snapshot_lock);
}

static esp_err_t load(ywp_saved_t *state)
{
    memset(state, 0, sizeof(*state));
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) return ESP_OK; /* first boot */
    uint8_t blob[YWP_SAVED_MAX];
    size_t size = sizeof(blob);
    esp_err_t err = nvs_get_blob(nvs, STATE_KEY, blob, &size);
    nvs_close(nvs);
    if (err == ESP_ERR_NVS_NOT_FOUND) return ESP_OK;
    /* Corrupt state is never silently replaced by a fresh one (ADR 0023). */
    if (err != ESP_OK || !ywp_saved_decode(blob, size, state)) return ESP_ERR_INVALID_STATE;
    return ESP_OK;
}

static bool save(const ywp_saved_t *state)
{
    uint8_t blob[YWP_SAVED_MAX];
    size_t size = ywp_saved_encode(state, blob);
    nvs_handle_t nvs;
    if (!size || nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(nvs, STATE_KEY, blob, size);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    return err == ESP_OK;
}

esp_err_t box_ywp_rebase(void)
{
    if (!box_clock_synced()) return ESP_ERR_INVALID_STATE; /* the bound of a real now */
    ywp_saved_t state = {.seq = ywp_admission_bound(wall())};
    return save(&state) ? ESP_OK : ESP_FAIL;
}

static void send_frame(const ywp_effect_t *fx)
{
    static uint8_t frame[YWP_MAX_FRAME];
    size_t len = ywp_node_encode(node, fx, wall(), frame);
    struct sockaddr_in group = {.sin_family = AF_INET, .sin_port = htons(YWP_CONTROL_PORT),
                                .sin_addr.s_addr = inet_addr(YWP_CONTROL_GROUP)};
    if (!len) ESP_LOGE(TAG, "frame could not be encoded; dropped");
    else if (sock >= 0 && sendto(sock, frame, len, 0, (struct sockaddr *)&group, sizeof(group)) < 0)
        ESP_LOGW(TAG, "send failed: errno %d", errno);
}

static void admit(const ywp_effect_t *fx)
{
    uint32_t addresses[ADMITTED_MAX];
    size_t count = fx->admission.count < ADMITTED_MAX ? fx->admission.count : ADMITTED_MAX;
    for (size_t i = 0; i < count; ++i) addresses[i] = fx->admission.peers[i].addr;
    role->admission(addresses, count);
}

static void drive_route(void)
{
    ywp_route_cmd_t cmd;
    if (ywp_route_next(&route, &cmd))
        role->route(cmd.generation, cmd.detach, cmd.producer, cmd.media, cmd.media_len);
}

/* Perform the effects of the last input; PERSIST ends a batch and its
 * completion is the next input. */
static void perform(void)
{
    for (;;) {
        const ywp_effect_t *fx;
        size_t count = ywp_node_effects(node, &fx);
        const ywp_effect_t *persist = NULL;
        for (size_t i = 0; i < count; ++i) {
            switch (fx[i].kind) {
            case YWP_FX_SEND: send_frame(&fx[i]); break;
            case YWP_FX_PERSIST: persist = &fx[i]; break;
            case YWP_FX_ATTACH:
            case YWP_FX_DETACH: ywp_route_submit(&route, &fx[i]); break;
            case YWP_FX_ADMISSION: admit(&fx[i]); break;
            case YWP_FX_LOG:
                if (fx[i].log.level >= YWP_LOG_WARNING) ESP_LOGW(TAG, "%s", fx[i].log.text);
                else ESP_LOGI(TAG, "%s", fx[i].log.text);
                break;
            }
        }
        take_snapshot();
        drive_route();
        if (!persist) return;
        ywp_node_persisted(node, persist->persist.request_id, save(persist->persist.state), now());
    }
}

static int open_socket(uint32_t ipv4)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0), on = 1;
    uint8_t ttl = 1, loop = 0;
    struct sockaddr_in any = {.sin_family = AF_INET, .sin_port = htons(YWP_CONTROL_PORT)};
    struct ip_mreq join = {.imr_multiaddr.s_addr = inet_addr(YWP_CONTROL_GROUP),
                           .imr_interface.s_addr = htonl(ipv4)};
    struct in_addr interface = {.s_addr = htonl(ipv4)};
    if (fd < 0 || setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on)) ||
        bind(fd, (struct sockaddr *)&any, sizeof(any)) ||
        setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &join, sizeof(join)) ||
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_IF, &interface, sizeof(interface)) ||
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl)) ||
        setsockopt(fd, IPPROTO_IP, IP_MULTICAST_LOOP, &loop, sizeof(loop)) ||
        fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK) < 0) {
        if (fd >= 0) close(fd);
        return -1;
    }
    return fd;
}

/* The node, once the wall clock is real (box_clock.h). A failed control
 * socket is retried by box_ywp_timers; the node runs meanwhile. */
static void start_node(void)
{
    char media[96];
    ywp_config_t cfg;
    ywp_config_default(&cfg);
    cfg.node_id = config->node_id;
    cfg.role = producer ? YWP_PRODUCER : YWP_CONSUMER;
    cfg.kind = YWP_USB;
    cfg.name = config->name;
    cfg.psk = config->open ? NULL : config->psk;
    cfg.psk_len = config->open ? 0 : config->psk_len;
    cfg.admission = producer;
    cfg.seed = (uint64_t)esp_random() << 32 | esp_random();
    /* Sized for the S3's internal RAM (USB-TIER1.md): 16 announcing peers,
     * the reference's 64 replay origins (admission needs origins >= peers),
     * 8 intents and rejections, 16 effects in 4 KiB; a consumer keeps two
     * worst-case producer media. */
    cfg.limits = (ywp_limits_t){.peers = 16, .origins = 64, .intents = 8, .rejections = 8,
                                .media_bytes = producer ? 0 : 2 * YWP_MEDIA_MAX,
                                .effects = 16, .effect_bytes = 4096};
    snapshot.footprint = ywp_node_footprint(&cfg.limits);
    if (producer) {
        cfg.media = media;
        cfg.media_len = box_media_producer(media, sizeof(media), own_ipv4);
    }
    ywp_err_t err = YWP_E_OK;
    node = ywp_node_create(&cfg, &crypto, &saved, now(), wall(), &err);
    if (!node) {
        char why[96];
        snprintf(why, sizeof(why), "YWP/1 node not created: %s", ywp_err_name(err));
        fail(why);
        return;
    }
    set_error("");
    sock = open_socket(own_ipv4);
    if (sock < 0) fail("YWP/1 control socket failed; retrying");
    ywp_route_init(&route);
    ESP_LOGI(TAG, "%s %s on %s:%u%s", producer ? "producer" : "consumer", config->node_id,
             YWP_CONTROL_GROUP, YWP_CONTROL_PORT, config->open ? " (open mode)" : "");
    ywp_node_start(node, now());
    perform();
    health_due = now() + HEALTH_S;
}

esp_err_t box_ywp_open(const box_config_t *c, const box_bridge_t *r, uint32_t ipv4)
{
    config = c;
    role = r;
    own_ipv4 = ipv4;
    producer = c->role == BOX_ROLE_DEVICE;
    snapshot_lock = xSemaphoreCreateMutex();
    if (!snapshot_lock) return ESP_ERR_NO_MEM;
    if (load(&saved) != ESP_OK) {
        fail("saved pairing state is corrupt; recover with {\"rebase_sequence\": true}");
        return ESP_ERR_INVALID_STATE;
    }
    owned = true;
    waiting = !box_clock_synced();
    if (waiting) fail("waiting for the wall clock (SNTP, or PUT /v1/usb/clock)");
    else start_node();
    return ESP_OK;
}

int box_ywp_fd(void) { return sock; }

bool box_ywp_running(void) { return owned; }

double box_ywp_deadline(void)
{
    if (!node) return now() + HEALTH_S; /* waiting for the clock */
    double next = ywp_node_next_deadline(node);
    return next < health_due ? next : health_due;
}

bool box_ywp_receive(void)
{
    static uint8_t datagram[YWP_MAX_FRAME + 1]; /* one more: oversize is a verdict */
    if (!node || sock < 0) return false;
    /* Sources are spoofable: throttle only under load, so a forged trickle
     * from a peer's address cannot mute the peer. */
    bool throttle = flooded;
    bool more = true;
    for (unsigned n = 0; n < RECEIVE_BATCH && more; ++n) {
        struct sockaddr_in from;
        socklen_t size = sizeof(from);
        ssize_t got = recvfrom(sock, datagram, sizeof(datagram), MSG_DONTWAIT,
                               (struct sockaddr *)&from, &size);
        if (got < 0) {
            more = false;
            continue;
        }
        uint32_t source = ntohl(from.sin_addr.s_addr);
        double t = now();
        if (throttle && !box_limit_allow(&limit, source, t)) continue; /* unparsed */
        uint64_t auth = snapshot.status.drops[YWP_DROP_AUTH]; /* this task's own writes */
        int64_t started = esp_timer_get_time();
        ywp_node_datagram(node, datagram, (size_t)got, t, wall(), &source);
        timed(true, started);
        perform();
        if (snapshot.status.drops[YWP_DROP_AUTH] > auth) box_limit_failed(&limit, source, t);
        esp_task_wdt_reset();
    }
    flooded = more;
    xSemaphoreTake(snapshot_lock, portMAX_DELAY);
    snapshot.throttled = limit.throttled;
    xSemaphoreGive(snapshot_lock);
    return more;
}

void box_ywp_timers(void)
{
    double t = now();
    if (!node) {
        if (waiting && box_clock_synced()) {
            waiting = false;
            start_node();
        }
        return;
    }
    if (ywp_node_next_deadline(node) <= t) {
        int64_t started = esp_timer_get_time();
        ywp_node_poll(node, t, wall());
        timed(false, started);
        perform();
    }
    if (health_due <= t && !health_outstanding) {
        health_outstanding = true;
        role->poll(ywp_route_poll_tag(&route));
    }
    if (health_due <= t && sock < 0 && (sock = open_socket(own_ipv4)) >= 0) {
        xSemaphoreTake(snapshot_lock, portMAX_DELAY);
        snapshot.error[0] = 0;
        xSemaphoreGive(snapshot_lock);
        ESP_LOGI(TAG, "YWP/1 control socket reopened");
    }
    if (health_due <= t) health_due = t + HEALTH_S;
}

void box_ywp_route_done(bool completed, ywp_state_t state, const char *detail)
{
    if (!node) return;
    size_t len = strlen(detail);
    if (ywp_route_done(&route, completed, &state, &detail, &len))
        ywp_node_driver_status(node, state, detail, len, now());
    perform();
}

void box_ywp_poll_done(uint32_t tag, ywp_state_t state, const char *detail)
{
    health_outstanding = false;
    if (!node || !ywp_route_poll_current(&route, tag)) return;
    ywp_node_driver_status(node, state, detail, strlen(detail), now());
    perform();
}

void box_ywp_address(uint32_t ipv4)
{
    if (ipv4 == own_ipv4) return;
    own_ipv4 = ipv4;
    if (!node) return; /* the node starts at this address */
    ESP_LOGW(TAG, "address changed; rejoining the control group");
    if (sock >= 0) close(sock);
    sock = open_socket(ipv4);
    if (sock < 0) fail("YWP/1 control socket failed after an address change; retrying");
    if (producer) {
        char media[96];
        size_t len = box_media_producer(media, sizeof(media), ipv4);
        if (ywp_node_set_media(node, media, len, now()) != YWP_E_OK) fail("producer media refused");
        perform();
    }
}

void box_ywp_status(cJSON *ywp)
{
    if (!snapshot_lock) {
        cJSON_AddStringToObject(ywp, "state", "not-started");
        return;
    }
    xSemaphoreTake(snapshot_lock, portMAX_DELAY);
    const ywp_status_t *s = &snapshot.status;
    cJSON_AddStringToObject(ywp, "error", snapshot.error);
    if (!node) cJSON_AddStringToObject(ywp, "state", waiting ? "waiting-for-clock" : "stopped");
    if (node) {
        cJSON_AddStringToObject(ywp, "state", ywp_state_name(s->state));
        cJSON_AddBoolToObject(ywp, "started", s->started);
        cJSON_AddBoolToObject(ywp, "paired", s->pairing.paired);
        cJSON_AddStringToObject(ywp, "producer", s->pairing.paired ? s->pairing.producer : "");
        cJSON_AddNumberToObject(ywp, "peers", (double)snapshot.peers);
        cJSON_AddNumberToObject(ywp, "admitted", (double)s->admitted);
        cJSON_AddNumberToObject(ywp, "overruns", s->overruns);
        cJSON_AddNumberToObject(ywp, "throttled", (double)snapshot.throttled);
        cJSON *drops = cJSON_AddObjectToObject(ywp, "drops");
        for (int i = 1; drops && i < YWP_DROP_COUNT; ++i)
            cJSON_AddNumberToObject(drops, ywp_drop_name((ywp_drop_t)i), (double)s->drops[i]);
        /* Bench evidence for the native core's cost on this chip ⚠. */
        cJSON *cost = cJSON_AddObjectToObject(ywp, "cost");
        if (cost) {
            cJSON_AddNumberToObject(cost, "node_bytes", (double)snapshot.footprint);
            cJSON_AddNumberToObject(cost, "task_stack_free", (double)snapshot.stack_free);
            cJSON_AddNumberToObject(cost, "datagrams", snapshot.datagrams);
            cJSON_AddNumberToObject(cost, "datagram_us_max", snapshot.datagram_us_max);
            cJSON_AddNumberToObject(cost, "datagram_us_mean",
                                    snapshot.datagrams ? (double)snapshot.datagram_us / snapshot.datagrams : 0);
            cJSON_AddNumberToObject(cost, "polls", snapshot.polls);
            cJSON_AddNumberToObject(cost, "poll_us_max", snapshot.poll_us_max);
            cJSON_AddNumberToObject(cost, "poll_us_mean",
                                    snapshot.polls ? (double)snapshot.poll_us / snapshot.polls : 0);
        }
    }
    xSemaphoreGive(snapshot_lock);
}
