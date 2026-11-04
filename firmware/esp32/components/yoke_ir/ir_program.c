/* sendir parsing (including the compressed A-O pair form), cycle-to-tick
 * compilation, learning conversion and the boot self-check. Pure C. */
#include <stdio.h>
#include <string.h>
#include "ir_core.h"

#define MAX_LETTERS 15

static bool digit(char c) { return c >= '0' && c <= '9'; }

/* 1..max_digits decimal digits; leaves *p after them. */
static bool number(const char **p, const char *end, unsigned max_digits, uint32_t *value)
{
    const char *s = *p;
    uint32_t v = 0;
    unsigned n = 0;
    while (s < end && digit(*s)) {
        if (++n > max_digits) return false;
        v = v * 10u + (uint32_t)(*s++ - '0');
    }
    if (!n) return false;
    *p = s;
    *value = v;
    return true;
}

static bool comma(const char **p, const char *end)
{
    if (*p >= end || **p != ',') return false;
    ++*p;
    return true;
}

ir_err_t ir_parse_sendir(const char *args, size_t len, unsigned outputs,
                         ir_program_t *prog, unsigned *connector)
{
    const char *p = args, *end = args + len;
    uint32_t module, port, value;
    *connector = 0;
    if (!number(&p, end, 3, &module) || p >= end || *p++ != ':' ||
        !number(&p, end, 3, &port) || !comma(&p, end)) return IR_ERR_SYNTAX;
    if (module != 1) return IR_ERR_MODULE;
    if (port < 1 || port > outputs) return IR_ERR_CONNECTOR;
    *connector = port;
    if (!number(&p, end, 5, &value) || value > 65535 || !comma(&p, end)) return IR_ERR_ID;
    prog->id = (uint16_t)value;
    if (!number(&p, end, 6, &value) || value < 15000 || value > 500000 || !comma(&p, end))
        return IR_ERR_FREQUENCY;
    prog->carrier_hz = value;
    if (!number(&p, end, 9, &value) || !value) return IR_ERR_REPEAT;
    if (value > 50) return IR_ERR_REPEAT_EXCEEDED;
    if (!comma(&p, end)) return IR_ERR_REPEAT;
    prog->repeat = (uint8_t)value;
    if (!number(&p, end, 3, &value) || !(value & 1u) || value > 383 || !comma(&p, end))
        return IR_ERR_OFFSET;
    prog->offset = (uint16_t)value;
    if (p >= end) return IR_ERR_PULSE_COUNT;

    uint16_t letters[MAX_LETTERS][2];
    unsigned nletters = 0, count = 0;
    bool need_item = true; /* after a comma, an item must follow */
    while (p < end) {
        char c = *p;
        if (digit(c)) {
            if (!number(&p, end, 5, &value) || !value || value > 65535) return IR_ERR_PULSE_DATA;
            if (count >= IR_MAX_DURATIONS) return IR_ERR_PAIR_LIMIT;
            prog->cycles[count++] = (uint16_t)value;
            if (!(count & 1u) && nletters < MAX_LETTERS) {
                uint16_t on = prog->cycles[count - 2], off = prog->cycles[count - 1];
                bool known = false;
                for (unsigned i = 0; i < nletters && !known; ++i)
                    known = letters[i][0] == on && letters[i][1] == off;
                if (!known) {
                    letters[nletters][0] = on;
                    letters[nletters][1] = off;
                    ++nletters;
                }
            }
            need_item = false;
        } else if (c >= 'A' && c <= 'O') {
            if (count & 1u) return IR_ERR_SYMBOL_ODD;
            unsigned index = (unsigned)(c - 'A');
            if (index >= nletters) return IR_ERR_SYMBOL_UNDEFINED;
            if (count + 2 > IR_MAX_DURATIONS) return IR_ERR_PAIR_LIMIT;
            prog->cycles[count++] = letters[index][0];
            prog->cycles[count++] = letters[index][1];
            ++p;
            need_item = false;
        } else if (c == ',') {
            if (need_item) return IR_ERR_PULSE_DATA;
            ++p;
            need_item = true;
        } else {
            return IR_ERR_PULSE_DATA;
        }
    }
    if (need_item) return count ? IR_ERR_PULSE_DATA : IR_ERR_PULSE_COUNT;
    if (count & 1u) return IR_ERR_UNEVEN;
    if (prog->offset > count - 1) return IR_ERR_OFFSET;
    prog->count = (uint16_t)count;
    return IR_OK;
}

