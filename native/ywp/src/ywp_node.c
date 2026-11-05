/* YWP/1 node state machine: a structural port of src/yoke/core/node.py.
 * Functions carry the reference's names in their comments so the two can
 * be read side by side; deliberate differences are listed in README.md. */
#include <math.h>
#include <stdalign.h>
#include <string.h>
#include "ywp_node_impl.h"
#include "ywp_state.h"

#define TIMEOUT_EPS 1e-3
#define FUTURE_LOG_INTERVAL 60.0
#define OWN_SEQ_LOG_INTERVAL 3600.0

/* Poll stops early (and asks to be polled again at once) rather than run
 * out of effect space: room for one step's worst case is checked first. */
#define ANN_BYTES YWP_MAX_FRAME
#define EVT_BYTES 256u
#define STEP_PEER_BYTES (LOG_MAX + 2 * EVT_BYTES + ANN_BYTES)
#define STEP_RETRY_BYTES (YWP_ID_MAX + 1 + YWP_MEDIA_MAX + EVT_BYTES)

static bool live(const peer_t *p)
{
    return !p->departed && !p->lost;
}

static const char *paired_producer(const ywp_node_t *n)
{
    return n->pairing.paired ? n->pairing.producer : "";
}

static void touch(ywp_node_t *n, double now)
{
    if (now > n->last_now) n->last_now = now;
}

/* _wall: a supplied reading always wins and re-bases the estimate. */
static double wall_now(ywp_node_t *n, double now, double wall)
{
    if (isfinite(wall) && wall >= 0) {
        n->wall_offset = wall - now;
        return wall;
    }
    return now + n->wall_offset;
}

static bool active_input(const ywp_node_t *n)
{
    return n->started && !n->save_pending && !n->deferred;
}

/* SHA-256 of the full driver detail: the log is repeated only when the
 * detail changes, without keeping an unbounded copy. */
static void detail_digest(const ywp_node_t *n, const char *p, size_t len,
                          uint8_t out[YWP_SHA256_LEN])
{
    const ywp_crypto_ops_t *ops = n->codec.mac.ops;
    alignas(max_align_t) unsigned char state[YWP_SHA256_STATE_MAX];
    ops->sha256_start(ops->ctx, state);
    if (len) ops->sha256_update(ops->ctx, state, (const uint8_t *)p, len);
    ops->sha256_finish(ops->ctx, state, out);
}

/* _bounded_detail of a UTF-8 detail: invalid bytes become '?', then the
 * longest prefix of whole characters within 128 bytes. */
static size_t bounded_detail(const char *in, size_t len, char out[YWP_DETAIL_MAX + 1])
{
    size_t n = 0;
    for (size_t i = 0; i < len;) {
        size_t step = 1;
        while (step <= 4 && i + step <= len && !ywp_utf8_valid((const uint8_t *)in + i, step))
            ++step;
        bool ok = step <= 4 && i + step <= len;
        size_t take = ok ? step : 1;
        if (n + take > YWP_DETAIL_MAX) break;
        if (ok) memcpy(out + n, in + i, take);
        else out[n] = '?';
        n += take;
        i += take;
    }
    out[n] = 0;
    return n;
}

static void set_detail(ywp_node_t *n, const char *bounded, size_t len)
{
    memcpy(n->detail, bounded, len + 1);
    n->detail_len = len;
}

static void clear_err(ywp_node_t *n)
{
    n->has_err = false;
    n->err_len = 0;
    n->err[0] = 0;
}

static void log_id_line(ywp_node_t *n, ywp_log_level_t level, const char *a,
                        const char *id, const char *b)
{
    logline_t l;
    if (!ywpn_log_begin(n, &l)) { ywpn_log_end(&l, level); return; }
    ywpn_log_text(&l, a);
    ywpn_log_text(&l, id);
    ywpn_log_text(&l, b);
    ywpn_log_end(&l, level);
}

/* -- tables with reference semantics ---------------------------------------- */

/* _cache_get: an entry older than INTENT_CACHE_TTL is dropped on access. */
static intent_t *cache_get(ywp_node_t *n, const char *consumer, double now)
{
    long i = ywpn_tab_find(&n->intents, consumer);
    if (i < 0) return NULL;
    intent_t *e = ywpn_tab_row(&n->intents, (size_t)i);
    if (now - e->cached_at > YWP_INTENT_CACHE_TTL) {
        ywpn_tab_remove(n, &n->intents, (size_t)i);
        return NULL;
    }
    return e;
}

static bool pvalue_eq(const pvalue_t *a, const pvalue_t *b)
{
    if (a->kind != b->kind) return false;
    if (a->kind == PVALUE_ID) return strcmp(a->id, b->id) == 0;
    if (a->kind == PVALUE_OTHER) return memcmp(a->fingerprint, b->fingerprint, YWP_SHA256_LEN) == 0;
    return true;
}

/* body.get("producer") of the frame being handled. */
static void producer_value(ywp_node_t *n, unsigned tok, pvalue_t *pv)
{
    const ywp_json_t *d = &n->codec.json;
    memset(pv, 0, sizeof(*pv));
    if (tok == YWP_JSON_NONE || ywp_json_type(d, tok) == YWP_JSON_NULL) return;
    if (ywp_json_type(d, tok) == YWP_JSON_STRING) {
        size_t len = ywp_json_str_copy(d, tok, pv->id, sizeof(pv->id));
        if (len != SIZE_MAX && ywp_id_valid(pv->id, len)) {
            pv->kind = PVALUE_ID;
            return;
        }
        memset(pv->id, 0, sizeof(pv->id));
    }
    pv->kind = PVALUE_OTHER;
    ywp_codec_value_mac(&n->codec, tok, pv->fingerprint);
}

/* _note_rejection: pin (version, producer) and evict that cached intent. */
static void note_rejection(ywp_node_t *n, const char *consumer, const ywp_version_t *v,
                           const pvalue_t *producer)
{
    rejection_t *r = ywpn_tab_get(&n->rejections, consumer);
    if (!r) {
        r = ywpn_tab_insert(n, &n->rejections, consumer);
        r->version = *v;
        r->producer = *producer;
    } else if (ywp_version_cmp(v, &r->version) >= 0) {
        r->version = *v;
        r->producer = *producer;
    }
    long i = ywpn_tab_find(&n->intents, consumer);
    if (i >= 0 && ywp_version_cmp(&((intent_t *)ywpn_tab_row(&n->intents, (size_t)i))->version, v) == 0)
        ywpn_tab_remove(n, &n->intents, (size_t)i);
}

/* -- driver retries ------------------------------------------------------------- */

