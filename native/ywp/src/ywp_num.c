/* JSON number literal <-> double, bit-identical to CPython (see ywp_num.h).
 * Integer arithmetic only; doubles are assembled from and split into bits.
 *
 * Literal -> double. The first 19 significant digits w, scaled 10^q, are
 * multiplied by 5^q rounded down to 128 bits (Eisel-Lemire, as in Lemire's
 * fast_float). The 192-bit product is exact for 0 <= q <= 55 and otherwise
 * low by less than w units in its last place, which decides the rounding
 * unless that error could carry the value across the midpoint above. That
 * happens only for -4 <= q <= -1, around exact ties (an exhaustive search
 * over all w < 10^19, tests/native_ywp/gen_num_tables.py). Only then, and
 * when more digits follow and w and w + 1 round apart, is the value compared
 * exactly with the midpoint (2m + 1) 2^(e-1): fixed-size 32-bit-limb bignums
 * hold the literal digits that can matter (a midpoint has at most 767
 * significant digits; the rest only act as a sticky flag) and (2m + 1) 5^n.
 * Cost grows with digits kept and |q|, at most ~7k limb multiplies.
 *
 * Double -> digits. Schubfach (R. Giulietti, as in the JDK's DoubleToDecimal)
 * yields the shortest digits in the rounding interval (bounds included for
 * an even significand), the closer of the two final candidates, ties to the
 * even one: what CPython's repr() (Gay's dtoa mode 0) prints. It reads the
 * same table, as g = floor(10^-k 2^-r) + 1 = (table >> 2) + 1.
 *
 * Canonical spelling of a literal with at most 15 significant digits in
 * [1e-307, DBL_MAX] needs no conversion: adjacent 15-digit decimals there are
 * more than 4 ulps apart, so the literal is the only decimal of 15 digits or
 * fewer that rounds to its double, and repr() prints exactly its digits. */
#include <stdint.h>
#include <string.h>
#include "ywp_num.h"
#include "ywp_num_pow.h"

#define MASK52 ((((uint64_t)1) << 52) - 1u)
#define MASK63 ((((uint64_t)1) << 63) - 1u)
#define SIGN ((uint64_t)1 << 63)
#define INF_BITS ((uint64_t)0x7ff << 52)
#define EXP_SAT 1000000000000000 /* exponent digits saturate; far past any range */
#define BIG_WORDS 84            /* 2688 bits; (2^54 - 1) 5^1075 and 10^768 need 2552 */

typedef struct {
    const char *dig, *end;      /* first significant digit ('.' may follow), mantissa
                                   end; dig is NULL for zero */
    int64_t e10;                /* power of ten of the first significant digit */
    uint64_t w;                 /* first nw (<= 19) significant digits */
    int nw, nz;                 /* nz: digits of w up to its last nonzero one */
    bool more, neg;             /* more: a nonzero digit after the 19th */
} lit_t;

typedef struct {
    int len;                    /* used words, no leading zero words */
    uint32_t w[BIG_WORDS];      /* little-endian */
} big_t;

/* ---- integer helpers -------------------------------------------------- */

static uint64_t umul128(uint64_t a, uint64_t b, uint64_t *lo)
{
    uint64_t a0 = (uint32_t)a, a1 = a >> 32, b0 = (uint32_t)b, b1 = b >> 32;
    uint64_t p00 = a0 * b0, p01 = a0 * b1, p10 = a1 * b0, p11 = a1 * b1;
    uint64_t mid = (p00 >> 32) + (uint32_t)p01 + (uint32_t)p10;
    *lo = mid << 32 | (uint32_t)p00;
    return p11 + (p01 >> 32) + (p10 >> 32) + (mid >> 32);
}

static int clz64(uint64_t x)    /* x != 0 */
{
    int n = 0;
    if (!(x >> 32)) { n += 32; x <<= 32; }
    if (!(x >> 48)) { n += 16; x <<= 16; }
    if (!(x >> 56)) { n += 8; x <<= 8; }
    if (!(x >> 60)) { n += 4; x <<= 4; }
    if (!(x >> 62)) { n += 2; x <<= 2; }
    return n + !(x >> 63);
}

