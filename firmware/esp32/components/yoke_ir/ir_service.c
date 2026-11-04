/* IR endpoint service (target only): settings, the TCP 4998 listener task
 * and the admin/status integration with the mutual-TLS server.
 *
 * The listener task owns the core; other tasks (HTTPS admin/status, restart
 * guard) take `lock` for short, non-blocking core calls. The task is
 * subscribed to the task watchdog and wakes at least every 100 ms. */
#include <string.h>
#include <sys/param.h>
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "nvs.h"
#include "ir_rmt.h"
#include "yoke_ir.h"

#define NVS_NAMESPACE "yoke_ir"
#define ACTIVE_POLL_MS 2
#define IDLE_POLL_MS 100
#define ADMIN_BODY_MAX 256
#define SCOPE_CHECK_US 1000000
#define REFUSED_LOG_US 10000000

/* Kconfig leaves disabled booleans undefined. */
#ifdef CONFIG_YOKE_IR_API_DEFAULT
#define API_DEFAULT true
#else
#define API_DEFAULT false
#endif
#ifdef CONFIG_YOKE_IR_LEARNING_DEFAULT
#define LEARNING_DEFAULT true
#else
#define LEARNING_DEFAULT false
#endif
#ifdef CONFIG_YOKE_IR_INVERT
#define INVERT true
#else
#define INVERT false
#endif
#ifdef CONFIG_YOKE_IR_SELFTEST_LOOPBACK
#define LOOPBACK true
#else
#define LOOPBACK false
#endif

typedef struct {
    bool api, learning, allowlist;
    uint32_t allow_net, allow_mask;
} settings_t;

static const char *TAG = "yoke-ir";
static ir_core_t *core;
static QueueHandle_t events;
static SemaphoreHandle_t lock;
static settings_t settings;
static bool selftest_ok, applied_learning;
static volatile bool dirty, ready, quiescing;
static int listener = -1;
static int fds[IR_MAX_CONNS];
static uint32_t peers[IR_MAX_CONNS];      /* host byte order */
static unsigned max_conns;
static bool scope_dirty;
static int64_t refused_logged;

static void load_settings(void)
{
    settings.api = API_DEFAULT;
    settings.learning = LEARNING_DEFAULT;
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) return;
    uint8_t value;
    if (nvs_get_u8(nvs, "api", &value) == ESP_OK) settings.api = value != 0;
    if (nvs_get_u8(nvs, "learn", &value) == ESP_OK) settings.learning = value != 0;
    char cidr[24];
    size_t size = sizeof(cidr);
    if (nvs_get_str(nvs, "allow", cidr, &size) == ESP_OK) {
        settings.allowlist = true;
        if (!ir_parse_cidr(cidr, &settings.allow_net, &settings.allow_mask)) {
            /* Fail closed: an unreadable allowlist admits nobody. */
            ESP_LOGE(TAG, "stored IR allowlist is invalid; refusing all peers");
            settings.allow_net = 0;
            settings.allow_mask = 0xffffffffu;
        }
    }
    nvs_close(nvs);
}

