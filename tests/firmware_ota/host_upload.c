#define YOKE_OTA_HOST_TEST
#include "../../firmware/components/yoke_ota/yoke_ota.c"
#include <assert.h>

static esp_partition_t active = {0x30000, 0x600000, 16, "ota_0"};
static esp_partition_t inactive = {0x630000, 0x600000, 17, "ota_1"};
static esp_app_desc_t app = {.version = "1", .project_name = "yoke-ir-updater"};
static const char *fault;
static char response[64];
static int began, aborted, ended, selected_count, timer_count, closed, confirmed;
static size_t written;
static int64_t ticks;
static int shutdown_count;
static int wdt_feeds, guard_calls, restarts, clock_saves, rollbacks;
static bool exit_on_restart;

const esp_partition_t *esp_ota_get_running_partition(void) { return &active; }
const esp_partition_t *esp_ota_get_boot_partition(void) { return &active; }
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *p) { (void)p; return &inactive; }
const esp_app_desc_t *esp_app_get_description(void) { return &app; }
esp_err_t esp_ota_begin(const esp_partition_t *p, size_t n, esp_ota_handle_t *h)
{ assert(p == &inactive); assert(n == OTA_WITH_SEQUENTIAL_WRITES); began++; *h = 1; return !strcmp(fault, "begin") ? ESP_FAIL : ESP_OK; }
esp_err_t esp_ota_write(esp_ota_handle_t h, const void *p, size_t n)
{ (void)p; assert(h == 1); written += n; return !strcmp(fault, "write") ? ESP_FAIL : ESP_OK; }
esp_err_t esp_ota_end(esp_ota_handle_t h) { assert(h == 1); ended++; return !strcmp(fault, "image") ? ESP_FAIL : ESP_OK; }
esp_err_t esp_ota_abort(esp_ota_handle_t h) { assert(h == 1); aborted++; return ESP_OK; }
esp_err_t esp_ota_get_partition_description(const esp_partition_t *p, esp_app_desc_t *d)
{ assert(p == &inactive); *d = app; strcpy(d->version, "2"); if (!strcmp(fault, "project")) strcpy(d->project_name, "other"); return ESP_OK; }
esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p)
{ assert(p == &inactive); selected_count++; return !strcmp(fault, "selection") ? ESP_FAIL : ESP_OK; }
esp_err_t nvs_set_blob(nvs_handle_t n, const char *key, const void *p, size_t size)
{
    (void)n;
    /* Only the versioned record is written; 0.1.0's "journal" stays as it was. */
    assert(!strcmp(key, "journal2")); assert(size == YOKE_JOURNAL_V1_SIZE);
    yoke_journal_t check;
    assert(yoke_ota_journal_decode(p, size, &check) == YOKE_JOURNAL_OK);
    assert(!strcmp(check.state, journal.state) && check.pending == journal.pending);
    return !strcmp(fault, "journal") ? ESP_FAIL : ESP_OK;
}
esp_err_t nvs_set_i64(nvs_handle_t n, const char *key, int64_t v)
{ (void)n; (void)v; assert(!strcmp(key, "epoch")); assert(!guard_calls || guard_calls == 1); clock_saves++; return !strcmp(fault, "clock") ? ESP_FAIL : ESP_OK; }
esp_err_t nvs_commit(nvs_handle_t n) { (void)n; return ESP_OK; }
int xSemaphoreTake(SemaphoreHandle_t h, int t) { (void)h; (void)t; return 1; }
int xSemaphoreGive(SemaphoreHandle_t h) { (void)h; return 1; }
esp_err_t esp_timer_start_once(esp_timer_handle_t h, uint64_t t)
{ if (h == upload_timer) { assert(t <= UPLOAD_US); return ESP_OK; } assert(t == 2000000); timer_count++; return ESP_OK; }
esp_err_t esp_timer_stop(esp_timer_handle_t h) { (void)h; return ESP_OK; }
int64_t esp_timer_get_time(void) { ticks += !strcmp(fault, "deadline") ? UPLOAD_US : 1000; return ticks; }
void esp_restart(void)
{
    assert(exit_on_restart && "unexpected restart in host test");
    /* Planned restart: the IR guard ran first and the clock was saved. */
    assert(guard_calls == 1 && clock_saves == 1);
    restarts++;
    exit(0);
}
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void) { confirmed++; return ESP_OK; }
esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(void)
{
    assert(guard_calls == 1); /* frames finished before the rollback reboot */
    rollbacks++;
    exit(0);
}
const char *esp_err_to_name(esp_err_t e) { (void)e; return "error"; }
esp_err_t esp_task_wdt_reset_user(esp_task_wdt_user_handle_t h) { assert(h == (void *)5); wdt_feeds++; return ESP_OK; }
static void guard(void) { guard_calls++; }
static int queued;
esp_err_t httpd_queue_work(httpd_handle_t h, void (*fn)(void *), void *arg)
{ (void)h; (void)arg; assert(fn == probe_work); queued++; return ESP_OK; }
int host_shutdown(int fd, int how) { assert(fd == 77 && how == SHUT_RDWR); shutdown_count++; return 0; }

