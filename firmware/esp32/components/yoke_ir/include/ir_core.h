#pragma once
/* Yoke IR endpoint core: an honest subset of the Global Cache iTach TCP API
 * (port 4998) as a sans-I/O parser and state machine. No ESP-IDF, socket,
 * clock or heap dependency beyond calloc/free at create/destroy; the host
 * tests compile this file unchanged. See doc/design/IR-ENDPOINT.md.
 *
 * The environment owns sockets, time and the RMT peripheral. It feeds bytes
 * and physical-completion events in, drains per-connection output, and
 * implements ir_ops_t. Time is a caller-supplied monotonic microsecond count.
 */
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define IR_MAX_OUTPUTS 4
#define IR_MAX_CONNS 8
#define IR_MAX_PAIRS 260
#define IR_MAX_DURATIONS (2 * IR_MAX_PAIRS)
#define IR_MAX_SYMBOLS 512
#define IR_LINE_MAX 4096
#define IR_OUT_MAX 4096
#define IR_IDENTITY_MAX 96
#define IR_HALF_MAX 32767u
#define IR_LOOKAHEAD_SYMBOLS 48u /* ESP32-S3 RMT channel memory */

/* Bit-identical to ESP-IDF rmt_symbol_word_t.val: duration0[14:0],
 * level0[15], duration1[30:16], level1[31]. A zero duration ends a stream. */
typedef uint32_t ir_symbol_t;

static inline ir_symbol_t ir_symbol(uint32_t d0, unsigned l0, uint32_t d1, unsigned l1)
{
    return (d0 & 0x7fffu) | ((uint32_t)(l0 & 1u) << 15) |
           ((d1 & 0x7fffu) << 16) | ((uint32_t)(l1 & 1u) << 31);
}

/* A validated sendir and its precompiled RMT symbols. Durations are carrier
 * cycles as on the wire; symbols are output ticks at the configured
 * resolution, rounded on cumulative edges so long frames never drift. */
typedef struct {
    uint32_t carrier_hz;
    uint16_t id;
    uint8_t repeat;
    uint16_t offset;            /* 1-based odd index of the first repeated on */
    uint16_t count;             /* durations: even, 2..IR_MAX_DURATIONS */
    uint16_t cycles[IR_MAX_DURATIONS];
    uint16_t nsym;              /* symbols in one full frame */
    uint16_t loop_sym;          /* symbol index where repeats restart */
    uint32_t frame_ticks;       /* full frame (first transmission) */
    uint32_t loop_ticks;        /* repeated part */
    ir_symbol_t sym[IR_MAX_SYMBOLS];
} ir_program_t;

/* Transmission state shared between the task and the encoder, which runs
 * from the RMT ISR (IRAM) on target. `remaining` counts frames still to
 * start after the current one; -1 means the encoder has ended the stream.
 * Only atomics cross that boundary. */
typedef struct {
    const ir_program_t *prog;
    atomic_int remaining;
    uint32_t seq;
    uint16_t pos;
    uint16_t frames;
} ir_tx_t;

/* Simple-encoder callback body (rmt_encode_simple_cb_t semantics):
 * symbols_written==0 starts a transaction. Writes at most `space` symbols
 * and sets *done after the last frame. Never returns 0 unless done. */
size_t ir_tx_fill(ir_tx_t *tx, size_t symbols_written, ir_symbol_t *out,
                  size_t space, bool *done);
/* Smooth repeat: reset remaining frames unless the stream already ended. */
bool ir_tx_extend(ir_tx_t *tx, int frames);
/* Finish after the frame in flight (quiesce/replace); false if ended. */
bool ir_tx_finish_frame(ir_tx_t *tx);

typedef enum {
    IR_OK = 0,
    IR_ERR_COMMAND = 1,       /* ERR_01 invalid command */
    IR_ERR_MODULE = 2,
    IR_ERR_CONNECTOR = 3,
    IR_ERR_ID = 4,
    IR_ERR_FREQUENCY = 5,
    IR_ERR_REPEAT = 6,
    IR_ERR_OFFSET = 7,
    IR_ERR_PULSE_COUNT = 8,
    IR_ERR_PULSE_DATA = 9,
    IR_ERR_UNEVEN = 10,
    IR_ERR_NO_CR = 11,
    IR_ERR_REPEAT_EXCEEDED = 12,
    IR_ERR_BUFFER_FULL = 15,
    IR_ERR_SYNTAX = 17,
    IR_ERR_TX_FAILURE = 19,
    IR_ERR_PAIR_LIMIT = 20,
    IR_ERR_SYMBOL_ODD = 21,
    IR_ERR_SYMBOL_UNDEFINED = 22,
} ir_err_t;

/* Parse the text after "sendir," (address included) into prog. Returns
 * IR_OK or the iTach error. *connector is set (1-based) once the address is
 * valid, else 0. Pure; no compilation. */
ir_err_t ir_parse_sendir(const char *args, size_t len, unsigned outputs,
                         ir_program_t *prog, unsigned *connector);
