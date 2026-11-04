/* HOST SIMULATION performance of the yoke_ir core on the build machine's
 * CPU. Numbers characterise algorithmic cost only; ESP32-S3 timing must be
 * measured on the bench (doc/design/IR-ENDPOINT.md). Prints one JSON line. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "ir_core.h"

#define ITERATIONS 4000

static uint64_t ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000u + (uint64_t)t.tv_nsec;
}

static int compare(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return x < y ? -1 : x > y;
}

static uint64_t samples[ITERATIONS];

static void report(const char *name, size_t n, double scale, int last)
{
    qsort(samples, n, sizeof(samples[0]), compare);
    printf("\"%s\":{\"median_ns\":%.1f,\"p99_ns\":%.1f,\"iterations\":%zu}%s", name,
           (double)samples[n / 2] / scale, (double)samples[n * 99 / 100] / scale, n,
           last ? "" : ",");
}

static const char NEC[] =
    "1:1,7,38000,2,69,342,171,21,21,21,21,21,21,21,21,21,21,21,21,21,21,21,21,"
    "21,64,21,64,21,64,21,64,21,64,21,64,21,64,21,64,21,21,21,64,21,21,21,21,"
    "21,64,21,21,21,21,21,21,21,64,21,21,21,64,21,64,21,21,21,64,21,64,21,64,"
    "21,1517,342,85,21,3655";
static char maximum[4096];
static char line[4200];

/* Fake driver: rmt_transmit's synchronous initial fill of channel memory. */
static uint64_t first_symbol_at;
static ir_tx_t *active;
static int tx_start(void *ctx, unsigned out, ir_tx_t *tx)
{
    (void)ctx; (void)out;
    ir_symbol_t memory[IR_LOOKAHEAD_SYMBOLS];
    bool done = false;
    size_t got = ir_tx_fill(tx, 0, memory, 1, &done);
    first_symbol_at = ns();
    if (!got) abort();
    ir_tx_fill(tx, 1, memory + 1, IR_LOOKAHEAD_SYMBOLS - 1, &done);
    active = tx;
    return 0;
}
static void tx_stop(void *ctx, unsigned out) { (void)ctx; (void)out; }

int main(void)
{
    static ir_program_t prog;
    unsigned connector;
    size_t used = (size_t)snprintf(maximum, sizeof(maximum), "1:1,65535,500000,50,383");
    for (int i = 0; i < 520; ++i) used += (size_t)snprintf(maximum + used, sizeof(maximum) - used, ",%d", 10 + i % 90);

    printf("{\"label\":\"HOST SIMULATION on the build machine CPU, not ESP32-S3\",\"results\":{");
    for (int i = 0; i < ITERATIONS; ++i) {
        uint64_t t0 = ns();
        if (ir_parse_sendir(NEC, sizeof(NEC) - 1, 1, &prog, &connector) != IR_OK) return 1;
        samples[i] = ns() - t0;
    }
    report("parse_nec_72", ITERATIONS, 1, 0);
    for (int i = 0; i < ITERATIONS; ++i) {
        ir_parse_sendir(NEC, sizeof(NEC) - 1, 1, &prog, &connector);
        uint64_t t0 = ns();
        if (ir_compile(&prog, 1000000, 2000000) != IR_OK) return 1;
        samples[i] = ns() - t0;
    }
    report("compile_nec_72", ITERATIONS, 1, 0);
    for (int i = 0; i < ITERATIONS; ++i) {
        uint64_t t0 = ns();
        if (ir_parse_sendir(maximum, used, 1, &prog, &connector) != IR_OK ||
            ir_compile(&prog, 1000000, 2000000) != IR_OK) return 1;
        samples[i] = ns() - t0;
    }
    report("parse_compile_max_520", ITERATIONS, 1, 0);

    /* Encoder: per-symbol cost in 24-symbol ISR refills (50 frames). */
    ir_parse_sendir(NEC, sizeof(NEC) - 1, 1, &prog, &connector);
    prog.repeat = 50;
    ir_compile(&prog, 1000000, 2000000);
    for (int i = 0; i < ITERATIONS; ++i) {
        ir_tx_t tx = {.prog = &prog};
        atomic_init(&tx.remaining, 49);
        ir_symbol_t buffer[24];
        size_t written = 0;
        bool done = false;
        uint64_t t0 = ns();
        while (!done) written += ir_tx_fill(&tx, written, buffer, 24, &done);
        samples[i] = (ns() - t0) * 1000u / written; /* in milli-ns */
    }
    report("encode_per_symbol", ITERATIONS, 1000, 0);

    /* Command line received to first RMT symbol, through the whole core. */
    ir_config_t cfg;
    ir_config_defaults(&cfg);
    cfg.rate_per_s = 1000000000u;
    cfg.burst = 1000000000u;
    ir_ops_t ops = {.tx_start = tx_start, .tx_stop = tx_stop};
    ir_core_t *core = ir_core_create(&cfg, &ops);
    int evicted;
    int slot = ir_core_accept(core, 0, &evicted);
    int n = snprintf(line, sizeof(line), "sendir,%s\r", NEC);
    for (int i = 0; i < ITERATIONS; ++i) {
        uint64_t t0 = ns();
        ir_core_input(core, slot, line, (size_t)n, (uint64_t)i * 1000u);
        samples[i] = first_symbol_at - t0;
        ir_core_tx_done(core, 0, active->seq, (uint64_t)i * 1000u);
        const char *data;
        ir_core_output_sent(core, slot, ir_core_output(core, slot, &data), (uint64_t)i * 1000u);
    }
    report("line_to_first_symbol", ITERATIONS, 1, 0);

    /* Smooth repeat: identical line while active (no compile). */
    ir_core_input(core, slot, line, (size_t)n, 0);
    for (int i = 0; i < ITERATIONS; ++i) {
        uint64_t t0 = ns();
        ir_core_input(core, slot, line, (size_t)n, 0);
        samples[i] = ns() - t0;
    }
    report("smooth_repeat_fold", ITERATIONS, 1, 0);
    for (int i = 0; i < ITERATIONS; ++i) {
        uint64_t t0 = ns();
        ir_core_input(core, slot, "getversion\r", 11, 0);
        samples[i] = ns() - t0;
        const char *data;
        ir_core_output_sent(core, slot, ir_core_output(core, slot, &data), 0);
    }
    report("getversion_line", ITERATIONS, 1, 0);
    for (int i = 0; i < ITERATIONS; ++i) {
        uint64_t t0 = ns();
        if (!ir_selftest(1000000)) return 1;
        samples[i] = ns() - t0;
    }
    report("boot_selftest", ITERATIONS, 1, 1);
    printf("},\"program_bytes\":%zu}\n", sizeof(ir_program_t));
    ir_core_destroy(core);
    return 0;
}