/* _peer_ready */
static bool peer_ready(const ywp_node_t *n)
{
    const peer_t *p = ywpn_tab_get(&n->peers, paired_producer(n));
    return p && p->role == YWP_PRODUCER && p->kind == n->kind && live(p) && ywpn_peer_media(n, p);
}

/* _schedule_retry: never postpones an already scheduled retry. */
static void schedule_retry(ywp_node_t *n, double now)
{
    if (n->role == YWP_CONSUMER && isnan(n->retry_at) && (n->teardown_pending || peer_ready(n)))
        n->retry_at = now + n->backoff;
}

static double doubled_backoff(double backoff)
{
    return backoff * 2 < YWP_RETRY_BACKOFF_CAP ? backoff * 2 : YWP_RETRY_BACKOFF_CAP;
}

static void attach_to(ywp_node_t *n, const char *producer, const char *media, size_t len)
{
    if (media != n->attached_media) memcpy(n->attached_media, media, len);
    n->attached = true;
    n->attached_len = len;
    ywpn_fx_attach(n, producer, n->attached_media, len);
}

/* _fire_retry */
static void fire_retry(ywp_node_t *n)
{
    if (n->teardown_pending) {
        n->backoff = doubled_backoff(n->backoff);
        ywpn_fx_detach(n);
        ywpn_send_evt(n, "driver-retry", "operation", "detach", 6);
        return;
    }
    if (!n->pairing.paired) return;
    const char *media = NULL;                       /* _retry_media */
    size_t len = 0;
    if (n->attached) {
        media = n->attached_media;
        len = n->attached_len;
    } else {
        const peer_t *p = ywpn_tab_get(&n->peers, n->pairing.producer);
        if (p && live(p) && (media = ywpn_peer_media(n, p))) len = p->media_len;
    }
    if (!media) return;
    n->backoff = doubled_backoff(n->backoff);
    attach_to(n, n->pairing.producer, media, len);
    ywpn_send_evt(n, "driver-retry", "producer", n->pairing.producer, strlen(n->pairing.producer));
}

/* _paired_producer_gone: the consumer's producer departed or timed out. */
static void paired_producer_gone(ywp_node_t *n, const char *peer, double now)
{
    if (n->role != YWP_CONSUMER || strcmp(paired_producer(n), peer) != 0 || !n->pairing.paired ||
        n->state == YWP_PENDING) return;
    if (n->teardown_pending && n->state == YWP_ERROR) return;
    n->state = YWP_PENDING;
    clear_err(n);
    if (!n->teardown_pending) n->retry_at = NAN;
    ywpn_send_evt(n, "media-pending", "producer", peer, strlen(peer));
    ywpn_send_ann(n, now);
}

/* -- data-plane admission (ADR 0024) --------------------------------------------- */

/* _note_source */
static void note_source(origin_t *o, const uint32_t *source)
{
    if (!source || (o->admitted_set && *source == o->admitted)) {
        o->candidate_set = false;
        o->count = 0;
        return;
    }
    if (o->candidate_set && *source == o->candidate) {
        ++o->count;
    } else {
        o->candidate = *source;
        o->candidate_set = true;
        o->count = 1;
    }
    if (o->count >= YWP_ADMISSION_CONFIRMATIONS) {
        o->admitted = *source;
        o->admitted_set = true;
        o->candidate_set = false;
        o->count = 0;
    }
}

static void log_ipv4(logline_t *l, uint32_t a)
{
    for (int shift = 24; shift >= 0; shift -= 8) {
        ywpn_log_u64(l, (a >> shift) & 0xffu);
        if (shift) ywpn_log_text(l, ".");
    }
}

/* _admission_update: publish the complete set when it changes. */
static void admission_update(ywp_node_t *n)
{
    if (!n->admission) return;
    ywp_admit_t *next = n->admitted + n->limits.peers;
    size_t count = 0;
    for (size_t i = 0; i < n->peers.count; ++i) {
        const peer_t *p = ywpn_tab_row(&n->peers, i);
        const origin_t *o = ywpn_tab_get(&n->origins, p->id);
        if (p->role != YWP_CONSUMER || p->kind != n->kind || !p->has_pairing ||
            !p->pairing.paired || strcmp(p->pairing.producer, n->id) != 0 || !live(p) ||
            !o || !o->admitted_set) continue;
        size_t at = count++;
        while (at > 0 && strcmp(next[at - 1].id, p->id) > 0) {   /* sorted by id */
            next[at] = next[at - 1];
            --at;
        }
        copy_id(next[at].id, p->id);
        next[at].addr = o->admitted;
    }
    bool same = count == n->admitted_count;
    for (size_t i = 0; same && i < count; ++i)
        same = strcmp(next[i].id, n->admitted[i].id) == 0 && next[i].addr == n->admitted[i].addr;
    if (same) return;
    memcpy(n->admitted, next, count * sizeof(*next));
    n->admitted_count = count;
    logline_t l;
    if (ywpn_log_begin(n, &l)) {
        ywpn_log_text(&l, "data-plane admission set: ");
        for (size_t i = 0; i < count; ++i) {
            if (i) ywpn_log_text(&l, ", ");
            ywpn_log_text(&l, n->admitted[i].id);
            ywpn_log_text(&l, "@");
            log_ipv4(&l, n->admitted[i].addr);
        }
        if (!count) ywpn_log_text(&l, "(none)");
    }
    ywpn_log_end(&l, YWP_LOG_INFO);
    ywpn_fx_admission(n);
}

/* -- clock diagnostics (ADR 0023) ------------------------------------------------- */

/* _floor_diagnostics, emitted with the start barrier. */
static void floor_diagnostics(ywp_node_t *n)
{
    uint64_t saved = n->boot_saved, secs = ywp_whole_seconds(n->boot_wall);
    uint64_t wall_ns = secs > UINT64_MAX / YWP_NS_PER_S ? UINT64_MAX : secs * YWP_NS_PER_S;
    uint64_t jump = (uint64_t)YWP_SEQ_FLOOR_JUMP_S * YWP_NS_PER_S;
    logline_t l;
    if (saved == 0) {
        if (ywpn_log_begin(n, &l)) {
            ywpn_log_text(&l, "sequence floor from wall clock ");
            ywpn_log_u64(&l, secs);
            ywpn_log_text(&l, " (no durable bound; commission only with synchronized time)");
        }
        ywpn_log_end(&l, YWP_LOG_INFO);
    } else if (saved <= UINT64_MAX - jump && wall_ns > saved + jump) {
        if (ywpn_log_begin(n, &l)) {
            ywpn_log_text(&l, "wall clock is ");
            ywpn_log_u64(&l, (wall_ns - saved) / YWP_NS_PER_S);
            ywpn_log_text(&l, "s past the durable sequence bound; restart floor advanced by "
                         "the 60s cap (ADR 0023)");
        }
        ywpn_log_end(&l, YWP_LOG_INFO);
    }
    if (saved > ywp_admission_bound(n->boot_wall)) {
        if (ywpn_log_begin(n, &l)) {
            ywpn_log_text(&l, "durable sequence bound is ");
            ywpn_log_u64(&l, (saved - wall_ns) / YWP_NS_PER_S);
            ywpn_log_text(&l, "s ahead of this wall clock: this clock is behind, or an earlier "
                         "boot persisted a future reservation; with the clock confirmed "
                         "correct, rebase the sequence (ADR 0023)");
        }
        ywpn_log_end(&l, YWP_LOG_WARNING);
    }
}

