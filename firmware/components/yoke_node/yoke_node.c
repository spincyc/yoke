#include <inttypes.h>
#include "bootloader_random.h"
#include "esp_app_desc.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "ota_policy.h"
#include "yoke_eth.h"
#include "yoke_node.h"
#include "yoke_ota.h"

/* The W5500 PHY driver polls link every 2 s; no link-up within 10 s of
 * driver start is an observed link-down (no cable on the bench). */
#define LINK_SETTLE_MS 10000
#define HEALTH_GIVE_UP_MS 50000      /* confirmed image: stop waiting */
#define ETH_ATTEMPTS 4               /* 1 + 2 + 4 s backoff inside one boot */
#define CLOCK_SAVE_US (3600LL * 1000000)
#define RESTART_BACKOFF_MAX_S 300
#define RTC_MAGIC 0x59455448u

static const char *TAG = "yoke-node";
/* Survives software restarts: consecutive boots whose network stack failed,
 * so a dead Ethernet controller backs off to one restart per 5 minutes. */
RTC_NOINIT_ATTR static uint32_t rtc_magic, rtc_failed_boots;

static void wait_ms(uint32_t ms)
{
    for (uint32_t waited = 0; waited < ms; waited += 1000) {
        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(ms - waited < 1000 ? ms - waited : 1000));
    }
}

bool yoke_node_begin(const char *name)
{
    ESP_LOGI(TAG, "%s %s", name, esp_app_get_description()->version);
    /* Wired-only operation has no RF entropy source. Reserve SAR ADC for
     * true entropy for initial and subsequent TLS DRBG seeds/reseeds. */
    bootloader_random_enable();
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    if (rtc_magic != RTC_MAGIC || esp_reset_reason() == ESP_RST_POWERON) {
        rtc_magic = RTC_MAGIC;
        rtc_failed_boots = 0;
    }
    /* Never auto-erase NVS on version/full/corruption errors. */
    esp_err_t err = yoke_ota_init();
    if (err == ESP_OK) err = nvs_flash_init();
    if (err == ESP_OK) return true;
    /* Trust or storage failure fails closed: no network services and no
     * restart loop. A pending image rolls back instead. */
    ESP_LOGE(TAG, "Trust/storage failure: %s; inert until USB recovery", esp_err_to_name(err));
    yoke_ota_fail_health();
    ESP_ERROR_CHECK(esp_task_wdt_delete(NULL));
    return false;
}

void yoke_node_restart_later(const char *why)
{
    yoke_ota_fail_health();
    uint32_t failures = rtc_failed_boots < 16 ? ++rtc_failed_boots : rtc_failed_boots;
    uint32_t delay_s = 5u << (failures < 6 ? failures - 1 : 6);
    if (delay_s > RESTART_BACKOFF_MAX_S) delay_s = RESTART_BACKOFF_MAX_S;
    ESP_LOGE(TAG, "%s; restarting in %" PRIu32 " s (consecutive failure %" PRIu32 ")", why,
             delay_s, failures);
    wait_ms(delay_s * 1000u);
    yoke_ota_restart();
}

static bool ethernet_with_retry(void)
{
    for (int attempt = 0; attempt < ETH_ATTEMPTS; ++attempt) {
        esp_err_t err = yoke_eth_start();
        if (err == ESP_OK) return true;
        ESP_LOGE(TAG, "Ethernet start attempt %d failed: %s", attempt + 1, esp_err_to_name(err));
        if (!yoke_eth_stop()) return false;
        if (attempt + 1 < ETH_ATTEMPTS) wait_ms(1000u << attempt);
    }
    return false;
}

void yoke_node_serve(const char *ip_note, void (*restart_guard)(void),
                     void (*status_hook)(cJSON *body), void (*before_ethernet)(void))
{
    /* Hooks first: every restart below already finishes physical output. */
    yoke_ota_set_restart_guard(restart_guard);
    yoke_ota_set_status_hook(status_hook);
    if (yoke_eth_init(ip_note) != ESP_OK) yoke_node_restart_later("network stack initialisation failed");
    if (before_ethernet) before_ethernet();
    if (!ethernet_with_retry()) yoke_node_restart_later("Ethernet did not start");
    if (yoke_ota_start() != ESP_OK) yoke_node_restart_later("HTTPS update server did not start");
    rtc_failed_boots = 0;
}

void yoke_node_confirm(bool app_ok, bool (*ready)(void))
{
    int64_t started = esp_timer_get_time();
    for (;;) {
        esp_task_wdt_reset();
        yoke_eth_state_t eth = yoke_eth_state();
        yoke_health_inputs_t in = {
            .local_ok = true, /* trust, storage, Ethernet and HTTPS are up to reach here */
            .app_ok = app_ok,
            .listeners_up = app_ok && ready(),
            .ip_acquired = eth.ip_acquired,
            .link_up = eth.link_up,
            .link_down_seen = eth.link_down_seen,
            .ms_since_eth_start = eth.ms_since_start,
            .link_settle_ms = LINK_SETTLE_MS,
        };
        switch (yoke_ota_health_decide(&in)) {
        case YOKE_HEALTH_CONFIRM:
            if (yoke_ota_confirm_health() != ESP_OK) yoke_ota_fail_health();
            ESP_LOGI(TAG, "Boot health confirmed (%s)",
                     in.ip_acquired ? "IP acquired" : "link observed down");
            return;
        case YOKE_HEALTH_FAIL:
            ESP_LOGE(TAG, "Local health failed; application unavailable");
            yoke_ota_fail_health(); /* pending image: rollback, no return */
            return;
        case YOKE_HEALTH_WAIT:
            break;
        }
        /* A pending image keeps waiting until confirmed or the 60 s
         * rollback deadline fires; a confirmed image only reports state. */
        if (!yoke_ota_pending_verify() &&
            esp_timer_get_time() - started > HEALTH_GIVE_UP_MS * 1000LL) return;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void yoke_node_run(void)
{
    int64_t next_save = esp_timer_get_time() + CLOCK_SAVE_US;
    for (;;) {
        esp_task_wdt_reset();
        if (esp_timer_get_time() >= next_save) {
            /* The documented clock floor advances with uptime. */
            if (yoke_ota_save_clock_floor() != ESP_OK) ESP_LOGE(TAG, "clock floor save failed");
            next_save += CLOCK_SAVE_US;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
