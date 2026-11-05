/* Bounded node tables. Rows are kept compact and in insertion order, so
 * iteration order, in-place updates and FIFO eviction match the reference's
 * dicts (`_peers`, `_replay`, `_intent_cache`, `_rejected`). Like the
 * reference, an input may insert first and the table is capped when the
 * input ends (`_bound_maps`): each table has one spare row for that, and
 * an input inserts at most one row per table. Capping never evicts the
 * paired producer, nor the replay row of a currently admitted consumer
 * (ADR 0029). */
#include <string.h>
#include "ywp_node_impl.h"

void *ywpn_tab_row(const table_t *t, size_t i)
{
    return (char *)t->rows + i * t->size;
}

long ywpn_tab_find(const table_t *t, const char *id)
{
    for (size_t i = 0; i < t->count; ++i)
        if (strcmp((const char *)ywpn_tab_row(t, i), id) == 0) return (long)i;
    return -1;
}

void *ywpn_tab_get(const table_t *t, const char *id)
{
    long i = ywpn_tab_find(t, id);
    return i < 0 ? NULL : ywpn_tab_row(t, (size_t)i);
}

void ywpn_tab_remove(ywp_node_t *n, table_t *t, size_t i)
{
    if (t == &n->peers) ywpn_media_free(n, ywpn_tab_row(t, i));
    memmove(ywpn_tab_row(t, i), ywpn_tab_row(t, i + 1), (t->count - i - 1) * t->size);
    --t->count;
}

static bool protected_row(const ywp_node_t *n, const table_t *t, const char *id)
{
    if (n->pairing.paired && strcmp(id, n->pairing.producer) == 0) return true;
    if (t != &n->origins) return false;
    for (size_t i = 0; i < n->admitted_count; ++i)
        if (strcmp(id, n->admitted[i].id) == 0) return true;
    return false;
}

/* _cap_fifo: drop the oldest unprotected rows down to the capacity. */
static void tab_cap(ywp_node_t *n, table_t *t, size_t cap)
{
    while (t->count > cap) {
        size_t victim = 0;
        while (victim < t->count && protected_row(n, t, ywpn_tab_row(t, victim))) ++victim;
        if (victim == t->count) victim = 0;      /* unreachable: limits are validated */
        ywpn_tab_remove(n, t, victim);
    }
}

void ywpn_bound_maps(ywp_node_t *n)
{
    tab_cap(n, &n->peers, n->peers.cap);
    tab_cap(n, &n->origins, n->origins.cap);
    tab_cap(n, &n->intents, n->intents.cap);
    tab_cap(n, &n->rejections, n->rejections.cap);
}

void *ywpn_tab_insert(ywp_node_t *n, table_t *t, const char *id)
{
    tab_cap(n, t, t->cap);                       /* keep the one spare row free */
    char *row = ywpn_tab_row(t, t->count++);
    memset(row, 0, t->size);
    memcpy(row, id, strlen(id) + 1);
    return row;
}

/* -- consumer media store ---------------------------------------------------
 * One arena holds every producer peer's canonical media. Removing a copy
 * compacts the arena; a store that does not fit first drops other copies,
 * oldest peer first and never the paired producer's. A dropped copy makes
 * that producer's media unknown until it announces again (README: memory
 * subset); the arena holds two maximal descriptions, so the paired
 * producer's always fits. */

const char *ywpn_peer_media(const ywp_node_t *n, const peer_t *p)
{
    return p->media_len ? (const char *)n->media_arena + p->media_off : NULL;
}

void ywpn_media_free(ywp_node_t *n, peer_t *p)
{
    if (!p->media_len) return;
    size_t off = p->media_off, len = p->media_len;
    memmove(n->media_arena + off, n->media_arena + off + len, n->media_used - off - len);
    n->media_used -= len;
    for (size_t i = 0; i < n->peers.count; ++i) {
        peer_t *q = ywpn_tab_row(&n->peers, i);
        if (q->media_len && q->media_off > off) q->media_off = (uint16_t)(q->media_off - len);
    }
    p->media_len = 0;
}

void ywpn_media_store(ywp_node_t *n, peer_t *p, const char *media, size_t len)
{
    size_t cap = n->limits.media_bytes;
    ywpn_media_free(n, p);
    for (size_t i = 0; n->media_used + len > cap && i < n->peers.count; ++i) {
        peer_t *q = ywpn_tab_row(&n->peers, i);
        if (q == p || (n->pairing.paired && strcmp(q->id, n->pairing.producer) == 0)) continue;
        ywpn_media_free(n, q);
    }
    if (!len || n->media_used + len > cap) return;
    memcpy(n->media_arena + n->media_used, media, len);
    p->media_off = (uint16_t)n->media_used;
    p->media_len = (uint16_t)len;
    n->media_used += len;
}
