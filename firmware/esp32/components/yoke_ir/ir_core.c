/* iTach-subset connection and output state machine. Pure C (see ir_core.h).
 * Every input is bounded: line length, output buffer, per-connection token
 * bucket, connection slots, pending queue depth (one per output). */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ir_core.h"

#define TOKEN 1000000ull       /* one command line, in micro-tokens */
#define REPLY_RESERVE 256u     /* room needed before handling another line */

typedef struct {
    bool open, close, discarding, learning, throttled, partial;
    uint32_t gen;
    size_t in_start, in_len;   /* unread bytes are in[in_start, in_start+in_len) */
    size_t out_len;
    uint64_t partial_since, last_activity, out_since, tokens, refilled, learn_deadline;
    char in[IR_LINE_MAX];
    char out[IR_OUT_MAX];
} conn_t;

typedef struct {
    ir_program_t *slot[2];
    ir_program_t *active, *pending;
    ir_tx_t tx;
    bool busy;
    int owner, pending_owner;
    uint32_t owner_gen, pending_gen;
} output_t;

struct ir_core {
    ir_config_t cfg;
    ir_ops_t ops;
    ir_program_t *scratch;
    ir_program_t *pool;
    uint32_t seq, gen;
    bool quiescing;
    int learner;
    ir_counters_t counters;
    output_t outputs[IR_MAX_OUTPUTS];
    conn_t *conns;             /* cfg.max_conns entries */
    char line[IR_LINE_MAX];    /* LF-stripped copy, only when needed */
};

void ir_config_defaults(ir_config_t *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->outputs = 1;
    cfg->max_conns = 4;
    cfg->resolution_hz = 1000000;
    cfg->max_frame_us = 2000000;
    cfg->rate_per_s = 20;
    cfg->burst = 40;
    cfg->line_timeout_ms = 10000;
    cfg->evict_idle_ms = 30000;
    cfg->stall_timeout_ms = 10000;
    cfg->learn_timeout_ms = 60000;
    cfg->learning = false;
    snprintf(cfg->identity, sizeof(cfg->identity), "Make=Yoke;Model=yoke-ir;Revision=0");
}

ir_core_t *ir_core_create(const ir_config_t *cfg, const ir_ops_t *ops)
{
    if (!cfg || !ops || cfg->outputs < 1 || cfg->outputs > IR_MAX_OUTPUTS ||
        cfg->max_conns < 1 || cfg->max_conns > IR_MAX_CONNS || !cfg->resolution_hz ||
        !cfg->rate_per_s || !cfg->burst) return NULL;
    ir_core_t *core = calloc(1, sizeof(*core));
    if (!core) return NULL;
    core->pool = calloc(2u * cfg->outputs + 1u, sizeof(ir_program_t));
    core->conns = calloc(cfg->max_conns, sizeof(conn_t));
    if (!core->pool || !core->conns) { ir_core_destroy(core); return NULL; }
    core->cfg = *cfg;
    core->cfg.identity[IR_IDENTITY_MAX - 1] = 0;
    core->ops = *ops;
    core->scratch = &core->pool[2u * cfg->outputs];
    core->learner = -1;
    for (unsigned i = 0; i < cfg->outputs; ++i) {
        output_t *out = &core->outputs[i];
        out->slot[0] = &core->pool[2u * i];
        out->slot[1] = &core->pool[2u * i + 1u];
        out->owner = out->pending_owner = -1;
        atomic_init(&out->tx.remaining, -1);
    }
    return core;
}

void ir_core_destroy(ir_core_t *core)
{
    if (!core) return;
    free(core->conns);
    free(core->pool);
    free(core);
}

static bool alive(const ir_core_t *core, int slot, uint32_t gen)
{
    return slot >= 0 && core->conns[slot].open && core->conns[slot].gen == gen;
}

/* Append one CR-terminated response. A connection that cannot take it is
 * not reading: it is closed rather than letting output grow. */