esp_err_t yoke_ir_init(void)
{
    if (core) return ESP_ERR_INVALID_STATE;
    load_settings();
    lock = xSemaphoreCreateMutex();
    events = xQueueCreate(16, sizeof(ir_event_t));
    if (!lock || !events) return ESP_ERR_NO_MEM;
    ir_rmt_config_t rmt = {
        .outputs = CONFIG_YOKE_IR_OUTPUTS,
        .gpio = {CONFIG_YOKE_IR_GPIO_1,
#if CONFIG_YOKE_IR_OUTPUTS > 1
                 CONFIG_YOKE_IR_GPIO_2,
#endif
#if CONFIG_YOKE_IR_OUTPUTS > 2
                 CONFIG_YOKE_IR_GPIO_3,
#endif
#if CONFIG_YOKE_IR_OUTPUTS > 3
                 CONFIG_YOKE_IR_GPIO_4,
#endif
        },
        .invert = INVERT,
        .duty = CONFIG_YOKE_IR_DUTY_PERCENT / 100.0f,
        .learn_gpio = CONFIG_YOKE_IR_LEARN_GPIO,
        .events = events,
    };
    esp_err_t err = ir_rmt_init(&rmt);
    if (err != ESP_OK) return err;
    selftest_ok = ir_rmt_selftest(LOOPBACK);
    if (!selftest_ok) return ESP_FAIL;
    ir_config_t cfg;
    ir_config_defaults(&cfg);
    cfg.outputs = CONFIG_YOKE_IR_OUTPUTS;
    cfg.max_conns = CONFIG_YOKE_IR_MAX_CONNECTIONS;
    cfg.resolution_hz = IR_RMT_RESOLUTION_HZ;
    cfg.max_frame_us = CONFIG_YOKE_IR_MAX_FRAME_MS * 1000u;
    cfg.rate_per_s = CONFIG_YOKE_IR_RATE_PER_S;
    cfg.burst = CONFIG_YOKE_IR_BURST;
    cfg.learning = settings.learning && ir_rmt_learning_hardware();
    applied_learning = cfg.learning;
    snprintf(cfg.identity, sizeof(cfg.identity), "Make=Yoke;Model=yoke-ir;Revision=%s",
             esp_app_get_description()->version);
    ir_ops_t ops = {.tx_start = ir_rmt_tx_start, .tx_stop = ir_rmt_tx_stop,
                    .learn_start = ir_rmt_learn_start, .learn_stop = ir_rmt_learn_stop};
    core = ir_core_create(&cfg, &ops);
    if (!core) return ESP_ERR_NO_MEM;
    max_conns = cfg.max_conns;
    for (unsigned i = 0; i < IR_MAX_CONNS; ++i) fds[i] = -1;
    ESP_LOGI(TAG, "%u IR output(s), self-test passed; API %s, learning %s",
             cfg.outputs, settings.api ? "ENABLED (cleartext TCP 4998)" : "disabled",
             cfg.learning ? "enabled" : "disabled");
    return ESP_OK;
}

static void close_slot(int slot)
{
    if (fds[slot] >= 0) close(fds[slot]);
    fds[slot] = -1;
    ir_core_closed(core, slot);
}

static void close_listener(void)
{
    if (listener >= 0) close(listener);
    listener = -1;
    for (unsigned i = 0; i < max_conns; ++i)
        if (fds[i] >= 0) close_slot((int)i);
}

static void open_listener(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) return;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(YOKE_IR_PORT),
                               .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) || listen(fd, 2) ||
        fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK)) {
        close(fd);
        return;
    }
    listener = fd;
    ESP_LOGW(TAG, "Unauthenticated iTach-subset API listening on TCP %d (LAN-scoped)",
             YOKE_IR_PORT);
}

static void flush(uint64_t now)
{
    for (unsigned i = 0; i < max_conns; ++i) {
        if (fds[i] < 0) continue;
        const char *data;
        size_t n = ir_core_output(core, (int)i, &data);
        if (n) {
            int sent = send(fds[i], data, n, MSG_DONTWAIT);
            if (sent > 0) ir_core_output_sent(core, (int)i, (size_t)sent, now);
            else if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
                close_slot((int)i);
                continue;
            }
        }
        if (ir_core_should_close(core, (int)i)) close_slot((int)i);
    }
}

static bool peer_allowed(uint32_t peer)
{
    esp_netif_ip_info_t info = {0};
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("ETH_DEF");
    if (netif) esp_netif_get_ip_info(netif, &info);
    return ir_peer_allowed(peer, ntohl(info.ip.addr), ntohl(info.netmask.addr),
                           settings.allowlist, settings.allow_net, settings.allow_mask);
}

/* Scope is enforced for the whole connection, not only at accept: a
 * narrowed allowlist or a changed/lost address drops admitted peers. */
static void recheck_scope(void)
{
    for (unsigned i = 0; i < max_conns; ++i)
        if (fds[i] >= 0 && !peer_allowed(peers[i])) close_slot((int)i);
}

static void accept_one(uint64_t now)
{
    struct sockaddr_in peer;
    socklen_t len = sizeof(peer);
    int fd = accept(listener, (struct sockaddr *)&peer, &len);
    if (fd < 0) return;
    int evicted, slot = -1;
    uint32_t address = ntohl(peer.sin_addr.s_addr);
    if (peer_allowed(address)) slot = ir_core_accept(core, now, &evicted);
    else if (esp_timer_get_time() - refused_logged >= REFUSED_LOG_US) {
        refused_logged = esp_timer_get_time();
        ESP_LOGW(TAG, "refused peer outside LAN scope (logged at most every 10 s)");
    }
    if (slot < 0) { close(fd); return; }
    peers[slot] = address;
    if (evicted >= 0 && fds[evicted] >= 0) { close(fds[evicted]); fds[evicted] = -1; }
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    fds[slot] = fd;
}

