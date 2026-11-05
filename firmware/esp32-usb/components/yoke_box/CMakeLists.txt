# Pure C shared with tests/firmware_usb: box_config.c, box_limit.c,
# box_media.c, box_pump.c (POSIX sockets), box_time.c, box_gadget.c,
# box_host.c and the self-tests.
# Target glue: box.c (storage, admin, status), box_clock.c (SNTP),
# box_net.c and box_ywp.c (the network task and YWP/1), box_computer.c and
# box_device.c (role bridges).
idf_component_register(
    SRCS "box.c" "box_clock.c" "box_config.c" "box_limit.c" "box_media.c" "box_pump.c"
         "box_time.c" "box_gadget.c" "box_host.c" "box_net.c" "box_ywp.c" "box_computer.c"
         "box_device.c" "selftest_cdc.c" "selftest_host.c"
    INCLUDE_DIRS "include"
    REQUIRES json esp_http_server
    PRIV_REQUIRES yusb ywp yoke_ota yoke_usbd yoke_usbh yoke_usbdesc esp_timer esp_event
                  esp_netif esp_hw_support lwip mbedtls nvs_flash vfs)