static void reply(ir_core_t *core, int slot, uint64_t now, const char *fmt, ...)
{
    conn_t *c = &core->conns[slot];
    if (!c->open || c->close) return;
    char line[160];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);
    if (n < 0 || (size_t)n >= sizeof(line)) return;
    if (c->out_len + (size_t)n + 1u > IR_OUT_MAX) {
        c->close = true;
        core->counters.closed_slow++;
        return;
    }
    if (!c->out_len) c->out_since = now;
    memcpy(c->out + c->out_len, line, (size_t)n);
    c->out[c->out_len + (size_t)n] = '\r';
    c->out_len += (size_t)n + 1u;
}

static void reply_error(ir_core_t *core, int slot, uint64_t now, unsigned connector, ir_err_t err)
{
    core->counters.errors++;
    if (connector) reply(core, slot, now, "ERR_1:%u,%03u", connector, (unsigned)err);
    else reply(core, slot, now, "ERR_0:0,%03u", (unsigned)err);
}

static void stop_learning(ir_core_t *core)
{
    if (core->learner < 0) return;
    core->conns[core->learner].learning = false;
    core->learner = -1;
    if (core->ops.learn_stop) core->ops.learn_stop(core->ops.ctx);
}

static ir_program_t *free_slot(output_t *out)
{
    for (int i = 0; i < 2; ++i)
        if (out->slot[i] != out->active && out->slot[i] != out->pending) return out->slot[i];
    return NULL;
}

/* Move the scratch program into a free output slot (pointer swap, no copy). */
static ir_program_t *take_scratch(ir_core_t *core, output_t *out)
{
    ir_program_t *slot = free_slot(out);
    if (!slot) return NULL;
    int index = out->slot[0] == slot ? 0 : 1;
    out->slot[index] = core->scratch;
    core->scratch = slot;
    return out->slot[index];
}

static bool start(ir_core_t *core, unsigned index, ir_program_t *prog, int owner,
                  uint32_t gen, uint64_t now)
{
    output_t *out = &core->outputs[index];
    out->active = prog;
    out->owner = owner;
    out->owner_gen = gen;
    out->tx.prog = prog;
    out->tx.pos = 0;
    out->tx.frames = 0;
    out->tx.seq = ++core->seq;
    atomic_store(&out->tx.remaining, (int)prog->repeat - 1);
    out->busy = true;
    if (core->ops.tx_start(core->ops.ctx, index, &out->tx) == 0) return true;
    out->busy = false;
    out->active = NULL;
    atomic_store(&out->tx.remaining, -1);
    if (alive(core, owner, gen)) reply_error(core, owner, now, index + 1u, IR_ERR_TX_FAILURE);
    return false;
}

static bool same_command(const ir_program_t *a, const ir_program_t *b)
{
    return a->id == b->id && a->carrier_hz == b->carrier_hz && a->repeat == b->repeat &&
           a->offset == b->offset && a->count == b->count &&
           !memcmp(a->cycles, b->cycles, (size_t)a->count * sizeof(a->cycles[0]));
}