static int floor_shr(int32_t v, unsigned s)
{
    if (v >= 0) return (int)(v >> s);
    return -(int)((((uint32_t)0 - (uint32_t)v) + ((1u << s) - 1u)) >> s);
}

static int fl2_10(int x)        /* floor(log2 10^x) */
{
    return floor_shr((int32_t)x * YWP_FL2_10_MUL + YWP_FL2_10_ADD, YWP_FL2_10_SHIFT);
}

static int fl10_2(int x)        /* floor(log10 2^x) */
{
    return floor_shr((int32_t)x * YWP_FL10_2_MUL + YWP_FL10_2_ADD, YWP_FL10_2_SHIFT);
}

static int fl10_34_2(int x)     /* floor(log10 (3/4 2^x)) */
{
    return floor_shr((int32_t)x * YWP_FL10_34_2_MUL + YWP_FL10_34_2_ADD,
                     YWP_FL10_34_2_SHIFT);
}

/* ---- bignums (exact fallback only) -------------------------------------- */

static void big_set(big_t *b, uint64_t x)
{
    b->len = 0;
    while (x) { b->w[b->len++] = (uint32_t)x; x >>= 32; }
}

static void big_muladd(big_t *b, uint32_t m, uint32_t add)
{
    uint64_t carry = add;
    for (int i = 0; i < b->len; ++i) {
        carry += (uint64_t)b->w[i] * m;
        b->w[i] = (uint32_t)carry;
        carry >>= 32;
    }
    if (carry) b->w[b->len++] = (uint32_t)carry;
}

static void big_mul_pow5(big_t *b, unsigned n)
{
    static const uint32_t pow5[14] = {
        1u, 5u, 25u, 125u, 625u, 3125u, 15625u, 78125u, 390625u, 1953125u,
        9765625u, 48828125u, 244140625u, 1220703125u,
    };
    for (; n >= 13u; n -= 13u) big_muladd(b, pow5[13], 0u);
    if (n) big_muladd(b, pow5[n], 0u);
}

static void big_shl(big_t *b, unsigned n)
{
    if (!b->len) return;
    int words = (int)(n / 32u), len = b->len;
    unsigned bits = n % 32u;
    if (bits) {
        b->w[len + words] = b->w[len - 1] >> (32u - bits);
        for (int i = len - 1; i > 0; --i)
            b->w[i + words] = b->w[i] << bits | b->w[i - 1] >> (32u - bits);
        b->w[words] = b->w[0] << bits;
        ++len;
    } else {
        for (int i = len - 1; i >= 0; --i) b->w[i + words] = b->w[i];
    }
    for (int i = 0; i < words; ++i) b->w[i] = 0;
    b->len = len + words;
    if (!b->w[b->len - 1]) --b->len;
}

static int big_bits(const big_t *b)
{
    if (!b->len) return 0;
    int n = 32 * (b->len - 1);
    for (uint32_t top = b->w[b->len - 1]; top; top >>= 1) ++n;
    return n;
}

static int big_cmp(const big_t *a, const big_t *b)
{
    if (a->len != b->len) return a->len < b->len ? -1 : 1;
    for (int i = a->len - 1; i >= 0; --i)
        if (a->w[i] != b->w[i]) return a->w[i] < b->w[i] ? -1 : 1;
    return 0;
}

/* Sign of x 10^qa (+ something below 10^qa when sticky) - (2m + 1) 2^(e-1).
 * x is consumed. */
