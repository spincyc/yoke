#pragma once
/* Internal structure of the YWP/1 node, shared by ywp_node.c (the state
 * machine), ywp_node_fx.c (effect batches and frame bodies) and
 * ywp_node_tab.c (bounded tables). Field comments name the reference's
 * attribute in src/yoke/core/node.py where there is one. */
#include <string.h>
#include "ywp.h"
#include "ywp_internal.h"

#define LOG_MAX 192u    /* local log line, truncated; never on the wire */

enum { SAVE_START = 1, SAVE_PAIRING, SAVE_EFFECTS };

/* A rejected or compared intent `producer` member: absent/null, a node id,
 * or any other JSON value identified by the fingerprint of its canonical
 * bytes. */
enum { PVALUE_NONE = YWP_REJECTED_NONE, PVALUE_ID = YWP_REJECTED_ID, PVALUE_OTHER = YWP_REJECTED_OTHER };

typedef struct {
    uint8_t kind;
    char id[YWP_ID_MAX + 1];
    uint8_t fingerprint[YWP_SHA256_LEN];
} pvalue_t;

/* Every row begins with its key; rows stay in insertion order. */
typedef struct {
    char id[YWP_ID_MAX + 1];
    uint8_t role, kind, state;
    bool departed, lost;            /* saw bye; peer-lost(timeout) emitted */
    bool has_pairing;
    ywp_pairing_t pairing;
    bool has_media;                 /* announced a non-null media object */
    uint16_t media_off, media_len;  /* stored canonical bytes; 0: none kept */
    double last_seen;
} peer_t;

typedef struct {                    /* _replay and _sources (ADR 0024) */
    char id[YWP_ID_MAX + 1];
    uint64_t seq;
    uint8_t fingerprint[YWP_SHA256_LEN];
    bool admitted_set, candidate_set;
    uint8_t count;
    uint32_t admitted, candidate;
} origin_t;

typedef struct {                    /* _intent_cache and _resend_at */
    char id[YWP_ID_MAX + 1];        /* consumer */
    bool pair;
    char producer[YWP_ID_MAX + 1];
    ywp_version_t version;
    double cached_at;
    bool resend;
    uint32_t resend_order;          /* _resend_at insertion order */
    double resend_at;
} intent_t;

typedef struct {                    /* _rejected */
    char id[YWP_ID_MAX + 1];        /* consumer */
    ywp_version_t version;
    pvalue_t producer;
} rejection_t;

typedef struct {
    void *rows;
    size_t size, count, cap;        /* cap rows plus one spare */
} table_t;

struct ywp_node {
    /* configuration */
    char id[YWP_ID_MAX + 1];
    ywp_role_t role;
    ywp_kind_t kind;
    char name[4 * YWP_NAME_MAX + 1];
    size_t name_len;
    bool admission;                 /* _admission */
    ywp_limits_t limits;
    size_t overhead;                /* announce envelope around the body */
    ywp_codec_t codec;
    ywp_mt_t rng;

    /* sequences, clocks and the durable barrier (ADRs 0012, 0023) */
    ywp_pairing_t pairing;
    uint64_t seq, reserved_until;
    double wall_offset, last_now;
    uint64_t boot_saved;            /* _startup_logs inputs */
    double boot_wall;
    bool boot_logs;
    double future_log_at, own_seq_log_at;
    bool started;
    bool save_pending;              /* _pending_save */
    uint8_t save_kind;
    uint32_t save_id, save_serial;
    ywp_saved_t save_state;
    bool deferred;                  /* _deferred_effects */
    double reserve_retry_at;        /* NaN: None */
    double reserve_backoff;

    /* media plane */
    ywp_state_t state;
    bool has_err;                   /* _err is a str */
    char err[YWP_DETAIL_MAX + 1];
    size_t err_len;
    char detail[YWP_DETAIL_MAX + 1];  /* _driver_detail */
    size_t detail_len;
    bool logged;                    /* _driver_logged: state and SHA-256 of */
    ywp_state_t logged_state;       /* the full, unbounded detail */
    uint8_t logged_digest[YWP_SHA256_LEN];
    bool teardown_pending;
    double retry_at;                /* NaN: None */
    double backoff;
    bool attached;                  /* _attached_media is not None */
    size_t attached_len;
    char attached_media[YWP_MEDIA_MAX];
    bool has_media;                 /* own producer media */
    size_t media_len;
    char media[YWP_MEDIA_MAX];
    double ann_due;
    double rx_drop_at;
    uint64_t drops[YWP_DROP_COUNT];     /* DUP slot is _dups */
    uint64_t reported[YWP_DROP_COUNT];  /* _drops_reported */

