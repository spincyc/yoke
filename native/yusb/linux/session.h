#pragma once
/* One proxy session of the native worker: a USB/IP connection and a Raw
 * Gadget instance around one yusb_proxy_t, with the threads Raw Gadget's
 * blocking ioctls need (reference: worker.production_factory and
 * proxy.Proxy). All core calls happen under the session lock. */
#include <pthread.h>
#include "yusb_fence.h"
#include "yusb_proxy.h"

typedef struct {
    char udc[128];
    char gadget_driver[128];   /* "" = infer from sysfs */
    char raw_gadget[256];
    char udc_sysfs[256];
    yusb_policy_t policy;
    uint32_t connect_timeout_ms;
} wk_config_t;

typedef struct {
    bool built;                /* the gadget is bound (session presenting) */
    bool ended;
    bool rejected;             /* slow retry */
    bool yx_unavailable;       /* the YX codec could not be allocated: retry without YX */
    char reason[256];          /* why it ended or failed to build */
    char rejected_text[200];
    char error[200];          /* the live session's last fault */
    yusb_proxy_status_t status;
    yusb_proxy_counters_t counters;
    char link_speed[24];
} session_view_t;

typedef struct session session_t;

typedef void (*wk_log_fn)(int level, const char *message);

/* Starts connecting in the background. NULL only on allocation failure. */
session_t *session_start(const wk_config_t *cfg, const yusb_target_t *target, wk_log_fn log);
void session_view(session_t *s, session_view_t *view);
/* Confirmed teardown: true when every thread stopped and the gadget and
 * socket are closed (then session_free). false: resources are retained
 * and `why` explains; the caller keeps the old generation (ADR 0022) and
 * may call it again later. The first call waits up to stop_timeout_ms +
 * 1 s, later calls only briefly. */
bool session_stop(session_t *s, char *why, size_t cap);
void session_free(session_t *s);
/* Monotonic milliseconds. */
uint64_t wk_now_ms(void);
