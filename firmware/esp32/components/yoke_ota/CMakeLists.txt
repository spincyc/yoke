idf_component_register(SRCS "yoke_ota.c" "ota_policy.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES app_update esp_https_server esp_timer nvs_flash mbedtls json)
