/* ESP32-S3 RMT glue: one TX channel per IR connector with hardware carrier,
 * the yoke_ir encoder as an RMT simple encoder, and optional RX learning.
 * ISR-reachable code (callbacks, encoder) is IRAM and touches DRAM only, so
 * TX keeps running while OTA flash writes disable the cache
 * (CONFIG_RMT_TX_ISR_CACHE_SAFE / CONFIG_RMT_RX_ISR_CACHE_SAFE). */
#include <stdlib.h>
#include <string.h>
#include "driver/gpio.h"
#include "driver/rmt_rx.h"
#include "driver/rmt_tx.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "hal/gpio_ll.h"
#include "soc/gpio_sig_map.h"
#include "ir_rmt.h"

#if !CONFIG_RMT_TX_ISR_CACHE_SAFE || !CONFIG_RMT_ENCODER_FUNC_IN_IRAM
#error "IR output must survive OTA flash writes: enable CONFIG_RMT_TX_ISR_CACHE_SAFE"
#endif
_Static_assert(sizeof(rmt_symbol_word_t) == sizeof(ir_symbol_t), "RMT symbol layout");

#define LEARN_SYMBOLS 4096u
#define LEARN_IDLE_NS 20000000u   /* 20 ms of silence ends a capture */
#define LEARN_GLITCH_NS 1000u

typedef struct {
    rmt_channel_handle_t channel;
    rmt_encoder_handle_t encoder;
    volatile uint32_t seq;
    uint8_t index;
} output_t;

static const char *TAG = "yoke-ir-rmt";
static output_t outputs[IR_MAX_OUTPUTS];
static ir_rmt_config_t config;
static rmt_channel_handle_t learn_channel;
/* Copies for the panic path (DRAM, read with the cache possibly off). */
static DRAM_ATTR int panic_gpio[IR_MAX_OUTPUTS];
static DRAM_ATTR unsigned panic_outputs, panic_idle;
static rmt_symbol_word_t *learn_buffer;
static bool learn_active;

static size_t IRAM_ATTR encode(const void *data, size_t data_size, size_t written,
                               size_t space, rmt_symbol_word_t *symbols, bool *done, void *arg)
{
    (void)data_size;
    (void)arg;
    return ir_tx_fill((ir_tx_t *)(uintptr_t)data, written, (ir_symbol_t *)symbols, space, done);
}

static bool IRAM_ATTR tx_done(rmt_channel_handle_t channel, const rmt_tx_done_event_data_t *data,
                              void *ctx)
{
    (void)channel;
    (void)data;
    output_t *out = ctx;
    ir_event_t event = {.kind = IR_EVENT_TX_DONE, .output = out->index, .seq = out->seq};
    BaseType_t woken = pdFALSE;
    xQueueSendFromISR(config.events, &event, &woken);
    return woken == pdTRUE;
}

static bool IRAM_ATTR rx_done(rmt_channel_handle_t channel, const rmt_rx_done_event_data_t *data,
                              void *ctx)
{
    (void)channel;
    QueueHandle_t queue = ctx;
    ir_event_t event = {.kind = IR_EVENT_RX_DONE,
                        .count = (uint16_t)(data->num_symbols > UINT16_MAX ? UINT16_MAX
                                                                            : data->num_symbols)};
    BaseType_t woken = pdFALSE;
    xQueueSendFromISR(queue, &event, &woken);
    return woken == pdTRUE;
}

