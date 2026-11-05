# usbd_plan.c and usbd_core.c are pure C shared with tests/firmware_usb;
# usbd_tinyusb.c is the TinyUSB glue.
idf_component_register(SRCS "usbd_plan.c" "usbd_core.c" "usbd_tinyusb.c"
    INCLUDE_DIRS "include"
    REQUIRES yoke_usbdesc
    PRIV_REQUIRES esp_timer esp_system heap)
# esp_tinyusb's descriptor callbacks serve one static configuration; TinyUSB
# calls these mirrors instead (usbd_tinyusb.c).
target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=tud_descriptor_device_cb"
    "-Wl,--wrap=tud_descriptor_configuration_cb"
    "-Wl,--wrap=tud_descriptor_string_cb")
