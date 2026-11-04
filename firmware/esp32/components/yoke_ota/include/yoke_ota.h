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
