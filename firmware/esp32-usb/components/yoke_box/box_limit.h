#pragma once
/* Per-source throttle for YWP/1 authentication failures (native/ywp
 * README, "Flooding"): a forged datagram costs a parse and two HMACs, so a
 * source that keeps failing is ignored, unparsed, until its bucket refills.
 * A token bucket per source over a small table; a new source replaces the
 * fullest bucket. Sources are spoofable, so box_ywp.c applies it only
 * under load (a forger must not mute a peer at a trickle); sources that
 * rotate addresses are bounded by the receive batch. Pure C (host-tested). */
#include <stdbool.h>
#include <stdint.h>

#define BOX_LIMIT_SOURCES 8
#define BOX_LIMIT_BURST 8.0   /* failures a source may make at once */
#define BOX_LIMIT_RATE 1.0    /* and then per second */

typedef struct {
    struct {
        uint32_t addr;
        double tokens, at;
        bool used;
    } entry[BOX_LIMIT_SOURCES];
    uint64_t throttled;       /* datagrams ignored */
} box_limit_t;

/* Whether to parse a datagram from addr (IPv4, host order); now: monotonic
 * seconds. */
bool box_limit_allow(box_limit_t *limit, uint32_t addr, double now);
/* addr's datagram failed authentication. */
void box_limit_failed(box_limit_t *limit, uint32_t addr, double now);