/* Split t ticks into k nearly equal halves; caller ensures t >= k. */
static void split(uint32_t t, unsigned k, unsigned level, uint32_t *halves,
                  uint8_t *levels, unsigned *n)
{
    uint32_t base = t / k, extra = t % k;
    for (unsigned i = 0; i < k; ++i) {
        halves[*n] = base + (i < extra ? 1u : 0u);
        levels[(*n)++] = (uint8_t)level;
    }
}

ir_err_t ir_compile(ir_program_t *prog, uint32_t resolution_hz, uint32_t max_frame_us)
{
    const uint64_t f = prog->carrier_hz, r = resolution_hz;
    uint64_t total = 0;
    for (unsigned i = 0; i < prog->count; ++i) total += prog->cycles[i];
    /* Frame bound in exact integer arithmetic: total/f <= max_frame_us/1e6. */
    if (total * 1000000u > (uint64_t)max_frame_us * f) return IR_ERR_PULSE_DATA;
    uint64_t cumulative = 0, previous = 0, loop_edge = 0;
    unsigned nsym = 0, loop_pair = (prog->offset - 1u) / 2u;
    for (unsigned pair = 0; pair < prog->count / 2u; ++pair) {
        uint32_t ticks[2];
        for (unsigned j = 0; j < 2; ++j) {
            cumulative += prog->cycles[pair * 2u + j];
            uint64_t edge = (cumulative * r + f / 2u) / f;
            if (edge == previous) return IR_ERR_PULSE_DATA;
            ticks[j] = (uint32_t)(edge - previous);
            previous = edge;
        }
        if (pair == loop_pair) {
            prog->loop_sym = (uint16_t)nsym;
            loop_edge = previous - ticks[0] - ticks[1];
        }
        unsigned k[2] = {(ticks[0] + IR_HALF_MAX - 1u) / IR_HALF_MAX,
                         (ticks[1] + IR_HALF_MAX - 1u) / IR_HALF_MAX};
        if (pair + 1u == prog->count / 2u) {
            /* Lookahead bound: the encoder runs at most one channel memory
             * (IR_LOOKAHEAD_SYMBOLS) ahead of the pin. Spreading the final
             * gap over enough same-level halves makes every repeated region
             * at least that long, so each repeat decision is taken while
             * its predecessor is physically on the pin (inside its final
             * gap). The waveform is unchanged. */
            unsigned before = nsym - prog->loop_sym;
            unsigned want = before < IR_LOOKAHEAD_SYMBOLS ? IR_LOOKAHEAD_SYMBOLS - before : 1u;
            unsigned halves = 2u * want > k[0] ? 2u * want - k[0] : k[1];
            if (halves > ticks[1]) halves = ticks[1];
            if (halves > k[1]) k[1] = halves;
        }
        if ((k[0] + k[1]) & 1u) {
            /* Whole symbols per pair: one more (or one fewer) half. */
            unsigned longer = ticks[1] > ticks[0] ? 1u : 0u, shorter = 1u - longer;
            if (ticks[longer] > k[longer]) k[longer]++;
            else if (ticks[shorter] > k[shorter]) k[shorter]++;
            else if (k[1] > 1u) k[1]--; /* all halves are one tick: merge two */
            else if (k[0] > 1u) k[0]--;
            else return IR_ERR_PULSE_DATA;
        }
        if (nsym + (k[0] + k[1]) / 2u > IR_MAX_SYMBOLS) return IR_ERR_PULSE_DATA;
        uint32_t halves[8];
        uint8_t levels[8];
        unsigned n = 0;
        if (k[0] + k[1] <= 8) {
            split(ticks[0], k[0], 1, halves, levels, &n);
            split(ticks[1], k[1], 0, halves, levels, &n);
            for (unsigned i = 0; i < n; i += 2)
                prog->sym[nsym++] = ir_symbol(halves[i], levels[i], halves[i + 1], levels[i + 1]);
        } else {
            /* Rare very long durations: stream halves pairwise. */
            uint32_t pending = 0;
            unsigned pending_level = 0;
            bool have = false;
            for (unsigned j = 0; j < 2; ++j) {
                uint32_t base = ticks[j] / k[j], extra = ticks[j] % k[j];
                for (unsigned i = 0; i < k[j]; ++i) {
                    uint32_t d = base + (i < extra ? 1u : 0u);
                    unsigned level = j == 0 ? 1u : 0u;
                    if (!have) { pending = d; pending_level = level; have = true; }
                    else { prog->sym[nsym++] = ir_symbol(pending, pending_level, d, level); have = false; }
                }
            }
        }
    }
    prog->nsym = (uint16_t)nsym;
    prog->frame_ticks = (uint32_t)previous;
    prog->loop_ticks = (uint32_t)(previous - loop_edge);
    return IR_OK;
}

