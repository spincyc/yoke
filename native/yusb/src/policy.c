/* Pure proxy decisions (reference: policy.py). */
#include <stdio.h>
#include <string.h>
#include "yusb_policy.h"
#include "yusb_yx.h"

void yusb_policy_defaults(yusb_policy_t *p)
{
    memset(p, 0, sizeof(*p));
    p->power_ma = 500;
    p->ep0_size = 64;
    p->budget.max_in = 7;
    p->budget.max_out = 7;
    p->budget.max_number = 15;
    p->in_depth_bulk = 16;
    p->in_depth_interrupt = 2;
    p->out_window = 1;
    p->bot_out_window = 1;
    p->bot_in_slots = 8;
    p->coalesce_in = true;
    p->bot_sizing = true;
    p->descriptor_cache = true;
    p->reset_remote_on_bus_reset = true;
    p->max_transfer = 65536;
    p->control_timeout_ms = 4500;
    p->import_timeout_ms = 5000;
    p->stop_timeout_ms = 2000;
    p->unlink_grace_ms = 200;
    p->ep0_max = 65535;
}

static int bad(char *why, size_t cap, const char *what)
{
    if (why && cap) snprintf(why, cap, "%s", what);
    return YUSB_ERR_RANGE;
}

static bool depth_ok(unsigned v) { return v >= 1 && v <= 64; }

int yusb_policy_check(const yusb_policy_t *p, char *why, size_t cap)
{
    if (p->power_ma > 500) return bad(why, cap, "power_ma must be 0..500 (USB 2 bus power)");
    if (p->ep0_size != 8 && p->ep0_size != 16 && p->ep0_size != 32 && p->ep0_size != 64)
        return bad(why, cap, "ep0_size must be 8, 16, 32 or 64");
    if (p->budget.max_in > 15 || p->budget.max_out > 15 || p->budget.max_number > 15)
        return bad(why, cap, "UDC endpoint budget values must be 0..15");
    if (!depth_ok(p->in_depth_bulk)) return bad(why, cap, "in_depth_bulk must be 1..64");
    if (!depth_ok(p->in_depth_interrupt)) return bad(why, cap, "in_depth_interrupt must be 1..64");
    if (!depth_ok(p->out_window)) return bad(why, cap, "out_window must be 1..64");
    if (!depth_ok(p->bot_out_window)) return bad(why, cap, "bot_out_window must be 1..64");
    if (!depth_ok(p->bot_in_slots)) return bad(why, cap, "bot_in_slots must be 1..64");
    if (p->max_transfer < 512 || p->max_transfer % 512 || p->max_transfer > (1u << 20))
        return bad(why, cap, "max_transfer must be a multiple of 512 up to 1 MiB");
    if (!p->control_timeout_ms || !p->import_timeout_ms || !p->stop_timeout_ms)
        return bad(why, cap, "timeouts must be positive");
    if (p->yx_levers & ~YUSB_YX_LEVERS || p->yx_cache_blocks > YUSB_YX_MAX_CACHE_BLOCKS)
        return bad(why, cap, "yx levers or cache size out of range");
    if (p->ep0_max < 512) return bad(why, cap, "ep0_max must be 512..65535");
    return YUSB_OK;
}

void yusb_control_reset(yusb_control_state_t *s)
{
    s->configuration = 0;
    s->interfaces = 0;
    memset(s->alternates, 0, sizeof(s->alternates));
    s->halted = 0;
}

static uint32_t halt_bit(unsigned address)
{
    return 1u << ((address & 0x0fu) + ((address & 0x80u) ? 16u : 0u));
}

bool yusb_control_active(const yusb_control_state_t *s, const yusb_presentation_t *p,
                         unsigned address)
{
    const yusb_config_t *c = yusb_presentation_config(p, s->configuration);
    if (!c) return false;
    for (unsigned n = 0; n < YUSB_MAX_INTERFACES; ++n) {
        if (!(s->interfaces & (1u << n))) continue;
        const yusb_alt_t *a = yusb_config_alt(c, n, s->alternates[n]);
        if (!a) continue;
        for (unsigned i = 0; i < a->num_endpoints; ++i)
            if (yusb_alt_endpoint(c, a, i)->address == address) return true;
    }
    return false;
}

