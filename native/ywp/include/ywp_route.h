#pragma once
/* Generation-tagged driver work (ADR 0013), the sans-I/O half of the Python
 * runner's driver queue (src/yoke/net/runner.py: _queue_driver,
 * _drive_pending, _publish_driver, _driver_poll_loop).
 *
 * The node's ATTACH/DETACH effects are submitted here instead of being run
 * directly. One command runs at a time; pending work is bounded to a
 * detach barrier plus the latest attach, so superseded routes never reach
 * the hardware. Every submission advances the route generation, and a
 * status reaches the node only when it belongs to the current generation:
 * a stale completion or poll can never activate or clear a newer route.
 *
 *   ywp_route_submit(&route, &fx[i]);              // ATTACH or DETACH
 *   while (ywp_route_next(&route, &cmd)) {         // driver idle
 *       ok = cmd.detach ? drv_detach() : drv_attach(cmd.producer, cmd.media);
 *       state = ok ? drv_poll(&detail) : (YWP_ERROR, error text);
 *       if (ywp_route_done(&route, ok, &state, &detail, &len))
 *           ywp_node_driver_status(node, state, detail, len, now);
 *   }
 *   // periodic health poll, serialized with commands on the same worker:
 *   uint32_t tag = ywp_route_poll_tag(&route);
 *   state = drv_poll(&detail);
 *   if (ywp_route_poll_current(&route, tag)) ywp_node_driver_status(...);
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ywp.h"

typedef struct {
    bool detach;                    /* else attach to producer/media */
    uint32_t generation;
    const char *producer;           /* valid until the next submit/next */
    const char *media;
    size_t media_len;
} ywp_route_cmd_t;

typedef struct {
    uint32_t generation;            /* advanced by every submission */
    uint32_t applied;               /* generation of the last completed command */
    bool closing;
    bool stalled;                   /* teardown unconfirmed: wait for a submission */
    bool woken;                     /* submitted while a command ran */
    bool busy;                      /* a command is running */
    bool running_detach;
    uint32_t running_generation;
    bool detach_pending;
    uint32_t detach_generation;
    bool attach_pending;
    char producer[YWP_ID_MAX + 1];
    size_t media_len;
    char media[YWP_MEDIA_MAX];
    char running_producer[YWP_ID_MAX + 1];
    size_t running_media_len;
    char running_media[YWP_MEDIA_MAX];
} ywp_route_t;

void ywp_route_init(ywp_route_t *route);
/* Queue an ATTACH or DETACH effect (others are ignored). A detach clears a
 * pending attach and stays as a barrier; a later attach replaces any
 * pending attach without removing the barrier. */
void ywp_route_submit(ywp_route_t *route, const ywp_effect_t *fx);
/* The next command to run, if the driver is idle and work is pending. */
bool ywp_route_next(ywp_route_t *route, ywp_route_cmd_t *cmd);
/* The running command finished; `completed` is false when the driver call
 * failed (then *state is YWP_ERROR with the failure text). A detach that
 * does not leave the driver idle becomes an error and stays queued as a
 * barrier (unless closing). It runs again at once when work was submitted
 * since the queue last went idle, else after the next submission (the
 * node's retry or a new route), never in a spin. Returns true when the
 * state and detail must be fed to ywp_node_driver_status now; *state and
 * *detail may be rewritten. */
bool ywp_route_done(ywp_route_t *route, bool completed, ywp_state_t *state,
                    const char **detail, size_t *len);
/* Health polls: take the tag before polling the driver, then publish the
 * result only if it is still current (no command pending or running). */
uint32_t ywp_route_poll_tag(const ywp_route_t *route);
bool ywp_route_poll_current(const ywp_route_t *route, uint32_t tag);
/* Orderly shutdown: drop pending routes and stop re-queueing teardown;
 * statuses are no longer published (the shell logs failures). Submit the
 * node's shutdown DETACH afterwards. */
void ywp_route_close(ywp_route_t *route);