esp_err_t ir_rmt_init(const ir_rmt_config_t *cfg)
{
    config = *cfg;
    for (unsigned i = 0; i < cfg->outputs; ++i) {
        output_t *out = &outputs[i];
        out->index = (uint8_t)i;
        rmt_tx_channel_config_t tx = {
            .gpio_num = cfg->gpio[i],
            .clk_src = RMT_CLK_SRC_DEFAULT, /* APB 80 MHz: carrier and tick base */
            .resolution_hz = IR_RMT_RESOLUTION_HZ,
            .mem_block_symbols = IR_LOOKAHEAD_SYMBOLS,
            .trans_queue_depth = 2,
            .flags.invert_out = cfg->invert,
        };
        ESP_RETURN_ON_ERROR(rmt_new_tx_channel(&tx, &out->channel), TAG, "TX channel %u", i);
        rmt_simple_encoder_config_t enc = {.callback = encode, .min_chunk_size = 1};
        ESP_RETURN_ON_ERROR(rmt_new_simple_encoder(&enc, &out->encoder), TAG, "encoder");
        rmt_tx_event_callbacks_t callbacks = {.on_trans_done = tx_done};
        ESP_RETURN_ON_ERROR(rmt_tx_register_event_callbacks(out->channel, &callbacks, out), TAG,
                            "TX callbacks");
        ESP_RETURN_ON_ERROR(rmt_enable(out->channel), TAG, "TX enable");
        panic_gpio[i] = cfg->gpio[i];
        panic_outputs = i + 1;
    }
    panic_idle = cfg->invert ? 1u : 0u;
    if (cfg->learn_gpio >= 0) {
        rmt_rx_channel_config_t rx = {
            .gpio_num = cfg->learn_gpio,
            .clk_src = RMT_CLK_SRC_DEFAULT,
            .resolution_hz = IR_RMT_RESOLUTION_HZ,
            .mem_block_symbols = IR_LOOKAHEAD_SYMBOLS,
        };
        ESP_RETURN_ON_ERROR(rmt_new_rx_channel(&rx, &learn_channel), TAG, "RX channel");
        rmt_rx_event_callbacks_t callbacks = {.on_recv_done = rx_done};
        ESP_RETURN_ON_ERROR(rmt_rx_register_event_callbacks(learn_channel, &callbacks,
                                                            cfg->events), TAG, "RX callbacks");
    }
    return ESP_OK;
}

bool ir_rmt_learning_hardware(void) { return learn_channel != NULL; }

int ir_rmt_tx_start(void *ctx, unsigned index, ir_tx_t *tx)
{
    (void)ctx;
    output_t *out = &outputs[index];
    rmt_carrier_config_t carrier = {
        .frequency_hz = tx->prog->carrier_hz,
        .duty_cycle = config.duty,
    };
    if (rmt_apply_carrier(out->channel, &carrier) != ESP_OK) return -1;
    out->seq = tx->seq;
    rmt_transmit_config_t transmit = {.loop_count = 0, .flags.eot_level = 0,
                                      .flags.queue_nonblocking = 1};
    return rmt_transmit(out->channel, out->encoder, tx, sizeof(*tx), &transmit) == ESP_OK ? 0 : -1;
}

void ir_rmt_tx_stop(void *ctx, unsigned index)
{
    (void)ctx;
    /* ESP32-S3 supports asynchronous stop: the line returns to idle now. A
     * queued completion for this seq is ignored by the core. */
    output_t *out = &outputs[index];
    if (rmt_disable(out->channel) != ESP_OK || rmt_enable(out->channel) != ESP_OK)
        ESP_LOGE(TAG, "output %u stop/restart failed", index + 1u);
}

static esp_err_t arm(void)
{
    rmt_receive_config_t receive = {.signal_range_min_ns = LEARN_GLITCH_NS,
                                    .signal_range_max_ns = LEARN_IDLE_NS};
    return rmt_receive(learn_channel, learn_buffer, LEARN_SYMBOLS * sizeof(*learn_buffer),
                       &receive);
}

int ir_rmt_learn_start(void *ctx)
{
    (void)ctx;
    if (!learn_channel) return -1;
    if (learn_active) return 0;
    learn_buffer = heap_caps_calloc(LEARN_SYMBOLS, sizeof(*learn_buffer),
                                    MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!learn_buffer) return -1;
    if (rmt_enable(learn_channel) != ESP_OK || arm() != ESP_OK) {
        rmt_disable(learn_channel);
        free(learn_buffer);
        learn_buffer = NULL;
        return -1;
    }
    learn_active = true;
    return 0;
}

void ir_rmt_learn_stop(void *ctx)
{
    (void)ctx;
    if (!learn_active) return;
    learn_active = false;
    rmt_disable(learn_channel);
    free(learn_buffer);
    learn_buffer = NULL;
}

const ir_symbol_t *ir_rmt_learn_symbols(void) { return (const ir_symbol_t *)learn_buffer; }

void ir_rmt_learn_rearm(void)
{
    if (learn_active && arm() != ESP_OK) ESP_LOGE(TAG, "learning receive re-arm failed");
}

/* A panic (including a task-watchdog panic) leaves RMT TX replaying its
 * ping-pong memory, carrier included, while the handler prints and writes
 * the core dump. Before anything else, detach every IR pin from RMT and
 * hold it idle. Linked with -Wl,--wrap=esp_panic_handler (see CMakeLists),
 * as ESP-IDF's own BT controller does; ROM and inline register access only. */
