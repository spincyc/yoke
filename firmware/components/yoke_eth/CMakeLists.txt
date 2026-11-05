idf_component_register(SRCS "yoke_eth.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES esp_eth esp_netif esp_event esp_driver_spi esp_driver_gpio esp_timer)
