#pragma once
/* State machine behind yoke_usbd.h: control relay, endpoint operations,
 * cancellation fences and halt tracking. Pure C: the TinyUSB glue
 * (usbd_tinyusb.c) supplies the port and feeds the inputs below; the host
 * tests supply a simulated port. Everything runs in the USB task. */
#include "usbd_plan.h"

/* Control stages (TinyUSB CONTROL_STAGE_*). */
enum { USBD_STAGE_SETUP = 1, USBD_STAGE_DATA = 2, USBD_STAGE_ACK = 3 };

typedef struct {
    /* Data stage of the current request (IN or OUT), or its status stage. */
    bool (*control_xfer)(void *data, uint16_t len);
    bool (*control_status)(void);
    void (*control_stall)(void);
    /* FIFO for one endpoint address; activate aborts, resets DATA0, NAKs. */
    bool (*fifo_alloc)(uint8_t address, uint16_t max_packet);
    bool (*activate)(const uint8_t *endpoint_descriptor);
    bool (*xfer)(uint8_t address, uint8_t *data, uint16_t len);
    void (*stall)(uint8_t address);
    void (*post)(void (*fn)(void *arg), void *arg);
    yoke_usbd_speed_t (*speed)(void);
    uint32_t (*now_ms)(void);
} usbd_port_t;

void usbd_core_init(const usbd_port_t *port, const usbd_limits_t *limits);
const usbd_limits_t *usbd_core_limits(void);
/* yoke_usbd_present after validation; the glue then connects and posts
 * the CONNECT event. */
void usbd_core_present(const yoke_usbd_descriptors_t *d, const yoke_usbd_sink_t *sink,
                       void *ctx);
const yoke_usbd_descriptors_t *usbd_core_descriptors(void);

/* TinyUSB class driver inputs. */
uint16_t usbd_core_open(const uint8_t *interface_descriptor, uint16_t max_len);
void usbd_core_reset(void);
/* request is usbd.c's own copy of the SETUP (it outlives the call). */
bool usbd_core_control(uint8_t stage, const uint8_t *request);
void usbd_core_xfer_done(uint8_t address, bool ok, uint32_t length);
/* usbd handled SET_CONFIGURATION (mount or SET_CONFIGURATION(0)): the sink
 * receives it as CONTROL_ACKED (no ep0 answer). */
void usbd_core_configured(void);
void usbd_core_bus(yoke_usbd_event_t event);
/* Withdraw: void every operation, then the glue disconnects. */
void usbd_core_withdraw(void);
/* Periodic: expire an unanswered deferred control request and end an armed
 * one the host abandoned. */
void usbd_core_tick(void);