static void read_slot(int slot, uint64_t now)
{
    static char buffer[512];
    size_t space = MIN(ir_core_rx_space(core, slot), sizeof(buffer));
    if (!space) return;
    int n = recv(fds[slot], buffer, space, MSG_DONTWAIT);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) { close_slot(slot); return; }
    if (n > 0) ir_core_input(core, slot, buffer, (size_t)n, now);
}

static void apply(uint64_t now)
{
    dirty = false;
    bool learning = settings.learning && ir_rmt_learning_hardware();
    if (learning != applied_learning) ir_core_set_learning(core, learning, now);
    applied_learning = learning;
    scope_dirty = true;
    if ((!settings.api || quiescing) && listener >= 0) close_listener();
}

static void task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    int64_t scope_checked = 0;
    for (;;) {
        esp_task_wdt_reset();
        fd_set readable, writable;
        FD_ZERO(&readable);
        FD_ZERO(&writable);
        int top = -1;
        uint64_t now = esp_timer_get_time(), deadline;
        bool active;
        xSemaphoreTake(lock, portMAX_DELAY);
        ir_event_t event;
        while (xQueueReceive(events, &event, 0) == pdTRUE) {
            if (event.kind == IR_EVENT_TX_DONE) {
                ir_core_tx_done(core, event.output, event.seq, now);
            } else if (event.kind == IR_EVENT_RX_DONE) {
                ir_core_learned(core, ir_rmt_learn_symbols(), event.count,
                                IR_RMT_RESOLUTION_HZ, now);
                ir_rmt_learn_rearm();
            }
        }
        if (dirty) apply(now);
        if (scope_dirty || (int64_t)now - scope_checked >= SCOPE_CHECK_US) {
            scope_dirty = false;
            scope_checked = (int64_t)now;
            recheck_scope();
        }
        if (settings.api && !quiescing && listener < 0) open_listener();
        ready = !settings.api || listener >= 0;
        deadline = ir_core_poll(core, now);
        flush(now);
        if (listener >= 0) { FD_SET(listener, &readable); top = MAX(top, listener); }
        for (unsigned i = 0; i < max_conns; ++i) {
            if (fds[i] < 0) continue;
            const char *data;
            if (ir_core_rx_space(core, (int)i)) FD_SET(fds[i], &readable);
            if (ir_core_output(core, (int)i, &data)) FD_SET(fds[i], &writable);
            top = MAX(top, fds[i]);
        }
        active = !ir_core_idle(core) || applied_learning;
        xSemaphoreGive(lock);
        uint64_t wait_us = (active ? ACTIVE_POLL_MS : IDLE_POLL_MS) * 1000ull;
        if (deadline != UINT64_MAX) wait_us = deadline > now ? MIN(wait_us, deadline - now) : 0;
        struct timeval tv = {.tv_sec = 0, .tv_usec = (suseconds_t)wait_us};
        int ready_fds = top >= 0 ? select(top + 1, &readable, &writable, NULL, &tv)
                                 : (vTaskDelay(pdMS_TO_TICKS(wait_us / 1000u) + 1), 0);
        if (ready_fds <= 0) continue;
        xSemaphoreTake(lock, portMAX_DELAY);
        now = esp_timer_get_time();
        if (listener >= 0 && FD_ISSET(listener, &readable)) accept_one(now);
        for (unsigned i = 0; i < max_conns; ++i)
            if (fds[i] >= 0 && FD_ISSET(fds[i], &readable)) read_slot((int)i, now);
        flush(now);
        xSemaphoreGive(lock);
    }
}

