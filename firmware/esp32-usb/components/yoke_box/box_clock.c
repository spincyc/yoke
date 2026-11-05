#include "box_clock.h"
#include <stdatomic.h>
#include <sys/time.h>
#include "box_time.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "yoke_ota.h"

#define KEEP_US 1000000 /* refresh the kept reading each second */

static const char *TAG = "yoke-clock";
static atomic_bool synced;
static bool initialized;
/* A synchronized reading that survives software, panic and watchdog
 * resets (box_time.h); a power-on clears it. */
RTC_NOINIT_ATTR static box_time_kept_t kept;
static esp_timer_handle_t keep_timer;

static int64_t now_s(void)
{
    struct timeval tv;
    return gettimeofday(&tv, NULL) ? -1 : (int64_t)tv.tv_sec;
}

static void keep(void *arg)
{
    (void)arg;
    int64_t now = now_s();
    if (atomic_load(&synced) && now >= 0) box_time_keep(&kept, now);
}

static void on_sync(struct timeval *tv)
{
    if (!atomic_exchange(&synced, true))
        ESP_LOGI(TAG, "wall clock set by SNTP: %lld", (long long)tv->tv_sec);
    keep(NULL);
}

static bool reset_kept_rtc(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_SW:
    case ESP_RST_PANIC:
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:
        return true;
    default:
        return false;
    }
}

/* After a software reset the clock restarts from the saved floor; the kept
 * reading is fresher (a watchdog reset can follow the last hourly save). */
static void restore(void)
{
    int64_t unix_s = box_time_restore(&kept, reset_kept_rtc(), now_s());
    if (unix_s < 0) return;
    const struct timeval tv = {.tv_sec = (time_t)unix_s};
    if (settimeofday(&tv, NULL)) return;
    atomic_store(&synced, true);
    box_time_keep(&kept, unix_s);
    ESP_LOGI(TAG, "wall clock kept across the restart: %lld", (long long)unix_s);
}

esp_err_t box_clock_init(void)
{
    restore();
    const esp_timer_create_args_t timer = {.callback = keep, .name = "clock-keep"};
    esp_err_t err = esp_timer_create(&timer, &keep_timer);
    if (err == ESP_OK) err = esp_timer_start_periodic(keep_timer, KEEP_US);
    if (err != ESP_OK) return err;
    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_YOKE_USB_NTP_SERVER);
    config.start = false;                  /* box_clock_online, with an address */
    config.wait_for_sync = false;
    config.sync_cb = on_sync;
    /* DHCP's server takes index 0 (and clears the others); the configured
     * one is restored at index 1 after every lease. */
    config.server_from_dhcp = true;
    config.renew_servers_after_new_IP = true;
    config.index_of_first_server = 1;
    config.ip_event_to_renew = IP_EVENT_ETH_GOT_IP;
    if (!CONFIG_YOKE_USB_NTP_SERVER[0]) config.num_of_servers = 0;
    err = esp_netif_sntp_init(&config);
    initialized = err == ESP_OK;
    return err;
}

void box_clock_online(void)
{
    if (initialized && esp_netif_sntp_start() != ESP_OK) ESP_LOGE(TAG, "SNTP did not start");
}

bool box_clock_synced(void) { return atomic_load(&synced); }

esp_err_t box_clock_set_forward(int64_t unix_s, bool force)
{
    int64_t now = now_s();
    if (now < 0) return ESP_FAIL;
    switch (box_time_step(now, unix_s, force)) {
    case BOX_TIME_BACKWARDS: return ESP_ERR_INVALID_ARG;
    case BOX_TIME_TOO_FAR: return ESP_ERR_INVALID_SIZE;
    case BOX_TIME_OK: break;
    }
    const struct timeval next = {.tv_sec = (time_t)unix_s};
    if (settimeofday(&next, NULL)) return ESP_FAIL;
    atomic_store(&synced, true);
    keep(NULL);
    ESP_LOGW(TAG, "wall clock set by the admin API: %lld%s", (long long)unix_s,
             force ? " (forced)" : "");
    /* The restored floor after a restart starts from here, not the old one. */
    if (yoke_ota_save_clock_floor() != ESP_OK) ESP_LOGW(TAG, "clock floor not saved");
    return ESP_OK;
}
