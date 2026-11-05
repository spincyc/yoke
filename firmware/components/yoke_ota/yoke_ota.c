#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <sys/socket.h>
#include <time.h>
#ifdef YOKE_OTA_HOST_TEST
#include "host_stubs.h"
#else
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_https_server.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
#include "esp_core_dump.h"
#endif
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "mbedtls/sha256.h"
#include "mbedtls/ctr_drbg.h"
#include "mbedtls/entropy.h"
#include "mbedtls/pk.h"
#include "mbedtls/oid.h"
#include "mbedtls/x509_crt.h"
#include "nvs_flash.h"
#endif
#include "yoke_ota.h"
#include "ota_policy.h"

#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE || !CONFIG_MBEDTLS_HAVE_TIME_DATE
#error "The updater requires boot rollback and TLS certificate validity checks"
#endif
#ifdef CONFIG_ESP_TLS_SERVER_MIN_AUTH_MODE_OPTIONAL
#error "The updater requires mandatory client certificate verification"
#endif

#define TRUST_PARTITION "otatrust"
#define HEALTH_US (60LL * 1000000)
#define UPLOAD_US (120LL * 1000000)
#define REBOOT_DELAY_US 2000000
#define WDT_PROBE_US 2000000
#define JOURNAL_KEY "journal2"
#define LEGACY_JOURNAL_KEY "journal"

static const char *TAG = "yoke-ota";
static nvs_handle_t trust;
static SemaphoreHandle_t lock;
static yoke_journal_t journal;
static uint32_t boot_count;
static void (*restart_guard)(void);
static void (*status_hook)(cJSON *body);
static esp_task_wdt_user_handle_t wdt_user;
static esp_timer_handle_t probe_timer;
static volatile bool probe_outstanding;
static bool initialized, pending_verify;
static httpd_handle_t server;
static esp_timer_handle_t health_timer, reboot_timer;
static esp_timer_handle_t upload_timer;
static SemaphoreHandle_t deadline_lock;
static int upload_fd = -1;
static int64_t upload_deadline;
static char *server_cert, *server_key, *client_cert;
static size_t server_cert_len, server_key_len, client_cert_len;

static void state(const char *value, const char *error)
{
    snprintf(journal.state, sizeof(journal.state), "%s", value);
    snprintf(journal.error, sizeof(journal.error), "%s", error ? error : "");
    ESP_LOGI(TAG, "state=%s error=%s", journal.state, journal.error);
}

static esp_err_t save_journal(void)
{
    uint8_t blob[YOKE_JOURNAL_BLOB_MAX];
    size_t size = yoke_ota_journal_encode(&journal, blob, sizeof(blob));
    esp_err_t err = size ? nvs_set_blob(trust, JOURNAL_KEY, blob, size) : ESP_ERR_INVALID_SIZE;
    if (err == ESP_OK) err = nvs_commit(trust);
    return err;
}

/* Read the versioned journal, or migrate 0.1.0's record once. Journal
 * problems never fail boot: the record is evidence, not trust. Returns a
 * note for status when evidence had to be discarded. */
static const char *load_journal(void)
{
    uint8_t blob[YOKE_JOURNAL_BLOB_MAX];
    size_t size = sizeof(blob);
    memset(&journal, 0, sizeof(journal));
    esp_err_t err = nvs_get_blob(trust, JOURNAL_KEY, blob, &size);
    if (err == ESP_OK) {
        yoke_journal_result_t result = yoke_ota_journal_decode(blob, size, &journal);
        if (result == YOKE_JOURNAL_OK) return NULL;
        memset(&journal, 0, sizeof(journal));
        return result == YOKE_JOURNAL_UNKNOWN_VERSION ? "journal-version-unknown-reset"
                                                      : "journal-corrupt-reset";
    }
    if (err != ESP_ERR_NVS_NOT_FOUND) return "journal-unreadable-reset";
    size = sizeof(blob);
    err = nvs_get_blob(trust, LEGACY_JOURNAL_KEY, blob, &size);
    if (err == ESP_ERR_NVS_NOT_FOUND) return NULL;
    if (err == ESP_OK && yoke_ota_journal_decode_legacy(blob, size, &journal) == YOKE_JOURNAL_OK)
        return NULL;
    memset(&journal, 0, sizeof(journal));
    return "legacy-journal-unreadable-reset";
}