size_t ir_format_sendir(const ir_program_t *prog, unsigned connector, char *out, size_t cap)
{
    int n = snprintf(out, cap, "sendir,1:%u,%u,%lu,%u,%u", connector, (unsigned)prog->id,
                     (unsigned long)prog->carrier_hz, (unsigned)prog->repeat,
                     (unsigned)prog->offset);
    if (n < 0 || (size_t)n >= cap) return 0;
    size_t used = (size_t)n;
    for (unsigned i = 0; i < prog->count; ++i) {
        n = snprintf(out + used, cap - used, ",%u", (unsigned)prog->cycles[i]);
        if (n < 0 || (size_t)n >= cap - used) return 0;
        used += (size_t)n;
    }
    return used;
}

bool ir_parse_cidr(const char *text, uint32_t *net, uint32_t *mask)
{
    if (!text) return false;
    const char *p = text, *end = text + strlen(text);
    uint32_t address = 0, value;
    for (int i = 0; i < 4; ++i) {
        if (!number(&p, end, 3, &value) || value > 255) return false;
        address = (address << 8) | value;
        if (i < 3 && (p >= end || *p++ != '.')) return false;
    }
    if (p >= end || *p++ != '/' || !number(&p, end, 2, &value) || value > 32 || p != end)
        return false;
    uint32_t m = value ? 0xffffffffu << (32u - value) : 0u;
    if (address & ~m) return false;
    *net = address;
    *mask = m;
    return true;
}

bool ir_peer_allowed(uint32_t peer, uint32_t if_addr, uint32_t if_mask,
                     bool allowlist, uint32_t allow_net, uint32_t allow_mask)
{
    if (peer == 0 || peer == 0xffffffffu) return false;
    if (allowlist) return (peer & allow_mask) == allow_net;
    if (!if_mask || !if_addr) return false;
    return (peer & if_mask) == (if_addr & if_mask);
}

/* Streaming view of RX symbols as merged (level, ticks) segments; a zero
 * duration ends the capture. No intermediate buffer: learning runs in the
 * IR task and its capture can hold thousands of carrier pulses. */
typedef struct {
    const ir_symbol_t *symbols;
    size_t count, index;
    unsigned half, peek_level;
    uint32_t peek_ticks;
    bool peeked, ended;
} segments_t;

static bool raw_half(segments_t *it, unsigned *level, uint32_t *ticks)
{
    if (it->ended || it->index >= it->count) return false;
    uint32_t word = it->symbols[it->index] >> (it->half * 16u);
    if (++it->half == 2) { it->half = 0; ++it->index; }
    *ticks = word & 0x7fffu;
    *level = (word >> 15) & 1u;
    if (!*ticks) it->ended = true;
    return *ticks != 0;
}