int httpd_req_get_hdr_value_str(httpd_req_t *r, const char *name, char *out, size_t n)
{
    (void)r;
    if (!strcmp(name, "Transfer-Encoding")) return ESP_FAIL;
    if (!strcmp(name, "Content-Type")) snprintf(out, n, "%s", !strcmp(fault, "type") ? "text/plain" : "application/octet-stream");
    else if (!strcmp(name, "Content-Length")) snprintf(out, n, "%zu", !strcmp(fault, "length-overflow") ? (size_t)4294968296ULL : r->content_len);
    else {
        memset(out, 'a', n - 1); out[n - 1] = 0;
        if (!strcmp(fault, "header")) out[0] = 'G';
    }
    return ESP_OK;
}
size_t httpd_req_get_hdr_value_len(httpd_req_t *r, const char *name)
{ (void)r; (void)name; return !strcmp(fault, "chunked") ? 7 : 0; }
int httpd_req_recv(httpd_req_t *r, char *out, size_t n)
{
    (void)r;
    if (!strcmp(fault, "disconnect")) return 0;
    if (!strcmp(fault, "timeout")) return -1;
    /* Deliberately short receives exercise partial-read accumulation. */
    if (n > 73) n = 73;
    memset(out, 0xab, n);
    return (int)n;
}
int httpd_req_to_sockfd(httpd_req_t *r) { (void)r; return 1; }
esp_err_t httpd_resp_set_status(httpd_req_t *r, const char *s) { (void)r; snprintf(response, sizeof(response), "%s", s); return ESP_OK; }
esp_err_t httpd_resp_set_type(httpd_req_t *r, const char *s) { (void)r; (void)s; return ESP_OK; }
esp_err_t httpd_resp_set_hdr(httpd_req_t *r, const char *k, const char *v) { (void)r; (void)k; (void)v; return ESP_OK; }
esp_err_t httpd_resp_send(httpd_req_t *r, const char *s, int n)
{ (void)r; (void)s; (void)n; return !strcmp(fault, "response-lost") ? ESP_FAIL : ESP_OK; }
esp_err_t httpd_sess_trigger_close(httpd_handle_t h, int s) { (void)h; (void)s; closed++; return ESP_OK; }
cJSON *cJSON_CreateObject(void) { return calloc(1, sizeof(cJSON)); }
void cJSON_AddStringToObject(cJSON *j, const char *k, const char *v) { (void)j; (void)k; (void)v; }
void cJSON_AddNumberToObject(cJSON *j, const char *k, double v) { (void)j; (void)k; (void)v; }
char *cJSON_PrintUnformatted(const cJSON *j) { (void)j; return strdup("{}"); }
void cJSON_Delete(cJSON *j) { free(j); }
void mbedtls_sha256_init(mbedtls_sha256_context *s) { (void)s; }
void mbedtls_sha256_free(mbedtls_sha256_context *s) { (void)s; }
int mbedtls_sha256_starts(mbedtls_sha256_context *s, int n) { (void)s; (void)n; return 0; }
int mbedtls_sha256_update(mbedtls_sha256_context *s, const uint8_t *p, size_t n) { (void)s; (void)p; (void)n; return 0; }
int mbedtls_sha256_finish(mbedtls_sha256_context *s, uint8_t *out)
{ (void)s; memset(out, !strcmp(fault, "digest") ? 0xbb : 0xaa, 32); return 0; }

