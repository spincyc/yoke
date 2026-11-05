#pragma once
/* Descriptor-mirroring rules for the TinyUSB DWC2 device backend. Pure C
 * (no ESP-IDF, no TinyUSB) so tests/firmware_usb runs it on the host.
 *
 * TinyUSB opens a configuration by offering each class driver the next
 * interface; the mirror driver claims the whole remaining configuration in
 * one open() (tu_bind_driver_to_ep_itf binds every interface and endpoint in
 * the span), allocates one FIFO per endpoint address at its largest packet
 * across alternate settings (DWC2 cannot free a FIFO before the next bus
 * reset or SET_CONFIGURATION), and activates alternate setting 0. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "usb_desc.h"
#include "yoke_usbd.h"

/* One DWC2 device controller as TinyUSB 0.21 drives it (dwc2_esp32.h,
 * dcd_dwc2.c dfifo_alloc). */
typedef struct {
    uint8_t ep_count;        /* endpoint numbers 0 .. ep_count - 1 */
    uint8_t ep_in_count;     /* TX FIFOs, EP0's included: IN numbers 1 .. ep_in_count - 1 */
    uint16_t fifo_words;     /* FIFO RAM in 32-bit words after DMA metadata */
    uint8_t max_interfaces;  /* CFG_TUD_INTERFACE_MAX */
    bool high_speed;
} usbd_limits_t;

typedef struct {
    uint8_t address;
    uint16_t max_packet;   /* largest across alternate settings */
} usbd_fifo_t;

/* 0, or -1 with the first refused property in why. */
int usbd_plan_check(const yoke_usbd_descriptors_t *d, const usbd_limits_t *lim,
                    char *why, size_t cap);
/* Distinct endpoint addresses of a (checked) configuration. */
size_t usbd_plan_fifos(const uint8_t *cfg, usbd_fifo_t *out, size_t max);
/* FIFO words the configuration needs (RX FIFO plus every TX FIFO). */
unsigned usbd_plan_fifo_words(const uint8_t *cfg, const usbd_limits_t *lim);
/* What a high-speed port serves for GET_DESCRIPTOR(DEVICE_QUALIFIER) and
 * (OTHER_SPEED_CONFIGURATION): esp_tinyusb answers both from buffers fixed
 * at install, which the backend fills from each presentation. The presented
 * descriptors when they fit in cap bytes; otherwise the device's own fields
 * and an empty configuration, with bNumConfigurations 0 in the qualifier. */
void usbd_plan_other_speed(const yoke_usbd_descriptors_t *d, uint8_t qualifier[10],
                           uint8_t *other, size_t cap);
/* The endpoint descriptor for address in (interface, alternate), or NULL. */
const uint8_t *usbd_plan_endpoint(const uint8_t *cfg, uint8_t interface, uint8_t alternate,
                                  uint8_t address);
const uint8_t *usbd_plan_string(const yoke_usbd_descriptors_t *d, uint8_t index, uint16_t lang);
/* The configuration whose descriptor bytes contain p, or -1. */
int usbd_plan_config_of(const yoke_usbd_descriptors_t *d, const void *p);
