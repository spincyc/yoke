idf_component_register(SRCS "yoke_ota.c" "ota_policy.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_https_server json
    PRIV_REQUIRES app_update esp_timer esp_system nvs_flash mbedtls espcoredump heap)
