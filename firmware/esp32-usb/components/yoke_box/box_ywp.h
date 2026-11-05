#pragma once
/* The YWP/1 shell of the box (network task only, except box_ywp_status):
 * the native node (native/ywp) with an lwIP multicast socket, NVS
 * persistence, mbedTLS hashing and the generation-tagged route queue. */
#include "bridge.h"

/* Restore the saved state; once the wall clock is synchronized (now, or
 * in a later box_ywp_timers), create the node, join the control group and
 * start it. ESP_ERR_INVALID_STATE: the saved state is corrupt (explicit
 * recovery: PUT /v1/usb/config {"rebase_sequence": true}). */
esp_err_t box_ywp_open(const box_config_t *config, const box_bridge_t *role, uint32_t ipv4);
/* The control socket; -1 before the node and after a failed (re)open,
 * which box_ywp_timers retries. */
int box_ywp_fd(void);
/* Monotonic seconds of the next node, driver-health or clock check. */
double box_ywp_deadline(void);
/* At most a batch of datagrams; true when more may be waiting (yield
 * before the next: a flood must not starve lower-priority tasks). Sources
 * that keep failing authentication are throttled (box_limit.h). */
bool box_ywp_receive(void);
void box_ywp_timers(void);
void box_ywp_route_done(bool completed, ywp_state_t state, const char *detail);
void box_ywp_poll_done(uint32_t tag, ywp_state_t state, const char *detail);
/* The box's own address changed (DHCP): new producer media, rejoin. */
void box_ywp_address(uint32_t ipv4);
/* Any task: a snapshot taken after each input. */
void box_ywp_status(cJSON *ywp);
/* The node owns the saved state (it loaded): no rebase. */
bool box_ywp_running(void);
/* The explicit recovery for a corrupt or lost state (ADR 0023
 * --rebase-sequence): unpaired, sequence at the admission bound. */
esp_err_t box_ywp_rebase(void);