int main(int argc, char **argv)
{
    assert(argc == 2);
    fault = argv[1];
    upload_timer = (void *)2;
    reboot_timer = (void *)3;
    deadline_lock = (void *)4;
    if (!strcmp(fault, "watchdog")) {
        upload_fd = 77;
        upload_deadline = 500;
        upload_expired(NULL);
        assert(shutdown_count == 1);
        /* Socket disarmed before HTTPD can close/reuse its fd. */
        upload_fd = -1;
        upload_expired(NULL);
        assert(shutdown_count == 1);
        /* An old queued callback cannot shutdown a newer upload. */
        upload_fd = 77;
        upload_deadline = ticks + 5000;
        upload_expired(NULL);
        assert(shutdown_count == 1);
        disarm_upload();
        assert(upload_fd == -1);
        return 0;
    }
    if (!strcmp(fault, "restart-guard")) {
        exit_on_restart = true;
        initialized = true;
        trust = 1;
        yoke_ota_set_restart_guard(guard);
        restart(NULL);
        assert(!"restart returned");
    }
    if (!strcmp(fault, "probe")) {
        /* At most one watchdog probe is queued to the server task; the
         * deadline-bounded TLS handshake loop feeds the same user. */
        server = (void *)1;
        wdt_user = (void *)5;
        probe(NULL); probe(NULL); probe(NULL);
        assert(queued == 1 && wdt_feeds == 0);
        probe_work(NULL);
        assert(wdt_feeds == 1);
        probe(NULL);
        assert(queued == 2);
        yoke_tls_handshake_tick();
        assert(wdt_feeds == 2);
        return 0;
    }
    if (!strcmp(fault, "fail-health")) {
        lock = (void *)1;
        yoke_ota_set_restart_guard(guard);
        pending_verify = false; /* confirmed image: left running, no guard */
        yoke_ota_fail_health();
        assert(guard_calls == 0 && rollbacks == 0);
        pending_verify = true;
        yoke_ota_fail_health();
        assert(!"rollback returned");
    }
    if (!strcmp(fault, "journal-codec")) {
        yoke_journal_t in = {.target_address = 0x630000, .pending = 1}, out;
        strcpy(in.digest, "ab"); strcpy(in.version, "0.2.0");
        strcpy(in.state, "pending-reboot"); strcpy(in.error, "");
        uint8_t blob[YOKE_JOURNAL_BLOB_MAX];
        size_t n = yoke_ota_journal_encode(&in, blob, sizeof(blob));
        assert(n == YOKE_JOURNAL_V1_SIZE && !yoke_ota_journal_encode(&in, blob, 10));
        assert(yoke_ota_journal_decode(blob, n, &out) == YOKE_JOURNAL_OK);
        assert(out.target_address == in.target_address && out.pending == in.pending &&
               !memcmp(out.digest, in.digest, sizeof(in.digest)) &&
               !memcmp(out.version, in.version, sizeof(in.version)) &&
               !memcmp(out.state, in.state, sizeof(in.state)) &&
               !memcmp(out.error, in.error, sizeof(in.error)));
        /* A newer writer that keeps the v1 prefix readable (compat 1). */
        uint8_t newer[YOKE_JOURNAL_BLOB_MAX];
        memcpy(newer, blob, n);
        memset(newer + n, 0x5a, 40);
        newer[4] = 2; newer[8] = (uint8_t)((n + 40) & 0xff); newer[9] = (uint8_t)((n + 40) >> 8);
        assert(yoke_ota_journal_decode(newer, n + 40, &out) == YOKE_JOURNAL_OK);
        assert(out.pending == 1 && !strcmp(out.version, "0.2.0"));
        /* A newer writer that declares v1 readers unable to read it. */
        newer[6] = 2;
        assert(yoke_ota_journal_decode(newer, n + 40, &out) == YOKE_JOURNAL_UNKNOWN_VERSION);
        /* Corruption: magic, size field, truncation, text, pending range. */
        uint8_t bad[YOKE_JOURNAL_BLOB_MAX];
        memcpy(bad, blob, n); bad[0] ^= 1;
        assert(yoke_ota_journal_decode(bad, n, &out) == YOKE_JOURNAL_CORRUPT);
        assert(yoke_ota_journal_decode(blob, n - 1, &out) == YOKE_JOURNAL_CORRUPT);
        assert(yoke_ota_journal_decode(blob, 4, &out) == YOKE_JOURNAL_CORRUPT);
        assert(yoke_ota_journal_decode(NULL, 0, &out) == YOKE_JOURNAL_CORRUPT);
        memcpy(bad, blob, n); memset(bad + 20, 'x', 65);
        assert(yoke_ota_journal_decode(bad, n, &out) == YOKE_JOURNAL_CORRUPT);
        memcpy(bad, blob, n); bad[16] = 7;
        assert(yoke_ota_journal_decode(bad, n, &out) == YOKE_JOURNAL_CORRUPT);
        memcpy(bad, blob, n); bad[4] = 0; bad[5] = 0;
        assert(yoke_ota_journal_decode(bad, n, &out) == YOKE_JOURNAL_CORRUPT);
        /* 0.1.0's unversioned struct: migrated once, identical fields. */
        struct {
            uint32_t magic, target_address, pending;
            char digest[65], version[33], state[24], error[80];
        } legacy = {0x594f5401U, 0x30000, 0, "cd", "0.1.0", "healthy", ""};
        assert(sizeof(legacy) == YOKE_JOURNAL_LEGACY_SIZE);
        assert(yoke_ota_journal_decode_legacy((uint8_t *)&legacy, sizeof(legacy), &out) ==
               YOKE_JOURNAL_OK);
        assert(out.target_address == 0x30000 && !strcmp(out.state, "healthy") &&
               !strcmp(out.version, "0.1.0") && !strcmp(out.digest, "cd"));
        assert(yoke_ota_journal_decode_legacy((uint8_t *)&legacy, sizeof(legacy) - 1, &out) ==
               YOKE_JOURNAL_CORRUPT);
        legacy.magic = 0x594f5402U;
        assert(yoke_ota_journal_decode_legacy((uint8_t *)&legacy, sizeof(legacy), &out) ==
               YOKE_JOURNAL_CORRUPT);
        assert(yoke_ota_journal_decode(blob, n, &out) == YOKE_JOURNAL_OK);
        return 0;
    }
    if (!strcmp(fault, "health")) {
        yoke_health_inputs_t in = {.local_ok = true, .app_ok = true, .listeners_up = true,
                                   .link_settle_ms = 10000};
        /* Link up, no IP yet: wait (never confirm on listener start alone). */
        in.link_up = true;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_WAIT);
        in.ms_since_eth_start = 60000;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_WAIT);
        in.ip_acquired = true;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_CONFIRM);
        /* IP but a listener not up yet: wait. */
        in.listeners_up = false;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_WAIT);
        in.listeners_up = true;
        /* Cable-less bench boot: link never came up within the window. */
        in = (yoke_health_inputs_t){.local_ok = true, .app_ok = true, .listeners_up = true,
                                    .link_settle_ms = 10000, .ms_since_eth_start = 9999};
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_WAIT);
        in.ms_since_eth_start = 10000;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_CONFIRM);
        /* Explicit link-down event confirms without waiting. */
        in.ms_since_eth_start = 0;
        in.link_down_seen = true;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_CONFIRM);
        /* Local failures always fail, even with IP. */
        in.ip_acquired = true;
        in.app_ok = false;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_FAIL);
        in.app_ok = true;
        in.local_ok = false;
        assert(yoke_ota_health_decide(&in) == YOKE_HEALTH_FAIL);
        return 0;
    }
    if (!strcmp(fault, "policy")) {
        uint8_t bytes[32];
        assert(!yoke_ota_parse_digest(NULL, bytes));
        assert(!yoke_ota_parse_digest("", bytes));
        assert(!yoke_ota_parse_digest("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaG", bytes));
        assert(yoke_ota_parse_digest("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", bytes));
        assert(bytes[0] == 0x01 && bytes[7] == 0xef);
        assert(!yoke_ota_length_allowed(0, 1000));
        assert(!yoke_ota_length_allowed(287, 1000));
        assert(yoke_ota_length_allowed(288, 1000));
        assert(yoke_ota_length_allowed(1000, 1000));
        assert(!yoke_ota_length_allowed(1001, 1000));
        size_t parsed;
        assert(yoke_ota_parse_length("1000", 1000, &parsed) && parsed == 1000);
        assert(!yoke_ota_parse_length("4294968296", 0x600000, &parsed));
        assert(!yoke_ota_parse_length("18446744073709551616", SIZE_MAX, &parsed));
        assert(!yoke_ota_parse_length("-1", SIZE_MAX, &parsed));
        assert(!yoke_ota_parse_length("0", SIZE_MAX, &parsed));
        assert(!strcmp(yoke_ota_boot_state(true, true, true), "pending-verify"));
        assert(!strcmp(yoke_ota_boot_state(false, true, false), "rolled-back"));
        assert(!strcmp(yoke_ota_boot_state(false, false, true), "idle"));
        return 0;
    }
    lock = (void *)1;
    server = (void *)1;
    wdt_user = (void *)5;
    initialized = true;
    pending_verify = !strcmp(fault, "pending");
    if (!strcmp(fault, "already-selected")) journal.pending = 1;
    httpd_req_t req = {.content_len = !strcmp(fault, "oversize") ? inactive.size + 1 : 1000};
    esp_err_t result = upload_request(&req);
    assert(closed == 1);
    if (!strcmp(fault, "ok") || !strcmp(fault, "response-lost")) {
        assert(!strcmp(response, "202 Accepted"));
        assert(began == 1 && ended == 1 && selected_count == 1 && timer_count == 1);
        assert(written == req.content_len && aborted == 0 && journal.pending == 1);
        assert(!strcmp(journal.version, "2"));
        assert(result == (!strcmp(fault, "ok") ? ESP_OK : ESP_FAIL));
        /* The server task fed its watchdog user once per received chunk. */
        assert(wdt_feeds == (int)((req.content_len + 72) / 73));
    } else {
        assert(strncmp(response, "202", 3));
        /* Rejections close the socket rather than draining the body. */
        assert(result == ESP_FAIL);
        assert(timer_count == 0);
        assert(selected_count == (!strcmp(fault, "selection") ? 1 : 0));
        if (!strcmp(fault, "digest") || !strcmp(fault, "write") ||
            !strcmp(fault, "disconnect") || !strcmp(fault, "timeout") ||
            !strcmp(fault, "begin")) assert(aborted == 1);
        if (!strcmp(fault, "pending") || !strcmp(fault, "already-selected") ||
            !strcmp(fault, "header") || !strcmp(fault, "type") ||
            !strcmp(fault, "oversize") || !strcmp(fault, "chunked") ||
            !strcmp(fault, "deadline") || !strcmp(fault, "length-overflow")) assert(began == 0);
        if (!strcmp(fault, "image") || !strcmp(fault, "project")) assert(aborted == 0 && ended == 1);
    }
    /* Confirmation requires local readiness, never a remote HTTP endpoint. */
    if (!strcmp(fault, "clock")) {
        pending_verify = true;
        assert(yoke_ota_confirm_health() != ESP_OK);
        assert(confirmed == 0 && pending_verify);
    }
    return 0;
}
