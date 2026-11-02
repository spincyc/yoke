#define YOKE_OTA_HOST_TEST
#include "../../firmware/esp32/components/yoke_ota/yoke_ota.c"
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

const esp_partition_t *esp_ota_get_running_partition(void) { return &active; }
const esp_partition_t *esp_ota_get_boot_partition(void) { return &active; }
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *p) { (void)p; return &inactive; }
const esp_app_desc_t *esp_app_get_description(void) { return &app; }
esp_err_t esp_ota_begin(const esp_partition_t *p, size_t n, esp_ota_handle_t *h)
{ assert(p == &inactive); assert(n <= inactive.size); began++; *h = 1; return !strcmp(fault, "begin") ? ESP_FAIL : ESP_OK; }
esp_err_t esp_ota_write(esp_ota_handle_t h, const void *p, size_t n)
{ (void)p; assert(h == 1); written += n; return !strcmp(fault, "write") ? ESP_FAIL : ESP_OK; }
esp_err_t esp_ota_end(esp_ota_handle_t h) { assert(h == 1); ended++; return !strcmp(fault, "image") ? ESP_FAIL : ESP_OK; }
esp_err_t esp_ota_abort(esp_ota_handle_t h) { assert(h == 1); aborted++; return ESP_OK; }
esp_err_t esp_ota_get_partition_description(const esp_partition_t *p, esp_app_desc_t *d)
{ assert(p == &inactive); *d = app; strcpy(d->version, "2"); if (!strcmp(fault, "project")) strcpy(d->project_name, "other"); return ESP_OK; }
esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p)
{ assert(p == &inactive); selected_count++; return !strcmp(fault, "selection") ? ESP_FAIL : ESP_OK; }
esp_err_t nvs_set_blob(nvs_handle_t n, const char *key, const void *p, size_t size)
{ (void)n; (void)p; assert(!strcmp(key, "journal")); assert(size == sizeof(journal)); return !strcmp(fault, "journal") ? ESP_FAIL : ESP_OK; }
esp_err_t nvs_set_i64(nvs_handle_t n, const char *key, int64_t v)
{ (void)n; (void)v; assert(!strcmp(key, "epoch")); return !strcmp(fault, "clock") ? ESP_FAIL : ESP_OK; }
esp_err_t nvs_commit(nvs_handle_t n) { (void)n; return ESP_OK; }
int xSemaphoreTake(SemaphoreHandle_t h, int t) { (void)h; (void)t; return 1; }
int xSemaphoreGive(SemaphoreHandle_t h) { (void)h; return 1; }
esp_err_t esp_timer_start_once(esp_timer_handle_t h, uint64_t t)
{ if (h == upload_timer) { assert(t <= UPLOAD_US); return ESP_OK; } assert(t == 2000000); timer_count++; return ESP_OK; }
esp_err_t esp_timer_stop(esp_timer_handle_t h) { (void)h; return ESP_OK; }
int64_t esp_timer_get_time(void) { ticks += !strcmp(fault, "deadline") ? UPLOAD_US : 1000; return ticks; }
void esp_restart(void) { assert(!"unexpected restart in host test"); }
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void) { confirmed++; return ESP_OK; }
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
    } else {
        assert(strncmp(response, "202", 3));
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
