idf_component_register(SRCS "usbh_esp.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES usb yoke_usbdesc esp_system freertos)