/* _future_drop_log: rate limited evidence that this clock is behind. */
static void future_drop_log(ywp_node_t *n, const ywp_frame_t *f, double wall, double now)
{
    if (now < n->future_log_at + FUTURE_LOG_INTERVAL) return;
    n->future_log_at = now;
    uint64_t secs = ywp_whole_seconds(wall), lead = f->seq / YWP_NS_PER_S;
    logline_t l;
    if (ywpn_log_begin(n, &l)) {
        ywpn_log_text(&l, "dropped ");
        ywpn_log_text(&l, ywp_frame_type_name(f->type));
        ywpn_log_text(&l, " from ");
        ywpn_log_text(&l, f->origin);
        ywpn_log_text(&l, ": sequence is ");
        ywpn_log_u64(&l, lead > secs ? lead - secs : 0);
        ywpn_log_text(&l, "s ahead of local wall clock ");
        ywpn_log_u64(&l, secs);
        ywpn_log_text(&l, " (margin 3600s); if peers are correct this clock is behind - "
                     "check time synchronization (ADR 0023)");
    }
    ywpn_log_end(&l, YWP_LOG_WARNING);
}

/* _own_seq_log */
static void own_seq_log(ywp_node_t *n, double wall, double now)
{
    uint64_t bound = ywp_admission_bound(wall);
    if (n->seq <= bound || now < n->own_seq_log_at + OWN_SEQ_LOG_INTERVAL) return;
    n->own_seq_log_at = now;
    logline_t l;
    if (ywpn_log_begin(n, &l)) {
        ywpn_log_text(&l, "own sequence is ");
        ywpn_log_u64(&l, (n->seq - bound) / YWP_NS_PER_S);
        ywpn_log_text(&l, "s beyond the future-admission bound of this wall clock; peers whose "
                     "clocks agree drop this node's frames. If the clock is correct, "
                     "rebase the sequence (ADR 0023)");
    }
    ywpn_log_end(&l, YWP_LOG_WARNING);
}

/* -- pairing ------------------------------------------------------------------------- */

static void log_pairing(logline_t *l, const ywp_pairing_t *p)
{
    ywpn_log_text(l, "producer=");
    ywpn_log_text(l, p->paired ? p->producer : "(none)");
    ywpn_log_text(l, " version=[");
    ywpn_log_u64(l, p->version.n);
    ywpn_log_text(l, ",");
    ywpn_log_text(l, p->version.origin);
    ywpn_log_text(l, "]");
}

/* _commit_pairing: after the durable save of an applied intent. */
static void commit_pairing(ywp_node_t *n, const ywp_pairing_t *next, double now)
{
    ywp_pairing_t previous = n->pairing;
    bool had_media = n->attached;
    n->pairing = *next;
    logline_t l;
    if (ywpn_log_begin(n, &l)) {
        ywpn_log_text(&l, "pairing committed: ");
        log_pairing(&l, next);
        ywpn_log_text(&l, " (previous ");
        log_pairing(&l, &previous);
        ywpn_log_text(&l, ")");
    }
    ywpn_log_end(&l, YWP_LOG_INFO);
    n->state = YWP_PENDING;
    clear_err(n);
    set_detail(n, "", 0);
    n->attached = false;
    n->retry_at = NAN;
    n->backoff = YWP_RETRY_BACKOFF_BASE;
    n->teardown_pending = !next->paired || had_media || previous.paired;
    if (n->teardown_pending) ywpn_fx_detach(n);
    if (!next->paired) {
        ywpn_send_ack(n, &next->version, true, "applied");
        ywpn_send_evt_pairing(n, next);
        ywpn_send_ann(n, now);
        return;
    }
    const peer_t *p = ywpn_tab_get(&n->peers, next->producer);
    const char *media = p && live(p) ? ywpn_peer_media(n, p) : NULL;
    if (media) {
        n->teardown_pending = false;
        attach_to(n, next->producer, media, p->media_len);
    } else {
        ywpn_send_evt(n, "media-pending", "producer", next->producer, strlen(next->producer));
    }
    ywpn_send_ack(n, &next->version, true, "applied");
    ywpn_send_evt_pairing(n, next);
    ywpn_send_ann(n, now);
}

/* _apply_intent: the consumer judges an intent addressed to itself. */
static void apply_intent(ywp_node_t *n, bool pair, const pvalue_t *producer,
                         const ywp_version_t *v)
{
    int order = ywp_version_cmp(v, &n->pairing.version);
    if (order < 0) {
        ywpn_send_ack(n, v, false, "superseded");
        return;
    }
    if (order == 0) {                   /* first application wins; acks repeat */
        bool same = pair == n->pairing.paired &&
                    (!pair || (producer->kind == PVALUE_ID &&
                               strcmp(producer->id, n->pairing.producer) == 0));
        ywpn_send_ack(n, v, same, same ? "applied" : "superseded");
        return;
    }
    ywp_pairing_t next;
    memset(&next, 0, sizeof(next));
    next.version = *v;
    if (pair) {
        const char *reason = NULL;
        if (producer->kind != PVALUE_ID) {
            reason = "unknown-producer";
        } else if (strcmp(producer->id, n->id) == 0) {
            reason = "self-pair";
        } else {
            const peer_t *p = ywpn_tab_get(&n->peers, producer->id);
            if (p && (p->kind != n->kind || p->role != YWP_PRODUCER)) reason = "kind-mismatch";
        }
        if (reason) {
            note_rejection(n, n->id, v, producer);
            ywpn_send_ack(n, v, false, reason);
            return;
        }
        next.paired = true;
        copy_id(next.producer, producer->id);
    }
    (void)ywpn_begin_save(n, SAVE_PAIRING, &next);
}

/* -- frame handlers ----------------------------------------------------------------- */

