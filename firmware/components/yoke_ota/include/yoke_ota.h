#pragma once

#include <stdbool.h>
#include "esp_err.h"
#ifndef YOKE_OTA_HOST_TEST
#include "cJSON.h"
#include "esp_http_server.h"
#endif

/* Single instance. Call init early; it arms the pending-image health timer.
 * The boot APIs are called by app_main, never HTTP clients.
 * Failures do not erase trust or application storage. No network required.
 * Caller must enable and retain a true entropy source before init and for
 * the full server lifetime (wired-only reference app reserves SAR ADC using
 * bootloader_random_enable). Do not disable it or use ADC concurrently. */
esp_err_t yoke_ota_init(void);
esp_err_t yoke_ota_start(void);
esp_err_t yoke_ota_confirm_health(void);
/* Pending image: guard, then bootloader rollback and reboot. A confirmed
 * image is left running; the caller decides whether to retry or restart. */
void yoke_ota_fail_health(void);
bool yoke_ota_pending_verify(void);
/* Persist the advancing wall-clock floor (call periodically). */
esp_err_t yoke_ota_save_clock_floor(void);
/* The mutual-TLS server, for further admin handlers (NULL before start). */
httpd_handle_t yoke_ota_server(void);
/* Called before every planned restart or rollback, from timer context; it
 * must return within a few seconds (e.g. finish IR frames in flight). */
void yoke_ota_set_restart_guard(void (*guard)(void));
/* Adds application fields to GET /v1/status. */
void yoke_ota_set_status_hook(void (*hook)(cJSON *body));
/* Guarded planned restart now (restart guard, clock floor). */
void yoke_ota_restart(void) __attribute__((noreturn));
/* Guarded planned restart two seconds from now (after a response). */
esp_err_t yoke_ota_schedule_restart(void);
/* Admin handler reply: JSON body (consumed), no-store, connection closed. */
esp_err_t yoke_ota_respond(httpd_req_t *req, const char *status, cJSON *body);
/* Admin handler body reads: at deadline_us (esp_timer time) the request's
 * socket is shut down, so a peer trickling bytes cannot hold the server
 * task (the upload's own deadline). One at a time; disarm before replying. */
esp_err_t yoke_ota_arm_deadline(httpd_req_t *req, int64_t deadline_us);
void yoke_ota_disarm_deadline(void);