static void sendir(ir_core_t *core, int slot, const char *args, size_t len, uint64_t now)
{
    core->counters.sendir++;
    unsigned connector;
    ir_program_t *prog = core->scratch;
    ir_err_t err = ir_parse_sendir(args, len, core->cfg.outputs, prog, &connector);
    if (err != IR_OK) { reply_error(core, slot, now, connector, err); return; }
    unsigned index = connector - 1u;
    output_t *out = &core->outputs[index];
    conn_t *c = &core->conns[slot];
    if (core->quiescing) {
        core->counters.busy++;
        reply(core, slot, now, "busyIR,1:%u,%u", connector, (unsigned)prog->id);
        return;
    }
    bool own = out->busy && out->owner == slot && out->owner_gen == c->gen;
    if (out->busy && !own) {
        core->counters.busy++;
        reply(core, slot, now, "busyIR,1:%u,%u", connector, (unsigned)prog->id);
        return;
    }
    bool identical = own && !out->pending && same_command(out->active, prog);
    if (identical && ir_tx_extend(&out->tx, prog->repeat)) {
        core->counters.folded++; /* smooth repeat: acknowledged by its completeir */
        return;
    }
    if (own && out->pending && same_command(out->pending, prog)) {
        core->counters.folded++; /* already queued in full; nothing to reset */
        return;
    }
    if (own && out->pending) {
        core->counters.busy++;
        reply(core, slot, now, "busyIR,1:%u,%u", connector, (unsigned)prog->id);
        return;
    }
    err = ir_compile(prog, core->cfg.resolution_hz, core->cfg.max_frame_us);
    if (err != IR_OK) { reply_error(core, slot, now, connector, err); return; }
    prog = take_scratch(core, out);
    if (!out->busy) { start(core, index, prog, slot, c->gen, now); return; }
    /* Same connection, new or too-late identical command: halt the current
     * transmission at its frame boundary and run this one next. */
    if (!identical) ir_tx_finish_frame(&out->tx);
    out->pending = prog;
    out->pending_owner = slot;
    out->pending_gen = c->gen;
}

static bool parse_address(const char *args, size_t len, unsigned outputs,
                          unsigned *connector, ir_err_t *err)
{
    if (len != 3 || args[1] != ':' || args[0] < '0' || args[0] > '9' ||
        args[2] < '0' || args[2] > '9') { *err = IR_ERR_SYNTAX; return false; }
    if (args[0] != '1') { *err = IR_ERR_MODULE; return false; }
    unsigned port = (unsigned)(args[2] - '0');
    if (port < 1 || port > outputs) { *err = IR_ERR_CONNECTOR; return false; }
    *connector = port;
    return true;
}

static void stopir(ir_core_t *core, int slot, const char *args, size_t len, uint64_t now)
{
    unsigned connector;
    ir_err_t err;
    if (!parse_address(args, len, core->cfg.outputs, &connector, &err)) {
        reply_error(core, slot, now, 0, err);
        return;
    }
    output_t *out = &core->outputs[connector - 1u];
    if (out->busy) {
        core->ops.tx_stop(core->ops.ctx, connector - 1u);
        out->busy = false;
        atomic_store(&out->tx.remaining, -1);
        core->counters.stopped++;
        if (alive(core, out->owner, out->owner_gen) && out->owner != slot)
            reply(core, out->owner, now, "stopir,1:%u", connector);
        out->active = NULL;
        out->pending = NULL; /* same owner; covered by its stopir */
        out->owner = out->pending_owner = -1;
    }
    reply(core, slot, now, "stopir,1:%u", connector);
}

static void learn(ir_core_t *core, int slot, uint64_t now)
{
    if (!core->cfg.learning || (core->learner >= 0 && core->learner != slot)) {
        reply(core, slot, now, "IR Learner Unavailable");
        return;
    }
    if (core->learner < 0) {
        if (!core->ops.learn_start || core->ops.learn_start(core->ops.ctx) != 0) {
            reply(core, slot, now, "IR Learner Unavailable");
            return;
        }
        core->learner = slot;
        core->conns[slot].learning = true;
    }
    core->conns[slot].learn_deadline = now + (uint64_t)core->cfg.learn_timeout_ms * 1000u;
    reply(core, slot, now, "IR Learner Enabled");
}

static bool is(const char *line, size_t len, const char *word)
{
    size_t n = strlen(word);
    return len == n && !memcmp(line, word, n);
}

static bool prefix(const char *line, size_t len, const char *word)
{
    size_t n = strlen(word);
    return len >= n && !memcmp(line, word, n);
}

