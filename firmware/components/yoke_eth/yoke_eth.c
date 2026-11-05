#include <string.h>
#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "yoke_eth.h"
#if CONFIG_YOKE_ETH_W5500
#include "driver/spi_master.h"
#include "esp_eth_mac_spi.h"
#endif

#define LINK_UP BIT0
#define LINK_DOWN_SEEN BIT1
#define GOT_IP BIT2

static const char *TAG = "yoke-eth";
static EventGroupHandle_t net;
static const char *note;
static int64_t started_us;

static struct {
    esp_netif_t *netif;
    bool bus;
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
        ESP_LOGI(TAG, "Ethernet IPv4 " IPSTR "; %s", IP2STR(&event->ip_info.ip), note);
    } else if (id == IP_EVENT_ETH_LOST_IP) {
        xEventGroupClearBits(net, GOT_IP);
    }
}

esp_err_t yoke_eth_init(const char *ip_note)
{
    note = ip_note;
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

bool yoke_eth_stop(void)
{
    if (eth.eth) (void)esp_eth_stop(eth.eth); /* also after a failed start */
    if (eth.glue) esp_eth_del_netif_glue(eth.glue);
    eth.glue = NULL;
    if (eth.eth && esp_eth_driver_uninstall(eth.eth) != ESP_OK) {
        ESP_LOGE(TAG, "Ethernet driver could not be uninstalled; not retrying in place");
        return false;
    }
    eth.eth = NULL;
#if CONFIG_YOKE_ETH_W5500
    /* A driver install that failed after mac->init leaves the W5500 INT
     * handler registered with the MAC as its argument. */
    if (CONFIG_YOKE_ETH_INT >= 0) (void)gpio_isr_handler_remove(CONFIG_YOKE_ETH_INT);
#endif
    if (eth.phy) eth.phy->del(eth.phy);
    if (eth.mac) eth.mac->del(eth.mac);
#if CONFIG_YOKE_ETH_W5500
    if (eth.bus) spi_bus_free(SPI2_HOST);
#endif
    if (eth.netif) esp_netif_destroy(eth.netif);
    memset(&eth, 0, sizeof(eth));
    return true;
}

static esp_err_t create_mac_phy(void)
{
    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.reset_gpio_num = CONFIG_YOKE_ETH_RESET;
#if CONFIG_YOKE_ETH_W5500
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
    phy_cfg.phy_addr = 1;
    eth.mac = esp_eth_mac_new_w5500(&w5500, &mac_cfg);
    eth.phy = esp_eth_phy_new_w5500(&phy_cfg);
#elif CONFIG_YOKE_ETH_EMAC
    eth_esp32_emac_config_t emac = ETH_ESP32_EMAC_DEFAULT_CONFIG();
    emac.smi_gpio.mdc_num = CONFIG_YOKE_ETH_MDC;
    emac.smi_gpio.mdio_num = CONFIG_YOKE_ETH_MDIO;
    phy_cfg.phy_addr = CONFIG_YOKE_ETH_PHY_ADDR;
    eth.mac = esp_eth_mac_new_esp32(&emac, &mac_cfg);
    eth.phy = esp_eth_phy_new_ip101(&phy_cfg);
#else
#error "Select a Yoke Ethernet controller (menuconfig: Yoke Ethernet board profile)"
#endif
    return eth.mac && eth.phy ? ESP_OK : ESP_ERR_NO_MEM;
}

esp_err_t yoke_eth_start(void)
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
    ESP_RETURN_ON_ERROR(create_mac_phy(), TAG, "MAC/PHY");
    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(eth.mac, eth.phy);
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&eth_cfg, &eth.eth), TAG, "driver install");
    uint8_t address[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(address, ESP_MAC_ETH), TAG, "device MAC");
    ESP_RETURN_ON_ERROR(esp_eth_ioctl(eth.eth, ETH_CMD_S_MAC_ADDR, address), TAG, "Ethernet MAC");
    eth.glue = esp_eth_new_netif_glue(eth.eth);
    if (!eth.glue) return ESP_ERR_NO_MEM;
    ESP_RETURN_ON_ERROR(esp_netif_attach(eth.netif, eth.glue), TAG, "attach Ethernet");
    xEventGroupClearBits(net, LINK_UP | LINK_DOWN_SEEN | GOT_IP);
    started_us = esp_timer_get_time();
    return esp_eth_start(eth.eth);
}

yoke_eth_state_t yoke_eth_state(void)
{
    EventBits_t bits = xEventGroupGetBits(net);
    return (yoke_eth_state_t){
        .link_up = bits & LINK_UP,
        .link_down_seen = bits & LINK_DOWN_SEEN,
        .ip_acquired = bits & GOT_IP,
        .ms_since_start = (uint32_t)((esp_timer_get_time() - started_us) / 1000),
    };
}