esp_err_t yoke_ir_start(void)
{
    if (!core) return ESP_ERR_INVALID_STATE;
    /* Above the HTTPS server (tskIDLE_PRIORITY + 5) so a stalled TLS peer
     * cannot delay IR; the work per wakeup is bounded and short. Pinned to
     * CPU0, where app_main created the RMT channels and their ISRs live, so
     * rmt_disable() and the ISRs never run concurrently on two cores. */
    return xTaskCreatePinnedToCore(task, "yoke-ir", 6144, NULL, tskIDLE_PRIORITY + 6, NULL,
                                   0) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

bool yoke_ir_ready(void) { return core && ready; }

void yoke_ir_quiesce(uint32_t max_wait_ms)
{
    if (!core) return;
    quiescing = true;
    dirty = true;
    xSemaphoreTake(lock, portMAX_DELAY);
    ir_core_quiesce(core, esp_timer_get_time());
    xSemaphoreGive(lock);
    int64_t until = esp_timer_get_time() + (int64_t)max_wait_ms * 1000;
    bool idle = false;
    while (!idle && esp_timer_get_time() < until) {
        xSemaphoreTake(lock, portMAX_DELAY);
        idle = ir_core_idle(core);
        xSemaphoreGive(lock);
        if (!idle) vTaskDelay(pdMS_TO_TICKS(5));
    }
    if (!idle) {
        ESP_LOGE(TAG, "IR still active after %" PRIu32 " ms; forcing outputs idle", max_wait_ms);
        xSemaphoreTake(lock, portMAX_DELAY);
        for (unsigned i = 0; i < CONFIG_YOKE_IR_OUTPUTS; ++i) ir_rmt_tx_stop(NULL, i);
        xSemaphoreGive(lock);
    }
}

void yoke_ir_status_json(cJSON *body)
{
    cJSON *ir = cJSON_AddObjectToObject(body, "ir");
    if (!ir) return;
    cJSON_AddBoolToObject(ir, "selftest_passed", selftest_ok);
    cJSON_AddNumberToObject(ir, "outputs", CONFIG_YOKE_IR_OUTPUTS);
    if (!core) return;
    xSemaphoreTake(lock, portMAX_DELAY);
    cJSON_AddBoolToObject(ir, "api_enabled", settings.api);
    cJSON_AddBoolToObject(ir, "listening", listener >= 0);
    cJSON_AddNumberToObject(ir, "port", YOKE_IR_PORT);
    cJSON_AddStringToObject(ir, "authentication", "none-lan-scoped");
    if (settings.allowlist) {
        char cidr[24];
        uint32_t net = settings.allow_net;
        int prefix = __builtin_popcount(settings.allow_mask);
        snprintf(cidr, sizeof(cidr), "%u.%u.%u.%u/%d", (unsigned)(net >> 24),
                 (unsigned)(net >> 16) & 255u, (unsigned)(net >> 8) & 255u,
                 (unsigned)net & 255u, prefix);
        cJSON_AddStringToObject(ir, "allow", cidr);
    } else {
        cJSON_AddStringToObject(ir, "allow", "interface-subnet");
    }
    cJSON_AddBoolToObject(ir, "learning_enabled", applied_learning);
    cJSON_AddBoolToObject(ir, "learning_hardware", ir_rmt_learning_hardware());
    cJSON_AddBoolToObject(ir, "quiescing", quiescing);
    cJSON_AddNumberToObject(ir, "connections", ir_core_open_conns(core));
    cJSON *busy = cJSON_AddArrayToObject(ir, "busy");
    for (unsigned i = 0; busy && i < CONFIG_YOKE_IR_OUTPUTS; ++i)
        cJSON_AddItemToArray(busy, cJSON_CreateBool(ir_core_output_busy(core, i)));
    const ir_counters_t *c = ir_core_counters(core);
    cJSON *counters = cJSON_AddObjectToObject(ir, "counters");
    if (counters) {
        cJSON_AddNumberToObject(counters, "accepted", c->accepted);
        cJSON_AddNumberToObject(counters, "refused", c->refused);
        cJSON_AddNumberToObject(counters, "evicted", c->evicted);
        cJSON_AddNumberToObject(counters, "closed_slow", c->closed_slow);
        cJSON_AddNumberToObject(counters, "lines", c->lines);
        cJSON_AddNumberToObject(counters, "errors", c->errors);
        cJSON_AddNumberToObject(counters, "sendir", c->sendir);
        cJSON_AddNumberToObject(counters, "completeir", c->completeir);
        cJSON_AddNumberToObject(counters, "busy", c->busy);
        cJSON_AddNumberToObject(counters, "stopped", c->stopped);
        cJSON_AddNumberToObject(counters, "folded", c->folded);
        cJSON_AddNumberToObject(counters, "rate_limited", c->rate_limited);
        cJSON_AddNumberToObject(counters, "learned", c->learned);
    }
    xSemaphoreGive(lock);
}

static esp_err_t admin_reply(httpd_req_t *req, const char *status, cJSON *body)
{
    char *json = body ? cJSON_PrintUnformatted(body) : NULL;
    cJSON_Delete(body);
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_hdr(req, "Connection", "close");
    esp_err_t err = httpd_resp_send(req, json ? json : "{}", HTTPD_RESP_USE_STRLEN);
    cJSON_free(json);
    httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));
    return err;
}

