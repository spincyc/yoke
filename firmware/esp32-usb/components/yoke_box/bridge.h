#pragma once
/* The USB/IP bridge of a configured box (doc/design/USB-TIER1.md, "Bridge"):
 *
 *  - the network task (box_net.c) runs the YWP/1 node (box_ywp.c) and
 *    reports socket readiness; it never touches a USB/IP core;
 *  - the role's USB task runs the USB/IP core with its USB backend:
 *    the presentation core on TinyUSB's task (box_computer.c), or the
 *    export core on the USB host task (box_device.c).
 *
 * The network task drives the role through box_bridge_t (each entry copies
 * its arguments and posts the work to the USB task); the USB task answers
 * with the box_net_* calls below, which are safe from any task. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "box_config.h"
#include "cJSON.h"
#include "esp_err.h"
#include "ywp_types.h"

#define BOX_DETAIL_MAX YWP_DETAIL_MAX
/* Internal RAM a USB/IP session must leave free for TLS handshakes, lwIP
 * and the Ethernet driver (⚠ estimate; GET /v1/status reports the
 * internal heap's free and minimum free bytes). */
#define BOX_HEAP_RESERVE (32u * 1024u)

typedef struct {
    /* Install the backend at boot; the USB task exists afterwards. No
     * sockets yet: lwIP may not be initialised. */
    esp_err_t (*start)(void);
    /* The box has its first address (lwIP is up): open listening sockets.
     * NULL: the role listens on nothing. */
    void (*online)(void);
    /* ATTACH (detach false) or DETACH of route `generation`: answer with
     * box_net_route_done once the driver reached its new state. */
    void (*route)(uint32_t generation, bool detach, const char *producer, const char *media,
                  size_t media_len);
    /* Report the driver state with box_net_poll_done(tag, ...). */
    void (*poll)(uint32_t tag);
    /* Producer data-plane admission: the complete set (ADR 0024). */
    void (*admission)(const uint32_t *ipv4, size_t count);
    /* Adds role fields to the status "usb" object (any task; evidence). */
    void (*status)(cJSON *usb);
} box_bridge_t;

extern const box_bridge_t box_computer;
extern const box_bridge_t box_device;

/* Any task: whether a session of `need` bytes (a native/yusb footprint)
 * may be created. Conservative: as if all of it came from internal RAM in
 * one block (with PSRAM, malloc puts the large pieces there), leaving
 * BOX_HEAP_RESERVE. The cores also fail cleanly on a refused allocation. */
bool box_heap_fits(size_t need);

/* Before the role starts: watches may be armed from then on. */
esp_err_t box_net_init(void);
/* With the box's first address: the network task and the YWP/1 node. */
esp_err_t box_net_start(const box_config_t *config, const box_bridge_t *role, uint32_t ipv4);
void box_net_status(cJSON *body);
/* Any task: the box acquired a (new) IPv4 address. */
void box_net_address(uint32_t ipv4);

/* USB task -> network task. */
void box_net_route_done(bool completed, ywp_state_t state, const char *detail);
void box_net_poll_done(uint32_t tag, ywp_state_t state, const char *detail);
/* Socket readiness: once fd is readable (with read) or writable (with
 * write), post ready(arg) to the role's USB task through post, then disarm.
 * Neither: disarmed. */
void box_net_watch(int fd, bool read, bool write, void (*post)(void (*fn)(void *), void *),
                   void (*ready)(void *arg), void *arg);
void box_net_unwatch(int fd);
