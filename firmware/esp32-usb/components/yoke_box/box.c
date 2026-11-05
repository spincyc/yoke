/* Target glue of the USB box: configuration storage, the admin endpoint,
 * status, and the start of either the USB/IP bridge (configured for YWP/1)
 * or the role's hardware self-test. */
#include <stdio.h>
#include <string.h>
#include "bridge.h"
#include "box_clock.h"
#include "box_ywp.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "lwip/inet.h"
#include "nvs.h"
#include "selftest_cdc.h"
#include "selftest_host.h"
#include "yoke_box.h"
#include "yoke_ota.h"
#include "yoke_usbd.h"
#include "yoke_usbh.h"

#define NVS_NAMESPACE "yoke_usb"
#define ADMIN_BODY_MAX 1024
#define ADMIN_BODY_US (10LL * 1000000) /* the whole body, however it trickles */

#if CONFIG_YOKE_USB_ROLE_DEVICE
#define ROLE_DEFAULT BOX_ROLE_DEVICE
#else
#define ROLE_DEFAULT BOX_ROLE_COMPUTER
#endif

static const char *TAG = "yoke-usb";
static box_config_t config;   /* as stored; takes effect at boot */
static box_config_t running;
static const box_bridge_t *bridge; /* NULL: self-test mode */
static bool started, networked;
static esp_timer_handle_t later_timer;
static void (*later_fn)(void *);
static void *later_arg;

/* ---- storage ---- */

static void load(void)
{
    config = (box_config_t){.role = ROLE_DEFAULT};
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) return;
    char role[16];
    size_t size = sizeof(role);
    if (nvs_get_str(nvs, "role", role, &size) == ESP_OK && !box_role_parse(role, &config.role))
        ESP_LOGE(TAG, "stored role \"%s\" is invalid; using %s", role, box_role_name(config.role));
    size = sizeof(config.node_id);
    if (nvs_get_str(nvs, "node_id", config.node_id, &size) != ESP_OK) config.node_id[0] = 0;
    size = sizeof(config.name);
    if (nvs_get_str(nvs, "name", config.name, &size) != ESP_OK) config.name[0] = 0;
    size = sizeof(config.psk);
    if (nvs_get_blob(nvs, "psk", config.psk, &size) == ESP_OK) config.psk_len = size;
    uint8_t open = 0;
    config.open = nvs_get_u8(nvs, "open", &open) == ESP_OK && open;
    nvs_close(nvs);
    if (box_config_check(&config) ||
        (config.psk_len && (config.psk_len < BOX_PSK_MIN || config.psk_len > BOX_PSK_MAX))) {
        ESP_LOGE(TAG, "stored YWP/1 configuration is inconsistent; not pairing");
        config.node_id[0] = 0;
    }
}

static esp_err_t save(const box_config_t *next)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) return err;
    err = nvs_set_str(nvs, "role", box_role_name(next->role));
    if (err == ESP_OK) err = nvs_set_str(nvs, "node_id", next->node_id);
    if (err == ESP_OK) err = nvs_set_str(nvs, "name", next->name);
    if (err == ESP_OK) err = next->psk_len ? nvs_set_blob(nvs, "psk", next->psk, next->psk_len)
                                           : nvs_erase_key(nvs, "psk");
    if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
    if (err == ESP_OK) err = nvs_set_u8(nvs, "open", next->open);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    return err;
}

/* ---- self-test mode ---- */

static void log_line(const char *line) { ESP_LOGI(TAG, "%s", line); }

/* The CDC self-test's simulated round trip: a timer, then the USB task. */
static void later_fire(void *arg)
{
    (void)arg;
    yoke_usbd_post(later_fn, later_arg);
}

static void later(void (*fn)(void *), void *arg, uint32_t ms)
{
    (void)esp_timer_stop(later_timer);
    later_fn = fn;
    later_arg = arg;
    (void)esp_timer_start_once(later_timer, ms * 1000u);
}

static void present_selftest(void *arg)
{
    char why[120];
    if (yoke_usbd_present(arg, &selftest_cdc_sink, NULL, why, sizeof(why)))
        ESP_LOGE(TAG, "self-test device not presented: %s", why);
}