static int cmp_mid(big_t *x, int qa, bool sticky, uint64_t m, int e)
{
    big_t y;
    if (!x->len) return -1;
    big_set(&y, 2u * m + 1u);
    if (qa >= 0) big_mul_pow5(x, (unsigned)qa);
    else big_mul_pow5(&y, (unsigned)-qa);
    int xs = qa, ys = e - 1;    /* powers of two still to apply */
    int bx = big_bits(x) + xs, by = big_bits(&y) + ys;
    if (bx != by) return bx < by ? -1 : 1;
    if (xs > ys) big_shl(x, (unsigned)(xs - ys));
    else big_shl(&y, (unsigned)(ys - xs));
    int c = big_cmp(x, &y);
    return c ? c : sticky;
}

/* x = the literal's significant digits at places >= 10^lo; *qa = place of the
 * last one taken; *sticky = a nonzero digit was left out. */
static void lit_big(const lit_t *n, int lo, big_t *x, int *qa, bool *sticky)
{
    static const uint32_t pow10[10] = {
        1u, 10u, 100u, 1000u, 10000u, 100000u, 1000000u, 10000000u, 100000000u,
        1000000000u,
    };
    int64_t cnt = n->e10 - lo + 1, taken = 0;
    uint32_t chunk = 0;
    int nchunk = 0;
    big_set(x, 0);
    *sticky = false;
    for (const char *p = n->dig; p < n->end; ++p) {
        if (*p == '.') continue;
        if (taken == cnt) {
            if (*p == '0') continue;
            *sticky = true;
            break;
        }
        chunk = chunk * 10u + (uint32_t)(*p - '0');
        ++taken;
        if (++nchunk == 9) { big_muladd(x, pow10[9], chunk); chunk = 0; nchunk = 0; }
    }
    if (nchunk) big_muladd(x, pow10[nchunk], chunk);
    *qa = (int)(n->e10 - taken + 1);
}

/* ---- literal -> double ------------------------------------------------ */

static bool scan(const char *s, size_t len, lit_t *n)
{
    size_t i = 0, mant = 0, frac = 0;
    bool dot = false;
    int64_t dp = 0;             /* value = 0.d1d2... 10^dp; counts digits, no overflow */
    memset(n, 0, sizeof(*n));
    n->dig = n->end = NULL;
    if (i < len && s[i] == '-') { n->neg = true; ++i; }
    for (; i < len; ++i) {
        char c = s[i];
        if (c == '.' && !dot && mant) { dot = true; continue; }
        if (c < '0' || c > '9') break;
        ++mant;
        if (dot) ++frac;
        if (!n->dig) {
            if (c == '0') { if (dot) --dp; continue; }    /* leading zero */
            n->dig = s + i;
        }
        if (!dot) ++dp;
        if (n->nw < 19) {
            n->w = n->w * 10u + (uint64_t)(c - '0');
            if (c != '0') n->nz = n->nw + 1;
            ++n->nw;
        } else if (c != '0') {
            n->more = true;
        }
    }
    if (!mant || (dot && !frac)) return false;
    n->end = s + i;
    if (i < len && (s[i] == 'e' || s[i] == 'E')) {
        bool eneg = false;
        int64_t e = 0;
        if (++i < len && (s[i] == '+' || s[i] == '-')) eneg = s[i++] == '-';
        size_t start = i;
        for (; i < len && s[i] >= '0' && s[i] <= '9'; ++i)
            if (e < EXP_SAT) e = e * 10 + (s[i] - '0');
        if (i == start) return false;
        dp += eneg ? -e : e;
    }
    n->e10 = dp - 1;
    return i == len;            /* else not a JSON number; the scanner prevents it */
}

/* For a literal at 10^308: below 2^1024 - 2^970, digit by digit. */
static bool below_max(const lit_t *n)
{
    const char *ov = YWP_OVERFLOW_DIGITS;
    for (const char *p = n->dig; p < n->end; ++p) {
        if (*p == '.') continue;
        if (!*ov) return false;                 /* equal so far, then more: above */
        if (*p != *ov) return *p < *ov;
        ++ov;
    }
    return *ov != 0;            /* a proper prefix of a number ending in 2 */
}

