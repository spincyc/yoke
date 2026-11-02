#pragma once

#include "esp_err.h"

/* Single instance. Call init early; it arms the pending-image health timer.
 * These three boot APIs are called by app_main, never HTTP clients.
 * Failures do not erase trust or application storage. No network required.
 * Caller must enable and retain a true entropy source before init and for
 * the full server lifetime (wired-only reference app reserves SAR ADC using
 * bootloader_random_enable). Do not disable it or use ADC concurrently. */
esp_err_t yoke_ota_init(void);
esp_err_t yoke_ota_start(void);
esp_err_t yoke_ota_confirm_health(void);
void yoke_ota_fail_health(void);