static bool segment(segments_t *it, unsigned *level, uint32_t *ticks)
{
    if (!it->peeked && !raw_half(it, &it->peek_level, &it->peek_ticks)) return false;
    it->peeked = false;
    *level = it->peek_level;
    uint64_t total = it->peek_ticks;
    unsigned next_level;
    uint32_t next_ticks;
    while (raw_half(it, &next_level, &next_ticks)) {
        if (next_level != *level) {
            it->peek_level = next_level;
            it->peek_ticks = next_ticks;
            it->peeked = true;
            break;
        }
        total += next_ticks;
    }
    *ticks = total > UINT32_MAX ? UINT32_MAX : (uint32_t)total;
    return true;
}

typedef struct {
    uint64_t start, mark_end;
    unsigned pulses;
} burst_t;

/* Group marks into bursts: a space longer than gap ends a burst (gap 0:
 * every mark is a burst). Calls back once per burst, in order. */
typedef bool (*burst_fn)(void *ctx, const burst_t *burst, uint64_t last_pulse);

static bool walk(const ir_symbol_t *symbols, size_t count, uint32_t gap, burst_fn fn, void *ctx)
{
    segments_t it = {.symbols = symbols, .count = count};
    unsigned level, mark = 2;
    uint32_t ticks;
    uint64_t t = 0, last_pulse = 0;
    burst_t burst = {0};
    while (segment(&it, &level, &ticks)) {
        if (mark == 2) mark = level;
        if (level == mark) {
            if (!burst.pulses) burst.start = t;
            last_pulse = t;
            burst.pulses++;
            burst.mark_end = t + ticks;
        } else if (burst.pulses && ticks > gap) {
            if (!fn(ctx, &burst, last_pulse)) return false;
            burst.pulses = 0;
        }
        t += ticks;
    }
    return !burst.pulses || fn(ctx, &burst, last_pulse);
}

typedef struct {
    uint64_t span_ticks, span_periods;
} carrier_t;

static bool measure(void *ctx, const burst_t *burst, uint64_t last_pulse)
{
    carrier_t *c = ctx;
    c->span_ticks += last_pulse - burst->start;
    c->span_periods += burst->pulses - 1u;
    return true;
}

typedef struct {
    ir_program_t *prog;
    uint64_t carrier, resolution;
    bool modulated, have;
    burst_t previous;
    ir_err_t error;
} emit_t;

static uint64_t to_cycles(const emit_t *e, uint64_t ticks)
{
    return (ticks * e->carrier + e->resolution / 2u) / e->resolution;
}

static bool push(emit_t *e, uint64_t on, uint64_t off)
{
    if (e->prog->count + 2u > IR_MAX_DURATIONS) { e->error = IR_ERR_PAIR_LIMIT; return false; }
    if (!on) on = 1;
    if (!off) off = 1;
    e->prog->cycles[e->prog->count++] = (uint16_t)(on > 65535u ? 65535u : on);
    e->prog->cycles[e->prog->count++] = (uint16_t)(off > 65535u ? 65535u : off);
    return true;
}

/* Emit the previous burst once the next start is known (edge-anchored, so
 * rounding never accumulates). Modulated on-counts are exact pulse counts. */
static bool emit(void *ctx, const burst_t *burst, uint64_t last_pulse)
{
    (void)last_pulse;
    emit_t *e = ctx;
    if (e->have) {
        const burst_t *p = &e->previous;
        uint64_t start = to_cycles(e, p->start), next = to_cycles(e, burst->start);
        uint64_t on = e->modulated ? p->pulses : to_cycles(e, p->mark_end) - start;
        if (!on) on = 1;
        if (!push(e, on, next > start + on ? next - start - on : 1u)) return false;
    }
    e->previous = *burst;
    e->have = true;
    return true;
}

