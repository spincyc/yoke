#pragma once
/* Host stand-ins for the ESP-IDF APIs the USB box's target glue uses, so
 * tests/firmware_usb runs that glue unchanged (idf_stubs.c and the host
 * programs implement them). Values follow ESP-IDF where they matter. */
#include <stdlib.h>

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NOT_FOUND 0x105
#define ESP_ERR_NVS_NOT_FOUND 0x1102
#define ESP_ERROR_CHECK(x) do { if ((x) != ESP_OK) abort(); } while (0)

const char *esp_err_to_name(esp_err_t err);