/* _parse_version */
static bool parse_version(const ywp_json_t *d, unsigned tok, uint64_t min_n, ywp_version_t *v)
{
    if (tok == YWP_JSON_NONE || ywp_json_type(d, tok) != YWP_JSON_ARRAY || ywp_json_count(d, tok) != 2)
        return false;
    unsigned count = ywp_json_at(d, tok, 0), origin = ywp_json_at(d, tok, 1);
    if (!ywp_json_uint(d, count, YWP_MAX_SEQUENCE, &v->n) || v->n < min_n ||
        ywp_json_type(d, origin) != YWP_JSON_STRING) return false;
    size_t len = ywp_json_str_copy(d, origin, v->origin, sizeof(v->origin));
    if (len == SIZE_MAX) return false;
    return v->n == 0 ? len == 0 : ywp_id_valid(v->origin, len);
}

static int str_choice(const ywp_json_t *d, unsigned tok, const char *const *names, int count)
{
    if (tok == YWP_JSON_NONE || ywp_json_type(d, tok) != YWP_JSON_STRING) return -1;
    for (int i = 0; i < count; ++i)
        if (ywp_json_str_is(d, tok, names[i])) return i;
    return -1;
}

/* _parse_ann: false for a malformed body. */
static bool parse_ann(const ywp_json_t *d, unsigned body, peer_t *rec, unsigned *media)
{
    static const char *const roles[] = {"producer", "consumer"};
    static const char *const kinds[] = {"hdmi", "usb"};
    static const char *const states[] = {"idle", "pending", "active", "error"};
    unsigned role_tok = ywp_json_get(d, body, "role"), kind_tok = ywp_json_get(d, body, "kind");
    unsigned state_tok = ywp_json_get(d, body, "state");
    int role = str_choice(d, role_tok, roles, 2), kind = str_choice(d, kind_tok, kinds, 2);
    int state = str_choice(d, state_tok, states, 4);
    unsigned name = ywp_json_get(d, body, "name"), err = ywp_json_get(d, body, "err");
    if (role < 0 || kind < 0 || state < 0) return false;
    if (name == YWP_JSON_NONE || ywp_json_type(d, name) != YWP_JSON_STRING ||
        ywp_json_str_chars(d, name) > YWP_NAME_MAX) return false;
    if (err != YWP_JSON_NONE && ywp_json_type(d, err) != YWP_JSON_NULL &&
        ywp_json_type(d, err) != YWP_JSON_STRING) return false;
    rec->role = (uint8_t)role;
    rec->kind = (uint8_t)kind;
    rec->state = (uint8_t)state;
    *media = YWP_JSON_NONE;
    if (role == YWP_CONSUMER) {
        unsigned pairing = ywp_json_get(d, body, "pairing");
        if (pairing == YWP_JSON_NONE || ywp_json_type(d, pairing) != YWP_JSON_OBJECT) return false;
        unsigned producer = ywp_json_get(d, pairing, "producer");
        if (producer != YWP_JSON_NONE && ywp_json_type(d, producer) != YWP_JSON_NULL) {
            if (ywp_json_type(d, producer) != YWP_JSON_STRING) return false;
            size_t len = ywp_json_str_copy(d, producer, rec->pairing.producer,
                                           sizeof(rec->pairing.producer));
            if (len == SIZE_MAX || !ywp_id_valid(rec->pairing.producer, len)) return false;
            rec->pairing.paired = true;
        }
        if (!parse_version(d, ywp_json_get(d, pairing, "version"), 0, &rec->pairing.version))
            return false;
        if (rec->pairing.version.n == 0 && rec->pairing.paired) return false;
        rec->has_pairing = true;
    } else {
        unsigned m = ywp_json_get(d, body, "media");
        if (m != YWP_JSON_NONE && ywp_json_type(d, m) != YWP_JSON_NULL) {
            if (ywp_json_type(d, m) != YWP_JSON_OBJECT) return false;
            *media = m;
        }
    }
    return true;
}

/* Canonical media of an announce into n->scratch; 0 if absent or too long
 * for local storage (non-integer numbers can expand; README subset). */
static size_t announced_media(ywp_node_t *n, unsigned tok)
{
    if (tok == YWP_JSON_NONE) return 0;
    ywp_out_t o;
    ywp_out_buffer(&o, (uint8_t *)n->scratch, sizeof(n->scratch));
    ywp_json_canonical(&n->codec.json, tok, YWP_JSON_NONE, &o);
    return ywp_out_finish(&o) ? o.len : 0;
}

/* _on_ann */
static void on_ann(ywp_node_t *n, const ywp_frame_t *f, double now)
{
    peer_t rec;
    unsigned media_tok;
    memset(&rec, 0, sizeof(rec));
    if (!parse_ann(&n->codec.json, f->body, &rec, &media_tok)) {
        ++n->drops[YWP_DROP_MALFORMED];
        log_id_line(n, YWP_LOG_INFO, "malformed ann body from ", f->origin, "");
        return;
    }
    size_t media_len = 0;
    bool consumer = n->role == YWP_CONSUMER;
    if (media_tok != YWP_JSON_NONE) {
        rec.has_media = true;
        if (consumer && !(media_len = announced_media(n, media_tok))) {
            rec.has_media = false;
            log_id_line(n, YWP_LOG_WARNING, "media of ", f->origin,
                        " exceeds local storage when canonical; treated as null");
        }
    }
    peer_t *p = ywpn_tab_get(&n->peers, f->origin);
    bool existed = p != NULL, returned = p && !live(p);
    ywp_state_t prev_state = p ? (ywp_state_t)p->state : YWP_IDLE;
    if (!p) p = ywpn_tab_insert(n, &n->peers, f->origin);
    bool same_media = media_len && p->media_len == media_len &&
                      memcmp(ywpn_peer_media(n, p), n->scratch, media_len) == 0;
    p->role = rec.role;
    p->kind = rec.kind;
    p->state = rec.state;
    p->departed = p->lost = false;
    p->has_pairing = rec.has_pairing;
    p->pairing = rec.pairing;
    p->has_media = rec.has_media;
    p->last_seen = now;
    if (!media_len) ywpn_media_free(n, p);
    else if (!same_media) ywpn_media_store(n, p, n->scratch, media_len);

    if (returned) {
        logline_t l;
        if (ywpn_log_begin(n, &l)) {
            ywpn_log_text(&l, "peer ");
            ywpn_log_text(&l, f->origin);
            ywpn_log_text(&l, " returned (role ");
            ywpn_log_text(&l, ywp_role_name(rec.role));
            ywpn_log_text(&l, ", kind ");
            ywpn_log_text(&l, ywp_kind_name(rec.kind));
            ywpn_log_text(&l, ", state ");
            ywpn_log_text(&l, ywp_state_name(rec.state));
            ywpn_log_text(&l, ")");
        }
        ywpn_log_end(&l, YWP_LOG_INFO);
        ywpn_send_evt(n, "peer-returned", "peer", f->origin, strlen(f->origin));
    }
    if (consumer && n->pairing.paired && strcmp(n->pairing.producer, f->origin) == 0) {
        const char *media = ywpn_peer_media(n, p);
        if (rec.role != YWP_PRODUCER || rec.kind != n->kind) {
            /* Not (or no longer) a compatible producer: never attach it. */
            if (n->attached) {
                n->attached = false;
                n->teardown_pending = true;
                ywpn_fx_detach(n);
            }
            paired_producer_gone(n, f->origin, now);
        } else if (media && (returned || !n->attached || n->attached_len != p->media_len ||
                             memcmp(n->attached_media, media, p->media_len) != 0)) {
            /* A changed description invalidates readiness first. */
            n->teardown_pending = false;
            n->retry_at = NAN;
            n->backoff = YWP_RETRY_BACKOFF_BASE;
            n->state = YWP_PENDING;
            clear_err(n);
            set_detail(n, "", 0);
            ywpn_send_evt(n, "media-pending", "producer", f->origin, strlen(f->origin));
            ywpn_send_ann(n, now);
            attach_to(n, f->origin, media, p->media_len);
        } else if (media && rec.state == YWP_ACTIVE && existed && prev_state != YWP_ACTIVE &&
                   !n->teardown_pending && (n->state == YWP_ERROR || !isnan(n->retry_at))) {
            /* Producer readiness edge: retry now, not after the backoff. */
            n->backoff = YWP_RETRY_BACKOFF_BASE;
            n->retry_at = NAN;
            logline_t l;
            if (ywpn_log_begin(n, &l)) {
                ywpn_log_text(&l, "producer ");
                ywpn_log_text(&l, f->origin);
                ywpn_log_text(&l, " became active after ");
                ywpn_log_text(&l, ywp_state_name(prev_state));
                ywpn_log_text(&l, "; retrying attachment now");
            }
            ywpn_log_end(&l, YWP_LOG_INFO);
            fire_retry(n);
        }
    }
    if (rec.role == YWP_CONSUMER) {               /* anti-entropy trigger */
        intent_t *e = cache_get(n, f->origin, now);
        if (e) {
            if (ywp_version_cmp(&rec.pairing.version, &e->version) < 0) {
                if (!e->resend) {
                    e->resend = true;
                    e->resend_order = ++n->resend_serial;
                    e->resend_at = now + ywp_mt_uniform(&n->rng, YWP_SUPPRESSION_MIN,
                                                        YWP_SUPPRESSION_MAX);
                }
            } else {
                e->resend = false;
            }
        }
    }
}