static void handle(ir_core_t *core, int slot, const char *line, size_t len, uint64_t now)
{
    conn_t *c = &core->conns[slot];
    core->counters.lines++;
    if (c->learning && !is(line, len, "get_IRL")) stop_learning(core);
    if (is(line, len, "getdevices")) {
        reply(core, slot, now, "device,0,0 ETHERNET");
        reply(core, slot, now, "device,1,%u IR", core->cfg.outputs);
        reply(core, slot, now, "endlistdevices");
    } else if (is(line, len, "getversion")) {
        reply(core, slot, now, "%s", core->cfg.identity);
    } else if (prefix(line, len, "getversion,")) {
        const char *m = line + 11;
        if (len == 12 && (*m == '0' || *m == '1'))
            reply(core, slot, now, "version,%c,%s", *m, core->cfg.identity);
        else reply_error(core, slot, now, 0, IR_ERR_MODULE);
    } else if (prefix(line, len, "sendir,")) {
        sendir(core, slot, line + 7, len - 7, now);
    } else if (prefix(line, len, "stopir,")) {
        stopir(core, slot, line + 7, len - 7, now);
    } else if (is(line, len, "get_IRL")) {
        learn(core, slot, now);
    } else if (is(line, len, "stop_IRL")) {
        reply(core, slot, now, "IR Learner Disabled");
    } else {
        core->counters.errors++;
        reply(core, slot, now, "ERR_01");
    }
}

static void refill(ir_core_t *core, conn_t *c, uint64_t now)
{
    if (now <= c->refilled) return;
    uint64_t cap = (uint64_t)core->cfg.burst * TOKEN;
    uint64_t add = (now - c->refilled) * core->cfg.rate_per_s;
    c->tokens = c->tokens + add > cap ? cap : c->tokens + add;
    c->refilled = now;
}

/* Consume complete lines while tokens and output room allow. */
static void process(ir_core_t *core, int slot, uint64_t now)
{
    conn_t *c = &core->conns[slot];
    refill(core, c, now);
    while (c->open && !c->close) {
        char *start = c->in + c->in_start;
        char *cr = c->in_len ? memchr(start, '\r', c->in_len) : NULL;
        if (!cr) {
            if (c->discarding) { c->in_start = c->in_len = 0; c->partial = false; }
            else if (c->in_len == IR_LINE_MAX) {
                core->counters.errors++;
                reply(core, slot, now, "ERR_15");
                c->discarding = true;
                c->in_len = 0;
                c->partial = false;
            } else if (c->in_len && !c->partial) {
                c->partial = true;
                c->partial_since = now;
            }
            break;
        }
        size_t len = (size_t)(cr - start);
        if (c->discarding) {
            c->discarding = false;
        } else {
            if (c->tokens < TOKEN) {
                if (!c->throttled) core->counters.rate_limited++;
                c->throttled = true;
                break;
            }
            if (IR_OUT_MAX - c->out_len < REPLY_RESERVE) break;
            c->tokens -= TOKEN;
            c->throttled = false;
            /* LF is ignored anywhere so CRLF clients work; CR terminates. */
            const char *line = start;
            size_t n = len;
            if (memchr(start, '\n', len)) {
                n = 0;
                for (size_t i = 0; i < len; ++i)
                    if (start[i] != '\n') core->line[n++] = start[i];
                line = core->line;
            }
            if (n) handle(core, slot, line, n, now);
        }
        c->in_start += len + 1u;
        c->in_len -= len + 1u;
        if (!c->in_len) c->in_start = 0;
        c->partial = false;
    }
}

int ir_core_accept(ir_core_t *core, uint64_t now, int *evicted)
{
    *evicted = -1;
    int slot = -1;
    for (unsigned i = 0; i < core->cfg.max_conns && slot < 0; ++i)
        if (!core->conns[i].open) slot = (int)i;
    if (slot < 0) {
        /* Full: reclaim the longest-idle connection that holds nothing. */
        uint64_t idle = (uint64_t)core->cfg.evict_idle_ms * 1000u, oldest = UINT64_MAX;
        for (unsigned i = 0; i < core->cfg.max_conns; ++i) {
            conn_t *c = &core->conns[i];
            bool holds = c->learning || c->in_len || c->out_len;
            for (unsigned o = 0; o < core->cfg.outputs; ++o)
                holds = holds || (core->outputs[o].busy && core->outputs[o].owner == (int)i);
            if (!holds && now - c->last_activity >= idle && c->last_activity < oldest) {
                oldest = c->last_activity;
                slot = (int)i;
            }
        }
        if (slot < 0) { core->counters.refused++; return -1; }
        ir_core_closed(core, slot);
        core->counters.evicted++;
        *evicted = slot;
    }
    conn_t *c = &core->conns[slot];
    uint32_t gen = ++core->gen;
    memset(c, 0, offsetof(conn_t, in));
    c->open = true;
    c->gen = gen;
    c->last_activity = c->refilled = now;
    c->tokens = (uint64_t)core->cfg.burst * TOKEN;
    core->counters.accepted++;
    return slot;
}

