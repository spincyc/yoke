#pragma once
/* The Yoke USB box application: its role (device: USB host for the
 * peripheral; computer: USB device toward the computer), the USB backend
 * for that role, and either the USB/IP bridge (configured for YWP/1) or
 * the role's hardware self-test. */
#include <stdbool.h>
#include "cJSON.h"
#include "esp_err.h"
#include "esp_http_server.h"

/* After NVS: load the role and start its USB stack. */
esp_err_t yoke_box_start(void);
bool yoke_box_ready(void);
/* Adds a "usb" object to GET /v1/status. */
void yoke_box_status(cJSON *body);
/* yoke_node_serve's before_ethernet: in bridge mode, SNTP for YWP/1's wall
 * clock (asking the first DHCP lease for NTP servers). */
void yoke_box_network(void);
/* Once the mutual-TLS server runs: PUT /v1/usb/config (role, YWP/1 node
 * id, name, PSK, open mode; persisted, then a restart), PUT /v1/usb/clock
 * (forward-only wall clock), and in bridge mode pairing from the box's
 * first IPv4 address. */
esp_err_t yoke_box_online(httpd_handle_t server);