static esp_err_t start_cdc_selftest(void)
{
    static const selftest_cdc_port_t port = {.later = later, .log = log_line};
    static char serial[13];
    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(mac, ESP_MAC_BASE), TAG, "MAC");
    snprintf(serial, sizeof(serial), "%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2], mac[3],
             mac[4], mac[5]);
    const esp_timer_create_args_t timer = {.callback = later_fire, .name = "usb-selftest"};
    ESP_RETURN_ON_ERROR(esp_timer_create(&timer, &later_timer), TAG, "timer");
    uint8_t *buffer = yoke_usbd_alloc(SELFTEST_CDC_BUFFER);
    if (!buffer) return ESP_ERR_NO_MEM;
#if CONFIG_IDF_TARGET_ESP32P4
    const yoke_usbd_speed_t speed = YOKE_USBD_SPEED_HIGH;
#else
    const yoke_usbd_speed_t speed = YOKE_USBD_SPEED_FULL;
#endif
    const yoke_usbd_descriptors_t *d = selftest_cdc_init(&port, speed, serial, buffer);
    ESP_RETURN_ON_ERROR(yoke_usbd_start(), TAG, "TinyUSB");
    yoke_usbd_post(present_selftest, (void *)d);
    ESP_LOGI(TAG, "self-test: CDC-ACM echo 1209:0001 serial %s; open the new serial port on the "
             "computer and type", serial);
    return ESP_OK;
}

/* ---- bridge mode ---- */

static void on_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base; (void)id;
    uint32_t ipv4 = ntohl(((ip_event_got_ip_t *)data)->ip_info.ip.addr);
    static portMUX_TYPE once = portMUX_INITIALIZER_UNLOCKED;
    taskENTER_CRITICAL(&once); /* the event task and yoke_box_online may race */
    bool first = !networked;
    networked = true;
    taskEXIT_CRITICAL(&once);
    if (!first) {
        box_net_address(ipv4);
        return;
    }
    box_clock_online();
    if (bridge->online) bridge->online();
    if (box_net_start(&running, bridge, ipv4) != ESP_OK) ESP_LOGE(TAG, "network task did not start");
}

esp_err_t yoke_box_start(void)
{
    load();
    running = config;
    esp_err_t err;
    if (box_config_bridging(&running)) {
        bridge = running.role == BOX_ROLE_DEVICE ? &box_device : &box_computer;
        ESP_LOGI(TAG, "role %s, YWP/1 node %s: USB/IP bridge", box_role_name(running.role),
                 running.node_id);
        err = box_net_init();
        if (err == ESP_OK) err = bridge->start();
    } else if (running.role == BOX_ROLE_DEVICE) {
        ESP_LOGI(TAG, "role device, YWP/1 not configured: hardware self-test");
        selftest_host_init(log_line);
        err = yoke_usbh_start(&selftest_host_sink, NULL);
    } else {
        ESP_LOGI(TAG, "role computer, YWP/1 not configured: hardware self-test");
        err = start_cdc_selftest();
    }
    started = err == ESP_OK;
    if (!started) ESP_LOGE(TAG, "USB %s role did not start: %s", box_role_name(running.role),
                           esp_err_to_name(err));
    return err;
}

bool yoke_box_ready(void) { return started; }

void yoke_box_network(void)
{
    if (bridge && box_clock_init() != ESP_OK)
        ESP_LOGE(TAG, "SNTP unavailable; pairing waits for PUT /v1/usb/clock");
}

bool box_heap_fits(size_t need)
{
    return heap_caps_get_free_size(MALLOC_CAP_INTERNAL) >= need + BOX_HEAP_RESERVE &&
           heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) >= need;
}

/* ---- status ---- */

static void selftest_status(cJSON *test)
{
    /* Counters are read without the USB task's cooperation: evidence only. */
    if (running.role == BOX_ROLE_DEVICE) {
        const selftest_host_status_t *s = selftest_host_status();
        cJSON_AddBoolToObject(test, "attached", s->attached);
        cJSON_AddBoolToObject(test, "claimed", s->claimed);
        cJSON_AddNumberToObject(test, "vendor", s->vendor);
        cJSON_AddNumberToObject(test, "product", s->product);
        cJSON_AddNumberToObject(test, "speed", s->speed);
        cJSON_AddNumberToObject(test, "poll_endpoint", s->poll_endpoint);
        cJSON_AddNumberToObject(test, "reports", s->reports);
        cJSON_AddNumberToObject(test, "errors", s->errors);
    } else {
        const selftest_cdc_status_t *s = selftest_cdc_status();
        cJSON_AddBoolToObject(test, "configured", s->configured);
        cJSON_AddBoolToObject(test, "dtr", s->dtr);
        cJSON_AddNumberToObject(test, "baud", s->baud);
        cJSON_AddNumberToObject(test, "control_requests", s->control_requests);
        cJSON_AddNumberToObject(test, "deferred_replies", s->deferred_replies);
        cJSON_AddNumberToObject(test, "echoed_bytes", s->echoed_bytes);
        cJSON_AddNumberToObject(test, "errors", s->errors);
    }
}

