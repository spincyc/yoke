/* Node construction and read-only introspection (ywp.h). The memory
 * layout is one block: the node, then each table, then the arenas, each
 * aligned for any type, so ywp_node_footprint is exact. */
#include <math.h>
#include <stdalign.h>
#include <stdlib.h>
#include <string.h>
#include "ywp_node_impl.h"
#include "ywp_state.h"

#define ALIGN(x) (((x) + alignof(max_align_t) - 1) & ~(alignof(max_align_t) - 1))

void ywp_limits_default(ywp_limits_t *l)
{
    l->peers = 32;
    l->origins = 64;
    l->intents = 32;
    l->rejections = 16;
    l->media_bytes = 4096;
    l->effects = 32;
    l->effect_bytes = 4096;
}

void ywp_config_default(ywp_config_t *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->role = YWP_CONSUMER;
    cfg->kind = YWP_USB;
    ywp_limits_default(&cfg->limits);
}

const char *ywp_err_name(ywp_err_t err)
{
    static const char *const names[] = {"ok", "config", "id", "name", "media",
                                        "state", "clock", "sequence", "nomem"};
    return (unsigned)err <= (unsigned)YWP_E_NOMEM ? names[err] : "config";
}

typedef struct {
    size_t peers, origins, intents, rejections, admitted, fx, media, arena, total;
} layout_t;

static bool layout(const ywp_limits_t *l, layout_t *out)
{
    if (!l || l->peers < 2 || l->origins < 2 || l->intents < 2 || l->rejections < 2 ||
        l->effects < 16 || l->effect_bytes < 4096 ||
        (l->media_bytes && l->media_bytes < 2 * YWP_MEDIA_MAX)) return false;
    size_t at = ALIGN(sizeof(ywp_node_t));
    out->peers = at;
    at += ALIGN((l->peers + 1u) * sizeof(peer_t));
    out->origins = at;
    at += ALIGN((l->origins + 1u) * sizeof(origin_t));
    out->intents = at;
    at += ALIGN((l->intents + 1u) * sizeof(intent_t));
    out->rejections = at;
    at += ALIGN((l->rejections + 1u) * sizeof(rejection_t));
    out->admitted = at;
    at += ALIGN(2u * l->peers * sizeof(ywp_admit_t));
    out->fx = at;
    at += ALIGN(l->effects * sizeof(ywp_effect_t));
    out->media = at;
    at += ALIGN(l->media_bytes);
    out->arena = at;
    at += ALIGN(l->effect_bytes);
    out->total = at;
    return true;
}

size_t ywp_node_footprint(const ywp_limits_t *limits)
{
    layout_t lay;
    return layout(limits, &lay) ? lay.total : 0;
}

static ywp_node_t *fail(ywp_err_t *err, ywp_err_t code, ywp_node_t *n)
{
    if (err) *err = code;
    free(n);
    return NULL;
}

ywp_node_t *ywp_node_create(const ywp_config_t *cfg, const ywp_crypto_ops_t *ops,
                            const ywp_saved_t *state, double now, double wall, ywp_err_t *err)
{
    layout_t lay;
    if (err) *err = YWP_E_OK;
    if (!cfg || !ops || !ops->sha256_start || !ops->sha256_update || !ops->sha256_finish ||
        !state || (cfg->role != YWP_PRODUCER && cfg->role != YWP_CONSUMER) ||
        (cfg->kind != YWP_HDMI && cfg->kind != YWP_USB) || !layout(&cfg->limits, &lay) ||
        (cfg->role == YWP_CONSUMER && cfg->limits.media_bytes == 0) ||
        (cfg->admission && cfg->limits.origins < cfg->limits.peers) ||
        (cfg->psk_len && !cfg->psk)) return fail(err, YWP_E_CONFIG, NULL);
    if (!cfg->node_id || !ywp_id_valid(cfg->node_id, strlen(cfg->node_id)))
        return fail(err, YWP_E_ID, NULL);
    const char *name = cfg->name ? cfg->name : "";
    size_t name_len = strlen(name), chars = 0;
    for (size_t i = 0; i < name_len; ++i) chars += ((uint8_t)name[i] & 0xc0) != 0x80;
    if (name_len > 4 * YWP_NAME_MAX || chars > YWP_NAME_MAX ||
        !ywp_utf8_valid((const uint8_t *)name, name_len)) return fail(err, YWP_E_NAME, NULL);
    if (!isfinite(now) || !isfinite(wall) || wall < 0) return fail(err, YWP_E_CLOCK, NULL);
    if (!ywp_saved_valid(state)) return fail(err, YWP_E_STATE, NULL);
    uint64_t floor = ywp_restart_floor(state->seq, wall);
    if (floor >= YWP_MAX_SEQUENCE) return fail(err, YWP_E_SEQUENCE, NULL);

    char *block = calloc(1, lay.total);
    if (!block) return fail(err, YWP_E_NOMEM, NULL);
    ywp_node_t *n = (ywp_node_t *)block;
    const ywp_limits_t *l = &cfg->limits;
    n->peers = (table_t){block + lay.peers, sizeof(peer_t), 0, l->peers};
    n->origins = (table_t){block + lay.origins, sizeof(origin_t), 0, l->origins};
    n->intents = (table_t){block + lay.intents, sizeof(intent_t), 0, l->intents};
    n->rejections = (table_t){block + lay.rejections, sizeof(rejection_t), 0, l->rejections};
    n->admitted = (ywp_admit_t *)(block + lay.admitted);
    n->fx = (ywp_effect_t *)(block + lay.fx);
    n->media_arena = l->media_bytes ? (uint8_t *)block + lay.media : NULL;
    n->arena = (uint8_t *)block + lay.arena;
    n->limits = *l;

    copy_id(n->id, cfg->node_id);
    n->role = cfg->role;
    n->kind = cfg->kind;
    memcpy(n->name, name, name_len + 1);
    n->name_len = name_len;
    n->admission = cfg->admission && cfg->role == YWP_PRODUCER;
    n->overhead = ywp_frame_overhead(n->id);
    ywp_codec_init(&n->codec, ops, cfg->psk, cfg->psk_len);
    ywp_mt_seed(&n->rng, cfg->seed);

    n->pairing = state->pairing;
    n->seq = floor;
    n->reserved_until = state->seq;
    n->wall_offset = wall - now;
    n->last_now = now;
    n->boot_saved = state->seq;
    n->boot_wall = wall;
    n->boot_logs = true;
    n->future_log_at = n->own_seq_log_at = -INFINITY;
    n->reserve_retry_at = NAN;
    n->reserve_backoff = YWP_RETRY_BACKOFF_BASE;
    n->teardown_pending = cfg->role == YWP_CONSUMER && state->pairing.version.n > 0;
    n->state = cfg->role == YWP_CONSUMER && (state->pairing.paired || n->teardown_pending)
                   ? YWP_PENDING : YWP_IDLE;
    n->retry_at = NAN;
    n->backoff = YWP_RETRY_BACKOFF_BASE;
    n->ann_due = now;
    n->rx_drop_at = -INFINITY;

    size_t media_len;
    bool present;
    if (ywpn_check_media(n, cfg->media, cfg->media_len, &media_len, &present) != YWP_E_OK)
        return fail(err, YWP_E_MEDIA, n);
    ywpn_adopt_media(n, present, media_len);
    return n;
}

