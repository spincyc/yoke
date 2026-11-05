#pragma once
/* Pure proxy decisions: tunables, control-request routing, IN coalescing,
 * BOT sizing (reference: src/yoke/usb/policy.py). No I/O; the presentation
 * core (yusb_proxy.h) executes the decisions.
 */
#include "yusb_desc.h"

#ifndef YUSB_MAX_MISSES
#define YUSB_MAX_MISSES 16          /* remembered forwarded GET_DESCRIPTOR answers */
#endif
#ifndef YUSB_MISS_ARENA
#define YUSB_MISS_ARENA 4096u
#endif
#define YUSB_MAX_INTERFACES 32      /* interface numbers 0..31 */

/* Tunables. Defaults favor exact USB semantics over throughput.
 *
 * A remote OUT STALL reaches the computer on its next OUT transaction,
 * because the UDC acknowledged the refused packet when a read was queued.
 * out_window > 1 pipelines OUT and delays that STALL by up to the window,
 * dropping data the UDC already acknowledged. bot_out_window applies the
 * same pipelining only inside a BOT data-out phase (write-behind, ADR
 * 0028): the device's CSW still reports the command's outcome. */
typedef struct {
    uint16_t power_ma;            /* declared draw, 0..500 (⚠ unmeasured) */
    uint8_t ep0_size;             /* 8, 16, 32, 64 */
    yusb_budget_t budget;
    uint8_t in_depth_bulk;        /* 1..64 */
    uint8_t in_depth_interrupt;   /* 1..64 */
    uint8_t out_window;           /* 1..64 */
    uint8_t bot_out_window;       /* 1..64; 1 disables write-behind */
    uint8_t bot_in_slots;         /* 1..64: max_transfer buffers per BOT IN pump */
    bool coalesce_in;
    bool bot_sizing;
    bool descriptor_cache;
    bool reset_remote_on_bus_reset;
    uint32_t max_transfer;        /* positive multiple of 512, <= 1 MiB */
    uint32_t control_timeout_ms;
    uint32_t import_timeout_ms;
    uint32_t stop_timeout_ms;
    uint32_t unlink_grace_ms;     /* wait for a late RET_SUBMIT after RET_UNLINK(own status) */
    uint8_t yx_levers;            /* ADR 0028 offer; 0 = plain USB/IP */
    uint16_t yx_cache_blocks;
    /* Also fetch string 0xEE (Microsoft OS descriptor probe) at import, so a
     * backend that answers GET_DESCRIPTOR itself (TinyUSB) has it cached.
     * Off by default: the reference forwards it on demand. */
    bool prefetch_os_string;
    /* Largest control data stage carried (512..65535; MCU e.g. 4096). Sizes
     * the ep0 buffer. A forwarded request with a longer wLength is STALLed
     * locally; a descriptor longer than this fails the import. */
    uint16_t ep0_max;
} yusb_policy_t;

/* Memory a proxy needs (yusb_proxy_footprint): `create` is allocated by
 * yusb_proxy_create, plus, when the policy offers YX, what an agreement
 * under the offer allocates at import (codecs, caches); `pumps_max` bounds
 * what SET_CONFIGURATION may add for the policy's UDC budget. Allocator
 * overhead is not included. */
typedef struct {
    size_t create;
    size_t pumps_max;
} yusb_footprint_t;

void yusb_policy_defaults(yusb_policy_t *policy);
/* YUSB_OK or YUSB_ERR_RANGE with the offending field in `why`. */
int yusb_policy_check(const yusb_policy_t *policy, char *why, size_t cap);

/* Mutable presentation state the control path owns. */
typedef struct {
    uint8_t configuration;
    uint32_t interfaces;            /* bit n: interface n has an alternate */
    uint8_t alternates[YUSB_MAX_INTERFACES];
    uint32_t halted;                /* bit (number + 16 * is_in) */
    /* Forwarded GET_DESCRIPTOR answers (policy.ControlState.descriptor_misses),
     * bounded FIFO; data lives in miss_arena. */
    uint8_t num_misses, next_miss;
    struct {
        uint16_t value, index, fetched, len;
        uint16_t offset;
        bool stalled;
    } misses[YUSB_MAX_MISSES];
    uint16_t miss_used;
    uint8_t miss_arena[YUSB_MISS_ARENA];
} yusb_control_state_t;

void yusb_control_reset(yusb_control_state_t *state);
/* Whether `address` belongs to the active configuration's current alternates. */
bool yusb_control_active(const yusb_control_state_t *state, const yusb_presentation_t *p,
                         unsigned address);
void yusb_control_remember(yusb_control_state_t *state, const yusb_setup_t *setup,
                           const uint8_t *data, size_t len, bool stalled);

typedef enum {
    YUSB_D_REPLY,               /* data/len: answer the IN request locally */
    YUSB_D_ACK,                 /* complete the status stage locally */
    YUSB_D_STALL,               /* reason */
    YUSB_D_FORWARD,             /* cache: remember the answer (descriptor miss) */
    YUSB_D_SET_CONFIGURATION,   /* value */
    YUSB_D_SET_INTERFACE,       /* interface, alternate */
    YUSB_D_ENDPOINT_HALT,       /* address, halt (forwarded) */
} yusb_decision_kind_t;

typedef struct {
    yusb_decision_kind_t kind;
    const uint8_t *data;        /* REPLY; points into the presentation, state or `local` */
    size_t len;
    uint8_t local[2];
    const char *reason;         /* STALL */
    bool cache;                 /* FORWARD */
    uint8_t value, interface, alternate, address;
    bool halt;
} yusb_decision_t;

/* Route one control request (policy.decide). */
void yusb_decide(const yusb_setup_t *setup, const yusb_presentation_t *p,
                 const yusb_control_state_t *state, const yusb_policy_t *policy,
                 yusb_decision_t *out);

/* Coalesce completed IN URBs into one gadget write without changing
 * packets (policy.in_write_plan). actual[i]/requested[i] describe
 * consecutive completions in order; every included URB except the last is
 * full, so the UDC re-packetizes the concatenation identically. Returns the
 * count; *zero asks for the ZLP that ended the last URB. */
size_t yusb_in_write_plan(const uint32_t *actual, const uint32_t *requested, size_t n,
                          unsigned packet_size, uint32_t limit, uint32_t *total, bool *zero);

#define YUSB_CBW_SIZE 31u
#define YUSB_CSW_SIZE 13u
#define YUSB_BOT_RESET 0xffu     /* class request: Bulk-Only Mass Storage Reset */
/* A valid Bulk-Only Transport CBW: signature USBC, CB length 1..16. */
bool yusb_parse_cbw(const uint8_t *packet, size_t len, uint32_t *data_length, bool *data_in);