/* _on_int */
static void on_int(ywp_node_t *n, const ywp_frame_t *f, double now)
{
    static const char *const ops[] = {"pair", "unpair"};
    const ywp_json_t *d = &n->codec.json;
    char consumer[YWP_ID_MAX + 1];
    ywp_version_t v;
    int op = str_choice(d, ywp_json_get(d, f->body, "op"), ops, 2);
    unsigned ctok = ywp_json_get(d, f->body, "consumer");
    size_t clen = ctok == YWP_JSON_NONE || ywp_json_type(d, ctok) != YWP_JSON_STRING
                      ? SIZE_MAX : ywp_json_str_copy(d, ctok, consumer, sizeof(consumer));
    if (op < 0 || clen == SIZE_MAX || !ywp_id_valid(consumer, clen) ||
        !parse_version(d, ywp_json_get(d, f->body, "version"), 1, &v)) {
        ++n->drops[YWP_DROP_MALFORMED];
        log_id_line(n, YWP_LOG_INFO, "malformed intent body from ", f->origin, "");
        return;
    }
    bool pair = op == 0;
    pvalue_t producer;
    producer_value(n, ywp_json_get(d, f->body, "producer"), &producer);
    /* Bystanders keep only normalized, re-issuable intents (ADR 0012). */
    bool cacheable = !pair || producer.kind == PVALUE_ID;
    const rejection_t *r = ywpn_tab_get(&n->rejections, consumer);
    bool rejected = r && ywp_version_cmp(&v, &r->version) == 0 && pvalue_eq(&producer, &r->producer);
    intent_t *e = cache_get(n, consumer, now);
    if (cacheable && (!e || ywp_version_cmp(&v, &e->version) > 0) && !rejected) {
        if (!e) e = ywpn_tab_insert(n, &n->intents, consumer);
        e->pair = pair;
        if (pair) copy_id(e->producer, producer.id);
        else e->producer[0] = 0;
        e->version = v;
        e->cached_at = now;
    }
    intent_t *cached = ywpn_tab_get(&n->intents, consumer);
    if (cached && cached->resend && ywp_version_cmp(&v, &cached->version) >= 0)
        cached->resend = false;                   /* suppression */
    if (n->role == YWP_CONSUMER && strcmp(consumer, n->id) == 0)
        apply_intent(n, pair, &producer, &v);
}

/* _on_ack: rejection acks evict exactly the rejected cached intent. */
static void on_ack(ywp_node_t *n, const ywp_frame_t *f)
{
    static const char *const rejections[] = {"self-pair", "kind-mismatch", "unknown-producer"};
    const ywp_json_t *d = &n->codec.json;
    ywp_version_t v;
    unsigned applied = ywp_json_get(d, f->body, "applied");
    if (!parse_version(d, ywp_json_get(d, f->body, "version"), 1, &v) ||
        applied == YWP_JSON_NONE || ywp_json_type(d, applied) != YWP_JSON_FALSE) return;
    unsigned reason = ywp_json_get(d, f->body, "reason");
    if (reason == YWP_JSON_NONE || ywp_json_type(d, reason) != YWP_JSON_STRING) {
        ++n->drops[YWP_DROP_MALFORMED];
        return;
    }
    if (str_choice(d, reason, rejections, 3) < 0) return;
    const intent_t *e = ywpn_tab_get(&n->intents, f->origin);
    if (e && ywp_version_cmp(&e->version, &v) == 0) {
        pvalue_t producer;
        memset(&producer, 0, sizeof(producer));
        if (e->pair) {
            producer.kind = PVALUE_ID;
            copy_id(producer.id, e->producer);
        }
        note_rejection(n, f->origin, &v, &producer);
    }
}

/* _on_bye */
static void on_bye(ywp_node_t *n, const ywp_frame_t *f, double now)
{
    peer_t *p = ywpn_tab_get(&n->peers, f->origin);
    if (!p || !live(p)) {
        if (p) p->departed = true;
        return;
    }
    p->departed = true;
    logline_t l;
    if (ywpn_log_begin(n, &l)) {
        ywpn_log_text(&l, "peer ");
        ywpn_log_text(&l, f->origin);
        ywpn_log_text(&l, " departed (bye; role ");
        ywpn_log_text(&l, ywp_role_name((ywp_role_t)p->role));
        ywpn_log_text(&l, ", last state ");
        ywpn_log_text(&l, ywp_state_name((ywp_state_t)p->state));
        ywpn_log_text(&l, ")");
    }
    ywpn_log_end(&l, YWP_LOG_INFO);
    ywpn_send_evt_peer_lost(n, f->origin, "bye");
    paired_producer_gone(n, f->origin, now);
}

