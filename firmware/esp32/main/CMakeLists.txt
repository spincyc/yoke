idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
    PRIV_REQUIRES yoke_ota yoke_ir esp_eth esp_netif esp_event esp_driver_spi
                  esp_driver_gpio nvs_flash bootloader_support esp_timer esp_system
                  esp_app_format)