void yoke_box_status(cJSON *body)
{
    cJSON *usb = cJSON_AddObjectToObject(body, "usb");
    if (!usb) return;
    cJSON_AddStringToObject(usb, "role", box_role_name(running.role));
    cJSON_AddStringToObject(usb, "configured_role", box_role_name(config.role));
    cJSON_AddStringToObject(usb, "mode", bridge ? "bridge" : "selftest");
    cJSON_AddStringToObject(usb, "node_id", config.node_id);
    cJSON_AddBoolToObject(usb, "psk", config.psk_len > 0); /* never the key itself */
    cJSON_AddBoolToObject(usb, "open", config.open);
    cJSON_AddBoolToObject(usb, "started", started);
    cJSON_AddBoolToObject(usb, "clock_synced", box_clock_synced());
    /* Sessions budget internal RAM; heap_free also counts any PSRAM. */
    cJSON_AddNumberToObject(usb, "heap_internal_free",
                            (double)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    cJSON_AddNumberToObject(usb, "heap_internal_min_free",
                            (double)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));
    if (!started) return;
    if (bridge) {
        bridge->status(usb);
        box_net_status(body);
    } else {
        cJSON *test = cJSON_AddObjectToObject(usb, "selftest");
        if (test) selftest_status(test);
    }
}

/* ---- admin ---- */

static esp_err_t admin_error(httpd_req_t *req, const char *status, const char *detail)
{
    cJSON *body = cJSON_CreateObject();
    if (body) cJSON_AddStringToObject(body, "error", detail);
    (void)yoke_ota_respond(req, status, body);
    return ESP_FAIL; /* close without purging any unread body */
}

/* A JSON body of at most cap - 1 bytes, read within ADMIN_BODY_US; NULL
 * after an error reply. */
static cJSON *admin_body(httpd_req_t *req, char *text, size_t cap)
{
    char type[40];
    if (httpd_req_get_hdr_value_str(req, "Content-Type", type, sizeof(type)) != ESP_OK ||
        strcmp(type, "application/json")) {
        admin_error(req, "415 Unsupported Media Type", "content-type-must-be-json");
        return NULL;
    }
    if (!req->content_len || req->content_len >= cap) {
        admin_error(req, "413 Payload Too Large", "body-size");
        return NULL;
    }
    int64_t deadline = esp_timer_get_time() + ADMIN_BODY_US;
    if (yoke_ota_arm_deadline(req, deadline) != ESP_OK) {
        admin_error(req, "503 Service Unavailable", "request-deadline-unavailable");
        return NULL;
    }
    size_t got = 0;
    while (got < req->content_len && esp_timer_get_time() < deadline) {
        int n = httpd_req_recv(req, text + got, req->content_len - got);
        if (n <= 0) break;
        got += (size_t)n;
    }
    yoke_ota_disarm_deadline();
    if (got < req->content_len) {
        admin_error(req, "400 Bad Request", "body-incomplete-or-too-slow");
        return NULL;
    }
    text[got] = 0;
    cJSON *json = cJSON_Parse(text);
    if (!cJSON_IsObject(json)) {
        cJSON_Delete(json);
        admin_error(req, "400 Bad Request", "invalid-json");
        return NULL;
    }
    return json;
}

/* PUT /v1/usb/config: role, node_id, name, psk, open, and the recovery flag
 * rebase_sequence. Accepted changes take effect at the restart that follows
 * the reply. */
