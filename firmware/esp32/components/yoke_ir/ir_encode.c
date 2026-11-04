/* RMT simple-encoder body. On target the whole object is placed in IRAM by
 * linker.lf so it keeps running from the RMT ISR while flash writes disable
 * the cache (CONFIG_RMT_TX_ISR_CACHE_SAFE). It only copies precompiled
 * symbols from DRAM and uses 32-bit atomics: no division, no flash data,
 * no library calls. The host simulation executes this same file. */
#include "ir_core.h"

size_t ir_tx_fill(ir_tx_t *tx, size_t symbols_written, ir_symbol_t *out,
                  size_t space, bool *done)
{
    const ir_program_t *prog = tx->prog;
    if (symbols_written == 0) {
        tx->pos = 0;
        tx->frames = 1;
    }
    size_t n = 0;
    while (n < space) {
        if (tx->pos < prog->nsym) {
            size_t chunk = prog->nsym - tx->pos;
            if (chunk > space - n) chunk = space - n;
            for (size_t i = 0; i < chunk; ++i) out[n + i] = prog->sym[tx->pos + i];
            n += chunk;
            tx->pos += (uint16_t)chunk;
            continue;
        }
        /* Frame boundary: one atomic decision per frame. */
        int left = atomic_load_explicit(&tx->remaining, memory_order_acquire);
        for (;;) {
            if (left <= 0) {
                if (atomic_compare_exchange_weak_explicit(&tx->remaining, &left, -1,
                        memory_order_acq_rel, memory_order_acquire)) break;
                continue;
            }
            if (atomic_compare_exchange_weak_explicit(&tx->remaining, &left, left - 1,
                    memory_order_acq_rel, memory_order_acquire)) break;
        }
        if (left <= 0) {
            *done = true;
            return n;
        }
        tx->pos = prog->loop_sym;
        tx->frames++;
    }
    return n;
}

bool ir_tx_extend(ir_tx_t *tx, int frames)
{
    int left = atomic_load_explicit(&tx->remaining, memory_order_acquire);
    while (left >= 0) {
        if (atomic_compare_exchange_weak_explicit(&tx->remaining, &left, frames,
                memory_order_acq_rel, memory_order_acquire)) return true;
    }
    return false;
}

bool ir_tx_finish_frame(ir_tx_t *tx)
{
    return ir_tx_extend(tx, 0);
}