static esp_err_t admin_error(httpd_req_t *req, const char *status, const char *detail)
{
    cJSON *body = cJSON_CreateObject();
    if (body) cJSON_AddStringToObject(body, "error", detail);
    (void)admin_reply(req, status, body);
    return ESP_FAIL; /* close without purging any unread body */
}

/* PUT /v1/ir/config {"api":bool,"learning":bool,"allow":"a.b.c.d/n"|""}
 * Fields are optional; the result is persisted and applied live. */
static esp_err_t admin_put(httpd_req_t *req)
{
    char type[40], text[ADMIN_BODY_MAX + 1];
    if (httpd_req_get_hdr_value_str(req, "Content-Type", type, sizeof(type)) != ESP_OK ||
        strcmp(type, "application/json"))
        return admin_error(req, "415 Unsupported Media Type", "content-type-must-be-json");
    if (!req->content_len || req->content_len > ADMIN_BODY_MAX)
        return admin_error(req, "413 Payload Too Large", "body-size");
    size_t got = 0;
    while (got < req->content_len) {
        int n = httpd_req_recv(req, text + got, req->content_len - got);
        if (n <= 0) return admin_error(req, "400 Bad Request", "body-incomplete");
        got += (size_t)n;
    }
    text[got] = 0;
    cJSON *json = cJSON_Parse(text);
    settings_t next = settings;
    const char *problem = json && cJSON_IsObject(json) ? NULL : "invalid-json";
    for (cJSON *item = json ? json->child : NULL; item && !problem; item = item->next) {
        if (!strcmp(item->string, "api") && cJSON_IsBool(item)) next.api = cJSON_IsTrue(item);
        else if (!strcmp(item->string, "learning") && cJSON_IsBool(item))
            next.learning = cJSON_IsTrue(item);
        else if (!strcmp(item->string, "allow") && cJSON_IsString(item)) {
            next.allowlist = item->valuestring[0] != 0;
            if (next.allowlist &&
                !ir_parse_cidr(item->valuestring, &next.allow_net, &next.allow_mask))
                problem = "invalid-allow-cidr";
        } else problem = "unknown-or-mistyped-field";
    }
    cJSON_Delete(json);
    if (problem) return admin_error(req, "400 Bad Request", problem);
    if (next.learning && !ir_rmt_learning_hardware())
        return admin_error(req, "409 Conflict", "no-learning-hardware");
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err == ESP_OK) {
        err = nvs_set_u8(nvs, "api", next.api);
        if (err == ESP_OK) err = nvs_set_u8(nvs, "learn", next.learning);
        if (err == ESP_OK) {
            if (next.allowlist) {
                char cidr[24];
                snprintf(cidr, sizeof(cidr), "%u.%u.%u.%u/%d", (unsigned)(next.allow_net >> 24),
                         (unsigned)(next.allow_net >> 16) & 255u,
                         (unsigned)(next.allow_net >> 8) & 255u, (unsigned)next.allow_net & 255u,
                         __builtin_popcount(next.allow_mask));
                err = nvs_set_str(nvs, "allow", cidr);
            } else {
                err = nvs_erase_key(nvs, "allow");
                if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
            }
        }
        if (err == ESP_OK) err = nvs_commit(nvs);
        nvs_close(nvs);
    }
    if (err != ESP_OK) return admin_error(req, "500 Internal Server Error", "nvs-write-failed");
    if (core) xSemaphoreTake(lock, portMAX_DELAY);
    settings = next;
    dirty = true;
    if (core) xSemaphoreGive(lock);
    ESP_LOGW(TAG, "IR settings changed by admin: API %s, learning %s",
             next.api ? "enabled" : "disabled", next.learning ? "enabled" : "disabled");
    cJSON *body = cJSON_CreateObject();
    if (body) {
        cJSON_AddBoolToObject(body, "api", next.api);
        cJSON_AddBoolToObject(body, "learning", next.learning);
        cJSON_AddBoolToObject(body, "allowlist", next.allowlist);
    }
    return admin_reply(req, "200 OK", body);
}

esp_err_t yoke_ir_register_admin(httpd_handle_t server)
{
    if (!server) return ESP_ERR_INVALID_STATE;
    httpd_uri_t uri = {.uri = "/v1/ir/config", .method = HTTP_PUT, .handler = admin_put};
    return httpd_register_uri_handler(server, &uri);
}
