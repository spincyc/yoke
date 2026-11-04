#include <string.h>
#include "bootloader_random.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_app_desc.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_eth.h"
#include "esp_eth_mac_spi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "ota_policy.h"
#include "yoke_ir.h"
#include "yoke_ota.h"

static const char *TAG = "yoke-ir-endpoint";

#define LINK_UP BIT0
#define LINK_DOWN_SEEN BIT1
#define GOT_IP BIT2
/* The W5500 PHY driver polls link every 2 s; no link-up within 10 s of
 * driver start is an observed link-down (no cable on the bench). */
#define LINK_SETTLE_MS 10000
#define HEALTH_GIVE_UP_MS 50000      /* confirmed image: stop waiting */
#define ETH_ATTEMPTS 4               /* 1 + 2 + 4 s backoff inside one boot */
#define CLOCK_SAVE_US (3600LL * 1000000)
#define RESTART_BACKOFF_MAX_S 300
#define RTC_MAGIC 0x59455448u

static EventGroupHandle_t net;
static int64_t eth_started_us;
/* Survives software restarts: consecutive boots whose network stack failed,
 * so a dead W5500 backs off to one restart per 5 minutes. */
RTC_NOINIT_ATTR static uint32_t rtc_magic, rtc_failed_boots;

static struct {
    esp_netif_t *netif;
    bool bus, started;
    esp_eth_mac_t *mac;
    esp_eth_phy_t *phy;
    esp_eth_handle_t eth;
    esp_eth_netif_glue_handle_t glue;
} eth;

static void on_eth(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base; (void)data;
    if (id == ETHERNET_EVENT_CONNECTED) {
        xEventGroupClearBits(net, LINK_DOWN_SEEN);
        xEventGroupSetBits(net, LINK_UP);
        ESP_LOGI(TAG, "Ethernet link up");
    } else if (id == ETHERNET_EVENT_DISCONNECTED) {
        xEventGroupClearBits(net, LINK_UP | GOT_IP);
        xEventGroupSetBits(net, LINK_DOWN_SEEN);
        ESP_LOGW(TAG, "Ethernet link down");
    }
}

static void on_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base;
    if (id == IP_EVENT_ETH_GOT_IP) {
        ip_event_got_ip_t *event = data;
        xEventGroupSetBits(net, GOT_IP);
        ESP_LOGI(TAG, "Ethernet IPv4 " IPSTR "; mTLS HTTPS 443; IR API TCP %d if enabled",
                 IP2STR(&event->ip_info.ip), YOKE_IR_PORT);
    } else if (id == IP_EVENT_ETH_LOST_IP) {
        xEventGroupClearBits(net, GOT_IP);
    }
}

static esp_err_t network_base(void)
{
    net = xEventGroupCreate();
    if (!net) return ESP_ERR_NO_MEM;
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_eth, NULL),
                        TAG, "Ethernet events");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, on_ip, NULL),
                        TAG, "IP events");
    return gpio_install_isr_service(0);
}

/* Undo whatever ethernet_start completed, in reverse order. Returns false
 * when the driver could not be uninstalled: its MAC/PHY may still be live
 * (a failed esp_eth_start leaves the driver in START), so nothing it uses
 * is freed and the caller restarts instead of retrying. */
static bool ethernet_stop(void)
{
    if (eth.eth) (void)esp_eth_stop(eth.eth); /* also after a failed start */
    if (eth.glue) esp_eth_del_netif_glue(eth.glue);
    eth.glue = NULL;
    if (eth.eth && esp_eth_driver_uninstall(eth.eth) != ESP_OK) {
        ESP_LOGE(TAG, "W5500 driver could not be uninstalled; not retrying in place");
        return false;
    }
    eth.eth = NULL;
    /* A driver install that failed after mac->init leaves the W5500 INT
     * handler registered with the MAC as its argument. */
    if (CONFIG_YOKE_ETH_INT >= 0) (void)gpio_isr_handler_remove(CONFIG_YOKE_ETH_INT);
    if (eth.phy) eth.phy->del(eth.phy);
    if (eth.mac) eth.mac->del(eth.mac);
    if (eth.bus) spi_bus_free(SPI2_HOST);
    if (eth.netif) esp_netif_destroy(eth.netif);
    memset(&eth, 0, sizeof(eth));
    return true;
}

