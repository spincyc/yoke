/* Effect batches of the node and the bodies of the frames it authors.
 *
 * Each input starts a batch (fx_begin) and publishes a window of records
 * (fx_publish / fx_finish). Variable data (bodies, log text, attach media)
 * lives in a byte arena that is reset with the batch. A batch whose frames
 * pass the durable sequence ceiling is held in place behind a PERSIST,
 * exactly as the reference's _finish defers it, and is released by the
 * matching successful completion. The last records are reserved for
 * PERSIST, DETACH and static logs, so the barrier itself always fits. */
#include <math.h>
#include <string.h>
#include "ywp_node_impl.h"

#define RESERVED_RECORDS 4u

bool ywpn_holding(const ywp_node_t *n)
{
    return (n->save_pending && n->save_kind == SAVE_EFFECTS) || n->deferred;
}

void ywpn_fx_begin(ywp_node_t *n)
{
    if (ywpn_holding(n)) {
        n->fx_len = n->held_end;
        n->arena_used = n->held_arena;
    } else {
        n->fx_len = n->arena_used = n->held_end = n->held_arena = 0;
    }
    n->vis_begin = n->vis_end = n->fx_len;
    n->poll_more = false;
}

void ywpn_fx_publish(ywp_node_t *n, size_t from)
{
    n->vis_begin = from;
    n->vis_end = n->fx_len;
}

bool ywpn_fx_room(const ywp_node_t *n, size_t records, size_t bytes)
{
    return n->fx_len + records + RESERVED_RECORDS <= n->limits.effects &&
           n->arena_used + bytes <= n->limits.effect_bytes;
}

static ywp_effect_t *push(ywp_node_t *n, ywp_fx_kind_t kind, size_t reserve)
{
    if (n->fx_len + reserve >= n->limits.effects) {
        ++n->overruns;
        return NULL;
    }
    ywp_effect_t *e = &n->fx[n->fx_len++];
    memset(e, 0, sizeof(*e));
    e->kind = kind;
    return e;
}

ywp_effect_t *ywpn_fx_push(ywp_node_t *n, ywp_fx_kind_t kind)
{
    return push(n, kind, RESERVED_RECORDS);
}

static char *arena_copy(ywp_node_t *n, const void *data, size_t len)
{
    if (n->arena_used + len > n->limits.effect_bytes) {
        ++n->overruns;
        return NULL;
    }
    char *p = (char *)n->arena + n->arena_used;
    memcpy(p, data, len);
    n->arena_used += len;
    return p;
}

void ywpn_fx_attach(ywp_node_t *n, const char *producer, const char *media, size_t len)
{
    size_t used = n->arena_used;
    char *id = arena_copy(n, producer, strlen(producer) + 1);
    char *copy = id ? arena_copy(n, media, len) : NULL;
    ywp_effect_t *e = copy ? ywpn_fx_push(n, YWP_FX_ATTACH) : NULL;
    if (!e) {
        n->arena_used = used;
        return;
    }
    e->attach.producer = id;
    e->attach.media = copy;
    e->attach.media_len = len;
}

void ywpn_fx_detach(ywp_node_t *n)
{
    (void)push(n, YWP_FX_DETACH, 0);
}

void ywpn_fx_admission(ywp_node_t *n)
{
    ywp_effect_t *e = ywpn_fx_push(n, YWP_FX_ADMISSION);
    if (!e) return;
    e->admission.peers = n->admitted;
    e->admission.count = n->admitted_count;
}

bool ywpn_begin_save(ywp_node_t *n, uint8_t kind, const ywp_pairing_t *pairing)
{
    if (n->save_pending) return false;
    ywp_effect_t *e = push(n, YWP_FX_PERSIST, 0);
    if (!e) return false;
    n->save_pending = true;
    n->save_kind = kind;
    n->save_id = ++n->save_serial;
    n->save_state.seq = n->seq <= YWP_MAX_SEQUENCE - YWP_SEQUENCE_RESERVATION
                            ? n->seq + YWP_SEQUENCE_RESERVATION : YWP_MAX_SEQUENCE;
    n->save_state.pairing = *pairing;
    e->persist.state = &n->save_state;
    e->persist.request_id = n->save_id;
    return true;
}