/* w 10^q (w >= 1, YWP_POW_MIN <= q <= 308) as m 2^e, m <= 2^53. False when
 * undecided: the result is m or m + 1, by comparison with (2m + 1) 2^(e-1). */
static bool el_round(uint64_t w, int q, uint64_t *mo, int *eo)
{
    int lz = clz64(w);
    uint64_t wn = w << lz, l0, h0;
    const uint64_t *t = ywp_num_pow5[q - YWP_POW_MIN];
    uint64_t l1 = umul128(wn, t[1], &l0), h1 = umul128(wn, t[0], &h0);
    uint64_t p0 = l0, p1 = h0 + l1, p2 = h1 + (p1 < l1);   /* P = wn T, 192 bits */
    int x = fl2_10(q) - 127 - lz;              /* value = P 2^x, P >= 2^190 */
    int top = 190 + (int)(p2 >> 63);           /* floor(log2 P) */
    int c = top + x >= -1022 ? top - 53 : -1075 - x;    /* bits below the round bit */
    if (c >= 192) { *mo = 0; *eo = -1074; return true; }   /* below 2^-1075 */
    unsigned sh = (unsigned)(c - 128);         /* c >= top - 53 >= 137 */
    uint64_t k = p2 >> sh, rmask = ((uint64_t)1 << sh) - 1u, rh = p2 & rmask;
    bool exact = q >= 0 && q <= 55;
    uint64_t m = k >> 1;
    *eo = c + 1 + x;
    *mo = m;
    if (!(k & 1u))              /* round down, unless the error reaches the midpoint */
        return exact || rh != rmask || p1 != UINT64_MAX || p0 <= UINT64_MAX - wn;
    *mo = m + (!exact || rh || p1 || p0 || (m & 1u));
    return true;
}

static void el_exact(uint64_t w, int q, uint64_t *m, int *e, big_t *x)
{
    if (el_round(w, q, m, e)) return;
    big_set(x, w);
    int c = cmp_mid(x, q, false, *m, *e);
    if (c > 0 || (c == 0 && (*m & 1u))) ++*m;
}

/* m 2^e, m <= 2^53 (m < 2^52 only with e = -1074), to bits; false on overflow. */
static bool assemble(uint64_t m, int e, uint64_t *bits)
{
    if (m >> 53) { m >>= 1; ++e; }
    if (!(m >> 52)) { *bits = m; return true; }
    if (e + 1075 >= 2047) return false;
    *bits = (uint64_t)(e + 1075) << 52 | (m & MASK52);
    return true;
}

/* Nearest double, ties to even, without sign; false when it is infinite. */
static bool lit_bits(const lit_t *n, uint64_t *bits)
{
    *bits = 0;
    if (!n->dig || n->e10 < -324) return true;     /* zero, or below 10^-324 < 2^-1075 */
    if (n->e10 > 308 || (n->e10 == 308 && !below_max(n))) return false;
    int q = (int)n->e10 - n->nw + 1, e;
    uint64_t m, hi;
    big_t x;                    /* one scratch bignum for every exact step */
    el_exact(n->w, q, &m, &e, &x);
    if (!assemble(m, e, bits)) return false;
    if (!n->more) return true;
    el_exact(n->w + 1u, q, &m, &e, &x);        /* the value lies strictly between */
    if (assemble(m, e, &hi) && hi == *bits) return true;
    /* One midpoint between them, the one above *bits: compare exactly. */
    uint64_t lo_m = *bits & MASK52;
    int lo_e = -1074, qa;
    if (*bits >> 52) { lo_m |= (uint64_t)1 << 52; lo_e = (int)(*bits >> 52) - 1075; }
    bool sticky;
    lit_big(n, lo_e - 1 < 0 ? lo_e - 1 : 0, &x, &qa, &sticky);
    int c = cmp_mid(&x, qa, sticky, lo_m, lo_e);
    if (c > 0 || (c == 0 && (*bits & 1u))) ++*bits;
    return (*bits >> 52) != 0x7ffu;
}

