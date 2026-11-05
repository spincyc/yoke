#pragma once
/* Shared vocabulary of the native YWP/1 core. Normative reference:
 * doc/design/PROTOCOL.md; executable reference: src/yoke/core/types.py.
 * Every name here has a Python counterpart with the same value. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define YWP_PROTOCOL_VERSION 1
#define YWP_CONTROL_GROUP "239.121.111.107"
#define YWP_CONTROL_PORT 9753
#define YWP_MAX_FRAME 1200                    /* bytes per datagram */
#define YWP_MAX_SEQUENCE ((uint64_t)INT64_MAX) /* 2^63 - 1 */
#define YWP_ID_MAX 32                         /* [a-z0-9][a-z0-9-]{0,31} */
#define YWP_NAME_MAX 64                       /* announce name, code points */
#define YWP_DETAIL_MAX 128                    /* wire detail text, UTF-8 bytes */
#define YWP_MEDIA_MAX YWP_MAX_FRAME           /* canonical media JSON bytes */

/* Timing (seconds) and sequence constants, as in yoke.core.types. */
#define YWP_ANN_INTERVAL 2.0
#define YWP_ANN_JITTER 0.25
#define YWP_PEER_TIMEOUT 6.5
#define YWP_RETRY_BACKOFF_BASE 1.0
#define YWP_RETRY_BACKOFF_CAP 30.0
#define YWP_INTENT_CACHE_TTL 86400.0
#define YWP_SUPPRESSION_MIN 0.2
#define YWP_SUPPRESSION_MAX 1.2
#define YWP_PEER_FORGET 300.0
#define YWP_NS_PER_S 1000000000ull
#define YWP_SEQUENCE_RESERVATION 4096u
#define YWP_SEQ_FUTURE_MARGIN_S 3600u
#define YWP_SEQ_FLOOR_JUMP_S 60u
#define YWP_ADMISSION_CONFIRMATIONS 2u

typedef enum { YWP_PRODUCER = 0, YWP_CONSUMER = 1 } ywp_role_t;
typedef enum { YWP_HDMI = 0, YWP_USB = 1 } ywp_kind_t;

/* NodeState: the media/driver state announced in `ann`. */
typedef enum { YWP_IDLE = 0, YWP_PENDING = 1, YWP_ACTIVE = 2, YWP_ERROR = 3 } ywp_state_t;

typedef enum { YWP_ANN = 0, YWP_INT = 1, YWP_ACK = 2, YWP_EVT = 3, YWP_BYE = 4 } ywp_frame_type_t;

/* Receive verdicts. The non-OK values are the PROTOCOL.md drop counters. */
typedef enum {
    YWP_OK = 0,
    YWP_DROP_MALFORMED,
    YWP_DROP_VERSION,
    YWP_DROP_AUTH,
    YWP_DROP_STALE,
    YWP_DROP_DUP,
    YWP_DROP_OVERSIZE,
    YWP_DROP_COUNT
} ywp_drop_t;

/* A pairing version [n, origin]: Lamport counter, then author id.
 * The zero version is exactly {0, ""}. */
typedef struct {
    uint64_t n;
    char origin[YWP_ID_MAX + 1];
} ywp_version_t;

/* A consumer's committed pairing; paired == false means producer null. */
typedef struct {
    bool paired;
    char producer[YWP_ID_MAX + 1];
    ywp_version_t version;
} ywp_pairing_t;

/* PersistedState (ADR 0012): the reserved outbound sequence ceiling and the
 * committed pairing. See ywp_state.h for its durable byte form. */
typedef struct {
    uint64_t seq;
    ywp_pairing_t pairing;
} ywp_saved_t;

const char *ywp_role_name(ywp_role_t role);
const char *ywp_kind_name(ywp_kind_t kind);
const char *ywp_state_name(ywp_state_t state);
const char *ywp_frame_type_name(ywp_frame_type_t type);
const char *ywp_drop_name(ywp_drop_t drop);    /* "malformed", ... */

/* Node id syntax: [a-z0-9][a-z0-9-]{0,31}, exactly `len` bytes. */
bool ywp_id_valid(const char *id, size_t len);
/* Total order of versions: counter, then origin bytes. */
int ywp_version_cmp(const ywp_version_t *a, const ywp_version_t *b);
