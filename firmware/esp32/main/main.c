#include <string.h>
#include "bootloader_random.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_eth.h"
#include "esp_eth_mac_spi.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "yoke_ota.h"

static const char *TAG = "yoke-reference";

static void got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg; (void)base; (void)id;
    ip_event_got_ip_t *event = data;
    ESP_LOGI(TAG, "Ethernet IPv4 " IPSTR "; mTLS HTTPS port 443",
             IP2STR(&event->ip_info.ip));
}

static esp_err_t ethernet_start(void)
{
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");
    esp_netif_config_t cfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&cfg);
    if (!netif) return ESP_ERR_NO_MEM;
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP,
                                                  got_ip, NULL), TAG, "IP event");
#if CONFIG_YOKE_STATIC_IPV4
    esp_netif_ip_info_t ip = {0};
    if (esp_netif_str_to_ip4(CONFIG_YOKE_IP, &ip.ip) != ESP_OK ||
        esp_netif_str_to_ip4(CONFIG_YOKE_MASK, &ip.netmask) != ESP_OK ||
        esp_netif_str_to_ip4(CONFIG_YOKE_GATEWAY, &ip.gw) != ESP_OK) return ESP_ERR_INVALID_ARG;
    ESP_RETURN_ON_ERROR(esp_netif_dhcpc_stop(netif), TAG, "stop DHCP");
    ESP_RETURN_ON_ERROR(esp_netif_set_ip_info(netif, &ip), TAG, "static IP");
#endif
    ESP_RETURN_ON_ERROR(gpio_install_isr_service(0), TAG, "GPIO ISR");
    spi_bus_config_t bus = {
        .mosi_io_num = CONFIG_YOKE_ETH_MOSI,
        .miso_io_num = CONFIG_YOKE_ETH_MISO,
        .sclk_io_num = CONFIG_YOKE_ETH_SCLK,
        .quadwp_io_num = -1, .quadhd_io_num = -1,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "SPI");
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
    esp_eth_mac_t *mac = esp_eth_mac_new_w5500(&w5500, &mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_w5500(&phy_cfg);
    if (!mac || !phy) return ESP_ERR_NO_MEM;
    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = NULL;
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&eth_cfg, &eth), TAG, "W5500 install");
    uint8_t address[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(address, ESP_MAC_ETH), TAG, "device MAC");
    ESP_RETURN_ON_ERROR(esp_eth_ioctl(eth, ETH_CMD_S_MAC_ADDR, address), TAG, "Ethernet MAC");
    esp_eth_netif_glue_handle_t glue = esp_eth_new_netif_glue(eth);
    if (!glue) return ESP_ERR_NO_MEM;
    ESP_RETURN_ON_ERROR(esp_netif_attach(netif, glue), TAG, "attach Ethernet");
    return esp_eth_start(eth);
}

void app_main(void)
{
    ESP_LOGW(TAG, "Updater reference only: IR routing is NOT IMPLEMENTED");
    /* Wired-only operation has no RF entropy source. Reserve SAR ADC for
     * true entropy for initial and subsequent TLS DRBG seeds/reseeds. */
    bootloader_random_enable();
    /* Never auto-erase NVS on version/full/corruption errors. */
    esp_err_t err = yoke_ota_init();
    if (err == ESP_OK) err = nvs_flash_init();
    if (err == ESP_OK) err = ethernet_start();
    if (err == ESP_OK) err = yoke_ota_start();
    if (err == ESP_OK) err = yoke_ota_confirm_health();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Boot health failed: %s; USB recovery may be needed", esp_err_to_name(err));
        yoke_ota_fail_health();
    }
}