void ir_core_closed(ir_core_t *core, int slot)
{
    if (slot < 0 || slot >= (int)core->cfg.max_conns || !core->conns[slot].open) return;
    if (core->learner == slot) stop_learning(core);
    for (unsigned o = 0; o < core->cfg.outputs; ++o) {
        output_t *out = &core->outputs[o];
        /* An orphaned transmission finishes its bounded remaining frames. */
        if (out->owner == slot) out->owner = -1;
        if (out->pending && out->pending_owner == slot) {
            out->pending = NULL;
            out->pending_owner = -1;
        }
    }
    core->conns[slot].open = false;
    core->conns[slot].gen = 0;
}

size_t ir_core_rx_space(const ir_core_t *core, int slot)
{
    const conn_t *c = &core->conns[slot];
    if (!c->open || c->close) return 0;
    return IR_LINE_MAX - c->in_len;
}

size_t ir_core_input(ir_core_t *core, int slot, const char *data, size_t len, uint64_t now)
{
    conn_t *c = &core->conns[slot];
    if (!c->open || c->close) return 0;
    if (c->in_start + c->in_len + len > IR_LINE_MAX) {
        memmove(c->in, c->in + c->in_start, c->in_len);
        c->in_start = 0;
    }
    size_t room = IR_LINE_MAX - c->in_start - c->in_len;
    if (len > room) len = room;
    memcpy(c->in + c->in_start + c->in_len, data, len);
    c->in_len += len;
    c->last_activity = now;
    process(core, slot, now);
    return len;
}

size_t ir_core_output(const ir_core_t *core, int slot, const char **data)
{
    *data = core->conns[slot].out;
    return core->conns[slot].out_len;
}

void ir_core_output_sent(ir_core_t *core, int slot, size_t sent, uint64_t now)
{
    conn_t *c = &core->conns[slot];
    if (!sent) return;
    if (sent > c->out_len) sent = c->out_len;
    memmove(c->out, c->out + sent, c->out_len - sent);
    c->out_len -= sent;
    c->out_since = now;
    if (c->open && !c->close && c->in_len) process(core, slot, now);
}

bool ir_core_should_close(const ir_core_t *core, int slot)
{
    const conn_t *c = &core->conns[slot];
    return c->open && c->close;
}

void ir_core_tx_done(ir_core_t *core, unsigned index, uint32_t seq, uint64_t now)
{
    if (index >= core->cfg.outputs) return;
    output_t *out = &core->outputs[index];
    if (!out->busy || out->tx.seq != seq) return; /* stale (stopped) stream */
    out->busy = false;
    if (alive(core, out->owner, out->owner_gen)) {
        core->counters.completeir++;
        reply(core, out->owner, now, "completeir,1:%u,%u", index + 1u, (unsigned)out->active->id);
    }
    out->active = NULL;
    out->owner = -1;
    ir_program_t *next = out->pending;
    out->pending = NULL;
    if (!next) return;
    int owner = out->pending_owner;
    out->pending_owner = -1;
    if (!alive(core, owner, out->pending_gen)) return;
    if (core->quiescing) {
        core->counters.busy++;
        reply(core, owner, now, "busyIR,1:%u,%u", index + 1u, (unsigned)next->id);
        return;
    }
    start(core, index, next, owner, out->pending_gen, now);
}

