# ir_core.c, ir_program.c and ir_encode.c are pure C shared with the host
# simulation (tests/firmware_ir); ir_rmt.c and ir_service.c are target glue.
idf_component_register(
    SRCS "ir_core.c" "ir_program.c" "ir_encode.c" "ir_rmt.c" "ir_service.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_http_server json
    PRIV_REQUIRES esp_driver_rmt esp_driver_gpio esp_timer esp_netif lwip nvs_flash
                  esp_system freertos esp_app_format
    LDFRAGMENTS "linker.lf")
# Park IR pins before the panic handler runs (ir_rmt.c).
target_link_options(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=esp_panic_handler")
