#pragma once
/* YWP/1 node: the controllerless pairing and control state machine of a
 * Yoke endpoint, as a sans-I/O C core. Executable reference:
 * src/yoke/core/node.py; normative reference: doc/design/PROTOCOL.md.
 *
 * The shell owns the socket, the clocks, durable storage and the driver.
 * It feeds inputs (datagrams, timer polls, storage and driver completions)
 * and performs the effects each input leaves behind:
 *
 *   ywp_node_datagram(node, raw, len, now, wall, &source);
 *   const ywp_effect_t *fx;
 *   size_t n = ywp_node_effects(node, &fx);
 *   for (size_t i = 0; i < n; ++i) perform(&fx[i]);
 *
 * Effects mirror the Python ones: SEND (encode with ywp_node_encode at send
 * time, then multicast), PERSIST (save, then report with ywp_node_persisted;
 * it is always the last effect of its batch), ATTACH/DETACH (driver work,
 * see ywp_route.h for generation tagging), ADMISSION (producer data-plane
 * allowlist, ADR 0024) and LOG (local diagnostics, never on the wire).
 * Effect data stays valid until the next input to the node.
 *
 * Time is two caller-supplied clocks in seconds: `now` is monotonic and
 * drives every timer; `wall` is Unix time read by the shell for each input
 * that takes it (NaN when unavailable) and only bounds sequences (ADR 0023).
 * Until the wall clock is near real time (SNTP), every peer frame counts as
 * far-future and is dropped as stale, while the node's own frames remain
 * admissible from its durable floor; the next frame after the clock steps
 * is admitted, with no restart. Commission (state seq 0) only with a
 * synchronized clock: the first floor comes from it.
 *
 * Inputs and ywp_node_encode run on one task (or under one lock): the node
 * is not reentrant, and encode reuses the decoder's token array.
 *
 * Memory is one block allocated by ywp_node_create, sized by ywp_limits_t
 * (ywp_node_footprint); nothing is allocated afterwards. Tables are FIFO
 * capped like the reference's MAX_TRACKED maps, never evicting the paired
 * producer. Jitter uses MT19937 seeded exactly like Python's
 * random.Random(seed), so a node is reproducible against the reference. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ywp_crypto.h"
#include "ywp_frame.h"
#include "ywp_state.h"
#include "ywp_types.h"

typedef struct {
    uint16_t peers;         /* announcing nodes tracked */
    uint16_t origins;       /* per-origin replay marks (any frame type) */
    uint16_t intents;       /* anti-entropy intent cache, per consumer */
    uint16_t rejections;    /* rejected intent versions, per consumer */
    uint16_t media_bytes;   /* consumer: producer media descriptions */
    uint16_t effects;       /* effect records per batch */
    uint16_t effect_bytes;  /* frame bodies, log text and attach media */
} ywp_limits_t;

typedef struct {
    const char *node_id;            /* [a-z0-9][a-z0-9-]{0,31} */
    ywp_role_t role;
    ywp_kind_t kind;
    const char *name;               /* UTF-8, <= 64 code points; NULL = "" */
    const uint8_t *psk;             /* site key; psk_len == 0: open mode */
    size_t psk_len;
    bool admission;                 /* producer data-plane admission */
    uint64_t seed;                  /* jitter seed (random.Random(seed)) */
    const char *media;              /* producer media: JSON object or NULL */
    size_t media_len;
    ywp_limits_t limits;
} ywp_config_t;

typedef enum {
    YWP_E_OK = 0,
    YWP_E_CONFIG,       /* bad role, kind, limits or null pointer */
    YWP_E_ID,           /* node id syntax */
    YWP_E_NAME,         /* not UTF-8 or longer than 64 code points */
    YWP_E_MEDIA,        /* not an integer-only JSON object that fits */
    YWP_E_STATE,        /* invalid saved state */
    YWP_E_CLOCK,        /* nonfinite now, or wall not finite and >= 0 */
    YWP_E_SEQUENCE,     /* outbound sequence space exhausted */
    YWP_E_NOMEM
} ywp_err_t;

typedef enum { YWP_LOG_DEBUG, YWP_LOG_INFO, YWP_LOG_WARNING, YWP_LOG_ERROR } ywp_log_level_t;

typedef enum {
    YWP_FX_SEND = 1,
    YWP_FX_PERSIST,
    YWP_FX_ATTACH,
    YWP_FX_DETACH,
    YWP_FX_ADMISSION,
    YWP_FX_LOG
} ywp_fx_kind_t;

/* One admitted consumer: id and IPv4 source (host byte order). */
typedef struct {
    char id[YWP_ID_MAX + 1];
    uint32_t addr;
} ywp_admit_t;

typedef struct {
    ywp_fx_kind_t kind;
    union {
        struct {                    /* sign and multicast */
            ywp_frame_type_t type;
            uint64_t seq;
            const uint8_t *body;    /* canonical JSON object */
            size_t body_len;
        } send;
        struct {                    /* save, then ywp_node_persisted() */
            const ywp_saved_t *state;
            uint32_t request_id;
        } persist;
        struct {                    /* driver: consume producer media */
            const char *producer;
            const char *media;      /* canonical JSON object */
            size_t media_len;
        } attach;
        struct {                    /* complete replacement set, sorted */
            const ywp_admit_t *peers;
            size_t count;
        } admission;
        struct {
            ywp_log_level_t level;
            const char *text;       /* NUL-terminated */
        } log;
    };
} ywp_effect_t;

typedef struct ywp_node ywp_node_t;