static void reverse(ywp_effect_t *fx, size_t a, size_t b)
{
    while (a + 1 < b) {
        ywp_effect_t t = fx[a];
        fx[a++] = fx[--b];
        fx[b] = t;
    }
}

void ywpn_fx_finish(ywp_node_t *n, size_t from)
{
    bool late = false;
    for (size_t i = from; i < n->fx_len; ++i) {
        if (n->fx[i].kind == YWP_FX_PERSIST) {
            ywpn_fx_publish(n, n->vis_begin);
            return;
        }
        late |= n->fx[i].kind == YWP_FX_SEND && n->fx[i].send.seq > n->reserved_until;
    }
    if (!late) {
        ywpn_fx_publish(n, n->vis_begin);
        return;
    }
    if (n->save_pending) {                  /* the reference drops the batch */
        n->fx_len = from;
        ywpn_fx_publish(n, n->vis_begin);
        return;
    }
    /* Nothing is held here, so the batch starts at record 0. Move the
     * deferred records [from, end) to the front and hold them; a prefix
     * (shutdown's DETACH) stays visible ahead of the PERSIST. */
    size_t end = n->fx_len;
    reverse(n->fx, 0, from);
    reverse(n->fx, from, end);
    reverse(n->fx, 0, end);
    n->held_end = end - from;
    n->held_arena = n->arena_used;
    (void)ywpn_begin_save(n, SAVE_EFFECTS, &n->pairing);
    ywpn_fx_publish(n, n->held_end);
}

/* -- logs ------------------------------------------------------------------- */

bool ywpn_log_begin(ywp_node_t *n, logline_t *l)
{
    size_t room = n->limits.effect_bytes - n->arena_used;
    l->node = n;
    if (room > LOG_MAX) room = LOG_MAX;
    if (room < 8 || n->fx_len + RESERVED_RECORDS >= n->limits.effects) room = 0;
    ywp_out_buffer(&l->out, n->arena + n->arena_used, room ? room - 1 : 0);
    return room != 0;
}

void ywpn_log_text(logline_t *l, const char *text)
{
    ywp_out_text(&l->out, text);
}

void ywpn_log_bytes(logline_t *l, const char *text, size_t len)
{
    ywp_out_raw(&l->out, text, len);
}

void ywpn_log_u64(logline_t *l, uint64_t v)
{
    ywp_out_uint(&l->out, v);
}

void ywpn_log_end(logline_t *l, ywp_log_level_t level)
{
    ywp_node_t *n = l->node;
    ywp_out_t *o = &l->out;
    if (!o->cap) {
        ++n->overruns;
        return;
    }
    if (o->overflow) {                      /* truncate on a character boundary */
        size_t keep = ywp_utf8_clip((const char *)o->buf, o->len, o->len - 3);
        memcpy(o->buf + keep, "...", 3);
        o->len = keep + 3;
    }
    o->buf[o->len] = 0;
    ywp_effect_t *e = ywpn_fx_push(n, YWP_FX_LOG);
    if (!e) return;
    e->log.level = level;
    e->log.text = (const char *)o->buf;
    n->arena_used += o->len + 1;
}

void ywpn_log_static(ywp_node_t *n, ywp_log_level_t level, const char *text)
{
    ywp_effect_t *e = push(n, YWP_FX_LOG, 0);
    if (!e) return;
    e->log.level = level;
    e->log.text = text;
}

/* -- frames authored by this node -------------------------------------------- */

static void body_begin(ywp_node_t *n, ywp_out_t *o)
{
    ywp_out_buffer(o, n->arena + n->arena_used, n->limits.effect_bytes - n->arena_used);
}

/* _send: assign the next sequence number to a finished body. */
static void body_send(ywp_node_t *n, ywp_out_t *o, ywp_frame_type_t type)
{
    if (o->overflow || n->seq >= YWP_MAX_SEQUENCE) {
        ++n->overruns;
        return;
    }
    ywp_effect_t *e = ywpn_fx_push(n, YWP_FX_SEND);
    if (!e) return;
    e->send.type = type;
    e->send.seq = ++n->seq;
    e->send.body = n->arena + n->arena_used;
    e->send.body_len = o->len;
    n->arena_used += o->len;
}

