/* Generation-tagged driver queue (ywp_route.h): the runner's _queue_driver,
 * _drive_pending and _publish_driver without the asyncio plumbing. */
#include <string.h>
#include "ywp_route.h"

#define UNCONFIRMED "driver teardown is not confirmed idle"

void ywp_route_init(ywp_route_t *r)
{
    memset(r, 0, sizeof(*r));
}

void ywp_route_submit(ywp_route_t *r, const ywp_effect_t *fx)
{
    if (fx->kind == YWP_FX_DETACH) {
        /* An unpair supersedes pending attaches; the barrier stays. */
        r->detach_pending = true;
        r->detach_generation = ++r->generation;
        r->attach_pending = false;
    } else if (fx->kind == YWP_FX_ATTACH) {
        size_t id_len = strlen(fx->attach.producer);
        if (id_len > YWP_ID_MAX || fx->attach.media_len > YWP_MEDIA_MAX) return;
        ++r->generation;
        r->attach_pending = true;
        memcpy(r->producer, fx->attach.producer, id_len + 1);
        memcpy(r->media, fx->attach.media, fx->attach.media_len);
        r->media_len = fx->attach.media_len;
    } else {
        return;
    }
    /* The runner's wakeup event: a submission during a command keeps the
     * worker going after that command, even if it fails its teardown. */
    r->woken |= r->busy;
    r->stalled = false;
}

bool ywp_route_next(ywp_route_t *r, ywp_route_cmd_t *cmd)
{
    if (r->busy || r->stalled) return false;
    if (!r->detach_pending && !r->attach_pending) {
        r->woken = false;                        /* idle: the worker waits */
        return false;
    }
    memset(cmd, 0, sizeof(*cmd));
    r->busy = true;
    r->running_detach = r->detach_pending;
    if (r->detach_pending) {
        r->detach_pending = false;
        r->running_generation = r->detach_generation;
        cmd->detach = true;
    } else {
        r->attach_pending = false;
        r->running_generation = r->generation;
        memcpy(r->running_producer, r->producer, sizeof(r->producer));
        memcpy(r->running_media, r->media, r->media_len);
        r->running_media_len = r->media_len;
        cmd->producer = r->running_producer;
        cmd->media = r->running_media;
        cmd->media_len = r->running_media_len;
    }
    cmd->generation = r->running_generation;
    return true;
}

static bool settled(const ywp_route_t *r)
{
    return !r->busy && !r->detach_pending && !r->attach_pending;
}

bool ywp_route_done(ywp_route_t *r, bool completed, ywp_state_t *state,
                    const char **detail, size_t *len)
{
    if (!r->busy) return false;
    r->busy = false;
    if (completed) r->applied = r->running_generation;
    if (r->running_detach && *state != YWP_IDLE) {
        /* Teardown uncertainty is a driver error (ADR 0017). Keep the
         * barrier ahead of the latest route and report it now. */
        if (*state != YWP_ERROR) {
            *state = YWP_ERROR;
            if (!*len) {
                *detail = UNCONFIRMED;
                *len = sizeof(UNCONFIRMED) - 1;
            }
        }
        if (r->closing) return false;
        r->detach_pending = true;
        r->stalled = !r->woken;
        r->woken = false;
        return true;
    }
    return !r->closing && r->running_generation == r->generation && settled(r);
}

uint32_t ywp_route_poll_tag(const ywp_route_t *r)
{
    return r->applied;
}

bool ywp_route_poll_current(const ywp_route_t *r, uint32_t tag)
{
    return !r->closing && tag == r->generation && settled(r);
}

void ywp_route_close(ywp_route_t *r)
{
    r->closing = true;
    ++r->generation;
    r->attach_pending = r->detach_pending = r->stalled = r->woken = false;
}