ir_err_t ir_learn(const ir_symbol_t *symbols, size_t count, uint32_t resolution_hz,
                  uint32_t assumed_hz, uint32_t lead_out_us, ir_program_t *prog)
{
    /* Pass 1: is a carrier present? Most marks carrier-period short. */
    const uint64_t r = resolution_hz;
    const uint32_t short_ticks = (uint32_t)(r * 100u / 1000000u); /* 100 us */
    segments_t it = {.symbols = symbols, .count = count};
    unsigned level, mark = 2, marks = 0, short_marks = 0, periods = 0;
    uint32_t ticks, last_mark = 0;
    uint64_t period_sum = 0;
    bool after_mark = false;
    while (segment(&it, &level, &ticks)) {
        if (mark == 2) mark = level;
        if (level == mark) {
            ++marks;
            if (ticks <= short_ticks) ++short_marks;
            last_mark = ticks;
            after_mark = true;
        } else if (after_mark) {
            if (last_mark <= short_ticks && ticks <= short_ticks) {
                period_sum += (uint64_t)last_mark + ticks;
                ++periods;
            }
            after_mark = false;
        }
    }
    if (!marks) return IR_ERR_PULSE_COUNT;
    bool modulated = periods >= 8 && short_marks * 5u >= marks * 4u;
    uint32_t gap = 0;
    uint64_t carrier = assumed_hz;
    if (modulated) {
        gap = (uint32_t)(period_sum / periods) * 3u;
        carrier_t c = {0};
        walk(symbols, count, gap, measure, &c);
        if (!c.span_periods || !c.span_ticks) return IR_ERR_PULSE_COUNT;
        carrier = (c.span_periods * r + c.span_ticks / 2u) / c.span_ticks;
    }
    if (carrier < 15000 || carrier > 500000) return IR_ERR_FREQUENCY;
    prog->carrier_hz = (uint32_t)carrier;
    prog->id = 0;
    prog->repeat = 1;
    prog->offset = 1;
    prog->count = 0;
    emit_t e = {.prog = prog, .carrier = carrier, .resolution = r, .modulated = modulated,
                .error = IR_OK};
    if (!walk(symbols, count, gap, emit, &e)) return e.error;
    if (!e.have) return IR_ERR_PULSE_COUNT;
    const burst_t *p = &e.previous;
    uint64_t on = modulated ? p->pulses : to_cycles(&e, p->mark_end) - to_cycles(&e, p->start);
    if (!push(&e, on, ((uint64_t)lead_out_us * carrier + 500000u) / 1000000u)) return e.error;
    return IR_OK;
}

bool ir_selftest(uint32_t resolution_hz)
{
    /* NEC address 0x00, command 0x12 at 38 kHz with a 9 ms repeat code. */
    static const char line[] =
        "1:1,7,38000,2,69,342,171,21,21,21,21,21,21,21,21,21,21,21,21,21,21,21,21,"
        "21,64,21,64,21,64,21,64,21,64,21,64,21,64,21,64,21,21,21,64,21,21,21,21,"
        "21,64,21,21,21,21,21,21,21,64,21,21,21,64,21,64,21,21,21,64,21,64,21,64,"
        "21,1517,342,85,21,3655";
    static ir_program_t prog;
    static ir_symbol_t buffer[24];
    unsigned connector;
    if (ir_parse_sendir(line, sizeof(line) - 1, 1, &prog, &connector) != IR_OK ||
        connector != 1 || prog.count != 72 || prog.offset != 69) return false;
    if (ir_compile(&prog, resolution_hz, 1000000) != IR_OK || !prog.loop_sym) return false;
    ir_tx_t tx = {.prog = &prog, .seq = 1};
    atomic_init(&tx.remaining, prog.repeat - 1);
    size_t written = 0;
    uint64_t ticks = 0;
    bool done = false;
    unsigned chunk = 1;
    while (!done) {
        size_t got = ir_tx_fill(&tx, written, buffer, chunk, &done);
        if (!got && !done) return false;
        for (size_t i = 0; i < got; ++i) {
            if (!(buffer[i] & 0x7fffu) || !((buffer[i] >> 16) & 0x7fffu)) return false;
            ticks += (buffer[i] & 0x7fffu) + ((buffer[i] >> 16) & 0x7fffu);
        }
        written += got;
        chunk = chunk % 24u + 1u;
        if (written > 4u * IR_MAX_SYMBOLS) return false;
    }
    return tx.frames == 2 && written == (size_t)prog.nsym * 2u - prog.loop_sym &&
           ticks == (uint64_t)prog.frame_ticks + prog.loop_ticks &&
           atomic_load(&tx.remaining) == -1;
}