static esp_err_t save_clock(void)
{
    int64_t now = (int64_t)time(NULL);
    esp_err_t err = nvs_set_i64(trust, "epoch", now);
    if (err == ESP_OK) err = nvs_commit(trust);
    return err;
}

static esp_err_t load_pem(const char *key, char **out, size_t *length)
{
    size_t size = 0;
    esp_err_t err = nvs_get_str(trust, key, NULL, &size);
    if (err != ESP_OK) return err;
    if (size < 32 || size > 8192) return ESP_ERR_INVALID_SIZE;
    char *value = calloc(1, size);
    if (!value) return ESP_ERR_NO_MEM;
    err = nvs_get_str(trust, key, value, &size);
    if (err != ESP_OK) { free(value); return err; }
    *out = value;
    *length = size; /* PEM lengths include final NUL for mbedTLS. */
    return ESP_OK;
}

static esp_err_t validate_trust(void)
{
    mbedtls_x509_crt device, operator;
    mbedtls_pk_context key;
    mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context rng;
    mbedtls_x509_crt_init(&device);
    mbedtls_x509_crt_init(&operator);
    mbedtls_pk_init(&key);
    mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_init(&rng);
    uint32_t flags = 0;
    int ret = mbedtls_ctr_drbg_seed(&rng, mbedtls_entropy_func, &entropy,
                                   (const uint8_t *)"yoke-ota", 8);
    if (!ret) ret = mbedtls_x509_crt_parse(&device, (uint8_t *)server_cert, server_cert_len);
    if (!ret) ret = mbedtls_x509_crt_parse(&operator, (uint8_t *)client_cert, client_cert_len);
    if (!ret) ret = mbedtls_pk_parse_key(&key, (uint8_t *)server_key, server_key_len,
                                        NULL, 0, mbedtls_ctr_drbg_random, &rng);
    if (!ret) ret = mbedtls_pk_check_pair(&device.pk, &key, mbedtls_ctr_drbg_random, &rng);
    if (!ret) ret = mbedtls_x509_crt_verify(&device, &device, NULL, NULL, &flags, NULL, NULL);
    if (!ret) ret = mbedtls_x509_crt_verify(&operator, &operator, NULL, NULL, &flags, NULL, NULL);
    /* Exactly one explicitly pinned certificate for each role. No CA bundle. */
    if (!ret) ret = mbedtls_x509_crt_check_extended_key_usage(
        &device, MBEDTLS_OID_SERVER_AUTH, MBEDTLS_OID_SIZE(MBEDTLS_OID_SERVER_AUTH));
    if (!ret) ret = mbedtls_x509_crt_check_extended_key_usage(
        &operator, MBEDTLS_OID_CLIENT_AUTH, MBEDTLS_OID_SIZE(MBEDTLS_OID_CLIENT_AUTH));
    if (device.next || operator.next || device.MBEDTLS_PRIVATE(ca_istrue) ||
        operator.MBEDTLS_PRIVATE(ca_istrue)) ret = -1;
    mbedtls_ctr_drbg_free(&rng);
    mbedtls_entropy_free(&entropy);
    mbedtls_pk_free(&key);
    mbedtls_x509_crt_free(&operator);
    mbedtls_x509_crt_free(&device);
    return ret ? ESP_ERR_INVALID_ARG : ESP_OK;
}

/* Every planned restart lets the application finish physical output
 * first (IR frames must never be cut by a reboot) and records the clock. */
static void __attribute__((noreturn)) restart(void *arg)
{
    (void)arg;
    if (restart_guard) restart_guard();
    (void)save_clock();
    esp_restart();
}

void yoke_ota_restart(void)
{
    restart(NULL);
}

static void wdt_feed(void)
{
    if (wdt_user) (void)esp_task_wdt_reset_user(wdt_user);
}

/* The HTTPS server task blocks in select(), so it cannot feed the task
 * watchdog itself. A periodic probe queued to that task feeds a TWDT user
 * only when the task actually runs it: a wedged server panics and leaves a
 * core dump. TLS handshakes (<= 10 s) stay below the 15 s timeout. */
static void probe_work(void *arg)
{
    (void)arg;
    probe_outstanding = false;
    wdt_feed();
}