/* -- inputs ------------------------------------------------------------------------- */

void ywp_node_start(ywp_node_t *n, double now)
{
    ywpn_fx_begin(n);
    touch(n, now);
    size_t base = n->fx_len;
    if (!n->started && !n->save_pending) {
        if (n->boot_logs) floor_diagnostics(n);
        n->boot_logs = false;
        (void)ywpn_begin_save(n, SAVE_START, &n->pairing);   /* the barrier comes last */
    }
    ywpn_fx_publish(n, base);
}

void ywp_node_persisted(ywp_node_t *n, uint32_t request_id, bool ok, double now)
{
    ywpn_fx_begin(n);
    touch(n, now);
    size_t base = n->fx_len;
    if (!n->save_pending || n->save_id != request_id) {
        ywpn_fx_publish(n, base);
        return;
    }
    n->save_pending = false;
    if (!ok) {
        if (n->save_kind == SAVE_EFFECTS) {
            n->deferred = true;
            n->reserve_retry_at = now + n->reserve_backoff;
            n->reserve_backoff = doubled_backoff(n->reserve_backoff);
        }
        ywpn_log_static(n, YWP_LOG_WARNING, "durable save failed; pairing unchanged and success withheld");
        ywpn_fx_publish(n, base);
        return;
    }
    n->reserved_until = n->save_state.seq;
    n->reserve_backoff = YWP_RETRY_BACKOFF_BASE;
    switch (n->save_kind) {
    case SAVE_START:
        n->started = true;
        if (n->teardown_pending) ywpn_fx_detach(n);
        ywpn_send_evt(n, "started", NULL, NULL, 0);
        ywpn_send_ann(n, now);
        if (n->admission) ywpn_fx_admission(n);    /* fail closed: start empty */
        ywpn_fx_finish(n, base);
        return;
    case SAVE_PAIRING:
        commit_pairing(n, &n->save_state.pairing, now);
        ywpn_fx_finish(n, base);
        return;
    default:                                  /* release the held batch */
        n->vis_begin = 0;
        n->vis_end = n->held_end;
        n->held_end = n->held_arena = 0;
        return;
    }
}

void ywp_node_datagram(ywp_node_t *n, const uint8_t *raw, size_t len, double now,
                       double wall, const uint32_t *source)
{
    ywpn_fx_begin(n);
    touch(n, now);
    size_t base = n->fx_len;
    ywp_frame_t f;
    ywp_drop_t verdict = ywp_decode(&n->codec, raw, len, &f);
    if (verdict != YWP_OK) {                         /* on_drop */
        ++n->drops[verdict];
        ywpn_fx_publish(n, base);
        return;
    }
    double current_wall = wall_now(n, now, wall);
    if (!active_input(n) || strcmp(f.origin, n->id) == 0) {   /* loopback */
        ywpn_fx_publish(n, base);
        return;
    }
    if (f.seq > ywp_admission_bound(current_wall)) {
        /* Implausibly far-future: reject without advancing the mark. */
        ++n->drops[YWP_DROP_STALE];
        future_drop_log(n, &f, current_wall, now);
        ywpn_fx_publish(n, base);
        return;
    }
    ywp_frame_fingerprint(&n->codec, &f);
    origin_t *o = ywpn_tab_get(&n->origins, f.origin);
    if (o && f.seq <= o->seq) {
        bool dup = f.seq == o->seq && memcmp(o->fingerprint, f.fingerprint, YWP_SHA256_LEN) == 0;
        ++n->drops[dup ? YWP_DROP_DUP : YWP_DROP_STALE];
        ywpn_fx_publish(n, base);
        return;
    }
    if (!o) o = ywpn_tab_insert(n, &n->origins, f.origin);
    o->seq = f.seq;
    memcpy(o->fingerprint, f.fingerprint, YWP_SHA256_LEN);
    if (n->admission) note_source(o, source);
    switch (f.type) {
    case YWP_ANN: on_ann(n, &f, now); break;
    case YWP_INT: on_int(n, &f, now); break;
    case YWP_ACK: on_ack(n, &f); break;
    case YWP_BYE: on_bye(n, &f, now); break;
    case YWP_EVT: break;
    }
    ywpn_bound_maps(n);
    admission_update(n);
    ywpn_fx_finish(n, base);
}

