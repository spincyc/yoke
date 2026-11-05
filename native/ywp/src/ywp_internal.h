#pragma once
/* Declarations shared between the library's translation units only. */
#include <stdint.h>

/* floor(wall) for wall >= 0, saturating (0 for NaN or negative readings). */
uint64_t ywp_whole_seconds(double wall);

/* MT19937 seeded exactly like CPython's random.Random(seed) for a
 * nonnegative integer seed, so jitter reproduces the reference node. */
typedef struct {
    uint32_t mt[624];
    unsigned index;
} ywp_mt_t;

void ywp_mt_seed(ywp_mt_t *rng, uint64_t seed);
/* random.random(): 53-bit uniform in [0, 1). */
double ywp_mt_random(ywp_mt_t *rng);
/* random.uniform(a, b) == a + (b - a) * random(). */
double ywp_mt_uniform(ywp_mt_t *rng, double a, double b);
