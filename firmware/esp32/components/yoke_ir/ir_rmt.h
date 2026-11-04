#pragma once
/* RMT glue for the IR core (target only). */
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "ir_core.h"

#define IR_RMT_RESOLUTION_HZ 1000000u

typedef enum { IR_EVENT_TX_DONE = 1, IR_EVENT_RX_DONE = 2 } ir_event_kind_t;

typedef struct {
    uint8_t kind;
    uint8_t output;
    uint16_t count;
    uint32_t seq;
} ir_event_t;

typedef struct {
    unsigned outputs;
    int gpio[IR_MAX_OUTPUTS];
    bool invert;
    float duty;
    int learn_gpio;            /* -1: none */
    QueueHandle_t events;      /* ir_event_t, posted from ISRs */
} ir_rmt_config_t;

esp_err_t ir_rmt_init(const ir_rmt_config_t *cfg);
/* Encoder/parser self-check plus (optionally) an RMT pin loopback. */
bool ir_rmt_selftest(bool loopback);
bool ir_rmt_learning_hardware(void);

/* ir_ops_t implementation. */
int ir_rmt_tx_start(void *ctx, unsigned output, ir_tx_t *tx);
void ir_rmt_tx_stop(void *ctx, unsigned output);
int ir_rmt_learn_start(void *ctx);
void ir_rmt_learn_stop(void *ctx);
/* After an IR_EVENT_RX_DONE: the captured symbols, then re-arm. */
const ir_symbol_t *ir_rmt_learn_symbols(void);
void ir_rmt_learn_rearm(void);