/* At most one probe is queued: httpd's control queue is small and shared
 * with session-close requests, which must never be crowded out. */
static void probe(void *arg)
{
    (void)arg;
    if (!server || probe_outstanding) return;
    probe_outstanding = true;
    if (httpd_queue_work(server, probe_work, NULL) != ESP_OK) probe_outstanding = false;
}

/* Strong definition of the weak hook in the generated ESP-TLS handshake
 * override (prepare_tls.py): a handshake is bounded by its own absolute
 * deadline, so its wait loop proves the server task is alive. Without this
 * an unauthenticated peer stalling handshakes back to back could starve
 * the probe for longer than the watchdog timeout. */
void yoke_tls_handshake_tick(void)
{
    wdt_feed();
}

static void health_expired(void *arg)
{
    (void)arg;
    yoke_ota_fail_health();
}

static void upload_expired(void *arg)
{
    (void)arg;
    /* Do not take the upload/state lock: recv may be blocked holding it.
     * Never close here: shutdown wakes TLS and HTTPD remains the fd owner.
     * An already queued old callback cannot terminate a newer generation:
     * the current deadline must itself have expired under this mutex. */
    xSemaphoreTake(deadline_lock, portMAX_DELAY);
    if (upload_fd >= 0 && esp_timer_get_time() >= upload_deadline) {
        ESP_LOGE(TAG, "Absolute request deadline; shutting down socket");
        shutdown(upload_fd, SHUT_RDWR);
    }
    xSemaphoreGive(deadline_lock);
}

static void disarm_upload(void)
{
    xSemaphoreTake(deadline_lock, portMAX_DELAY);
    upload_fd = -1;
    xSemaphoreGive(deadline_lock);
    (void)esp_timer_stop(upload_timer);
}

static esp_err_t arm_upload(httpd_req_t *req, int64_t deadline)
{
    xSemaphoreTake(deadline_lock, portMAX_DELAY);
    upload_fd = httpd_req_to_sockfd(req);
    upload_deadline = deadline;
    int64_t remaining = upload_deadline - esp_timer_get_time();
    xSemaphoreGive(deadline_lock);
    if (remaining <= 0) return ESP_ERR_INVALID_STATE;
    return esp_timer_start_once(upload_timer, (uint64_t)remaining);
}

