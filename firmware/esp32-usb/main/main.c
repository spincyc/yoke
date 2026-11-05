#include "esp_log.h"
#include "yoke_box.h"
#include "yoke_node.h"
#include "yoke_ota.h"

void app_main(void)
{
    if (!yoke_node_begin("Yoke USB box")) return;
    /* USB first: its self-test runs even while Ethernet is missing. */
    bool usb_ok = yoke_box_start() == ESP_OK;
    yoke_node_serve("mTLS HTTPS 443", NULL, yoke_box_status, yoke_box_network);
    if (yoke_box_online(yoke_ota_server()) != ESP_OK)
        ESP_LOGE("yoke-usb-box", "USB admin endpoint or pairing unavailable");
    yoke_node_confirm(usb_ok, yoke_box_ready);
    yoke_node_run();
}
