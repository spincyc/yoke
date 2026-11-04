#pragma once
/* IR endpoint service: RMT outputs, boot self-test, and the iTach-subset
 * listener task on TCP 4998 (separate from the mutual-TLS OTA server task).
 * See doc/design/IR-ENDPOINT.md. */
#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#include "esp_err.h"
#include "esp_http_server.h"

#define YOKE_IR_PORT 4998

/* Call after nvs_flash_init(). Loads settings, creates RMT channels and
 * runs the self-test. ESP_OK only when the self-test passed. */
esp_err_t yoke_ir_init(void);
/* Start the listener task (it listens only when the API is enabled). */
esp_err_t yoke_ir_start(void);
/* Listener is up, or the API is disabled so nothing has to listen. */
bool yoke_ir_ready(void);
/* Before any restart: finish frames in flight, refuse new sendir, wait for
 * idle (bounded), then force outputs idle. Safe from any task. */
void yoke_ir_quiesce(uint32_t max_wait_ms);
/* Adds an "ir" object to the authenticated status document. */
void yoke_ir_status_json(cJSON *body);
/* PUT /v1/ir/config on the mutual-TLS server (admin authority). */
esp_err_t yoke_ir_register_admin(httpd_handle_t server);