esp_err_t yoke_ota_init(void)
{
    if (lock) return ESP_ERR_INVALID_STATE;
    lock = xSemaphoreCreateMutex();
    if (!lock) return ESP_ERR_NO_MEM;
    deadline_lock = xSemaphoreCreateMutex();
    if (!deadline_lock) return ESP_ERR_NO_MEM;
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t ota_state;
    pending_verify = esp_ota_get_state_partition(running, &ota_state) == ESP_OK &&
                     ota_state == ESP_OTA_IMG_PENDING_VERIFY;
    /* Arm before loading trust, so failures still have a rollback deadline. */
    esp_timer_create_args_t timer_args = {
        .callback = health_expired, .name = "ota-health",
    };
    esp_err_t err = esp_timer_create(&timer_args, &health_timer);
    if (err != ESP_OK) return err;
    if (pending_verify) {
        err = esp_timer_start_once(health_timer, HEALTH_US);
        if (err != ESP_OK) return err;
    }
    err = nvs_flash_init_partition(TRUST_PARTITION);
    if (err != ESP_OK) return err; /* Never auto-erase commissioned data. */
    err = nvs_open_from_partition(TRUST_PARTITION, "ota", NVS_READWRITE, &trust);
    if (err != ESP_OK) return err;
    int64_t epoch = 0;
    err = nvs_get_i64(trust, "epoch", &epoch);
    if (err != ESP_OK) return err;
    if (epoch < 1704067200LL || epoch > 7258118400LL) return ESP_ERR_INVALID_ARG;
    struct timeval tv = {.tv_sec = (time_t)epoch};
    if (settimeofday(&tv, NULL) != 0) return ESP_FAIL;
    err = load_pem("server_cert", &server_cert, &server_cert_len);
    if (err == ESP_OK) err = load_pem("server_key", &server_key, &server_key_len);
    if (err == ESP_OK) err = load_pem("client_cert", &client_cert, &client_cert_len);
    if (err == ESP_OK) err = validate_trust();
    if (err != ESP_OK) return err;
    const char *journal_note = load_journal();
    bool target_matches = running->address == journal.target_address;
    const char *boot = yoke_ota_boot_state(pending_verify, journal.pending, target_matches);
    if (!strcmp(boot, "rolled-back")) {
        const esp_partition_t *target = esp_partition_find_first(
            ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_ANY,
            running->subtype == ESP_PARTITION_SUBTYPE_APP_OTA_0 ? "ota_1" : "ota_0");
        esp_ota_img_states_t target_state;
        if (target && esp_ota_get_state_partition(target, &target_state) == ESP_OK &&
            (target_state == ESP_OTA_IMG_ABORTED || target_state == ESP_OTA_IMG_INVALID))
            state("rolled-back", "new-image-failed-health");
        else state("error", "activation-not-completed");
        journal.pending = 0;
    } else if (pending_verify) state("pending-verify", NULL);
    else if (target_matches && journal.pending) {
        journal.pending = 0;
        state("healthy", NULL); /* Reset after confirmation, before journal commit. */
    } else if (!journal.state[0]) state("idle", NULL);
    if (journal_note && !journal.error[0])
        snprintf(journal.error, sizeof(journal.error), "%s", journal_note);
    if (nvs_get_u32(trust, "boots", &boot_count) != ESP_OK) boot_count = 0;
    if (boot_count < UINT32_MAX) ++boot_count;
    (void)nvs_set_u32(trust, "boots", boot_count); /* evidence only; committed below */
    err = save_journal();
    if (err == ESP_OK) err = save_clock();
    if (err != ESP_OK) return err;
    timer_args.callback = restart;
    timer_args.name = "ota-restart";
    err = esp_timer_create(&timer_args, &reboot_timer);
    if (err != ESP_OK) return err;
    timer_args.callback = upload_expired;
    timer_args.name = "ota-upload";
    err = esp_timer_create(&timer_args, &upload_timer);
    if (err != ESP_OK) return err;
    timer_args.callback = probe;
    timer_args.name = "ota-wdt-probe";
    err = esp_timer_create(&timer_args, &probe_timer);
    if (err != ESP_OK) return err;
    initialized = true;
    ESP_LOGI(TAG, "version=%s slot=%s clock_source=provisioned-floor",
             esp_app_get_description()->version, running->label);
    return ESP_OK;
}

esp_err_t yoke_ota_confirm_health(void)
{
    if (!initialized || !server) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(lock, portMAX_DELAY);
    esp_err_t err = save_clock();
    /* Persist first; marking valid is the last irreversible health decision. */
    if (err == ESP_OK && pending_verify) err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        pending_verify = false;
        (void)esp_timer_stop(health_timer);
        journal.pending = 0;
        /* Keep previous failure/rollback evidence visible until the next upload. */
        if (!strcmp(journal.state, "pending-verify") || !strcmp(journal.state, "idle"))
            state("healthy", NULL);
        err = save_journal();
    }
    xSemaphoreGive(lock);
    return err;
}

void yoke_ota_fail_health(void)
{
    if (!lock) return;
    xSemaphoreTake(lock, portMAX_DELAY);
    /* A confirmed image is never rolled back here; the application retries
     * or restarts (see yoke_node_restart_later). */
    if (!pending_verify) { xSemaphoreGive(lock); return; }
    ESP_LOGE(TAG, "Boot health failed or deadline expired; rolling back");
    if (restart_guard) restart_guard();
    /* Bootloader rollback still works if trust could not be opened. */
    esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    ESP_LOGE(TAG, "Rollback request returned %s; resetting", esp_err_to_name(err));
    esp_restart();
}

esp_err_t yoke_ota_respond(httpd_req_t *req, const char *status, cJSON *body)
{
    if (!body) return ESP_ERR_NO_MEM;
    char *json = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (!json) return ESP_ERR_NO_MEM;
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    httpd_resp_set_hdr(req, "Connection", "close");
    esp_err_t err = httpd_resp_send(req, json, HTTPD_RESP_USE_STRLEN);
    free(json);
    /* Close even on metadata errors with unread bodies. */
    httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));
    return err;
}

