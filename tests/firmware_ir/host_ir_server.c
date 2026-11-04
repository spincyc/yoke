/* Host simulation of the IR endpoint: the unchanged yoke_ir core behind real
 * localhost TCP sockets, with a virtual clock and a model of one RMT TX
 * channel per output. HOST SIMULATION ONLY: no RMT, GPIO, LED or W5500.
 *
 * The RMT model mirrors ESP-IDF v5.5.6 non-DMA TX: a 48-symbol channel
 * memory filled by the simple encoder (ir_tx_fill, the target ISR code)
 * when a transaction starts, then refilled one 24-symbol half each time the
 * hardware has consumed a half. Carrier ticks reproduce rmt_apply_carrier()
 * on an 80 MHz group clock. Time unit: 1 us == 1 output tick (1 MHz).
 *
 * stdout line 1: TCP port. Control on stdin, one reply line each:
 *   advance <us> | now | waveform <out> | clear <out> | counters | quiesce |
 *   learning <0|1> | capture <l,d,l,d...> | selftest | quit
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include "ir_core.h"

#define MEM_SYMBOLS 48u
#define HALF_SYMBOLS 24u
#define GROUP_HZ 80000000u

typedef struct { uint64_t start; uint32_t level, ticks; } half_t;
typedef struct {
    uint64_t start, end;
    uint32_t carrier_hz, total, high, low, frames;
    int stopped;
} trans_t;

typedef struct {
    int running;
    ir_tx_t *tx;
    uint64_t start;          /* hardware start of the transaction */
    uint64_t fill_time;      /* start time of the next symbol to be filled */
    size_t filled, consumed_threshold;
    int enc_done;
    half_t *halves;
    size_t nhalves, cap;
    trans_t trans[1024];
    size_t ntrans;
    uint64_t *sym_start;     /* start time of each filled symbol */
    size_t sym_cap;
} sim_out_t;

static ir_core_t *core;
static sim_out_t sims[IR_MAX_OUTPUTS];
static unsigned outputs = 1;
static uint64_t now_us;
static float duty = 0.33f;
static int learning_active;
static int fds[IR_MAX_CONNS];
static unsigned max_conns = 4;
static int sndbuf;
static int allowlist;
static uint32_t allow_net, allow_mask;
static uint32_t if_addr = 0x7f000001u, if_mask = 0xff000000u;

static void push_half(sim_out_t *s, uint64_t start, uint32_t level, uint32_t ticks)
{
    if (s->nhalves == s->cap) {
        s->cap = s->cap ? s->cap * 2 : 1024;
        s->halves = realloc(s->halves, s->cap * sizeof(*s->halves));
        if (!s->halves) abort();
    }
    s->halves[s->nhalves++] = (half_t){start, level, ticks};
}

static void record_symbol(sim_out_t *s, ir_symbol_t sym)
{
    if (s->filled == s->sym_cap) {
        s->sym_cap = s->sym_cap ? s->sym_cap * 2 : 1024;
        s->sym_start = realloc(s->sym_start, s->sym_cap * sizeof(*s->sym_start));
        if (!s->sym_start) abort();
    }
    s->sym_start[s->filled++] = s->fill_time;
    uint32_t d0 = sym & 0x7fffu, d1 = (sym >> 16) & 0x7fffu;
    if (!d0 || !d1) { fprintf(stderr, "zero-duration half inside stream\n"); abort(); }
    push_half(s, s->fill_time, (sym >> 15) & 1u, d0);
    push_half(s, s->fill_time + d0, (sym >> 31) & 1u, d1);
    s->fill_time += d0 + d1;
}

/* One encoder invocation with `space` free symbols, as the driver does. */
static void encode(sim_out_t *s, size_t space)
{
    ir_symbol_t buffer[MEM_SYMBOLS];
    while (space && !s->enc_done) {
        bool done = false;
        size_t got = ir_tx_fill(s->tx, s->filled, buffer, space, &done);
        for (size_t i = 0; i < got; ++i) record_symbol(s, buffer[i]);
        space -= got;
        if (done) s->enc_done = 1;
        else if (!got) { fprintf(stderr, "encoder returned 0 with space\n"); abort(); }
    }
}

static int tx_start(void *ctx, unsigned out, ir_tx_t *tx)
{
    (void)ctx;
    sim_out_t *s = &sims[out];
    if (s->running) { fprintf(stderr, "start while running\n"); abort(); }
    s->running = 1;
    s->tx = tx;
    s->start = s->fill_time = now_us;
    s->filled = 0;
    s->consumed_threshold = HALF_SYMBOLS;
    s->enc_done = 0;
    if (s->ntrans == sizeof(s->trans) / sizeof(s->trans[0])) abort();
    trans_t *t = &s->trans[s->ntrans++];
    memset(t, 0, sizeof(*t));
    t->start = now_us;
    t->carrier_hz = tx->prog->carrier_hz;
    t->total = GROUP_HZ / t->carrier_hz;           /* rmt_tx_modulate_carrier */
    t->high = (uint32_t)(t->total * duty);
    t->low = t->total - t->high;
    encode(s, MEM_SYMBOLS);
    return 0;
}

