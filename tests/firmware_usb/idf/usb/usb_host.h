#pragma once
/* The ESP-IDF 5.5 USB Host Library surface usbh_esp.c uses; host_usbh.c
 * models its behaviour (transfers in flight until their callbacks return,
 * interface release refused meanwhile, flushes completing as cancelled). */
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"

#define ESP_INTR_FLAG_LEVEL1 (1 << 1)
#define USB_TRANSFER_FLAG_ZERO_PACK 0x01

typedef struct stub_usb_device *usb_device_handle_t;
typedef struct stub_usb_client *usb_host_client_handle_t;
typedef struct usb_device_desc usb_device_desc_t;    /* bytes, as on the wire */
typedef struct usb_config_desc usb_config_desc_t;

typedef enum { USB_SPEED_LOW, USB_SPEED_FULL, USB_SPEED_HIGH } usb_speed_t;
typedef struct {
    usb_speed_t speed;
} usb_device_info_t;

typedef enum {
    USB_TRANSFER_STATUS_COMPLETED,
    USB_TRANSFER_STATUS_ERROR,
    USB_TRANSFER_STATUS_TIMED_OUT,
    USB_TRANSFER_STATUS_CANCELED,
    USB_TRANSFER_STATUS_STALL,
    USB_TRANSFER_STATUS_OVERFLOW,
    USB_TRANSFER_STATUS_SKIPPED,
    USB_TRANSFER_STATUS_NO_DEVICE,
} usb_transfer_status_t;

typedef struct usb_transfer_s usb_transfer_t;
struct usb_transfer_s {
    uint8_t *data_buffer;
    size_t data_buffer_size;
    int num_bytes;
    int actual_num_bytes;
    uint32_t flags;
    usb_device_handle_t device_handle;
    uint8_t bEndpointAddress;
    usb_transfer_status_t status;
    void (*callback)(usb_transfer_t *transfer);
    void *context;
};

typedef enum { USB_HOST_CLIENT_EVENT_NEW_DEV, USB_HOST_CLIENT_EVENT_DEV_GONE } usb_host_client_event_t;
typedef struct {
    usb_host_client_event_t event;
    union {
        struct {
            uint8_t address;
        } new_dev;
        struct {
            usb_device_handle_t dev_hdl;
        } dev_gone;
    };
} usb_host_client_event_msg_t;

typedef struct {
    int intr_flags;
} usb_host_config_t;
typedef struct {
    int max_num_event_msg;
    struct {
        void (*client_event_callback)(const usb_host_client_event_msg_t *msg, void *arg);
        void *callback_arg;
    } async;
} usb_host_client_config_t;

esp_err_t usb_host_install(const usb_host_config_t *config);
esp_err_t usb_host_lib_handle_events(TickType_t timeout, uint32_t *flags);
esp_err_t usb_host_client_register(const usb_host_client_config_t *config,
                                   usb_host_client_handle_t *client);
esp_err_t usb_host_client_handle_events(usb_host_client_handle_t client, TickType_t timeout);
esp_err_t usb_host_client_unblock(usb_host_client_handle_t client);
esp_err_t usb_host_device_open(usb_host_client_handle_t client, uint8_t address,
                               usb_device_handle_t *dev);
esp_err_t usb_host_device_close(usb_host_client_handle_t client, usb_device_handle_t dev);
esp_err_t usb_host_device_info(usb_device_handle_t dev, usb_device_info_t *info);
esp_err_t usb_host_get_device_descriptor(usb_device_handle_t dev, const usb_device_desc_t **desc);
esp_err_t usb_host_get_active_config_descriptor(usb_device_handle_t dev,
                                                const usb_config_desc_t **desc);
esp_err_t usb_host_interface_claim(usb_host_client_handle_t client, usb_device_handle_t dev,
                                   uint8_t interface, uint8_t alternate);
esp_err_t usb_host_interface_release(usb_host_client_handle_t client, usb_device_handle_t dev,
                                     uint8_t interface);
esp_err_t usb_host_endpoint_halt(usb_device_handle_t dev, uint8_t endpoint);
esp_err_t usb_host_endpoint_flush(usb_device_handle_t dev, uint8_t endpoint);
esp_err_t usb_host_endpoint_clear(usb_device_handle_t dev, uint8_t endpoint);
esp_err_t usb_host_transfer_alloc(size_t size, int isoc_packets, usb_transfer_t **transfer);
esp_err_t usb_host_transfer_submit(usb_transfer_t *transfer);
esp_err_t usb_host_transfer_submit_control(usb_host_client_handle_t client,
                                           usb_transfer_t *transfer);