void ywp_node_driver_status(ywp_node_t *n, ywp_state_t status, const char *detail_in,
                            size_t detail_len, double now)
{
    ywpn_fx_begin(n);
    touch(n, now);
    size_t base = n->fx_len;
    if (!active_input(n)) {
        ywpn_fx_publish(n, base);
        return;
    }
    if (!detail_in) detail_len = 0;
    uint8_t digest[YWP_SHA256_LEN];
    detail_digest(n, detail_in, detail_len, digest);
    if (!n->logged || n->logged_state != status ||
        memcmp(n->logged_digest, digest, sizeof(digest)) != 0) {
        /* Full backend detail stays local; the wire copy is bounded. */
        n->logged = true;
        n->logged_state = status;
        memcpy(n->logged_digest, digest, sizeof(digest));
        logline_t l;
        if (ywpn_log_begin(n, &l)) {
            ywpn_log_text(&l, "driver ");
            ywpn_log_text(&l, ywp_state_name(status));
            ywpn_log_text(&l, ": ");
            if (detail_len) ywpn_log_bytes(&l, detail_in, detail_len);
            else ywpn_log_text(&l, "(no detail)");
        }
        ywpn_log_end(&l, status == YWP_ERROR ? YWP_LOG_WARNING : YWP_LOG_INFO);
    }
    char bounded[YWP_DETAIL_MAX + 1];
    size_t blen = bounded_detail(detail_in, detail_len, bounded);
    ywp_state_t prev = n->state;
    bool consumer = n->role == YWP_CONSUMER;
    if (consumer && !n->pairing.paired) {
        if (status == YWP_IDLE) {
            n->teardown_pending = false;
            n->retry_at = NAN;
            n->backoff = YWP_RETRY_BACKOFF_BASE;
        } else {
            n->teardown_pending = true;
            schedule_retry(n, now);
            if (status == YWP_ACTIVE) {
                /* Evidence of unfinished cleanup, never of a desired route. */
                if (prev != YWP_PENDING && prev != YWP_ERROR) {
                    n->state = YWP_PENDING;
                    ywpn_send_evt(n, "media-pending", NULL, NULL, 0);
                    ywpn_send_ann(n, now);
                }
                ywpn_fx_finish(n, base);
                return;
            }
        }
    }
    if (status == YWP_ACTIVE) {
        if (consumer && n->teardown_pending) {
            schedule_retry(n, now);
            ywpn_fx_finish(n, base);
            return;
        }
        if (consumer) {
            const peer_t *p = ywpn_tab_get(&n->peers, paired_producer(n));
            if (!p || !live(p) || p->role != YWP_PRODUCER || p->kind != n->kind) {
                ywpn_fx_publish(n, base);
                return;
            }
        }
        set_detail(n, bounded, blen);
        n->teardown_pending = false;
        clear_err(n);
        n->backoff = YWP_RETRY_BACKOFF_BASE;
        n->retry_at = NAN;
        if (prev != YWP_ACTIVE) {
            n->state = YWP_ACTIVE;
            ywpn_send_evt(n, "media-up", blen ? "detail" : NULL, n->detail, blen);
        }
    } else if (status == YWP_ERROR) {
        set_detail(n, bounded, blen);
        n->state = YWP_ERROR;
        n->has_err = blen > 0;
        memcpy(n->err, bounded, blen + 1);
        n->err_len = blen;
        ywpn_send_evt(n, "driver-error", "detail", n->detail, blen);
        schedule_retry(n, now);
    } else if (status == YWP_PENDING) {
        set_detail(n, bounded, blen);
        if (prev != YWP_PENDING) {
            n->state = YWP_PENDING;
            clear_err(n);
            ywpn_send_evt(n, "media-pending", NULL, NULL, 0);
        }
        schedule_retry(n, now);
    } else if (consumer && n->pairing.paired) {
        n->teardown_pending = false;
        set_detail(n, bounded, blen);
        if (prev != YWP_PENDING) {
            n->state = YWP_PENDING;
            clear_err(n);
            ywpn_send_evt(n, "media-pending", "producer", n->pairing.producer,
                     strlen(n->pairing.producer));
        }
        schedule_retry(n, now);
    } else if (prev != YWP_IDLE) {
        set_detail(n, "", 0);
        n->state = YWP_IDLE;
        clear_err(n);
        ywpn_send_evt(n, "media-down", NULL, NULL, 0);
    }
    if (n->state != prev) ywpn_send_ann(n, now);
    ywpn_fx_finish(n, base);
}

/* _validate_media_description: a producer's opaque description is a JSON
 * object (or null) without non-integer numbers whose announce, in the
 * longest readiness state without optional text, fits the frame budget.
 * Consumers announce media null, so only their mandatory fields count.
 * On success the canonical description is in n->scratch. */
ywp_err_t ywpn_check_media(ywp_node_t *n, const char *json, size_t len, size_t *out_len,
                      bool *present)
{
    ywp_json_t *d = &n->codec.json;
    *present = false;
    *out_len = 0;
    if (n->role == YWP_PRODUCER && json && len) {
        if (!ywp_json_parse(d, (const uint8_t *)json, len)) return YWP_E_MEDIA;
        if (ywp_json_type(d, 0) == YWP_JSON_OBJECT) {
            for (unsigned i = 0; i < d->count; ++i)
                if (d->tok[i].type == YWP_JSON_FLOAT) return YWP_E_MEDIA;
            if (d->depth > YWP_JSON_MAX_DEPTH - 2) return YWP_E_MEDIA;
            ywp_out_t o;
            ywp_out_buffer(&o, (uint8_t *)n->scratch, sizeof(n->scratch));
            ywp_json_canonical(d, 0, YWP_JSON_NONE, &o);
            if (!ywp_out_finish(&o)) return YWP_E_MEDIA;
            *out_len = o.len;
            *present = true;
        } else if (ywp_json_type(d, 0) != YWP_JSON_NULL) {
            return YWP_E_MEDIA;
        }
    }
    ywp_out_t o;
    ywp_out_buffer(&o, NULL, 0);
    size_t body = ywpn_ann_body(n, &o, YWP_PENDING, *present ? n->scratch : NULL, *out_len,
                           false, false, 0);
    return n->overhead + body > YWP_MAX_FRAME ? YWP_E_MEDIA : YWP_E_OK;
}

void ywpn_adopt_media(ywp_node_t *n, bool present, size_t len)
{
    n->has_media = present;
    n->media_len = present ? len : 0;
    if (present) memcpy(n->media, n->scratch, len);
}

ywp_err_t ywp_node_set_media(ywp_node_t *n, const char *json, size_t len, double now)
{
    ywpn_fx_begin(n);
    touch(n, now);
    size_t base = n->fx_len, media_len;
    bool present;
    ywp_err_t err = ywpn_check_media(n, json, len, &media_len, &present);
    if (err == YWP_E_OK) {
        ywpn_adopt_media(n, present, media_len);
        if (active_input(n)) ywpn_send_ann(n, now);
    }
    ywpn_fx_finish(n, base);
    return err;
}

/* _prune: forget peers lost or departed for longer than PEER_FORGET, with
 * their replay marks; never the paired producer. */
static void prune(ywp_node_t *n, double now)
{
    for (size_t i = 0; i < n->peers.count;) {
        peer_t *p = ywpn_tab_row(&n->peers, i);
        if (strcmp(p->id, paired_producer(n)) != 0 && !live(p) &&
            now - p->last_seen > YWP_PEER_FORGET) {
            long o = ywpn_tab_find(&n->origins, p->id);
            if (o >= 0) ywpn_tab_remove(n, &n->origins, (size_t)o);
            ywpn_tab_remove(n, &n->peers, i);
        } else {
            ++i;
        }
    }
    ywpn_bound_maps(n);
}

static bool drops_changed(const ywp_node_t *n)
{
    for (unsigned k = YWP_DROP_MALFORMED; k < YWP_DROP_COUNT; ++k)
        if (k != YWP_DROP_DUP && n->drops[k] != n->reported[k]) return true;
    return false;
}

/* The due resend with the earliest scheduling (the _resend_at order). */
static intent_t *next_due_resend(ywp_node_t *n, double now)
{
    intent_t *best = NULL;
    for (size_t i = 0; i < n->intents.count; ++i) {
        intent_t *e = ywpn_tab_row(&n->intents, i);
        if (e->resend && e->resend_at <= now && (!best || e->resend_order < best->resend_order))
            best = e;
    }
    return best;
}