static void tx_stop(void *ctx, unsigned out)
{
    (void)ctx;
    sim_out_t *s = &sims[out];
    if (!s->running) return;
    s->running = 0;
    /* Async stop: everything at or after now is never emitted. */
    while (s->nhalves && s->halves[s->nhalves - 1].start >= now_us) s->nhalves--;
    if (s->nhalves) {
        half_t *h = &s->halves[s->nhalves - 1];
        if (h->start + h->ticks > now_us) h->ticks = (uint32_t)(now_us - h->start);
        if (!h->ticks) s->nhalves--;
    }
    trans_t *t = &s->trans[s->ntrans - 1];
    t->stopped = 1;
    t->end = now_us;
    t->frames = s->tx->frames;
}

static int learn_start(void *ctx) { (void)ctx; learning_active = 1; return 0; }
static void learn_stop(void *ctx) { (void)ctx; learning_active = 0; }

/* Next RMT event for an output: a half-memory threshold refill or the
 * physical end of the stream. */
static uint64_t next_event(const sim_out_t *s, int *is_done)
{
    *is_done = 0;
    if (!s->running) return UINT64_MAX;
    if (!s->enc_done && s->consumed_threshold < s->filled)
        return s->sym_start[s->consumed_threshold];
    if (s->enc_done) { *is_done = 1; return s->fill_time; }
    fprintf(stderr, "RMT underrun model error\n");
    abort();
}

static void flush(void);

static void run_event(unsigned out, int is_done)
{
    sim_out_t *s = &sims[out];
    if (is_done) {
        s->running = 0;
        trans_t *t = &s->trans[s->ntrans - 1];
        t->end = now_us;
        t->frames = s->tx->frames;
        ir_core_tx_done(core, out, s->tx->seq, now_us);
    } else {
        s->consumed_threshold += HALF_SYMBOLS;
        encode(s, HALF_SYMBOLS);
    }
}

static void advance(uint64_t delta)
{
    uint64_t target = now_us + delta;
    for (;;) {
        uint64_t when = ir_core_poll(core, now_us);
        int done_kind = 0;
        unsigned which = 0;
        for (unsigned o = 0; o < outputs; ++o) {
            int d;
            uint64_t t = next_event(&sims[o], &d);
            if (t < when || (t == when && d)) { when = t; which = o + 1; done_kind = d; }
        }
        if (when > target) break;
        if (when > now_us) now_us = when;
        if (which) run_event(which - 1, done_kind);
        flush();
    }
    now_us = target;
    ir_core_poll(core, now_us);
    flush();
}

static void close_slot(int slot)
{
    if (fds[slot] >= 0) close(fds[slot]);
    fds[slot] = -1;
    ir_core_closed(core, slot);
}

static void flush(void)
{
    for (unsigned i = 0; i < max_conns; ++i) {
        if (fds[i] < 0) continue;
        const char *data;
        size_t n = ir_core_output(core, (int)i, &data);
        if (n) {
            ssize_t sent = send(fds[i], data, n, MSG_DONTWAIT | MSG_NOSIGNAL);
            if (sent > 0) ir_core_output_sent(core, (int)i, (size_t)sent, now_us);
            else if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
                close_slot((int)i);
                continue;
            }
        }
        if (ir_core_should_close(core, (int)i)) close_slot((int)i);
    }
}

static void accept_one(int listener)
{
    struct sockaddr_in peer;
    socklen_t len = sizeof(peer);
    int fd = accept(listener, (struct sockaddr *)&peer, &len);
    if (fd < 0) return;
    if (!ir_peer_allowed(ntohl(peer.sin_addr.s_addr), if_addr, if_mask, allowlist,
                         allow_net, allow_mask)) { close(fd); return; }
    int evicted;
    int slot = ir_core_accept(core, now_us, &evicted);
    if (evicted >= 0 && fds[evicted] >= 0) { close(fds[evicted]); fds[evicted] = -1; }
    if (slot < 0) { close(fd); return; }
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    fds[slot] = fd;
}

static void read_slot(int slot)
{
    char buffer[IR_LINE_MAX];
    size_t space = ir_core_rx_space(core, slot);
    if (!space) return;
    if (space > sizeof(buffer)) space = sizeof(buffer);
    ssize_t n = recv(fds[slot], buffer, space, MSG_DONTWAIT);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) { close_slot(slot); return; }
    if (n > 0) ir_core_input(core, slot, buffer, (size_t)n, now_us);
}

