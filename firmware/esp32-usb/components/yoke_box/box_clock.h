#pragma once
/* The wall clock YWP/1 needs: sequences are Unix time, and peers drop
 * frames more than an hour ahead of theirs as stale. The provisioned clock
 * floor loses the time a box spends switched off, so a bridging box waits
 * (box_ywp) until SNTP answers, from DHCP option 42 or
 * CONFIG_YOKE_USB_NTP_SERVER, or until the admin API sets the clock. */
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/* After the network stack, before Ethernet: the first DHCP request then
 * asks for NTP servers. A synchronized clock survives software restarts
 * (box_time.h): the bridge after PUT /v1/usb/config, an update or a
 * watchdog reset needs no new reading. */
esp_err_t box_clock_init(void);
/* The box has an address: start polling. */
void box_clock_online(void);
/* Any task. */
bool box_clock_synced(void);
/* An operator's reading (mutual-TLS admin API): set the clock, forward
 * only (ESP_ERR_INVALID_ARG when unix_s is behind it) and by at most
 * BOX_TIME_STEP_MAX unless forced (ESP_ERR_INVALID_SIZE), and count it as
 * synchronized. */
esp_err_t box_clock_set_forward(int64_t unix_s, bool force);