/* ---- double -> shortest digits ------------------------------------------ */

/* Round to odd of g cp 2^-127, g = g1 2^63 + g0 (Schubfach, figure 8). */
static uint64_t rop(uint64_t g1, uint64_t g0, uint64_t cp)
{
    uint64_t x0, y0;
    uint64_t x1 = umul128(g0, cp, &x0), y1 = umul128(g1, cp, &y0);
    uint64_t z = (y0 >> 1) + x1;
    return (y1 + (z >> 63)) | (((z & MASK63) + MASK63) >> 63);
}

/* Shortest decimal f 10^*k of c 2^q (c >= 1), f < 10^17, maybe with trailing
 * zeros. */
static uint64_t to_decimal(uint64_t c, int q, int *k10)
{
    uint64_t out = c & 1u, cb = c << 2, cbr = cb + 2u, cbl;
    int k;
    if (c != (uint64_t)1 << 52 || q == -1074) {
        cbl = cb - 2u;
        k = fl10_2(q);
    } else {                    /* the predecessor is half as far */
        cbl = cb - 1u;
        k = fl10_34_2(q);
    }
    int h = q + fl2_10(-k) + 2;
    const uint64_t *t = ywp_num_pow5[-k - YWP_POW_MIN];
    uint64_t glo = (t[1] >> 2 | t[0] << 62) + 1u, ghi = (t[0] >> 2) + !glo;
    uint64_t g1 = ghi << 1 | glo >> 63, g0 = glo & MASK63;
    uint64_t vb = rop(g1, g0, cb << h), vbl = rop(g1, g0, cbl << h);
    uint64_t vbr = rop(g1, g0, cbr << h), s = vb >> 2, f;
    *k10 = k;
    /* One digit shorter: the interval is narrower than 10^(k+1), so at most
     * one candidate fits. (The JDK starts at s >= 100 because Java prints at
     * least two digits; Python does not.) */
    if (s >= 10u) {
        uint64_t sp10 = 10u * umul128(s, 115292150460684698u << 4, &f), tp10 = sp10 + 10u;
        bool upin = vbl + out <= sp10 << 2, wpin = (tp10 << 2) + out <= vbr;
        if (upin != wpin) return upin ? sp10 : tp10;
    }
    uint64_t t1 = s + 1u, mid = (s + t1) << 1;
    bool uin = vbl + out <= s << 2, win = (t1 << 2) + out <= vbr;
    if (uin != win) return uin ? s : t1;
    return vb < mid || (vb == mid && !(s & 1u)) ? s : t1;
}

/* Shortest repr digits of a finite nonzero double (sign ignored), trailing
 * zeros dropped; returns their count, *decpt as in 0.d1d2... 10^decpt. */
static int shortest(uint64_t bits, char dig[17], int *decpt)
{
    unsigned be = (unsigned)(bits >> 52) & 0x7ffu;
    uint64_t c = bits & MASK52, f, x;
    int q = -1074, k = 0;
    if (be) {
        c |= (uint64_t)1 << 52;
        q = (int)be - 1075;
    }
    if (q < 0 && q > -53 && !(c & (((uint64_t)1 << -q) - 1u))) f = c >> -q;
    else f = to_decimal(c, q, &k);
    /* f < 10^17: split at 10^8 (0xabcc...: ceil(2^90 / 10^8), exact for any
     * 64-bit f) and print with 32-bit arithmetic. */
    uint32_t hi = (uint32_t)(umul128(f, 0xabcc77118461cefdu, &x) >> 26);
    uint32_t lo = (uint32_t)(f - (uint64_t)hi * 100000000u);
    char buf[17];
    for (int i = 16; i >= 9; --i) { buf[i] = (char)('0' + lo % 10u); lo /= 10u; }
    for (int i = 8; i >= 0; --i) { buf[i] = (char)('0' + hi % 10u); hi /= 10u; }
    int start = 0, end = 17;
    while (buf[start] == '0') ++start;
    while (buf[end - 1] == '0') --end;
    memcpy(dig, buf + start, (size_t)(end - start));
    *decpt = 17 - start + k;
    return end - start;
}

