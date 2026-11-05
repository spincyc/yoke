#pragma once
/* Endpoint lifecycle shared by the Yoke ESP32 applications (ADR 0026), in
 * app_main order:
 *
 *   if (!yoke_node_begin("Yoke IR endpoint")) return;   inert, fail closed
 *   ...application init (NVS ready)...
 *   yoke_node_serve(note, guard, status, NULL);         Ethernet + mTLS API
 *   ...register admin handlers, start services...
 *   yoke_node_confirm(app_ok, ready);                   boot health gate
 *   yoke_node_run();                                    never returns
 */
#include <stdbool.h>
#include "cJSON.h"

/* Entropy, task watchdog, updater trust and application NVS. false: trust or
 * storage failed; a pending image has rolled back, otherwise the caller
 * returns from app_main and stays inert until USB recovery (no restart loop). */
bool yoke_node_begin(const char *name);
/* Ethernet with in-boot retries, then the mutual-TLS server with the given
 * restart guard and status hook. Failures restart with backoff.
 * before_ethernet (may be NULL) runs once the network stack and the default
 * event loop exist, before DHCP can start (e.g. SNTP taking DHCP's servers). */
void yoke_node_serve(const char *ip_note, void (*restart_guard)(void),
                     void (*status_hook)(cJSON *body), void (*before_ethernet)(void));
/* Confirm a pending image once app_ok, ready() and the network gate pass
 * (yoke_ota_health_decide); a local failure rolls a pending image back. */
void yoke_node_confirm(bool app_ok, bool (*ready)(void));
/* Feed the watchdog and save the clock floor hourly. */
void yoke_node_run(void) __attribute__((noreturn));
/* Network-plane failure: a pending image rolls back; a confirmed image
 * restarts with a backoff that grows across restarts (capped at 5 min). */
void yoke_node_restart_later(const char *why) __attribute__((noreturn));