static void out_version(ywp_out_t *o, const ywp_version_t *v)
{
    ywp_out_raw(o, "[", 1);
    ywp_out_uint(o, v->n);
    ywp_out_raw(o, ",", 1);
    ywp_out_str(o, v->origin, strlen(v->origin));
    ywp_out_raw(o, "]", 1);
}

static void out_id_or_null(ywp_out_t *o, bool present, const char *id)
{
    if (present) ywp_out_str(o, id, strlen(id));
    else ywp_out_text(o, "null");
}

static void evt_end(ywp_node_t *n, ywp_out_t *o, const char *event)
{
    ywp_out_text(o, "},\"event\":");
    ywp_out_str(o, event, strlen(event));
    ywp_out_raw(o, "}", 1);
    body_send(n, o, YWP_EVT);
}

void ywpn_send_evt(ywp_node_t *n, const char *event, const char *key, const char *value,
              size_t value_len)
{
    ywp_out_t o;
    body_begin(n, &o);
    ywp_out_text(&o, "{\"detail\":{");
    if (key) {
        ywp_out_str(&o, key, strlen(key));
        ywp_out_raw(&o, ":", 1);
        ywp_out_str(&o, value, value_len);
    }
    evt_end(n, &o, event);
}

void ywpn_send_evt_peer_lost(ywp_node_t *n, const char *peer, const char *reason)
{
    ywp_out_t o;
    body_begin(n, &o);
    ywp_out_text(&o, "{\"detail\":{\"peer\":");
    ywp_out_str(&o, peer, strlen(peer));
    ywp_out_text(&o, ",\"reason\":");
    ywp_out_str(&o, reason, strlen(reason));
    evt_end(n, &o, "peer-lost");
}

void ywpn_send_evt_pairing(ywp_node_t *n, const ywp_pairing_t *p)
{
    ywp_out_t o;
    body_begin(n, &o);
    ywp_out_text(&o, "{\"detail\":{\"producer\":");
    out_id_or_null(&o, p->paired, p->producer);
    ywp_out_text(&o, ",\"version\":");
    out_version(&o, &p->version);
    evt_end(n, &o, "pairing-applied");
}

void ywpn_send_evt_rx_drop(ywp_node_t *n)
{
    static const struct { const char *key; ywp_drop_t drop; } order[] = {
        {"{\"detail\":{\"auth\":", YWP_DROP_AUTH},
        {",\"malformed\":", YWP_DROP_MALFORMED},
        {",\"oversize\":", YWP_DROP_OVERSIZE},
        {",\"stale\":", YWP_DROP_STALE},
        {",\"version\":", YWP_DROP_VERSION}};
    ywp_out_t o;
    body_begin(n, &o);
    for (unsigned i = 0; i < 5; ++i) {
        ywp_out_text(&o, order[i].key);
        ywp_out_uint(&o, n->drops[order[i].drop]);
    }
    evt_end(n, &o, "rx-drop");
}

void ywpn_send_ack(ywp_node_t *n, const ywp_version_t *v, bool applied, const char *reason)
{
    ywp_out_t o;
    body_begin(n, &o);
    ywp_out_text(&o, applied ? "{\"applied\":true,\"reason\":" : "{\"applied\":false,\"reason\":");
    ywp_out_str(&o, reason, strlen(reason));
    ywp_out_text(&o, ",\"version\":");
    out_version(&o, v);
    ywp_out_raw(&o, "}", 1);
    body_send(n, &o, YWP_ACK);
}

void ywpn_send_int(ywp_node_t *n, const intent_t *intent)
{
    ywp_out_t o;
    body_begin(n, &o);
    ywp_out_text(&o, "{\"consumer\":");
    ywp_out_str(&o, intent->id, strlen(intent->id));
    ywp_out_text(&o, intent->pair ? ",\"op\":\"pair\",\"producer\":" : ",\"op\":\"unpair\"");
    if (intent->pair) ywp_out_str(&o, intent->producer, strlen(intent->producer));
    ywp_out_text(&o, ",\"version\":");
    out_version(&o, &intent->version);
    ywp_out_raw(&o, "}", 1);
    body_send(n, &o, YWP_INT);
}

