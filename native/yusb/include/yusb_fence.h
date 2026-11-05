#pragma once
/* Route following with generation fencing (ADR 0022; reference:
 * src/yoke/usb/worker.py Worker and src/yoke/usb/control.py Target).
 *
 * The owner (Linux worker, MCU firmware) asks this pure state machine what
 * to do; it performs the I/O itself:
 *  - a route is adopted only when its generation is newer AND every older
 *    session's teardown was confirmed (yusb_fence_adopt after the stop);
 *    an unconfirmed teardown keeps the old generation and reports `error`;
 *  - a repeated route for the same generation is a no-op;
 *  - failed builds retry with backoff (retry_min doubling to retry_max);
 *    an unsupported device retries every retry_rejected_ms;
 *  - a session that ends retries after retry_min and counts a reconnect.
 * States and wording are the worker's (control.STATES).
 */
#include "yusb.h"

#define YUSB_HOST_MAX 253
#define YUSB_TARGET_BUSID_MAX 31

typedef struct {
    char host[YUSB_HOST_MAX + 1];  /* validated hostname or IP literal */
    uint16_t port;
    char busid[YUSB_TARGET_BUSID_MAX + 1];
    char source[16];               /* IPv4 to bind before connecting, or "" */
} yusb_target_t;

/* control.validate_*: canonical form into out; YUSB_ERR_RANGE if invalid.
 * Hosts: an IPv4/IPv6 literal (canonical text) or a hostname of labels
 * [A-Za-z0-9-] (1..63, no edge hyphen), lowercased, trailing dot removed. */
int yusb_validate_host(const char *text, size_t len, char out[YUSB_HOST_MAX + 1]);
int yusb_validate_busid(const char *text, size_t len);
/* Strict dotted-quad IPv4 (no leading zeros). */
int yusb_validate_ipv4(const char *text, size_t len, char out[16]);
bool yusb_target_equal(const yusb_target_t *a, const yusb_target_t *b);

typedef enum {
    YUSB_W_IDLE,
    YUSB_W_CONNECTING,
    YUSB_W_PRESENTING,
    YUSB_W_ATTACHED,
    YUSB_W_DEFAULT,
    YUSB_W_ADDRESSED,
    YUSB_W_CONFIGURED,
    YUSB_W_SUSPENDED,
    YUSB_W_DISCONNECTED,
    YUSB_W_REJECTED,
    YUSB_W_ERROR,
} yusb_wstate_t;

const char *yusb_wstate_name(yusb_wstate_t state);
/* YUSB_W_PRESENTING for names outside control.STATES. */
yusb_wstate_t yusb_wstate_from_name(const char *name);

typedef struct {
    uint32_t retry_min_ms;       /* 250 */
    uint32_t retry_max_ms;       /* 2000 */
    uint32_t retry_rejected_ms;  /* 30000 */
} yusb_fence_cfg_t;

typedef enum {
    YUSB_BUILD_RETRY,            /* connecting; backoff */
    YUSB_BUILD_REJECTED,         /* unsupported device; slow retry */
} yusb_build_failure_t;

typedef struct {
    yusb_fence_cfg_t cfg;
    uint64_t generation;
    bool has_target;
    yusb_target_t target;
    yusb_wstate_t state;
    char detail[501];
    uint32_t backoff_ms;
    uint64_t next_attempt_ms;
    uint32_t reconnects;
} yusb_fence_t;

void yusb_fence_init(yusb_fence_t *f, const yusb_fence_cfg_t *cfg);
/* A route request must replace the current one (generation is newer). */
bool yusb_fence_newer(const yusb_fence_t *f, uint64_t generation);
/* Every older session is confirmed stopped: adopt. target NULL detaches. */
void yusb_fence_adopt(yusb_fence_t *f, uint64_t generation, const yusb_target_t *target);
/* Teardown or build abort was not confirmed: keep the generation, error. */
void yusb_fence_hold(yusb_fence_t *f, const char *detail);
/* Whether to build a session now (a target, no session, attempt due). */
bool yusb_fence_due(const yusb_fence_t *f, bool have_session, uint64_t now_ms);
void yusb_fence_built(yusb_fence_t *f);
void yusb_fence_build_failed(yusb_fence_t *f, yusb_build_failure_t kind, const char *detail,
                             uint64_t now_ms);
/* A live session ended (after confirmed stop). */
void yusb_fence_ended(yusb_fence_t *f, const char *rejected, const char *reason,
                      uint64_t now_ms);
/* Mirror a live session's state (rejected wins; detail is its error). */
void yusb_fence_observe(yusb_fence_t *f, yusb_wstate_t state, const char *rejected,
                        const char *error);
void yusb_fence_set(yusb_fence_t *f, yusb_wstate_t state, const char *detail);
