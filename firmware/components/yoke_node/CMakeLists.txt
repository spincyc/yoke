idf_component_register(SRCS "yoke_node.c"
    INCLUDE_DIRS "include"
    REQUIRES json
    PRIV_REQUIRES yoke_ota yoke_eth bootloader_support esp_app_format esp_system esp_timer
                  nvs_flash)
