#pragma once
/* Ethernet bring-up shared by Yoke ESP32 endpoints: a W5500 on SPI2 or the
 * ESP32-P4 internal EMAC with an IP101 PHY, DHCP or static IPv4. The board
 * profile is Kconfig (Kconfig.projbuild); every pin is bench-unverified ⚠. */
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    bool link_up;
    bool link_down_seen;     /* explicit link-down event since the last start */
    bool ip_acquired;
    uint32_t ms_since_start; /* since the last successful driver start */
} yoke_eth_state_t;

/* Once: netif, default event loop, link/IP handlers, GPIO ISR service. The
 * IPv4 log line ends with ip_note (the application's services). */
esp_err_t yoke_eth_init(const char *ip_note);
/* One driver start attempt. After a failure call yoke_eth_stop(). */
esp_err_t yoke_eth_start(void);
/* Undo whatever yoke_eth_start completed. false: the driver could not be
 * uninstalled (its MAC/PHY may still be live), so do not retry in place. */
bool yoke_eth_stop(void);
yoke_eth_state_t yoke_eth_state(void);