/* Convert cycles to symbols at resolution_hz; bounded by max_frame_us. */
ir_err_t ir_compile(ir_program_t *prog, uint32_t resolution_hz, uint32_t max_frame_us);
/* Encode the same duration list as a sendir argument string (learning). */
size_t ir_format_sendir(const ir_program_t *prog, unsigned connector, char *out, size_t cap);

typedef struct {
    unsigned outputs;          /* 1..IR_MAX_OUTPUTS connectors 1:1..1:N */
    unsigned max_conns;        /* 1..IR_MAX_CONNS */
    uint32_t resolution_hz;    /* RMT output tick rate */
    uint32_t max_frame_us;     /* one transmission frame */
    uint32_t rate_per_s;       /* sustained command lines per connection */
    uint32_t burst;            /* token bucket depth */
    uint32_t line_timeout_ms;  /* partial line without CR */
    uint32_t evict_idle_ms;    /* evict idle connection when slots full */
    uint32_t stall_timeout_ms; /* unsent output without progress */
    uint32_t learn_timeout_ms;
    bool learning;             /* get_IRL allowed (off by default) */
    char identity[IR_IDENTITY_MAX];
} ir_config_t;

void ir_config_defaults(ir_config_t *cfg);

typedef struct {
    void *ctx;
    /* Start tx on output (0-based). tx stays owned by the core until
     * ir_core_tx_done(seq) or tx_stop returns. Return 0 on success. */
    int (*tx_start)(void *ctx, unsigned output, ir_tx_t *tx);
    /* Synchronous abort; the output is idle when this returns and no
     * completion for that seq is delivered afterwards (stale ones ignored). */
    void (*tx_stop)(void *ctx, unsigned output);
    int (*learn_start)(void *ctx);
    void (*learn_stop)(void *ctx);
} ir_ops_t;

typedef struct {
    uint32_t accepted, refused, evicted, closed_slow, lines, errors;
    uint32_t sendir, completeir, busy, stopped, folded, rate_limited;
    uint32_t learned;
} ir_counters_t;

typedef struct ir_core ir_core_t;

ir_core_t *ir_core_create(const ir_config_t *cfg, const ir_ops_t *ops);
void ir_core_destroy(ir_core_t *core);

/* Connection lifecycle. accept returns a slot or -1 (refused). When it
 * evicts an idle connection to make room, *evicted receives that slot,
 * which the environment must close; else -1. */
int ir_core_accept(ir_core_t *core, uint64_t now_us, int *evicted);
void ir_core_closed(ir_core_t *core, int slot);
/* Bytes the core will accept now (0 under backpressure/rate limit). */
size_t ir_core_rx_space(const ir_core_t *core, int slot);
size_t ir_core_input(ir_core_t *core, int slot, const char *data, size_t len, uint64_t now_us);
/* Pending output for slot; report sent bytes with ir_core_output_sent. */
size_t ir_core_output(const ir_core_t *core, int slot, const char **data);
void ir_core_output_sent(ir_core_t *core, int slot, size_t sent, uint64_t now_us);
bool ir_core_should_close(const ir_core_t *core, int slot);

/* Physical completion of the stream started with this seq. */
void ir_core_tx_done(ir_core_t *core, unsigned output, uint32_t seq, uint64_t now_us);
/* A learned capture: RMT RX symbols at resolution_hz. */
void ir_core_learned(ir_core_t *core, const ir_symbol_t *symbols, size_t count,
                     uint32_t resolution_hz, uint64_t now_us);
/* Timers, refills and deferred lines. Returns the next deadline (absolute
 * microseconds) or UINT64_MAX when nothing is scheduled. */
uint64_t ir_core_poll(ir_core_t *core, uint64_t now_us);

/* Finish frames in flight, refuse new sendir with busyIR; idle check. */
void ir_core_quiesce(ir_core_t *core, uint64_t now_us);
bool ir_core_idle(const ir_core_t *core);
bool ir_core_output_busy(const ir_core_t *core, unsigned output);
void ir_core_set_learning(ir_core_t *core, bool enabled, uint64_t now_us);
const ir_counters_t *ir_core_counters(const ir_core_t *core);
unsigned ir_core_open_conns(const ir_core_t *core);

/* LAN scope: with an allowlist (mask != 0 or allow_all) the peer must be
 * inside it; otherwise it must share the interface's IPv4 subnet. Host
 * byte order. */
bool ir_peer_allowed(uint32_t peer, uint32_t if_addr, uint32_t if_mask,
                     bool allowlist, uint32_t allow_net, uint32_t allow_mask);
/* Strict dotted-quad CIDR ("192.168.20.0/24"); host bits must be zero. */
bool ir_parse_cidr(const char *text, uint32_t *net, uint32_t *mask);

/* Learner: RX symbols (raw carrier or demodulated) to a program.
 * assumed_hz is used only when no carrier is present in the capture. */
ir_err_t ir_learn(const ir_symbol_t *symbols, size_t count, uint32_t resolution_hz,
                  uint32_t assumed_hz, uint32_t lead_out_us, ir_program_t *prog);

/* Boot self-check of parser, compiler and the ISR encoder path. */
bool ir_selftest(uint32_t resolution_hz);