static esp_err_t ethernet_start(void)
{
    esp_netif_config_t cfg = ESP_NETIF_DEFAULT_ETH();
    eth.netif = esp_netif_new(&cfg);
    if (!eth.netif) return ESP_ERR_NO_MEM;
#if CONFIG_YOKE_STATIC_IPV4
    esp_netif_ip_info_t ip = {0};
    if (esp_netif_str_to_ip4(CONFIG_YOKE_IP, &ip.ip) != ESP_OK ||
        esp_netif_str_to_ip4(CONFIG_YOKE_MASK, &ip.netmask) != ESP_OK ||
        esp_netif_str_to_ip4(CONFIG_YOKE_GATEWAY, &ip.gw) != ESP_OK) return ESP_ERR_INVALID_ARG;
    ESP_RETURN_ON_ERROR(esp_netif_dhcpc_stop(eth.netif), TAG, "stop DHCP");
    ESP_RETURN_ON_ERROR(esp_netif_set_ip_info(eth.netif, &ip), TAG, "static IP");
#endif
    spi_bus_config_t bus = {
        .mosi_io_num = CONFIG_YOKE_ETH_MOSI,
        .miso_io_num = CONFIG_YOKE_ETH_MISO,
        .sclk_io_num = CONFIG_YOKE_ETH_SCLK,
        .quadwp_io_num = -1, .quadhd_io_num = -1,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "SPI");
    eth.bus = true;
    spi_device_interface_config_t spi = {
        .mode = 0, .clock_speed_hz = CONFIG_YOKE_ETH_SPI_MHZ * 1000000,
        .spics_io_num = CONFIG_YOKE_ETH_CS, .queue_size = 20,
    };
    eth_w5500_config_t w5500 = ETH_W5500_DEFAULT_CONFIG(SPI2_HOST, &spi);
    w5500.int_gpio_num = CONFIG_YOKE_ETH_INT;
    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.phy_addr = 1;
    phy_cfg.reset_gpio_num = CONFIG_YOKE_ETH_RESET;
    eth.mac = esp_eth_mac_new_w5500(&w5500, &mac_cfg);
    eth.phy = esp_eth_phy_new_w5500(&phy_cfg);
    if (!eth.mac || !eth.phy) return ESP_ERR_NO_MEM;
    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(eth.mac, eth.phy);
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&eth_cfg, &eth.eth), TAG, "W5500 install");
    uint8_t address[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(address, ESP_MAC_ETH), TAG, "device MAC");
    ESP_RETURN_ON_ERROR(esp_eth_ioctl(eth.eth, ETH_CMD_S_MAC_ADDR, address), TAG, "Ethernet MAC");
    eth.glue = esp_eth_new_netif_glue(eth.eth);
    if (!eth.glue) return ESP_ERR_NO_MEM;
    ESP_RETURN_ON_ERROR(esp_netif_attach(eth.netif, eth.glue), TAG, "attach Ethernet");
    xEventGroupClearBits(net, LINK_UP | LINK_DOWN_SEEN | GOT_IP);
    eth_started_us = esp_timer_get_time();
    ESP_RETURN_ON_ERROR(esp_eth_start(eth.eth), TAG, "Ethernet start");
    eth.started = true;
    return ESP_OK;
}

static void wait_ms(uint32_t ms)
{
    for (uint32_t waited = 0; waited < ms; waited += 1000) {
        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(ms - waited < 1000 ? ms - waited : 1000));
    }
}

/* Never reboot in the middle of an IR frame. */
static void ir_guard(void)
{
    yoke_ir_quiesce(CONFIG_YOKE_IR_MAX_FRAME_MS + 500);
}

/* Network-plane failure of a confirmed image: back off across restarts.
 * A pending image rolls back instead (yoke_ota_fail_health never returns). */