/* Drain all sockets without blocking, so control commands observe every
 * byte a test wrote before it (loopback writes are already queued). */
static void drain(int listener)
{
    for (int round = 0; round < 64; ++round) {
        struct pollfd p[IR_MAX_CONNS + 1];
        int map[IR_MAX_CONNS + 1], n = 0;
        p[n] = (struct pollfd){listener, POLLIN, 0}; map[n++] = -1;
        for (unsigned i = 0; i < max_conns; ++i) {
            if (fds[i] < 0) continue;
            short events = ir_core_rx_space(core, (int)i) ? POLLIN : 0;
            const char *data;
            if (ir_core_output(core, (int)i, &data)) events |= POLLOUT;
            p[n] = (struct pollfd){fds[i], events, 0}; map[n++] = (int)i;
        }
        if (poll(p, (nfds_t)n, 0) <= 0) return;
        for (int k = 0; k < n; ++k) {
            if (!p[k].revents) continue;
            if (map[k] < 0) accept_one(listener);
            else if (fds[map[k]] >= 0 && (p[k].revents & (POLLIN | POLLHUP | POLLERR)))
                read_slot(map[k]);
        }
        flush();
    }
}

static void print_waveform(unsigned out)
{
    sim_out_t *s = &sims[out];
    printf("{\"group_hz\":%u,\"resolution_hz\":1000000,\"duty\":%.4f,\"transmissions\":[",
           GROUP_HZ, (double)duty);
    for (size_t i = 0; i < s->ntrans; ++i) {
        trans_t *t = &s->trans[i];
        printf("%s{\"start\":%llu,\"end\":%llu,\"carrier_hz\":%u,\"total\":%u,\"high\":%u,"
               "\"low\":%u,\"frames\":%u,\"stopped\":%s}", i ? "," : "",
               (unsigned long long)t->start, (unsigned long long)t->end, t->carrier_hz,
               t->total, t->high, t->low, t->frames, t->stopped ? "true" : "false");
    }
    printf("],\"halves\":[");
    for (size_t i = 0; i < s->nhalves; ++i)
        printf("%s[%llu,%u,%u]", i ? "," : "", (unsigned long long)s->halves[i].start,
               s->halves[i].level, s->halves[i].ticks);
    printf("]}\n");
}

static void control(char *line, int listener)
{
    drain(listener);
    char *arg = strchr(line, ' ');
    if (arg) *arg++ = 0;
    if (!strcmp(line, "advance")) {
        advance(strtoull(arg, NULL, 10));
        drain(listener);
        printf("ok %llu\n", (unsigned long long)now_us);
    } else if (!strcmp(line, "now")) {
        printf("%llu\n", (unsigned long long)now_us);
    } else if (!strcmp(line, "waveform")) {
        print_waveform((unsigned)atoi(arg));
    } else if (!strcmp(line, "clear")) {
        sim_out_t *s = &sims[atoi(arg)];
        s->nhalves = 0;
        s->ntrans = 0;
        printf("ok\n");
    } else if (!strcmp(line, "counters")) {
        const ir_counters_t *c = ir_core_counters(core);
        printf("{\"accepted\":%u,\"refused\":%u,\"evicted\":%u,\"closed_slow\":%u,\"lines\":%u,"
               "\"errors\":%u,\"sendir\":%u,\"completeir\":%u,\"busy\":%u,\"stopped\":%u,"
               "\"folded\":%u,\"rate_limited\":%u,\"learned\":%u,\"open\":%u,\"idle\":%s,"
               "\"learning\":%s}\n", c->accepted, c->refused, c->evicted, c->closed_slow,
               c->lines, c->errors, c->sendir, c->completeir, c->busy, c->stopped, c->folded,
               c->rate_limited, c->learned, ir_core_open_conns(core),
               ir_core_idle(core) ? "true" : "false", learning_active ? "true" : "false");
    } else if (!strcmp(line, "quiesce")) {
        ir_core_quiesce(core, now_us);
        flush();
        printf("ok\n");
    } else if (!strcmp(line, "learning")) {
        ir_core_set_learning(core, atoi(arg) != 0, now_us);
        flush();
        printf("ok\n");
    } else if (!strcmp(line, "capture")) {
        size_t cap = 16, n = 0, halves = 0;
        ir_symbol_t *symbols = calloc(cap, sizeof(*symbols));
        uint32_t pending = 0;
        for (char *tok = strtok(arg, ","); tok; tok = strtok(NULL, ",")) {
            uint32_t level = (uint32_t)strtoul(tok, NULL, 10);
            char *d = strtok(NULL, ",");
            if (!d) break;
            uint32_t ticks = (uint32_t)strtoul(d, NULL, 10);
            while (ticks) {
                uint32_t part = ticks > IR_HALF_MAX ? IR_HALF_MAX : ticks;
                ticks -= part;
                uint32_t half = part | (level << 15);
                if (halves++ & 1u) {
                    if (n == cap) symbols = realloc(symbols, (cap *= 2) * sizeof(*symbols));
                    symbols[n++] = pending | (half << 16);
                } else pending = half;
            }
        }
        if (halves & 1u) {
            if (n == cap) symbols = realloc(symbols, (cap *= 2) * sizeof(*symbols));
            symbols[n++] = pending; /* idle terminator: zero second half */
        }
        ir_core_learned(core, symbols, n, 1000000, now_us);
        free(symbols);
        flush();
        printf("ok\n");
    } else if (!strcmp(line, "selftest")) {
        printf("%s\n", ir_selftest(1000000) ? "ok" : "fail");
    } else if (!strcmp(line, "quit")) {
        exit(0);
    } else {
        printf("error unknown control\n");
    }
    fflush(stdout);
}