static esp_err_t reject(httpd_req_t *req, const char *status, const char *detail)
{
    state("error", detail);
    if (save_journal() != ESP_OK) ESP_LOGE(TAG, "Failed to persist update error");
    cJSON *body = cJSON_CreateObject();
    if (body) cJSON_AddStringToObject(body, "error", detail);
    (void)yoke_ota_respond(req, status, body);
    /* The response is sent; failing the handler makes HTTPD close the
     * socket instead of purging an unread body (up to a whole slot, at the
     * peer's pace) without feeding the watchdog. */
    return ESP_FAIL;
}

static const char *reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_EXT: return "external-pin";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt-watchdog";
    case ESP_RST_TASK_WDT: return "task-watchdog";
    case ESP_RST_WDT: return "other-watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    case ESP_RST_EFUSE: return "efuse";
    case ESP_RST_PWR_GLITCH: return "power-glitch";
    case ESP_RST_CPU_LOCKUP: return "cpu-lockup";
    default: return "unknown";
    }
}

static bool coredump_present(void)
{
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
    return esp_core_dump_image_check() == ESP_OK;
#else
    return false;
#endif
}

static esp_err_t status_request(httpd_req_t *req)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *selected = esp_ota_get_boot_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    const esp_app_desc_t *app = esp_app_get_description();
    cJSON *body = cJSON_CreateObject();
    if (body) {
        cJSON_AddNumberToObject(body, "protocol", 1);
        cJSON_AddStringToObject(body, "project", app->project_name);
        cJSON_AddStringToObject(body, "version", app->version);
        cJSON_AddStringToObject(body, "running_slot", running->label);
        cJSON_AddStringToObject(body, "selected_slot", selected ? selected->label : "unknown");
        cJSON_AddNumberToObject(body, "max_image_bytes", next ? next->size : 0);
        cJSON_AddStringToObject(body, "state", journal.state);
        cJSON_AddStringToObject(body, "error", journal.error);
        cJSON_AddStringToObject(body, "attempt_sha256", journal.digest);
        cJSON_AddStringToObject(body, "attempt_version", journal.version);
        cJSON_AddStringToObject(body, "clock_source", "provisioned-floor");
        cJSON_AddNumberToObject(body, "local_epoch", (double)time(NULL));
        cJSON_AddStringToObject(body, "authentication", "mutual-tls-pinned-client");
        cJSON_AddNumberToObject(body, "journal_version", YOKE_JOURNAL_VERSION);
        cJSON_AddStringToObject(body, "reset_reason", reset_reason());
        cJSON_AddNumberToObject(body, "uptime_s", (double)(esp_timer_get_time() / 1000000));
        cJSON_AddNumberToObject(body, "boot_count", boot_count);
        cJSON_AddNumberToObject(body, "heap_free", esp_get_free_heap_size());
        cJSON_AddNumberToObject(body, "heap_min_free", esp_get_minimum_free_heap_size());
        cJSON_AddBoolToObject(body, "coredump_present", coredump_present());
        if (status_hook) status_hook(body);
    }
    esp_err_t err = yoke_ota_respond(req, "200 OK", body);
    xSemaphoreGive(lock);
    return err;
}