static void poll_steps(ywp_node_t *n, double now, double wall)
{
    own_seq_log(n, wall, now);
    for (size_t i = 0; i < n->peers.count; ++i) {
        peer_t *p = ywpn_tab_row(&n->peers, i);
        if (!live(p) || !(now - p->last_seen > YWP_PEER_TIMEOUT)) continue;
        if (!ywpn_fx_room(n, 5, STEP_PEER_BYTES)) { n->poll_more = true; return; }
        p->lost = true;
        logline_t l;
        if (ywpn_log_begin(n, &l)) {
            ywpn_log_text(&l, "peer ");
            ywpn_log_text(&l, p->id);
            ywpn_log_text(&l, " lost: no announce for ");
            ywpn_log_u64(&l, (uint64_t)((now - p->last_seen) * 10 + 0.5) / 10);
            ywpn_log_text(&l, "s (role ");
            ywpn_log_text(&l, ywp_role_name((ywp_role_t)p->role));
            ywpn_log_text(&l, ", kind ");
            ywpn_log_text(&l, ywp_kind_name((ywp_kind_t)p->kind));
            ywpn_log_text(&l, ", last state ");
            ywpn_log_text(&l, ywp_state_name((ywp_state_t)p->state));
            ywpn_log_text(&l, ")");
        }
        ywpn_log_end(&l, YWP_LOG_WARNING);
        ywpn_send_evt_peer_lost(n, p->id, "timeout");
        paired_producer_gone(n, p->id, now);
    }
    if (!ywpn_fx_room(n, 3, LOG_MAX)) { n->poll_more = true; return; }
    prune(n, now);
    admission_update(n);
    if (!isnan(n->retry_at) && now >= n->retry_at) {
        if (!ywpn_fx_room(n, 3, STEP_RETRY_BYTES)) { n->poll_more = true; return; }
        n->retry_at = NAN;
        fire_retry(n);
    }
    for (intent_t *e; (e = next_due_resend(n, now)) != NULL;) {
        if (!ywpn_fx_room(n, 1, EVT_BYTES)) { n->poll_more = true; return; }
        e->resend = false;
        /* Re-issue under this node's own origin and seq (ADR 0009). */
        e = cache_get(n, e->id, now);
        if (e) ywpn_send_int(n, e);
    }
    if (now >= n->ann_due) {
        if (!ywpn_fx_room(n, 1, ANN_BYTES)) { n->poll_more = true; return; }
        ywpn_send_ann(n, now);
    }
    if (drops_changed(n) && now >= n->rx_drop_at + YWP_ANN_INTERVAL) {
        if (!ywpn_fx_room(n, 2, LOG_MAX + EVT_BYTES)) { n->poll_more = true; return; }
        bool hostile = n->drops[YWP_DROP_AUTH] > n->reported[YWP_DROP_AUTH] ||
                       n->drops[YWP_DROP_STALE] > n->reported[YWP_DROP_STALE];
        memcpy(n->reported, n->drops, sizeof(n->reported));
        n->rx_drop_at = now;
        logline_t l;
        if (ywpn_log_begin(n, &l)) {
            static const ywp_drop_t order[] = {YWP_DROP_MALFORMED, YWP_DROP_VERSION, YWP_DROP_AUTH,
                                               YWP_DROP_STALE, YWP_DROP_OVERSIZE, YWP_DROP_DUP};
            ywpn_log_text(&l, "rx-drop counters");
            for (unsigned k = 0; k < 6; ++k) {
                ywpn_log_text(&l, " ");
                ywpn_log_text(&l, ywp_drop_name(order[k]));
                ywpn_log_text(&l, "=");
                ywpn_log_u64(&l, n->drops[order[k]]);
            }
        }
        ywpn_log_end(&l, hostile ? YWP_LOG_WARNING : YWP_LOG_INFO);
        ywpn_send_evt_rx_drop(n);
    }
}

void ywp_node_poll(ywp_node_t *n, double now, double wall)
{
    ywpn_fx_begin(n);
    touch(n, now);
    double current_wall = wall_now(n, now, wall);
    size_t base = n->fx_len;
    if (n->save_pending || !n->started) {
        ywpn_fx_publish(n, base);
        return;
    }
    if (n->deferred) {                    /* retry the held reservation */
        if (!isnan(n->reserve_retry_at) && now >= n->reserve_retry_at) {
            n->deferred = false;
            n->reserve_retry_at = NAN;
            (void)ywpn_begin_save(n, SAVE_EFFECTS, &n->pairing);
        }
        ywpn_fx_publish(n, base);
        return;
    }
    poll_steps(n, now, current_wall);
    ywpn_fx_finish(n, base);
}

void ywp_node_shutdown(ywp_node_t *n, double now)
{
    ywpn_fx_begin(n);
    touch(n, now);
    size_t base = n->fx_len;
    if (n->role == YWP_CONSUMER) ywpn_fx_detach(n);
    if (!active_input(n)) {
        ywpn_fx_publish(n, base);
        return;
    }
    size_t from = n->fx_len;
    ywpn_send_evt(n, "stopping", NULL, NULL, 0);
    ywpn_send_bye(n);
    ywpn_fx_finish(n, from);
}

double ywp_node_next_deadline(const ywp_node_t *n)
{
    if (n->poll_more) return n->last_now;
    if (n->save_pending || !n->started) return n->last_now + YWP_ANN_INTERVAL;
    if (!isnan(n->reserve_retry_at)) return n->reserve_retry_at;
    double deadline = n->ann_due;
    for (size_t i = 0; i < n->peers.count; ++i) {
        const peer_t *p = ywpn_tab_row(&n->peers, i);
        double at = p->last_seen + YWP_PEER_TIMEOUT + TIMEOUT_EPS;
        if (live(p) && at < deadline) deadline = at;
    }
    if (!isnan(n->retry_at) && n->retry_at < deadline) deadline = n->retry_at;
    for (size_t i = 0; i < n->intents.count; ++i) {
        const intent_t *e = ywpn_tab_row(&n->intents, i);
        if (e->resend && e->resend_at < deadline) deadline = e->resend_at;
    }
    if (drops_changed(n) && n->rx_drop_at + YWP_ANN_INTERVAL < deadline)
        deadline = n->rx_drop_at + YWP_ANN_INTERVAL;
    return deadline > n->last_now ? deadline : n->last_now;
}

/* -- effects -------------------------------------------------------------------------- */

size_t ywp_node_effects(const ywp_node_t *n, const ywp_effect_t **effects)
{
    *effects = n->fx + n->vis_begin;
    return n->vis_end - n->vis_begin;
}

size_t ywp_node_encode(ywp_node_t *n, const ywp_effect_t *fx, double ts, uint8_t out[YWP_MAX_FRAME])
{
    if (!fx || fx->kind != YWP_FX_SEND) return 0;
    if (isnan(ts)) ts = n->last_now + n->wall_offset;
    return ywp_encode(&n->codec, fx->send.type, n->id, fx->send.seq, ts, fx->send.body,
                      fx->send.body_len, out);
}
