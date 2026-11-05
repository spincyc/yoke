#pragma once
/* Private state of the computer-side core (yusb_proxy.h). Three modules
 * share it: client.c (URB table, unlinks, tombstones, framing), pump.c
 * (endpoint pumps) and proxy.c (session, control relay, bus events). */
#include "yusb_proxy.h"
#include "buffers.h"
#include "wire.h"

#define YUSB_MAX_PUMPS 32u          /* indexed by endpoint: number + 16 * is_in */
#ifndef YUSB_MAX_BOTS
#define YUSB_MAX_BOTS 8u            /* BOT interfaces presented at once */
#endif
#define YUSB_BOT_COMMANDS 8u        /* announced commands whose IN URBs are pending */
#define YUSB_BOT_REQUEUE 66u        /* sizes put back after a stall: in flight (<= 64) + 1 */
#define PARK_MIN_MS 50u
#define PARK_MAX_MS 2000u
#define NO_URB 0xffffu

enum urb_state { U_FREE, U_PENDING, U_UNLINKING, U_ACKED, U_UNLINK };
enum urb_owner { O_NONE, O_BUILDER, O_EP0, O_RESET, O_PUMP, O_CLEAR };

typedef struct {
    uint32_t seqnum;
    uint8_t state, owner, pump, dir, bulk;
    uint16_t unlink;          /* U_UNLINKING: its CMD_UNLINK entry */
    union {
        struct {              /* a submitted URB */
            uint16_t slot;    /* pump slot */
            uint32_t length;
        };
        struct {              /* U_UNLINK: a CMD_UNLINK entry */
            uint16_t target;  /* entry index, NO_URB once the target resolved */
            uint32_t target_seq;   /* its seqnum, to detect reuse */
        };
    };
    uint8_t *buf;             /* data at buf + YUSB_HEADROOM */
    int32_t ack_status;       /* U_ACKED: RET_UNLINK carried the URB's own status */
    uint64_t deadline;        /* 0: none */
    uint64_t tx_end;          /* absolute tx offset just past this command */
    uint8_t hdr[YUSB_USBIP_HEADER_SIZE];
} urb_t;

typedef struct {
    int32_t status;
    uint32_t actual;
    bool unlinked;            /* resolved by CMD_UNLINK, no data */
    bool lost;                /* the connection is gone */
    bool timeout;             /* no reply within the URB's deadline (now unlinked) */
    const uint8_t *data;
} urb_result_t;

typedef struct {
    uint32_t seqnum;
    uint8_t dir;
} tomb_t;

/* BOT transfer sizes shared by an interface's pumps (proxy._BotLink). The
 * reference queues one size per URB; here each CBW is one entry expanded
 * lazily (data chunks, then the CSW), so memory does not grow with the
 * command length. Sizes put back after a stall are popped first. */
typedef struct {
    uint32_t left;            /* data bytes not yet given a URB */
    uint32_t packet;          /* data URB sizes round up to this */
    uint32_t csw;
} bot_cmd_t;

typedef struct {
    bool used;
    uint8_t interface;
    int in_pump;
    unsigned head, count;
    bot_cmd_t cmds[YUSB_BOT_COMMANDS];
    unsigned nrequeue;
    uint32_t requeue[YUSB_BOT_REQUEUE];   /* stack: the last entry is next */
} bot_t;

enum pump_run { P_RUN, P_STOPPING, P_STOPPED, P_DEAD };
enum pump_wait {
    W_NONE,
    W_PARTIAL,      /* IN: writing the data that preceded a STALL */
    W_CANCEL,       /* unlinking later URBs after a STALL */
    W_CLEAR,        /* remote clear-halt in flight after a STALL or park */
    W_PARKED,       /* IN: device stays halted; wait for host activity */
    W_HCANCEL,      /* host cleared the halt: unlinking in-flight URBs */
    W_HCLEAR,       /* host cleared the halt: mirroring it to the device */
    W_PRECLEAR,     /* OUT: re-clearing the remote halt before a submit */
};
enum slot_state { S_FREE, S_INFLIGHT, S_DONE, S_CANCEL, S_READING };