/* Python repr() layout of 0.dig 10^decpt with sign. */
static size_t format(char *out, bool neg, const char *dig, int nd, int decpt)
{
    char *p = out;
    if (neg) *p++ = '-';
    if (decpt <= -4 || decpt > 16) {
        int x = decpt - 1;
        *p++ = dig[0];
        if (nd > 1) {
            *p++ = '.';
            memcpy(p, dig + 1, (size_t)(nd - 1));
            p += nd - 1;
        }
        *p++ = 'e';
        *p++ = x < 0 ? '-' : '+';
        if (x < 0) x = -x;
        if (x >= 100) *p++ = (char)('0' + x / 100);
        *p++ = (char)('0' + x / 10 % 10);
        *p++ = (char)('0' + x % 10);
    } else if (decpt <= 0) {
        *p++ = '0';
        *p++ = '.';
        for (int i = decpt; i < 0; ++i) *p++ = '0';
        memcpy(p, dig, (size_t)nd);
        p += nd;
    } else if (decpt < nd) {
        memcpy(p, dig, (size_t)decpt);
        p += decpt;
        *p++ = '.';
        memcpy(p, dig + decpt, (size_t)(nd - decpt));
        p += nd - decpt;
    } else {
        memcpy(p, dig, (size_t)nd);
        p += nd;
        for (int i = nd; i < decpt; ++i) *p++ = '0';
        *p++ = '.';
        *p++ = '0';
    }
    *p = 0;
    return (size_t)(p - out);
}

static size_t repr_bits(uint64_t bits, char *out)
{
    bool neg = bits >> 63;
    bits &= ~SIGN;
    if (bits > INF_BITS) { memcpy(out, "nan", 4); return 3; }
    if (bits == INF_BITS) {
        memcpy(out + neg, "inf", 4);
        if (neg) out[0] = '-';
        return 3u + neg;
    }
    if (!bits) return format(out, neg, "0", 1, 1);
    char dig[17];
    int decpt, nd = shortest(bits, dig, &decpt);
    return format(out, neg, dig, nd, decpt);
}

/* ---- API ---------------------------------------------------------------- */

bool ywp_num_parse(const char *lit, size_t len, double *out)
{
    lit_t n;
    uint64_t bits;
    if (!scan(lit, len, &n) || !lit_bits(&n, &bits)) return false;
    if (n.neg) bits |= SIGN;
    memcpy(out, &bits, sizeof(*out));
    return true;
}

bool ywp_num_finite(const char *lit, size_t len)
{
    lit_t n;
    if (!scan(lit, len, &n)) return false;
    return !n.dig || n.e10 < 308 || (n.e10 == 308 && below_max(&n));
}

size_t ywp_num_canonical(const char *lit, size_t len, char out[YWP_REPR_MAX])
{
    lit_t n;
    uint64_t bits;
    if (!scan(lit, len, &n)) { out[0] = 0; return 0; }
    if (n.dig && !n.more && n.nz <= 15 && n.e10 >= -307 &&
        (n.e10 < 308 || (n.e10 == 308 && below_max(&n)))) {
        char dig[15];           /* the literal's own digits: no conversion */
        int nd = 0;
        for (const char *p = n.dig; nd < n.nz; ++p)
            if (*p != '.') dig[nd++] = *p;
        return format(out, n.neg, dig, nd, (int)n.e10 + 1);
    }
    if (!lit_bits(&n, &bits)) bits = INF_BITS;
    return repr_bits(n.neg ? bits | SIGN : bits, out);
}

size_t ywp_num_repr(double value, char out[YWP_REPR_MAX])
{
    uint64_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return repr_bits(bits, out);
}