void ir_core_learned(ir_core_t *core, const ir_symbol_t *symbols, size_t count,
                     uint32_t resolution_hz, uint64_t now)
{
    if (core->learner < 0) return;
    ir_program_t *prog = core->scratch;
    if (ir_learn(symbols, count, resolution_hz, 38000, 50000, prog) != IR_OK) return;
    conn_t *c = &core->conns[core->learner];
    size_t n = ir_format_sendir(prog, 1, c->out + c->out_len, IR_OUT_MAX - c->out_len - 1u);
    if (!n) {
        c->close = true;
        core->counters.closed_slow++;
        return;
    }
    if (!c->out_len) c->out_since = now;
    c->out[c->out_len + n] = '\r';
    c->out_len += n + 1u;
    core->counters.learned++;
}

uint64_t ir_core_poll(ir_core_t *core, uint64_t now)
{
    uint64_t next = UINT64_MAX;
#define SOONER(t) do { uint64_t when_ = (t); if (when_ < next) next = when_; } while (0)
    for (unsigned i = 0; i < core->cfg.max_conns; ++i) {
        conn_t *c = &core->conns[i];
        if (!c->open || c->close) continue;
        if (c->in_len) process(core, (int)i, now);
        if (c->close) continue;
        if (c->in_len && memchr(c->in + c->in_start, '\r', c->in_len)) {
            if (c->tokens < TOKEN)
                SOONER(now + (TOKEN - c->tokens + core->cfg.rate_per_s - 1u) / core->cfg.rate_per_s);
        } else if (c->partial) {
            uint64_t deadline = c->partial_since + (uint64_t)core->cfg.line_timeout_ms * 1000u;
            if (now >= deadline) {
                core->counters.errors++;
                c->in_start = c->in_len = 0;
                c->partial = false;
                reply(core, (int)i, now, "ERR_11");
            } else SOONER(deadline);
        }
        if (c->out_len) {
            uint64_t deadline = c->out_since + (uint64_t)core->cfg.stall_timeout_ms * 1000u;
            if (now >= deadline) { c->close = true; core->counters.closed_slow++; continue; }
            SOONER(deadline);
        }
        if (c->learning) {
            if (now >= c->learn_deadline) {
                stop_learning(core);
                reply(core, (int)i, now, "IR Learner Disabled");
            } else SOONER(c->learn_deadline);
        }
    }
#undef SOONER
    return next;
}

void ir_core_quiesce(ir_core_t *core, uint64_t now)
{
    core->quiescing = true;
    stop_learning(core);
    for (unsigned o = 0; o < core->cfg.outputs; ++o) {
        output_t *out = &core->outputs[o];
        if (out->busy) ir_tx_finish_frame(&out->tx);
        if (out->pending) {
            if (alive(core, out->pending_owner, out->pending_gen)) {
                core->counters.busy++;
                reply(core, out->pending_owner, now, "busyIR,1:%u,%u", o + 1u,
                      (unsigned)out->pending->id);
            }
            out->pending = NULL;
            out->pending_owner = -1;
        }
    }
}

bool ir_core_idle(const ir_core_t *core)
{
    for (unsigned o = 0; o < core->cfg.outputs; ++o)
        if (core->outputs[o].busy) return false;
    return true;
}

bool ir_core_output_busy(const ir_core_t *core, unsigned output)
{
    return output < core->cfg.outputs && core->outputs[output].busy;
}

void ir_core_set_learning(ir_core_t *core, bool enabled, uint64_t now)
{
    core->cfg.learning = enabled;
    if (!enabled && core->learner >= 0) {
        int slot = core->learner;
        stop_learning(core);
        reply(core, slot, now, "IR Learner Disabled");
    }
}

const ir_counters_t *ir_core_counters(const ir_core_t *core) { return &core->counters; }

unsigned ir_core_open_conns(const ir_core_t *core)
{
    unsigned n = 0;
    for (unsigned i = 0; i < core->cfg.max_conns; ++i) n += core->conns[i].open;
    return n;
}