void ywp_node_destroy(ywp_node_t *n)
{
    free(n);
}

/* -- introspection -------------------------------------------------------------------- */

void ywp_node_status(const ywp_node_t *n, ywp_status_t *s)
{
    memset(s, 0, sizeof(*s));
    s->state = n->state;
    s->pairing = n->pairing;
    s->started = n->started;
    s->save_pending = n->save_pending;
    s->held = ywpn_holding(n);
    s->last_seq = n->seq;
    s->reserved_until = n->reserved_until;
    s->retry_at = n->retry_at;
    s->backoff = n->backoff;
    s->ann_due = n->ann_due;
    memcpy(s->drops, n->drops, sizeof(s->drops));
    memcpy(s->drops_reported, n->reported, sizeof(s->drops_reported));
    s->peers = n->peers.count;
    s->origins = n->origins.count;
    s->intents = n->intents.count;
    s->rejections = n->rejections.count;
    s->admitted = n->admitted_count;
    s->overruns = n->overruns;
}

size_t ywp_node_peer_count(const ywp_node_t *n)
{
    return n->peers.count;
}

bool ywp_node_peer(const ywp_node_t *n, size_t i, ywp_peer_t *out)
{
    if (i >= n->peers.count) return false;
    const peer_t *p = ywpn_tab_row(&n->peers, i);
    memset(out, 0, sizeof(*out));
    copy_id(out->id, p->id);
    out->role = (ywp_role_t)p->role;
    out->kind = (ywp_kind_t)p->kind;
    out->state = (ywp_state_t)p->state;
    out->has_pairing = p->has_pairing;
    out->pairing = p->pairing;
    out->has_media = p->has_media;
    out->last_seen = p->last_seen;
    out->departed = p->departed;
    out->lost = p->lost;
    return true;
}

const char *ywp_node_peer_media(const ywp_node_t *n, size_t i, size_t *len)
{
    if (i >= n->peers.count) return NULL;
    const peer_t *p = ywpn_tab_row(&n->peers, i);
    *len = p->media_len;
    return ywpn_peer_media(n, p);
}

size_t ywp_node_intent_count(const ywp_node_t *n)
{
    return n->intents.count;
}

bool ywp_node_intent(const ywp_node_t *n, size_t i, ywp_intent_t *out)
{
    if (i >= n->intents.count) return false;
    const intent_t *e = ywpn_tab_row(&n->intents, i);
    memset(out, 0, sizeof(*out));
    copy_id(out->consumer, e->id);
    out->pair = e->pair;
    copy_id(out->producer, e->producer);
    out->version = e->version;
    out->cached_at = e->cached_at;
    out->resend = e->resend;
    out->resend_at = e->resend_at;
    return true;
}

size_t ywp_node_rejection_count(const ywp_node_t *n)
{
    return n->rejections.count;
}

bool ywp_node_rejection(const ywp_node_t *n, size_t i, ywp_rejection_t *out)
{
    if (i >= n->rejections.count) return false;
    const rejection_t *r = ywpn_tab_row(&n->rejections, i);
    memset(out, 0, sizeof(*out));
    copy_id(out->consumer, r->id);
    out->version = r->version;
    out->producer_kind = (ywp_rejected_producer_t)r->producer.kind;
    copy_id(out->producer, r->producer.id);
    return true;
}