int main(int argc, char **argv)
{
    signal(SIGPIPE, SIG_IGN);
    ir_config_t cfg;
    ir_config_defaults(&cfg);
    snprintf(cfg.identity, sizeof(cfg.identity), "Make=Yoke;Model=yoke-ir;Revision=host");
    for (int i = 1; i + 1 < argc; i += 2) {
        const char *k = argv[i], *v = argv[i + 1];
        if (!strcmp(k, "--outputs")) cfg.outputs = (unsigned)atoi(v);
        else if (!strcmp(k, "--conns")) cfg.max_conns = (unsigned)atoi(v);
        else if (!strcmp(k, "--rate")) cfg.rate_per_s = (uint32_t)atoi(v);
        else if (!strcmp(k, "--burst")) cfg.burst = (uint32_t)atoi(v);
        else if (!strcmp(k, "--learning")) cfg.learning = atoi(v) != 0;
        else if (!strcmp(k, "--sndbuf")) sndbuf = atoi(v);
        else if (!strcmp(k, "--duty")) duty = (float)atof(v);
        else if (!strcmp(k, "--allow")) {
            allowlist = 1;
            if (!ir_parse_cidr(v, &allow_net, &allow_mask)) return 2;
        } else return 2;
    }
    outputs = cfg.outputs;
    max_conns = cfg.max_conns;
    ir_ops_t ops = {.tx_start = tx_start, .tx_stop = tx_stop,
                    .learn_start = learn_start, .learn_stop = learn_stop};
    core = ir_core_create(&cfg, &ops);
    if (!core) return 3;
    for (unsigned i = 0; i < IR_MAX_CONNS; ++i) fds[i] = -1;
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    if (sndbuf) { /* small kernel buffers make slow-reader tests fast */
        setsockopt(listener, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
        setsockopt(listener, SOL_SOCKET, SO_RCVBUF, &sndbuf, sizeof(sndbuf));
    }
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    if (bind(listener, (struct sockaddr *)&addr, sizeof(addr)) || listen(listener, 8)) return 4;
    socklen_t len = sizeof(addr);
    getsockname(listener, (struct sockaddr *)&addr, &len);
    fcntl(listener, F_SETFL, fcntl(listener, F_GETFL, 0) | O_NONBLOCK);
    printf("%u\n", (unsigned)ntohs(addr.sin_port));
    fflush(stdout);
    char line[1 << 20];
    size_t used = 0;
    for (;;) {
        struct pollfd p[IR_MAX_CONNS + 2];
        int map[IR_MAX_CONNS + 2], n = 0;
        p[n] = (struct pollfd){0, POLLIN, 0}; map[n++] = -2;
        p[n] = (struct pollfd){listener, POLLIN, 0}; map[n++] = -1;
        for (unsigned i = 0; i < max_conns; ++i) {
            if (fds[i] < 0) continue;
            short events = ir_core_rx_space(core, (int)i) ? POLLIN : 0;
            const char *data;
            if (ir_core_output(core, (int)i, &data)) events |= POLLOUT;
            p[n] = (struct pollfd){fds[i], events, 0}; map[n++] = (int)i;
        }
        if (poll(p, (nfds_t)n, -1) < 0) continue;
        for (int k = 1; k < n; ++k) {
            if (!p[k].revents) continue;
            if (map[k] == -1) accept_one(listener);
            else if (fds[map[k]] >= 0 && (p[k].revents & (POLLIN | POLLHUP | POLLERR)))
                read_slot(map[k]);
        }
        flush();
        if (p[0].revents & (POLLIN | POLLHUP)) {
            ssize_t got = read(0, line + used, sizeof(line) - used - 1);
            if (got <= 0) return 0;
            used += (size_t)got;
            char *nl;
            while ((nl = memchr(line, '\n', used))) {
                *nl = 0;
                control(line, listener);
                size_t rest = used - (size_t)(nl + 1 - line);
                memmove(line, nl + 1, rest);
                used = rest;
            }
        }
    }
}