void yusb_control_remember(yusb_control_state_t *s, const yusb_setup_t *setup,
                           const uint8_t *data, size_t len, bool stalled)
{
    if (stalled) len = 0;
    if (len > sizeof(s->miss_arena)) return;
    unsigned slot = s->num_misses;
    for (unsigned i = 0; i < s->num_misses; ++i)
        if (s->misses[i].value == setup->value && s->misses[i].index == setup->index) slot = i;
    if (slot == s->num_misses) {
        /* Bounded: when the table or arena is full, start over. The cache
         * only saves round trips; forgetting is always safe. */
        if (s->num_misses >= YUSB_MAX_MISSES) { s->num_misses = 0; s->miss_used = 0; slot = 0; }
        s->num_misses++;
    }
    if (len > sizeof(s->miss_arena) - s->miss_used) {
        s->num_misses = 1;
        s->miss_used = 0;
        slot = 0;
    }
    s->misses[slot].value = setup->value;
    s->misses[slot].index = setup->index;
    s->misses[slot].fetched = setup->length;
    s->misses[slot].stalled = stalled;
    s->misses[slot].len = (uint16_t)len;
    s->misses[slot].offset = s->miss_used;
    if (len) memcpy(s->miss_arena + s->miss_used, data, len);
    s->miss_used = (uint16_t)(s->miss_used + len);
}

static void reply(yusb_decision_t *d, const uint8_t *data, size_t len)
{
    d->kind = YUSB_D_REPLY;
    d->data = data;
    d->len = len;
}

static void reply_local(yusb_decision_t *d, uint8_t a, uint8_t b, size_t len)
{
    d->local[0] = a;
    d->local[1] = b;
    reply(d, d->local, len);
}

static void stall(yusb_decision_t *d, const char *reason)
{
    d->kind = YUSB_D_STALL;
    d->reason = reason;
}