    /* bounded tables, FIFO-capped, never evicting the paired producer */
    table_t peers, origins, intents, rejections;
    uint32_t resend_serial;
    ywp_admit_t *admitted;          /* _admitted, capacity limits.peers */
    size_t admitted_count;
    uint8_t *media_arena;           /* consumer: peers' canonical media */
    size_t media_used;
    char scratch[YWP_MEDIA_MAX];

    /* effects of the current input */
    ywp_effect_t *fx;
    size_t fx_len, vis_begin, vis_end;
    size_t held_end, held_arena;    /* a batch held behind a reservation */
    uint8_t *arena;
    size_t arena_used;
    bool poll_more;                 /* poll stopped early for room */
    uint32_t overruns;              /* effects dropped for room (a bug) */
};

static inline void copy_id(char dst[YWP_ID_MAX + 1], const char *src)
{
    size_t len = strlen(src);
    memcpy(dst, src, len + 1);
}

/* _validate_media_description (ywp_node.c): the canonical description is
 * left in n->scratch; adopt_media installs it. */
ywp_err_t ywpn_check_media(ywp_node_t *n, const char *json, size_t len, size_t *out_len,
                      bool *present);
void ywpn_adopt_media(ywp_node_t *n, bool present, size_t len);

/* -- tables (ywp_node_tab.c) ----------------------------------------------- */

void *ywpn_tab_row(const table_t *t, size_t i);
long ywpn_tab_find(const table_t *t, const char *id);
void *ywpn_tab_get(const table_t *t, const char *id);
/* Append a zeroed row keyed `id` into the spare row; bound_maps caps every
 * table when the input ends. */
void *ywpn_tab_insert(ywp_node_t *n, table_t *t, const char *id);
void ywpn_tab_remove(ywp_node_t *n, table_t *t, size_t i);
void ywpn_bound_maps(ywp_node_t *n);
/* Consumer media store: keep `len` canonical bytes for `peer`, evicting
 * other producers' copies (oldest first, never the paired producer). */
void ywpn_media_store(ywp_node_t *n, peer_t *peer, const char *media, size_t len);
void ywpn_media_free(ywp_node_t *n, peer_t *peer);
const char *ywpn_peer_media(const ywp_node_t *n, const peer_t *peer);

/* -- effects and frame bodies (ywp_node_fx.c) ------------------------------- */

void ywpn_fx_begin(ywp_node_t *n);
void ywpn_fx_publish(ywp_node_t *n, size_t from);
bool ywpn_fx_room(const ywp_node_t *n, size_t records, size_t bytes);
bool ywpn_holding(const ywp_node_t *n);
ywp_effect_t *ywpn_fx_push(ywp_node_t *n, ywp_fx_kind_t kind);
void ywpn_fx_attach(ywp_node_t *n, const char *producer, const char *media, size_t len);
void ywpn_fx_detach(ywp_node_t *n);
void ywpn_fx_admission(ywp_node_t *n);
/* _finish over records [from, end): hold the batch behind a reservation
 * save when one of its frames passes the reserved ceiling. */
void ywpn_fx_finish(ywp_node_t *n, size_t from);
/* _begin_save: false (no effect) when a save is already pending. */
bool ywpn_begin_save(ywp_node_t *n, uint8_t kind, const ywp_pairing_t *pairing);

typedef struct {
    ywp_out_t out;
    ywp_node_t *node;
} logline_t;

bool ywpn_log_begin(ywp_node_t *n, logline_t *l);
void ywpn_log_text(logline_t *l, const char *text);
void ywpn_log_bytes(logline_t *l, const char *text, size_t len);
void ywpn_log_u64(logline_t *l, uint64_t v);
void ywpn_log_end(logline_t *l, ywp_log_level_t level);
void ywpn_log_static(ywp_node_t *n, ywp_log_level_t level, const char *text);

/* Frames authored by this node (_send, _evt, _ack, _ann). */
void ywpn_send_evt(ywp_node_t *n, const char *event, const char *key, const char *value,
              size_t value_len);
void ywpn_send_evt_peer_lost(ywp_node_t *n, const char *peer, const char *reason);
void ywpn_send_evt_pairing(ywp_node_t *n, const ywp_pairing_t *pairing);
void ywpn_send_evt_rx_drop(ywp_node_t *n);
void ywpn_send_ack(ywp_node_t *n, const ywp_version_t *v, bool applied, const char *reason);
void ywpn_send_int(ywp_node_t *n, const intent_t *intent);
void ywpn_send_bye(ywp_node_t *n);
void ywpn_send_ann(ywp_node_t *n, double now);
/* Announce body with the given state, producer media (NULL: null) and
 * optional fields; returns its length (the budget is overhead + length). */
size_t ywpn_ann_body(const ywp_node_t *n, ywp_out_t *o, ywp_state_t state, const char *media,
                size_t media_len, bool status, bool err, size_t err_len);