static void restart_later(const char *why)
{
    yoke_ota_fail_health();
    uint32_t failures = rtc_failed_boots < 16 ? ++rtc_failed_boots : rtc_failed_boots;
    uint32_t delay_s = 5u << (failures < 6 ? failures - 1 : 6);
    if (delay_s > RESTART_BACKOFF_MAX_S) delay_s = RESTART_BACKOFF_MAX_S;
    ESP_LOGE(TAG, "%s; restarting in %" PRIu32 " s (consecutive failure %" PRIu32 ")", why,
             delay_s, failures);
    wait_ms(delay_s * 1000u);
    ir_guard();
    (void)yoke_ota_save_clock_floor();
    esp_restart();
}

static bool ethernet_with_retry(void)
{
    for (int attempt = 0; attempt < ETH_ATTEMPTS; ++attempt) {
        esp_err_t err = ethernet_start();
        if (err == ESP_OK) return true;
        ESP_LOGE(TAG, "Ethernet start attempt %d failed: %s", attempt + 1, esp_err_to_name(err));
        if (!ethernet_stop()) return false;
        if (attempt + 1 < ETH_ATTEMPTS) wait_ms(1000u << attempt);
    }
    return false;
}

static void confirm_when_healthy(bool ir_ok)
{
    int64_t started = esp_timer_get_time();
    for (;;) {
        esp_task_wdt_reset();
        EventBits_t bits = xEventGroupGetBits(net);
        int64_t now = esp_timer_get_time();
        yoke_health_inputs_t in = {
            .local_ok = true, /* trust, storage, W5500 and HTTPS are up to reach here */
            .ir_ok = ir_ok,
            .listeners_up = ir_ok && yoke_ir_ready(),
            .ip_acquired = bits & GOT_IP,
            .link_up = bits & LINK_UP,
            .link_down_seen = bits & LINK_DOWN_SEEN,
            .ms_since_eth_start = (uint32_t)((now - eth_started_us) / 1000),
            .link_settle_ms = LINK_SETTLE_MS,
        };
        switch (yoke_ota_health_decide(&in)) {
        case YOKE_HEALTH_CONFIRM:
            if (yoke_ota_confirm_health() != ESP_OK) yoke_ota_fail_health();
            ESP_LOGI(TAG, "Boot health confirmed (%s)",
                     in.ip_acquired ? "IP acquired" : "link observed down");
            return;
        case YOKE_HEALTH_FAIL:
            ESP_LOGE(TAG, "Local health failed; IR unavailable");
            yoke_ota_fail_health(); /* pending image: rollback, no return */
            return;
        case YOKE_HEALTH_WAIT:
            break;
        }
        /* A pending image keeps waiting until confirmed or the 60 s
         * rollback deadline fires; a confirmed image only reports state. */
        if (!yoke_ota_pending_verify() && now - started > HEALTH_GIVE_UP_MS * 1000LL) return;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "Yoke IR endpoint %s", esp_app_get_description()->version);
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
    if (err != ESP_OK) {
        /* Trust or storage failure fails closed: no network services and
         * no restart loop. A pending image rolls back instead. */
        ESP_LOGE(TAG, "Trust/storage failure: %s; inert until USB recovery",
                 esp_err_to_name(err));
        yoke_ota_fail_health();
        ESP_ERROR_CHECK(esp_task_wdt_delete(NULL));
        return;
    }
    bool ir_ok = yoke_ir_init() == ESP_OK;
    if (!ir_ok) ESP_LOGE(TAG, "IR outputs or self-test failed");
    if (network_base() != ESP_OK) restart_later("network stack initialisation failed");
    if (!ethernet_with_retry()) restart_later("W5500 Ethernet did not start");
    /* Hooks before the server can accept an upload. */
    yoke_ota_set_restart_guard(ir_guard);
    yoke_ota_set_status_hook(yoke_ir_status_json);
    if (yoke_ota_start() != ESP_OK) restart_later("HTTPS update server did not start");
    if (yoke_ir_register_admin(yoke_ota_server()) != ESP_OK)
        ESP_LOGE(TAG, "IR admin endpoint unavailable");
    if (ir_ok && yoke_ir_start() != ESP_OK) {
        ESP_LOGE(TAG, "IR listener task did not start");
        ir_ok = false;
    }
    rtc_failed_boots = 0;
    confirm_when_healthy(ir_ok);
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