typedef struct {
    uint8_t state;
    bool unlinked, planned;
    uint16_t urb;
    uint32_t requested;       /* IN: URB length; OUT: payload length */
    int32_t status;
    uint32_t actual;
} slot_t;

typedef struct {
    bool used, in, bot_link;
    uint8_t address, interface, type;
    int handle;
    unsigned packet, size;    /* wMaxPacketSize bits, bytes per (micro)frame */
    int32_t interval;
    bot_t *bot;
    uint8_t run, wait;
    bool busy, cancelled;     /* gadget operation outstanding; ep_cancel sent */
    unsigned nslots, slot_size, depth;
    void *block;              /* slot buffers (buffers.h layout) */
    size_t stride;
    uint8_t *yxmem;           /* OUT: encoded payloads, one per slot */
    slot_t *slots;
    unsigned head, count;     /* submission-ordered ring */
    unsigned writing;         /* IN: ring slots the outstanding write covers */
    void *scratch_block;
    uint8_t *scratch;         /* IN: coalescing buffer (buf convention) */
    uint8_t *wbuf;
    uint32_t wlen;
    bool wzero;
    bool probing, remote_halted, kicked;
    uint64_t parked_until;
    uint32_t backoff;
    uint16_t clear_urb;
    unsigned cancel_wait;
    uint64_t cancel_deadline;
    unsigned hc_from;         /* first ring position the host clear cancelled */
    uint8_t hc_resume;        /* wait state to resume after the host clear */
    bool halt_after;          /* OUT: halt locally once the cancel completes */
    uint64_t tx_mark;         /* tx offset past this pump's last OUT payload */
    /* OUT */
    unsigned read_slot;
    uint32_t read_size;
    bool read_planned;
    unsigned out_head, out_count;  /* BOT data-out phases still to read */
    uint32_t out_left[4];
    unsigned bot_in_packet;
} pump_t;

enum ep0_flow { E0_IDLE, E0_REPLY, E0_DATA, E0_QUIESCE, E0_GATE, E0_REMOTE };
#define E0_QUEUE 4u                 /* SETUPs waiting behind a request that must finish */

struct yusb_proxy {
    yusb_policy_t policy;
    yusb_gadget_ops_t ops;
    char busid[YUSB_BUSID_SIZE];
    yusb_phase_t phase;
    uint64_t now;
    yusb_device_info_t info;
    uint32_t devid;
    bool yx;
    yusb_yx_params_t yxp;
    yusb_yx_enc_t *enc;       /* allocated only when YX framing was agreed */
    yusb_yx_dec_t *dec;
    yusb_iov_t *yx_seg;       /* encode_iov scratch, when YX framing is offered */
    size_t yx_nseg;
    /* transport */
    rx_t rx;
    txq_t tx;
    uint8_t op_out[YUSB_REQ_IMPORT_SIZE];
    uint8_t rstate;           /* which element rx is receiving (client.c) */
    uint16_t receiving;       /* URB whose payload is arriving */
    uint8_t *late_buf;        /* YX payloads of tombstoned URBs decode here */
    yusb_usbip_hdr_t rhdr;
    bool rhdr_yx, lost;
    uint64_t import_deadline;
    /* URBs */
    urb_t *urbs;
    unsigned nurbs;
    uint32_t seq;
    uint64_t next_deadline;
    unsigned tomb_head, tomb_count, ntombs;
    tomb_t *tombs;
    /* presentation */
    yusb_builder_t builder;
    uint16_t builder_urb;
    yusb_control_state_t control;
    /* endpoint zero */
    struct {
        uint8_t flow;
        bool op;              /* an ep0 operation is outstanding */
        bool done;            /* the current request's status stage is complete on the bus */
        bool stall_owed;      /* after the outstanding op: release a SETUP a reset abandoned */
        uint8_t nqueued;
        struct {
            uint8_t raw[8];
            bool acked;       /* YUSB_EV_CONTROL_ACKED */
        } queue[E0_QUEUE];
        uint64_t tx_mark;     /* ep0buf may be named by the transmit queue until here */
        yusb_setup_t setup;
        yusb_decision_t decision;
        uint16_t urb;
        uint32_t out_len;
        uint32_t quiesce;     /* pump mask that must stop first */
        uint64_t gate_deadline;
    } e0;
    /* pumps, endpoint handles, BOT links */
    pump_t pumps[YUSB_MAX_PUMPS];
    int handles[YUSB_MAX_PUMPS];
    bot_t bots[YUSB_MAX_BOTS];
    /* status */
    const char *state;
    bool suspended, speed_checked, gadget, remote_dirty, built, bus_quiesce;
    yusb_speed_t link_speed;
    uint16_t reset_urb;
    bool reset_wanted;
    uint64_t stop_deadline;
    yusb_end_t end;
    char end_reason[200], error[200], rejected[200];
    yusb_proxy_counters_t counters;
    ybuf_layout_t layout;
    void *ep0_block;
    uint8_t *ep0buf;          /* data at ep0buf + YUSB_HEADROOM */
    yusb_presentation_t pres;
};