void yusb_decide(const yusb_setup_t *setup, const yusb_presentation_t *p,
                 const yusb_control_state_t *state, const yusb_policy_t *policy,
                 yusb_decision_t *d)
{
    memset(d, 0, sizeof(*d));
    d->kind = YUSB_D_FORWARD;
    if (yusb_setup_kind(setup) != YUSB_TYPE_STANDARD) return;
    unsigned request = setup->request, recipient = yusb_setup_recipient(setup);
    bool is_in = yusb_setup_is_in(setup);
    if (request == YUSB_REQ_GET_DESCRIPTOR && is_in) {
        if (recipient == YUSB_RECIP_INTERFACE) {
            size_t len;
            const uint8_t *cached = yusb_presentation_class(p, setup->index, setup->value >> 8,
                                                            setup->value & 0xffu, &len);
            if (cached && policy->descriptor_cache) reply(d, cached, len);
            return;
        }
        if (recipient != YUSB_RECIP_DEVICE || !policy->descriptor_cache) return;
        const uint8_t *data;
        size_t len;
        if (yusb_presentation_lookup(p, setup, &data, &len)) {
            if (data) reply(d, data, len);
            else stall(d, "descriptor absent");
            return;
        }
        for (unsigned i = 0; i < state->num_misses; ++i) {
            if (state->misses[i].value != setup->value || state->misses[i].index != setup->index)
                continue;
            if (state->misses[i].stalled) { stall(d, "descriptor absent (cached)"); return; }
            if (state->misses[i].len < state->misses[i].fetched ||
                setup->length <= state->misses[i].fetched) {
                reply(d, state->miss_arena + state->misses[i].offset, state->misses[i].len);
                return;
            }
        }
        d->cache = true;
        return;
    }
    if (request == YUSB_REQ_SET_ADDRESS) { d->kind = YUSB_D_ACK; return; }   /* the UDC owns addressing */
    if (request == YUSB_REQ_SET_CONFIGURATION && recipient == YUSB_RECIP_DEVICE) {
        if (setup->value == 0 || yusb_presentation_config(p, setup->value)) {
            d->kind = YUSB_D_SET_CONFIGURATION;
            d->value = (uint8_t)setup->value;
        } else {
            stall(d, "unknown configuration");
        }
        return;
    }
    if (request == YUSB_REQ_GET_CONFIGURATION && is_in) {
        reply_local(d, state->configuration, 0, 1);
        return;
    }
    const yusb_config_t *config = yusb_presentation_config(p, state->configuration);
    if (request == YUSB_REQ_SET_INTERFACE && recipient == YUSB_RECIP_INTERFACE) {
        if (config && yusb_config_alt(config, setup->index, setup->value)) {
            d->kind = YUSB_D_SET_INTERFACE;
            d->interface = (uint8_t)setup->index;
            d->alternate = (uint8_t)setup->value;
        } else {
            stall(d, "unknown interface or alternate setting");
        }
        return;
    }
    if (request == YUSB_REQ_GET_INTERFACE && is_in) {
        if (config && setup->index < 256 && yusb_config_has_interface(config, setup->index))
            reply_local(d, setup->index < YUSB_MAX_INTERFACES ? state->alternates[setup->index] : 0,
                        0, 1);
        else
            stall(d, "unknown interface");
        return;
    }
    if (request == YUSB_REQ_GET_STATUS && is_in) {
        if (recipient == YUSB_RECIP_DEVICE) {
            reply_local(d, 0, 0, 2);   /* bus powered, remote wakeup disabled */
        } else if (recipient == YUSB_RECIP_INTERFACE) {
            if (config && setup->index < 256 && yusb_config_has_interface(config, setup->index))
                reply_local(d, 0, 0, 2);
            else
                stall(d, "unknown interface");
        } else {
            unsigned address = setup->index & 0x8fu;
            if (recipient == YUSB_RECIP_ENDPOINT && !(address & 0x0fu))
                reply_local(d, 0, 0, 2);
            else if (recipient == YUSB_RECIP_ENDPOINT && yusb_control_active(state, p, address))
                reply_local(d, (state->halted & halt_bit(address)) ? 1 : 0, 0, 2);
            else
                stall(d, "unknown status recipient");
        }
        return;
    }
    if (request == YUSB_REQ_CLEAR_FEATURE || request == YUSB_REQ_SET_FEATURE) {
        if (recipient == YUSB_RECIP_ENDPOINT && setup->value == YUSB_FEATURE_ENDPOINT_HALT) {
            unsigned address = setup->index & 0x8fu;
            if (!(address & 0x0fu)) { d->kind = YUSB_D_ACK; return; }
            if (yusb_control_active(state, p, address)) {
                d->kind = YUSB_D_ENDPOINT_HALT;
                d->address = (uint8_t)address;
                d->halt = request == YUSB_REQ_SET_FEATURE;
                return;
            }
        }
        stall(d, "feature not supported (remote wakeup is not advertised)");
        return;
    }
    if (request == YUSB_REQ_SYNCH_FRAME || request == YUSB_REQ_SET_DESCRIPTOR)
        stall(d, "isochronous and SET_DESCRIPTOR are unsupported");
}

size_t yusb_in_write_plan(const uint32_t *actual, const uint32_t *requested, size_t n,
                          unsigned packet_size, uint32_t limit, uint32_t *total, bool *zero)
{
    size_t count = 0;
    uint32_t sum = 0;
    *zero = false;
    for (size_t i = 0; i < n; ++i) {
        if (count && sum + actual[i] > limit) break;
        count++;
        sum += actual[i];
        if (actual[i] < requested[i]) {
            *zero = actual[i] % packet_size == 0 && sum > 0;
            break;
        }
    }
    *total = sum;
    return count;
}

bool yusb_parse_cbw(const uint8_t *packet, size_t len, uint32_t *data_length, bool *data_in)
{
    if (len != YUSB_CBW_SIZE || memcmp(packet, "USBC", 4) || packet[14] < 1 || packet[14] > 16)
        return false;
    *data_length = (uint32_t)packet[8] | (uint32_t)packet[9] << 8 | (uint32_t)packet[10] << 16 |
                   (uint32_t)packet[11] << 24;
    *data_in = packet[12] & 0x80u;
    return true;
}
