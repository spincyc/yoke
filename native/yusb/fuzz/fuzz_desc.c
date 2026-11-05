/* libFuzzer: descriptors from the remote device. The input drives
 * parse/check/rewrite and the presentation builder, whose every control
 * answer is sliced from the input (or a STALL), then control routing on
 * whatever presentation results. */
#include <stdlib.h>
#include <string.h>
#include "yusb_policy.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static yusb_presentation_t pres;
static yusb_builder_t builder;
static yusb_config_t config;

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    yusb_device_desc_t dev;
    char why[200];
    yusb_parse_device(data, size, &dev, why, sizeof(why));
    if (!yusb_parse_config(data, size, &config, why, sizeof(why))) {
        yusb_budget_t budget = {7, 7, 15};
        yusb_check_supported(&dev, &config, 1, YUSB_SPEED_HIGH, &budget, why, sizeof(why));
        uint8_t copy[4096];
        size_t n = size < sizeof(copy) ? size : sizeof(copy);
        memcpy(copy, data, n);
        yusb_rewrite_config(copy, n, 500);
        yusb_rewrite_bos(copy, n);
    }
    if (size < 2) return 0;
    yusb_budget_t budget = {7, 7, 15};
    yusb_builder_init(&builder, &pres, 1 + data[0] % 3, 500, 64, &budget);
    builder.prefetch_os = data[1] & 1;
    size_t pos = 2;
    yusb_setup_t req;
    for (int steps = 0; steps < 512 && yusb_builder_next(&builder, &req) == 1; ++steps) {
        if (pos >= size) { yusb_builder_feed(&builder, NULL, 0, true); continue; }
        size_t len = data[pos++];
        if (len == 0xff) { yusb_builder_feed(&builder, NULL, 0, true); continue; }
        if (len > req.length) len = req.length;
        if (len > size - pos) len = size - pos;
        yusb_builder_feed(&builder, data + pos, len, false);
        pos += len;
    }
    yusb_control_state_t state;
    memset(&state, 0, sizeof(state));
    yusb_control_reset(&state);
    yusb_policy_t policy;
    yusb_policy_defaults(&policy);
    for (; pos + 8 <= size; pos += 8) {
        yusb_setup_t s = yusb_setup_unpack(data + pos);
        yusb_decision_t d;
        yusb_decide(&s, &pres, &state, &policy, &d);
        if (d.kind == YUSB_D_REPLY && d.len && !d.data) abort();
        if (d.kind == YUSB_D_SET_CONFIGURATION) state.configuration = d.value;
        if (d.kind == YUSB_D_FORWARD && d.cache) yusb_control_remember(&state, &s, data, pos % 64, pos & 1);
    }
    return 0;
}