/* proxy.c */
void px_log(yusb_proxy_t *p, const char *fmt, ...);
void px_fault(yusb_proxy_t *p, const char *fmt, ...);
void px_end(yusb_proxy_t *p, yusb_end_t end, const char *fmt, ...);
void px_lost(yusb_proxy_t *p, const char *fmt, ...);
void px_deadline(yusb_proxy_t *p, uint64_t at);
void px_kick_parked(yusb_proxy_t *p);
void px_urb_done(yusb_proxy_t *p, const urb_t *u, unsigned index, const urb_result_t *r);
void px_imported(yusb_proxy_t *p);

/* client.c */
void cl_sizes(const yusb_policy_t *policy, unsigned *nurbs, unsigned *ntombs, size_t *stage);
size_t cl_tx_segments(const yusb_policy_t *policy, unsigned nurbs);
int cl_init(yusb_proxy_t *p);
void cl_free(yusb_proxy_t *p);
void cl_free_yx(yusb_proxy_t *p);
typedef struct {
    unsigned ep, dir;
    uint32_t length;
    uint8_t *buf;             /* data at buf + YUSB_HEADROOM; NULL for none */
    const uint8_t *setup;     /* 8 bytes, control only */
    int32_t interval;
    bool bulk;
    uint8_t owner, pump;
    uint16_t slot;
    uint64_t timeout_ms;      /* 0: none */
} cl_req_t;
uint16_t cl_submit(yusb_proxy_t *p, const cl_req_t *req);
uint16_t cl_control(yusb_proxy_t *p, const yusb_setup_t *setup, uint8_t *buf, uint8_t owner,
                    uint8_t pump, uint64_t timeout_ms);
void cl_cancel(yusb_proxy_t *p, uint16_t index);
/* The owner no longer wants entry index: unlink it and drop its payload. */
void cl_orphan(yusb_proxy_t *p, uint16_t index);
void cl_fail_all(yusb_proxy_t *p);
int cl_rx_element(void *owner);
void cl_poll(yusb_proxy_t *p);
bool cl_urb_receiving(const yusb_proxy_t *p, uint16_t index);

/* pump.c */
size_t pump_footprint(const yusb_policy_t *policy, const ybuf_layout_t *layout, bool in);
int pump_create(yusb_proxy_t *p, const yusb_alt_t *alt, const yusb_endpoint_t *ep, int handle,
                bot_t *bot);
void pump_start(yusb_proxy_t *p, pump_t *pump);
void pump_stop(yusb_proxy_t *p, pump_t *pump);
void pump_free(yusb_proxy_t *p, pump_t *pump);
void pump_urb(yusb_proxy_t *p, pump_t *pump, uint16_t slot, const urb_result_t *r);
void pump_clear_done(yusb_proxy_t *p, pump_t *pump, const urb_result_t *r);
void pump_ep_done(yusb_proxy_t *p, pump_t *pump, int result);
void pump_poll(yusb_proxy_t *p, pump_t *pump);
void pump_kick(yusb_proxy_t *p, pump_t *pump);
void pump_bot_wake(yusb_proxy_t *p, bot_t *bot);
void pump_bot_abort(yusb_proxy_t *p, pump_t *pump);
static inline unsigned pump_index(unsigned address) { return (address & 0x0fu) + ((address & 0x80u) ? 16u : 0u); }
