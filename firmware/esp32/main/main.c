#include "esp_log.h"
#include "yoke_ir.h"
#include "yoke_node.h"
#include "yoke_ota.h"

#define IP_NOTE "mTLS HTTPS 443; IR API TCP 4998 if enabled"
_Static_assert(YOKE_IR_PORT == 4998, "IP_NOTE names the IR port");

static const char *TAG = "yoke-ir-endpoint";

/* Never reboot in the middle of an IR frame. */
static void ir_guard(void)
{
    yoke_ir_quiesce(CONFIG_YOKE_IR_MAX_FRAME_MS + 500);
}

void app_main(void)
{
    if (!yoke_node_begin("Yoke IR endpoint")) return;
    bool ir_ok = yoke_ir_init() == ESP_OK;
    if (!ir_ok) ESP_LOGE(TAG, "IR outputs or self-test failed");
    yoke_node_serve(IP_NOTE, ir_guard, yoke_ir_status_json, NULL);
    if (yoke_ir_register_admin(yoke_ota_server()) != ESP_OK)
        ESP_LOGE(TAG, "IR admin endpoint unavailable");
    if (ir_ok && yoke_ir_start() != ESP_OK) {
        ESP_LOGE(TAG, "IR listener task did not start");
        ir_ok = false;
    }
    yoke_node_confirm(ir_ok, yoke_ir_ready);
    yoke_node_run();
}