void __real_esp_panic_handler(void *info);
void IRAM_ATTR __wrap_esp_panic_handler(void *info)
{
    for (unsigned i = 0; i < panic_outputs; ++i) {
        gpio_ll_set_level(&GPIO, (uint32_t)panic_gpio[i], panic_idle);
        esp_rom_gpio_connect_out_signal((uint32_t)panic_gpio[i], SIG_GPIO_OUT_IDX, false, false);
    }
    __real_esp_panic_handler(info);
}

/* Loopback: a temporary RX channel on the 1:1 pin (RX enables the pad
 * input; TX keeps driving it) captures three unmodulated pulses. */
static bool loopback(void)
{
    static const ir_program_t *prog;
    static ir_program_t pattern;
    static rmt_symbol_word_t captured[16];
    static ir_tx_t tx; /* static: a timed-out transfer must not see a dead stack */
    memset(&pattern, 0, sizeof(pattern));
    pattern.carrier_hz = 38000;
    pattern.repeat = 1;
    pattern.nsym = 3;
    pattern.sym[0] = ir_symbol(40, 1, 40, 0);
    pattern.sym[1] = ir_symbol(80, 1, 40, 0);
    pattern.sym[2] = ir_symbol(120, 1, 300, 0);
    prog = &pattern;
    QueueHandle_t queue = xQueueCreate(2, sizeof(ir_event_t));
    if (!queue) return false;
    rmt_channel_handle_t rx = NULL;
    rmt_rx_channel_config_t rx_cfg = {
        .gpio_num = config.gpio[0],
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = IR_RMT_RESOLUTION_HZ,
        .mem_block_symbols = IR_LOOKAHEAD_SYMBOLS,
        .flags.invert_in = config.invert,
    };
    rmt_rx_event_callbacks_t callbacks = {.on_recv_done = rx_done};
    rmt_receive_config_t receive = {.signal_range_min_ns = LEARN_GLITCH_NS,
                                    .signal_range_max_ns = 200000};
    tx = (ir_tx_t){.prog = prog, .seq = 0};
    atomic_init(&tx.remaining, 0);
    ir_event_t event = {0};
    bool ok = rmt_new_rx_channel(&rx_cfg, &rx) == ESP_OK &&
              rmt_rx_register_event_callbacks(rx, &callbacks, queue) == ESP_OK &&
              rmt_enable(rx) == ESP_OK &&
              rmt_receive(rx, captured, sizeof(captured), &receive) == ESP_OK &&
              rmt_apply_carrier(outputs[0].channel, NULL) == ESP_OK; /* no carrier */
    if (ok) {
        rmt_transmit_config_t transmit = {.loop_count = 0};
        ok = rmt_transmit(outputs[0].channel, outputs[0].encoder, &tx, sizeof(tx), &transmit) ==
                 ESP_OK &&
             rmt_tx_wait_all_done(outputs[0].channel, 100) == ESP_OK &&
             xQueueReceive(queue, &event, pdMS_TO_TICKS(100)) == pdTRUE && event.count >= 3;
        /* The TX-done event for seq 0 went to the service queue; it is
         * stale by construction and ignored by the core. */
    }
    if (ok) {
        static const uint32_t expect[6] = {40, 40, 80, 40, 120};
        for (unsigned i = 0; i < 5 && ok; ++i) {
            const rmt_symbol_word_t *s = &captured[i / 2];
            uint32_t ticks = i % 2 ? s->duration1 : s->duration0;
            unsigned level = i % 2 ? s->level1 : s->level0;
            ok = level == (i % 2 ? 0u : 1u) && ticks + 3 >= expect[i] && ticks <= expect[i] + 3;
        }
    }
    if (!ok) { /* cancel anything still in flight on 1:1 */
        rmt_disable(outputs[0].channel);
        rmt_enable(outputs[0].channel);
    }
    if (rx) {
        rmt_disable(rx);
        rmt_del_channel(rx);
        gpio_pullup_dis(config.gpio[0]); /* RX enabled the pull-up on the emitter pin */
    }
    vQueueDelete(queue);
    if (!ok) ESP_LOGE(TAG, "RMT loopback on connector 1:1 (GPIO %d) failed", config.gpio[0]);
    return ok;
}

bool ir_rmt_selftest(bool with_loopback)
{
    if (!ir_selftest(IR_RMT_RESOLUTION_HZ)) {
        ESP_LOGE(TAG, "encoder self-check failed");
        return false;
    }
    return !with_loopback || loopback();
}