static esp_err_t admin_put(httpd_req_t *req)
{
    char text[ADMIN_BODY_MAX + 1];
    cJSON *json = admin_body(req, text, sizeof(text));
    if (!json) return ESP_FAIL;
    box_config_t next = config;
    bool rebase = false;
    const char *problem = NULL;
    for (cJSON *item = json->child; item && !problem; item = item->next) {
        if (cJSON_IsString(item)) problem = box_config_set_text(&next, item->string, item->valuestring);
        else if (!cJSON_IsBool(item)) problem = "unknown-or-mistyped-field";
        else if (!strcmp(item->string, "rebase_sequence")) rebase = cJSON_IsTrue(item);
        else problem = box_config_set_flag(&next, item->string, cJSON_IsTrue(item));
    }
    cJSON_Delete(json);
    if (!problem) problem = box_config_check(&next);
    if (problem) return admin_error(req, "400 Bad Request", problem);
    /* A running node owns its state; rebase only what failed to load, and
     * only on a real clock (the new sequence is the admission bound of now). */
    if (rebase && box_ywp_running()) return admin_error(req, "409 Conflict", "pairing-state-is-in-use");
    if (rebase && !box_clock_synced())
        return admin_error(req, "409 Conflict", "wall-clock-not-synchronized");
    if (save(&next) != ESP_OK || (rebase && box_ywp_rebase() != ESP_OK))
        return admin_error(req, "500 Internal Server Error", "nvs-write-failed");
    config = next;
    ESP_LOGW(TAG, "configuration changed by admin (role %s, node \"%s\"); restarting",
             box_role_name(config.role), config.node_id);
    cJSON *body = cJSON_CreateObject();
    if (body) {
        cJSON_AddStringToObject(body, "role", box_role_name(config.role));
        cJSON_AddStringToObject(body, "node_id", config.node_id);
        cJSON_AddBoolToObject(body, "bridge", box_config_bridging(&config));
        cJSON_AddBoolToObject(body, "restart", true);
    }
    if (yoke_ota_schedule_restart() != ESP_OK) ESP_LOGE(TAG, "restart could not be scheduled");
    return yoke_ota_respond(req, "202 Accepted", body);
}

/* PUT /v1/usb/clock {"unix": seconds[, "force": true]}: the wall clock where
 * neither DHCP nor the configured server offers NTP. Forward only, and by
 * at most a year unless forced (a typo would advance the clock floor and
 * then the YWP/1 sequence floor for good). */
static esp_err_t clock_put(httpd_req_t *req)
{
    char text[80];
    cJSON *json = admin_body(req, text, sizeof(text));
    if (!json) return ESP_FAIL;
    const cJSON *unix_s = cJSON_GetObjectItemCaseSensitive(json, "unix");
    const cJSON *force = cJSON_GetObjectItemCaseSensitive(json, "force");
    bool valid = cJSON_GetArraySize(json) == (force ? 2 : 1) && cJSON_IsNumber(unix_s) &&
                 unix_s->valuedouble >= 1.6e9 && unix_s->valuedouble < 4.0e9 &&
                 (!force || cJSON_IsBool(force));
    int64_t seconds = valid ? (int64_t)unix_s->valuedouble : 0;
    bool forced = valid && cJSON_IsTrue(force);
    cJSON_Delete(json);
    if (!valid) return admin_error(req, "400 Bad Request", "unix-must-be-seconds-since-1970");
    esp_err_t err = box_clock_set_forward(seconds, forced);
    if (err == ESP_ERR_INVALID_ARG) return admin_error(req, "409 Conflict", "clock-never-moves-back");
    if (err == ESP_ERR_INVALID_SIZE)
        return admin_error(req, "409 Conflict", "step-over-a-year-needs-force");
    if (err != ESP_OK) return admin_error(req, "500 Internal Server Error", "clock-not-set");
    cJSON *body = cJSON_CreateObject();
    if (body) cJSON_AddNumberToObject(body, "unix", (double)seconds);
    return yoke_ota_respond(req, "200 OK", body);
}

esp_err_t yoke_box_online(httpd_handle_t server)
{
    if (!server) return ESP_ERR_INVALID_STATE;
    const httpd_uri_t uris[] = {
        {.uri = "/v1/usb/config", .method = HTTP_PUT, .handler = admin_put},
        {.uri = "/v1/usb/clock", .method = HTTP_PUT, .handler = clock_put},
    };
    esp_err_t err = ESP_OK;
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]) && err == ESP_OK; ++i)
        err = httpd_register_uri_handler(server, &uris[i]);
    if (err != ESP_OK || !bridge || !started) return err;
    /* Pairing starts with an address; DHCP may have answered already. */
    err = esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_ip, NULL);
    esp_netif_ip_info_t ip;
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("ETH_DEF");
    if (err == ESP_OK && netif && esp_netif_get_ip_info(netif, &ip) == ESP_OK && ip.ip.addr) {
        ip_event_got_ip_t event = {.ip_info = ip};
        on_ip(NULL, IP_EVENT, IP_EVENT_ETH_GOT_IP, &event);
    }
    return err;
}