static esp_err_t upload_request(httpd_req_t *req)
{
    int64_t started = esp_timer_get_time();
    xSemaphoreTake(lock, portMAX_DELAY);
    esp_err_t result;
    esp_ota_handle_t handle = 0;
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    const char *failure = NULL;
    const char *failure_status = "500 Internal Server Error";
    bool watchdog_armed = false;
    char hex[65], type[40], transfer[8], content_length[24];
    uint8_t expected[32], actual[32];
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *selected = esp_ota_get_boot_partition();
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    if (pending_verify || journal.pending || !selected || selected->address != running->address) {
        failure = "update-or-health-pending"; failure_status = "409 Conflict"; goto failed;
    }
    if (httpd_req_get_hdr_value_str(req, "Content-Type", type, sizeof(type)) != ESP_OK ||
        strcmp(type, "application/octet-stream")) {
        failure = "content-type-must-be-octet-stream"; failure_status = "415 Unsupported Media Type"; goto failed;
    }
    if (httpd_req_get_hdr_value_len(req, "Transfer-Encoding") ||
        httpd_req_get_hdr_value_str(req, "Transfer-Encoding", transfer, sizeof(transfer)) == ESP_OK ||
        httpd_req_get_hdr_value_str(req, "X-Image-SHA256", hex, sizeof(hex)) != ESP_OK ||
        !yoke_ota_parse_digest(hex, expected)) {
        failure = "invalid-length-or-sha256-header"; failure_status = "400 Bad Request"; goto failed;
    }
    size_t advertised_length = 0;
    /* IDF narrows parser uint64 content_length to 32-bit size_t on ESP32.
     * Validate the original header before trusting that narrowed value. */
    if (!next ||
        httpd_req_get_hdr_value_str(req, "Content-Length", content_length, sizeof(content_length)) != ESP_OK ||
        !yoke_ota_parse_length(content_length, next->size, &advertised_length) ||
        advertised_length != req->content_len ||
        !yoke_ota_length_allowed(advertised_length, next->size)) {
        failure = "image-size-outside-slot-limits"; failure_status = "413 Payload Too Large"; goto failed;
    }
    snprintf(journal.digest, sizeof(journal.digest), "%s", hex);
    journal.version[0] = 0;
    state("receiving", NULL);
    watchdog_armed = true;
    if (arm_upload(req, started + UPLOAD_US) != ESP_OK) {
        failure = "upload-watchdog-failed"; goto failed;
    }
    /* Erase sector by sector while writing: no single multi-second erase
     * stalls the server task (watchdog) or the IR task (cache disabled). */
    if (esp_ota_begin(next, OTA_WITH_SEQUENTIAL_WRITES, &handle) != ESP_OK) {
        failure = "ota-begin-failed"; goto failed;
    }
    if (mbedtls_sha256_starts(&sha, 0) != 0) { failure = "sha256-init-failed"; goto failed; }
    size_t received = 0;
    char buffer[2048];
    while (received < req->content_len) {
        if (esp_timer_get_time() - started >= UPLOAD_US) { failure = "upload-deadline"; goto failed; }
        size_t wanted = req->content_len - received;
        if (wanted > sizeof(buffer)) wanted = sizeof(buffer);
        int count = httpd_req_recv(req, buffer, wanted);
        if (count <= 0) { failure = "upload-disconnected-or-timeout"; goto failed; }
        if (esp_timer_get_time() - started >= UPLOAD_US) { failure = "upload-deadline"; goto failed; }
        if (mbedtls_sha256_update(&sha, (uint8_t *)buffer, count) != 0 ||
            esp_ota_write(handle, buffer, count) != ESP_OK) {
            failure = "hash-or-flash-write-failed"; goto failed;
        }
        received += count;
        wdt_feed();
    }
    if (esp_timer_get_time() - started >= UPLOAD_US) { failure = "upload-deadline"; goto failed; }
    disarm_upload();
    watchdog_armed = false;
    state("verifying", NULL);
    if (mbedtls_sha256_finish(&sha, actual) != 0 || memcmp(expected, actual, sizeof(actual))) {
        failure = "sha256-mismatch"; goto failed;
    }
    esp_err_t end = esp_ota_end(handle);
    handle = 0; /* esp_ota_end releases the handle on both success and failure. */
    if (end != ESP_OK) { failure = "invalid-esp-image"; goto failed; }
    esp_app_desc_t description;
    if (esp_ota_get_partition_description(next, &description) != ESP_OK ||
        !memchr(description.project_name, 0, sizeof(description.project_name)) ||
        strcmp(description.project_name, esp_app_get_description()->project_name) ||
        !memchr(description.version, 0, sizeof(description.version)) || !description.version[0]) {
        failure = "wrong-project-or-invalid-version"; goto failed;
    }
    snprintf(journal.version, sizeof(journal.version), "%s", description.version);
    journal.pending = 1;
    journal.target_address = next->address;
    state("pending-reboot", NULL);
    if (save_clock() != ESP_OK || save_journal() != ESP_OK) {
        journal.pending = 0; failure = "journal-or-clock-write-failed"; goto failed;
    }
    if (esp_ota_set_boot_partition(next) != ESP_OK) {
        journal.pending = 0; failure = "boot-selection-failed"; goto failed;
    }
    /* Schedule before sending: a dropped response must not cancel activation. */
    if (yoke_ota_schedule_restart() != ESP_OK) {
        ESP_LOGE(TAG, "Reboot timer failed after boot selection; resetting");
        restart(NULL); /* still guarded: never cut an IR frame */
    }
    cJSON *body = cJSON_CreateObject();
    if (body) {
        cJSON_AddStringToObject(body, "state", "pending-reboot");
        cJSON_AddStringToObject(body, "version", journal.version);
        cJSON_AddStringToObject(body, "sha256", journal.digest);
    }
    result = yoke_ota_respond(req, "202 Accepted", body);
    goto done;
failed:
    if (watchdog_armed) disarm_upload();
    if (handle) esp_ota_abort(handle);
    result = reject(req, failure_status, failure);
done:
    mbedtls_sha256_free(&sha);
    xSemaphoreGive(lock);
    return result;
}

