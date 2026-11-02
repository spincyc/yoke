idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
    PRIV_REQUIRES yoke_ota esp_eth esp_netif esp_event esp_driver_spi
                  esp_driver_gpio nvs_flash bootloader_support)