void ywp_limits_default(ywp_limits_t *limits);
void ywp_config_default(ywp_config_t *cfg);
const char *ywp_err_name(ywp_err_t err);

/* Bytes ywp_node_create allocates for these limits (0 if invalid). */
size_t ywp_node_footprint(const ywp_limits_t *limits);

/* Restore a node from its saved state (ywp_state.h) at monotonic `now` and
 * wall time `wall`. The outbound floor is ywp_restart_floor(state->seq,
 * wall). Produces no effects; call ywp_node_start. */
ywp_node_t *ywp_node_create(const ywp_config_t *cfg, const ywp_crypto_ops_t *ops,
                            const ywp_saved_t *state, double now, double wall,
                            ywp_err_t *err);
void ywp_node_destroy(ywp_node_t *node);

/* -- inputs: each one replaces the effects of the previous input -------- */

/* Durable start barrier (PERSIST), then started/announce. */
void ywp_node_start(ywp_node_t *node, double now);
/* One received datagram, with the shell's wall reading at receipt and the
 * transport IPv4 source (host order; NULL when unknown). Decodes,
 * authenticates, applies replay defense and handles the frame. */
void ywp_node_datagram(ywp_node_t *node, const uint8_t *raw, size_t len,
                       double now, double wall, const uint32_t *source);
/* Completion of the PERSIST effect with this request id. */
void ywp_node_persisted(ywp_node_t *node, uint32_t request_id, bool ok, double now);
/* Current-generation driver status (ywp_route.h filters stale ones).
 * detail is UTF-8 (invalid sequences are replaced by '?'). */
void ywp_node_driver_status(ywp_node_t *node, ywp_state_t state,
                            const char *detail, size_t len, double now);
/* Producer: replace the media description (validated as at create). */
ywp_err_t ywp_node_set_media(ywp_node_t *node, const char *json, size_t len, double now);
/* Timers: announce, peer timeout, retries, anti-entropy, rx-drop. */
void ywp_node_poll(ywp_node_t *node, double now, double wall);
/* Orderly stop: consumer DETACH, then stopping/bye when started. */
void ywp_node_shutdown(ywp_node_t *node, double now);

/* When the next poll is due (monotonic seconds; may be <= now). */
double ywp_node_next_deadline(const ywp_node_t *node);

/* -- effects --------------------------------------------------------------- */

size_t ywp_node_effects(const ywp_node_t *node, const ywp_effect_t **effects);
/* Encode a SEND effect for transmission at wall time `ts` (NaN: the node's
 * estimate from the last wall reading). Returns the datagram length, or 0
 * if it cannot be encoded (a bug: log and drop, as the reference does).
 * It overwrites the decoder's tokens, so call it on the node's task. */
size_t ywp_node_encode(ywp_node_t *node, const ywp_effect_t *send, double ts,
                       uint8_t out[YWP_MAX_FRAME]);

/* -- introspection (read-only; status pages, telemetry, tests) ----------- */

typedef struct {
    ywp_state_t state;
    ywp_pairing_t pairing;
    bool started;
    bool save_pending;              /* a PERSIST awaits its completion */
    bool held;                      /* effects held for a reservation retry */
    uint64_t last_seq;              /* last sequence number used */
    uint64_t reserved_until;
    double retry_at;                /* driver retry deadline, NaN if none */
    double backoff;
    double ann_due;
    uint64_t drops[YWP_DROP_COUNT];          /* indexed by ywp_drop_t */
    uint64_t drops_reported[YWP_DROP_COUNT]; /* as of the last rx-drop */
    size_t peers, origins, intents, rejections, admitted;
    uint32_t overruns;              /* effects dropped for room: limits too small */
} ywp_status_t;

void ywp_node_status(const ywp_node_t *node, ywp_status_t *out);

typedef struct {
    char id[YWP_ID_MAX + 1];
    ywp_role_t role;
    ywp_kind_t kind;
    ywp_state_t state;
    bool has_pairing;               /* consumers */
    ywp_pairing_t pairing;
    bool has_media;                 /* producers announcing media */
    double last_seen;
    bool departed, lost;
} ywp_peer_t;

/* Peers in table order (the reference's dict order). */
size_t ywp_node_peer_count(const ywp_node_t *node);
bool ywp_node_peer(const ywp_node_t *node, size_t index, ywp_peer_t *out);
/* Canonical media of a producer peer, or NULL. */
const char *ywp_node_peer_media(const ywp_node_t *node, size_t index, size_t *len);

typedef struct {
    char consumer[YWP_ID_MAX + 1];
    bool pair;                      /* op: pair (with producer) or unpair */
    char producer[YWP_ID_MAX + 1];
    ywp_version_t version;
    double cached_at;
    bool resend;                    /* anti-entropy re-issue scheduled */
    double resend_at;
} ywp_intent_t;

size_t ywp_node_intent_count(const ywp_node_t *node);
bool ywp_node_intent(const ywp_node_t *node, size_t index, ywp_intent_t *out);

typedef enum {
    YWP_REJECTED_NONE = 0,          /* producer absent or null */
    YWP_REJECTED_ID,                /* producer was a valid node id */
    YWP_REJECTED_OTHER              /* any other JSON value */
} ywp_rejected_producer_t;

typedef struct {
    char consumer[YWP_ID_MAX + 1];
    ywp_version_t version;
    ywp_rejected_producer_t producer_kind;
    char producer[YWP_ID_MAX + 1];  /* YWP_REJECTED_ID */
} ywp_rejection_t;

size_t ywp_node_rejection_count(const ywp_node_t *node);
bool ywp_node_rejection(const ywp_node_t *node, size_t index, ywp_rejection_t *out);
