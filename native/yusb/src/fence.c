/* Route following with generation fencing (reference: worker.py Worker). */
#include <stdio.h>
#include <string.h>
#include "yusb_fence.h"

static const char *const names[] = {
    "idle", "connecting", "presenting", "attached", "default", "addressed",
    "configured", "suspended", "disconnected", "rejected", "error",
};

const char *yusb_wstate_name(yusb_wstate_t state)
{
    return (unsigned)state < sizeof(names) / sizeof(names[0]) ? names[state] : "error";
}

yusb_wstate_t yusb_wstate_from_name(const char *name)
{
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i)
        if (name && !strcmp(names[i], name)) return (yusb_wstate_t)i;
    return YUSB_W_PRESENTING;
}

void yusb_fence_set(yusb_fence_t *f, yusb_wstate_t state, const char *detail)
{
    f->state = state;
    snprintf(f->detail, sizeof(f->detail), "%s", detail ? detail : "");
}

void yusb_fence_init(yusb_fence_t *f, const yusb_fence_cfg_t *cfg)
{
    memset(f, 0, sizeof(*f));
    f->cfg = *cfg;
    f->state = YUSB_W_IDLE;
    f->backoff_ms = cfg->retry_min_ms;
}

bool yusb_fence_newer(const yusb_fence_t *f, uint64_t generation)
{
    return generation > f->generation;
}

void yusb_fence_adopt(yusb_fence_t *f, uint64_t generation, const yusb_target_t *target)
{
    f->generation = generation;
    f->has_target = target != NULL;
    if (target) f->target = *target;
    else memset(&f->target, 0, sizeof(f->target));
    f->backoff_ms = f->cfg.retry_min_ms;
    f->next_attempt_ms = 0;
    yusb_fence_set(f, target ? YUSB_W_CONNECTING : YUSB_W_IDLE, "");
}

void yusb_fence_hold(yusb_fence_t *f, const char *detail)
{
    yusb_fence_set(f, YUSB_W_ERROR, detail);
}

bool yusb_fence_due(const yusb_fence_t *f, bool have_session, uint64_t now_ms)
{
    return f->has_target && !have_session && now_ms >= f->next_attempt_ms;
}

void yusb_fence_built(yusb_fence_t *f)
{
    f->backoff_ms = f->cfg.retry_min_ms;
    yusb_fence_set(f, YUSB_W_PRESENTING, "");
}

static void schedule(yusb_fence_t *f, yusb_wstate_t state, const char *detail, bool slow,
                     uint64_t now_ms)
{
    yusb_fence_set(f, state, detail);
    f->next_attempt_ms = now_ms + (slow ? f->cfg.retry_rejected_ms : f->backoff_ms);
    f->backoff_ms = f->backoff_ms * 2 > f->cfg.retry_max_ms ? f->cfg.retry_max_ms : f->backoff_ms * 2;
}

void yusb_fence_build_failed(yusb_fence_t *f, yusb_build_failure_t kind, const char *detail,
                             uint64_t now_ms)
{
    bool rejected = kind == YUSB_BUILD_REJECTED;
    schedule(f, rejected ? YUSB_W_REJECTED : YUSB_W_CONNECTING, detail, rejected, now_ms);
}

void yusb_fence_ended(yusb_fence_t *f, const char *rejected, const char *reason, uint64_t now_ms)
{
    f->reconnects++;
    if (rejected && *rejected) {
        schedule(f, YUSB_W_REJECTED, rejected, true, now_ms);
        return;
    }
    char detail[sizeof(f->detail)];
    snprintf(detail, sizeof(detail), "session ended: %s", reason ? reason : "ended");
    yusb_fence_set(f, YUSB_W_CONNECTING, detail);
    f->next_attempt_ms = now_ms + f->cfg.retry_min_ms;
}

void yusb_fence_observe(yusb_fence_t *f, yusb_wstate_t state, const char *rejected,
                        const char *error)
{
    if (rejected && *rejected) yusb_fence_set(f, YUSB_W_REJECTED, rejected);
    else yusb_fence_set(f, state, error);
}
