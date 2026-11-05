/* MT19937 (Matsumoto and Nishimura) with CPython's seeding and random():
 * the reference node draws announce and suppression jitter from
 * random.Random(seed), and reproducing that stream makes a C node's frames
 * byte-identical to a Python node's in the same simulation. random() is
 * exact, and the reference's uniform() spans (0.5 and 1.0) multiply
 * exactly, so FMA contraction cannot change a result. */
#include "ywp_internal.h"

#define N 624u
#define M 397u

static void init_genrand(ywp_mt_t *r, uint32_t s)
{
    r->mt[0] = s;
    for (unsigned i = 1; i < N; ++i)
        r->mt[i] = 1812433253u * (r->mt[i - 1] ^ (r->mt[i - 1] >> 30)) + i;
    r->index = N;
}

/* CPython random_seed(): the absolute value as little-endian 32-bit words,
 * at least one word, fed to init_by_array. */
void ywp_mt_seed(ywp_mt_t *r, uint64_t seed)
{
    uint32_t key[2] = {(uint32_t)seed, (uint32_t)(seed >> 32)};
    unsigned len = key[1] ? 2u : 1u, i = 1, j = 0;
    init_genrand(r, 19650218u);
    for (unsigned k = N > len ? N : len; k; --k) {
        r->mt[i] = (r->mt[i] ^ ((r->mt[i - 1] ^ (r->mt[i - 1] >> 30)) * 1664525u)) + key[j] + j;
        if (++i >= N) { r->mt[0] = r->mt[N - 1]; i = 1; }
        if (++j >= len) j = 0;
    }
    for (unsigned k = N - 1; k; --k) {
        r->mt[i] = (r->mt[i] ^ ((r->mt[i - 1] ^ (r->mt[i - 1] >> 30)) * 1566083941u)) - i;
        if (++i >= N) { r->mt[0] = r->mt[N - 1]; i = 1; }
    }
    r->mt[0] = 0x80000000u;
}

static uint32_t genrand(ywp_mt_t *r)
{
    if (r->index >= N) {
        for (unsigned k = 0; k < N; ++k) {
            uint32_t y = (r->mt[k] & 0x80000000u) | (r->mt[(k + 1) % N] & 0x7fffffffu);
            r->mt[k] = r->mt[(k + M) % N] ^ (y >> 1) ^ ((y & 1u) ? 0x9908b0dfu : 0u);
        }
        r->index = 0;
    }
    uint32_t y = r->mt[r->index++];
    y ^= y >> 11;
    y ^= (y << 7) & 0x9d2c5680u;
    y ^= (y << 15) & 0xefc60000u;
    y ^= y >> 18;
    return y;
}

double ywp_mt_random(ywp_mt_t *r)
{
    uint32_t a = genrand(r) >> 5, b = genrand(r) >> 6;
    return ((double)a * 67108864.0 + (double)b) * (1.0 / 9007199254740992.0);
}

double ywp_mt_uniform(ywp_mt_t *r, double a, double b)
{
    double unit = ywp_mt_random(r);
    double span = b - a;
    return a + span * unit;
}