void ywpn_send_bye(ywp_node_t *n)
{
    ywp_out_t o;
    body_begin(n, &o);
    ywp_out_raw(&o, "{}", 2);
    body_send(n, &o, YWP_BYE);
}

size_t ywpn_ann_body(const ywp_node_t *n, ywp_out_t *o, ywp_state_t state, const char *media,
                size_t media_len, bool status, bool err, size_t err_len)
{
    bool consumer = n->role == YWP_CONSUMER;
    ywp_out_raw(o, "{", 1);
    if (status) {
        ywp_out_text(o, "\"driver_status\":{\"detail\":");
        ywp_out_str(o, n->detail, n->detail_len);
        ywp_out_text(o, "},");
    }
    ywp_out_text(o, "\"err\":");
    if (err) ywp_out_str(o, n->err, err_len);
    else ywp_out_text(o, "null");
    ywp_out_text(o, ",\"kind\":");
    ywp_out_str(o, ywp_kind_name(n->kind), strlen(ywp_kind_name(n->kind)));
    ywp_out_text(o, ",\"media\":");
    if (!consumer && media) ywp_out_raw(o, media, media_len);
    else ywp_out_text(o, "null");
    ywp_out_text(o, ",\"name\":");
    ywp_out_str(o, n->name, n->name_len);
    ywp_out_text(o, ",\"pairing\":");
    if (consumer) {
        ywp_out_text(o, "{\"producer\":");
        out_id_or_null(o, n->pairing.paired, n->pairing.producer);
        ywp_out_text(o, ",\"version\":");
        out_version(o, &n->pairing.version);
        ywp_out_raw(o, "}", 1);
    } else {
        ywp_out_text(o, "null");
    }
    ywp_out_text(o, ",\"role\":");
    ywp_out_str(o, ywp_role_name(n->role), strlen(ywp_role_name(n->role)));
    ywp_out_text(o, ",\"state\":");
    ywp_out_str(o, ywp_state_name(state), strlen(ywp_state_name(state)));
    ywp_out_raw(o, "}", 1);
    return o->total;
}

static const char *own_media(const ywp_node_t *n)
{
    return n->has_media ? n->media : NULL;
}

static size_t ann_size(const ywp_node_t *n, bool status, bool err, size_t err_len)
{
    ywp_out_t o;
    ywp_out_buffer(&o, NULL, 0);
    return n->overhead +
           ywpn_ann_body(n, &o, n->state, own_media(n), n->media_len, status, err, err_len);
}

static size_t escaped_len(uint8_t b)
{
    if (b == '"' || b == '\\' || b == '\b' || b == '\f' || b == '\n' || b == '\r' || b == '\t')
        return 2;
    return b < 0x20 ? 6 : 1;
}

/* _ann: reschedule the periodic announce, then fit the body to the frame
 * budget by dropping driver_status and then shortening err to the longest
 * prefix of whole characters that fits (the reference's binary search). */
void ywpn_send_ann(ywp_node_t *n, double now)
{
    double jitter = ywp_mt_uniform(&n->rng, -YWP_ANN_JITTER, YWP_ANN_JITTER);
    n->ann_due = now + YWP_ANN_INTERVAL * (1.0 + jitter);
    bool status = n->detail_len > 0;
    size_t err_len = n->err_len;
    if (status && ann_size(n, true, n->has_err, err_len) > YWP_MAX_FRAME) status = false;
    size_t size = ann_size(n, status, n->has_err, err_len);
    if (n->has_err && size > YWP_MAX_FRAME) {
        size_t base = ann_size(n, status, true, 0), used = 0, keep = 0;
        for (size_t i = 0; i < n->err_len;) {
            size_t end = i + 1;
            while (end < n->err_len && ((uint8_t)n->err[end] & 0xc0) == 0x80) ++end;
            for (size_t k = i; k < end; ++k) used += escaped_len((uint8_t)n->err[k]);
            if (base + used > YWP_MAX_FRAME) break;
            keep = i = end;
        }
        err_len = keep;
    }
    ywp_out_t o;
    body_begin(n, &o);
    ywpn_ann_body(n, &o, n->state, own_media(n), n->media_len, status, n->has_err, err_len);
    body_send(n, &o, YWP_ANN);
}
