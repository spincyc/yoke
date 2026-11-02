#pragma once
/* Host fault-injection boundary. No test here claims hardware or TLS proof.
 * SHA functions return an injected digest; target uses unmodified mbedTLS. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE 1
#define CONFIG_MBEDTLS_HAVE_TIME_DATE 1
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define portMAX_DELAY 0
#define NVS_READWRITE 0
#define ESP_PARTITION_TYPE_APP 0
#define ESP_PARTITION_SUBTYPE_ANY 255
#define ESP_PARTITION_SUBTYPE_APP_OTA_0 16
#define ESP_OTA_IMG_PENDING_VERIFY 1
#define ESP_OTA_IMG_ABORTED 2
#define ESP_OTA_IMG_INVALID 3
#define HTTPD_RESP_USE_STRLEN -1
#define HTTP_GET 0
#define HTTP_PUT 1
#define HTTPD_SSL_TRANSPORT_SECURE 1
#define HTTPD_SSL_CONFIG_DEFAULT() {0}
#define shutdown host_shutdown
int host_shutdown(int fd, int how);

typedef int nvs_handle_t;
typedef void *SemaphoreHandle_t;
typedef void *httpd_handle_t;
typedef unsigned esp_ota_handle_t;
typedef int esp_ota_img_states_t;
typedef struct { uint32_t address, size, subtype; const char *label; } esp_partition_t;
typedef struct { char version[32], project_name[32]; } esp_app_desc_t;
typedef void *esp_timer_handle_t;
typedef struct { void (*callback)(void *); const char *name; } esp_timer_create_args_t;
typedef struct { size_t content_len; void *handle; } httpd_req_t;
typedef struct { int unused; } cJSON;
typedef struct { const char *uri; int method; esp_err_t (*handler)(httpd_req_t *); } httpd_uri_t;
typedef struct {
    const uint8_t *servercert, *prvtkey_pem, *cacert_pem;
    size_t servercert_len, prvtkey_len, cacert_len;
    int transport_mode;
    struct { int max_open_sockets, recv_wait_timeout, send_wait_timeout, stack_size; } httpd;
} httpd_ssl_config_t;
#ifdef YOKE_OTA_REAL_MBEDTLS
#include "mbedtls/sha256.h"
#include "mbedtls/ctr_drbg.h"
#include "mbedtls/entropy.h"
#include "mbedtls/pk.h"
#include "mbedtls/oid.h"
#include "mbedtls/x509_crt.h"
#else
#define MBEDTLS_PRIVATE(member) member
#define MBEDTLS_OID_SERVER_AUTH "server"
#define MBEDTLS_OID_CLIENT_AUTH "client"
#define MBEDTLS_OID_SIZE(value) (sizeof(value) - 1)
typedef struct { int unused; } mbedtls_sha256_context;
typedef struct { int unused; } mbedtls_pk_context;
typedef struct { int unused; } mbedtls_entropy_context;
typedef struct { int unused; } mbedtls_ctr_drbg_context;
typedef struct mbedtls_x509_crt { struct mbedtls_x509_crt *next; int pk, ca_istrue; } mbedtls_x509_crt;

/* Unreached init/TLS APIs only need declarations for this translation unit. */
int mbedtls_x509_crt_parse(); int mbedtls_pk_parse_key(); int mbedtls_pk_check_pair();
int mbedtls_x509_crt_verify(); int mbedtls_x509_crt_check_extended_key_usage();
int mbedtls_ctr_drbg_seed(); int mbedtls_ctr_drbg_random(); int mbedtls_entropy_func();
void mbedtls_x509_crt_init(); void mbedtls_x509_crt_free();
void mbedtls_pk_init(); void mbedtls_pk_free();
void mbedtls_entropy_init(); void mbedtls_entropy_free();
void mbedtls_ctr_drbg_init(); void mbedtls_ctr_drbg_free();
#endif
esp_err_t nvs_get_str(); esp_err_t nvs_get_i64(); esp_err_t nvs_get_blob();
esp_err_t nvs_flash_init_partition(); esp_err_t nvs_open_from_partition();
esp_err_t httpd_ssl_start(); esp_err_t httpd_ssl_stop(); esp_err_t httpd_register_uri_handler();

const esp_partition_t *esp_ota_get_running_partition(void);
const esp_partition_t *esp_ota_get_boot_partition(void);
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *);
const esp_partition_t *esp_partition_find_first(int, int, const char *);
const esp_app_desc_t *esp_app_get_description(void);
esp_err_t esp_ota_get_state_partition(const esp_partition_t *, esp_ota_img_states_t *);
esp_err_t esp_ota_begin(const esp_partition_t *, size_t, esp_ota_handle_t *);
esp_err_t esp_ota_write(esp_ota_handle_t, const void *, size_t);
esp_err_t esp_ota_end(esp_ota_handle_t);
esp_err_t esp_ota_abort(esp_ota_handle_t);
esp_err_t esp_ota_get_partition_description(const esp_partition_t *, esp_app_desc_t *);
esp_err_t esp_ota_set_boot_partition(const esp_partition_t *);
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void);
esp_err_t esp_ota_mark_app_invalid_rollback_and_reboot(void);
void esp_restart(void);
const char *esp_err_to_name(esp_err_t);
esp_err_t nvs_set_blob(nvs_handle_t, const char *, const void *, size_t);
esp_err_t nvs_set_i64(nvs_handle_t, const char *, int64_t);
esp_err_t nvs_commit(nvs_handle_t);
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t, int);
int xSemaphoreGive(SemaphoreHandle_t);
esp_err_t esp_timer_create(const esp_timer_create_args_t *, esp_timer_handle_t *);
esp_err_t esp_timer_start_once(esp_timer_handle_t, uint64_t);
esp_err_t esp_timer_stop(esp_timer_handle_t);
int64_t esp_timer_get_time(void);
int httpd_req_get_hdr_value_str(httpd_req_t *, const char *, char *, size_t);
size_t httpd_req_get_hdr_value_len(httpd_req_t *, const char *);
int httpd_req_recv(httpd_req_t *, char *, size_t);
int httpd_req_to_sockfd(httpd_req_t *);
esp_err_t httpd_resp_set_status(httpd_req_t *, const char *);
esp_err_t httpd_resp_set_type(httpd_req_t *, const char *);
esp_err_t httpd_resp_set_hdr(httpd_req_t *, const char *, const char *);
esp_err_t httpd_resp_send(httpd_req_t *, const char *, int);
esp_err_t httpd_sess_trigger_close(httpd_handle_t, int);
cJSON *cJSON_CreateObject(void);
void cJSON_AddStringToObject(cJSON *, const char *, const char *);
void cJSON_AddNumberToObject(cJSON *, const char *, double);
char *cJSON_PrintUnformatted(const cJSON *);
void cJSON_Delete(cJSON *);
#ifndef YOKE_OTA_REAL_MBEDTLS
void mbedtls_sha256_init(mbedtls_sha256_context *);
void mbedtls_sha256_free(mbedtls_sha256_context *);
int mbedtls_sha256_starts(mbedtls_sha256_context *, int);
int mbedtls_sha256_update(mbedtls_sha256_context *, const uint8_t *, size_t);
int mbedtls_sha256_finish(mbedtls_sha256_context *, uint8_t *);
#endif