static esp_err_t coredump_delete(httpd_req_t *req)
{
    cJSON *body = cJSON_CreateObject();
    esp_err_t err = ESP_OK;
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
    if (coredump_present()) err = esp_core_dump_image_erase();
#endif
    if (body) cJSON_AddBoolToObject(body, "coredump_present", coredump_present());
    return yoke_ota_respond(req, err == ESP_OK ? "200 OK" : "500 Internal Server Error", body);
}

esp_err_t yoke_ota_arm_deadline(httpd_req_t *req, int64_t deadline_us)
{
    return arm_upload(req, deadline_us);
}

void yoke_ota_disarm_deadline(void) { disarm_upload(); }

esp_err_t yoke_ota_schedule_restart(void)
{
    return initialized ? esp_timer_start_once(reboot_timer, REBOOT_DELAY_US)
                       : ESP_ERR_INVALID_STATE;
}

esp_err_t yoke_ota_start(void)
{
    if (!initialized || server) return ESP_ERR_INVALID_STATE;
    httpd_ssl_config_t cfg = HTTPD_SSL_CONFIG_DEFAULT();
    cfg.servercert = (uint8_t *)server_cert;
    cfg.servercert_len = server_cert_len;
    cfg.prvtkey_pem = (uint8_t *)server_key;
    cfg.prvtkey_len = server_key_len;
    cfg.cacert_pem = (uint8_t *)client_cert;
    cfg.cacert_len = client_cert_len;
    cfg.httpd.max_open_sockets = 2;
    cfg.httpd.recv_wait_timeout = 5;
    cfg.httpd.send_wait_timeout = 5;
    cfg.httpd.stack_size = 12288;
    cfg.transport_mode = HTTPD_SSL_TRANSPORT_SECURE;
    esp_err_t err = httpd_ssl_start(&server, &cfg);
    if (err != ESP_OK) return err;
    httpd_uri_t status_uri = {.uri = "/v1/status", .method = HTTP_GET, .handler = status_request};
    httpd_uri_t upload_uri = {.uri = "/v1/firmware", .method = HTTP_PUT, .handler = upload_request};
    httpd_uri_t coredump_uri = {.uri = "/v1/coredump", .method = HTTP_DELETE,
                                .handler = coredump_delete};
    err = httpd_register_uri_handler(server, &status_uri);
    if (err == ESP_OK) err = httpd_register_uri_handler(server, &upload_uri);
    if (err == ESP_OK) err = httpd_register_uri_handler(server, &coredump_uri);
    if (err == ESP_OK) err = esp_task_wdt_add_user("ota-httpd", &wdt_user);
    if (err == ESP_OK) err = esp_timer_start_periodic(probe_timer, WDT_PROBE_US);
    if (err != ESP_OK) {
        if (wdt_user) { esp_task_wdt_delete_user(wdt_user); wdt_user = NULL; }
        httpd_ssl_stop(server);
        server = NULL;
    }
    return err;
}

bool yoke_ota_pending_verify(void) { return pending_verify; }

esp_err_t yoke_ota_save_clock_floor(void)
{
    /* NVS has its own lock; never take the upload lock from a timer. */
    return initialized ? save_clock() : ESP_ERR_INVALID_STATE;
}

httpd_handle_t yoke_ota_server(void) { return server; }

void yoke_ota_set_restart_guard(void (*guard)(void)) { restart_guard = guard; }

void yoke_ota_set_status_hook(void (*hook)(cJSON *body)) { status_hook = hook; }
