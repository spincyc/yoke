#pragma once
/* YX: the optional, negotiated USB/IP payload extension (ADR 0028).
 *
 * Plain USB/IP 1.1.1 is always the fallback: a peer that does not offer or
 * accept the extension sees standard bytes. Negotiation rides in bytes that
 * every stock peer ignores (after the NUL of the OP_REQ_IMPORT busid and of
 * the OP_REP_IMPORT device path). Once agreed, a sender may carry a bulk
 * transfer's payload as a block stream in YX_CMD_SUBMIT (OUT data) or
 * YX_RET_SUBMIT (IN data); the 48-byte header keeps the plain layout and
 * the decoded length. Control and interrupt transfers, and bulk transfers
 * shorter than YUSB_YX_MIN_LENGTH, are never encoded.
 *
 * Block stream: blocks of YUSB_YX_BLOCK decoded bytes (the last holds the
 * remainder), each a 4-byte header {kind u8, aux u8 = 0, wire_len u16 BE}
 * and wire_len body bytes:
 *   RAW  body is the block;                      wire_len == block length
 *   FILL body is a unit tiled over the block;    1..512, <= block length
 *   LZ4  body is an LZ4 raw block;               1..block length - 1
 *   REF  {slot u32 BE, xxh32 u32 BE} of a cached full block; wire_len 8
 * Cache: per direction, both ends store every full-size RAW or LZ4 block
 * in slot (inserted++ % cache_blocks). The sender emits REF only when its
 * mirror slot holds identical bytes, so a hash collision cannot corrupt.
 */
#include "yusb_ch9.h"

#define YUSB_YX_VERSION 1u
#define YUSB_YX_FILL 0x01u      /* zero/repeat-block elimination */
#define YUSB_YX_LZ4 0x02u       /* adaptive LZ4 with incompressible bypass */
#define YUSB_YX_CACHE 0x04u     /* block cache with hash references */
#define YUSB_YX_READAHEAD 0x08u /* exporter BOT read-ahead (no wire change) */
#define YUSB_YX_LEVERS 0x0fu

#define YUSB_YX_CMD_SUBMIT 0x59580001u
#define YUSB_YX_RET_SUBMIT 0x59580003u

#define YUSB_YX_BLOCK 4096u
#define YUSB_YX_BLOCK_HEADER 4u
#define YUSB_YX_MIN_LENGTH 512u
#define YUSB_YX_MAX_FILL 512u
#define YUSB_YX_MAX_CACHE_BLOCKS 4096u

enum {
    YUSB_YX_KIND_RAW = 0,
    YUSB_YX_KIND_FILL = 1,
    YUSB_YX_KIND_LZ4 = 2,
    YUSB_YX_KIND_REF = 3,
};

typedef struct {
    uint8_t version;
    uint8_t levers;
    uint16_t cache_blocks;  /* 0 unless YUSB_YX_CACHE */
} yusb_yx_params_t;

/* Offer: busid field bytes 24..31 = 'Y','X', version, levers, cache_blocks
 * (u16 BE), 0, 0. Requires the busid string to end before byte 24. */
int yusb_yx_offer_put(uint8_t busid_field[32], const yusb_yx_params_t *offer);
bool yusb_yx_offer_get(const uint8_t busid_field[32], yusb_yx_params_t *offer);
/* Accept: device path field bytes 248..255, same layout; the path string
 * must end before byte 248. Written only in answer to a valid offer. */
int yusb_yx_accept_put(uint8_t path_field[256], const yusb_yx_params_t *agreed);
bool yusb_yx_accept_get(const uint8_t path_field[256], yusb_yx_params_t *agreed);
/* agreed = offer ∩ limits; cache_blocks = min of both when CACHE survives,
 * and CACHE is dropped when that minimum is 0. */
void yusb_yx_agree(const yusb_yx_params_t *offer, const yusb_yx_params_t *limits,
                   yusb_yx_params_t *agreed);

/* Levers that change the wire. YX framing (YX_CMD/RET_SUBMIT) is used only
 * when one of them was agreed; READAHEAD alone keeps plain USB/IP. */
#define YUSB_YX_WIRE_LEVERS (YUSB_YX_FILL | YUSB_YX_LZ4 | YUSB_YX_CACHE)
static inline bool yusb_yx_framed(const yusb_yx_params_t *agreed)
{
    return (agreed->levers & YUSB_YX_WIRE_LEVERS) != 0;
}

/* The 48-byte header's command field, YX code <-> plain code. unframe:
 * if raw carries YX_CMD_SUBMIT or YX_RET_SUBMIT, rewrite it to the plain
 * CMD_SUBMIT/RET_SUBMIT and return true; otherwise leave raw unchanged.
 * frame: rewrite a plain CMD_SUBMIT/RET_SUBMIT header to its YX code (yx
 * true) or a YX one back to plain (yx false). */
static inline bool yusb_yx_unframe(uint8_t raw[48])
{
    if (raw[0] != (uint8_t)(YUSB_YX_CMD_SUBMIT >> 24) ||
        raw[1] != (uint8_t)(YUSB_YX_CMD_SUBMIT >> 16) || raw[2] != 0 ||
        (raw[3] != (uint8_t)YUSB_YX_CMD_SUBMIT && raw[3] != (uint8_t)YUSB_YX_RET_SUBMIT))
        return false;
    raw[0] = raw[1] = 0;
    return true;
}

static inline void yusb_yx_frame(uint8_t raw[48], bool yx)
{
    raw[0] = yx ? (uint8_t)(YUSB_YX_CMD_SUBMIT >> 24) : 0;
    raw[1] = yx ? (uint8_t)(YUSB_YX_CMD_SUBMIT >> 16) : 0;
    raw[2] = 0;
}

static inline bool yusb_yx_eligible(unsigned transfer_type, size_t length)
{
    return transfer_type == YUSB_XFER_BULK && length >= YUSB_YX_MIN_LENGTH;
}
/* Worst-case wire bytes for `length` decoded bytes (all RAW). */
size_t yusb_yx_bound(size_t length);
/* Most segments yusb_yx_encode_iov emits for `length` decoded bytes. */
static inline size_t yusb_yx_segments(size_t length)
{
    return 2u * (length / YUSB_YX_BLOCK + (length % YUSB_YX_BLOCK != 0));
}

typedef struct {
    uint64_t blocks[4];     /* by kind */
    uint64_t raw_bytes;     /* decoded bytes */
    uint64_t wire_bytes;    /* headers + bodies */
    uint64_t lz4_tries, lz4_bypass;
} yusb_yx_stats_t;

/* Sender side, one per direction. Fields are private. */
typedef struct {
    uint8_t levers;
    uint16_t cache_blocks;
    uint8_t *cache;         /* mirror: cache_blocks * YUSB_YX_BLOCK */
    uint32_t *cache_hash;
    uint16_t *index;        /* bucket -> slot + 1 */
    uint32_t index_mask;
    uint32_t inserted;
    uint16_t lz4_misses, lz4_skip;
    void *lz4_state;
    yusb_yx_stats_t stats;
} yusb_yx_enc_t;

/* Allocates (setup time). YUSB_ERR_RANGE/NOMEM; on failure nothing stays
 * allocated. */
int yusb_yx_enc_init(yusb_yx_enc_t *e, const yusb_yx_params_t *agreed);
/* Safe on a zeroed struct, after a failed init, and when called twice. */
void yusb_yx_enc_free(yusb_yx_enc_t *e);
/* Exact heap bytes yusb_yx_enc_init / yusb_yx_dec_init allocate for
 * `agreed` (0 for invalid params); the structs themselves are the caller's.
 * Both grow monotonically with the levers and cache_blocks, so the value
 * for a peer's limits bounds any agreement under them. */
size_t yusb_yx_enc_footprint(const yusb_yx_params_t *agreed);
size_t yusb_yx_dec_footprint(const yusb_yx_params_t *agreed);
/* Encode length (> 0) bytes into out, which holds yusb_yx_bound(length).
 * Returns the wire length. Deterministic for a given encoder history. */
size_t yusb_yx_encode(yusb_yx_enc_t *e, const uint8_t *src, size_t length, uint8_t *out);
/* The same wire bytes as yusb_yx_encode from the same encoder state, as a
 * segment list without copying RAW bodies: block headers and FILL/LZ4/REF
 * bodies go to out (which holds yusb_yx_bound(length)), adjacent pieces of
 * out share a segment, and RAW bodies point into src, which must stay valid
 * and unchanged until the segments are sent. Returns the segment count
 * (<= yusb_yx_segments(length)) and the wire length in *wire; 0, with the
 * encoder untouched, when length is 0 or max_iov < yusb_yx_segments(length). */
size_t yusb_yx_encode_iov(yusb_yx_enc_t *e, const uint8_t *src, size_t length, uint8_t *out,
                          yusb_iov_t *iov, size_t max_iov, size_t *wire);

/* Receiver side, one per direction; incremental so RAW bodies are received
 * straight into the destination. Fields are private. */
typedef struct {
    uint8_t levers;
    uint16_t cache_blocks;
    uint8_t *cache;
    uint32_t inserted;
    uint8_t *dst;
    size_t length, done;    /* decoded bytes */
    uint8_t header[YUSB_YX_BLOCK_HEADER];
    uint8_t have;           /* header bytes received */
    uint8_t kind;
    uint16_t wire, got;     /* current body */
    yusb_yx_stats_t stats;
    uint8_t staging[YUSB_YX_BLOCK];
} yusb_yx_dec_t;

/* As enc_init/enc_free: nothing stays allocated after a failed init, and
 * free is safe on a zeroed or failed struct and when called twice. */
int yusb_yx_dec_init(yusb_yx_dec_t *d, const yusb_yx_params_t *agreed);
void yusb_yx_dec_free(yusb_yx_dec_t *d);
/* Start decoding a stream of `length` (> 0) decoded bytes into dst. */
void yusb_yx_dec_begin(yusb_yx_dec_t *d, uint8_t *dst, size_t length);
/* Where the next wire bytes go and how many are wanted now (> 0), or 0 when
 * the stream is complete. */
size_t yusb_yx_dec_space(yusb_yx_dec_t *d, uint8_t **buf);
/* n bytes arrived at the last yusb_yx_dec_space buffer. YUSB_OK or
 * YUSB_ERR_PROTOCOL (unknown kind, bad length, lever not agreed, LZ4
 * failure, REF to an empty slot or with a wrong check); YUSB_ERR_RANGE if
 * n exceeds the space offered. A failure is permanent (dec_space then
 * returns 0 with *buf NULL), so callers check every result. */
int yusb_yx_dec_feed(yusb_yx_dec_t *d, size_t n);
/* Whole-stream convenience for tests and benchmarks. *consumed receives the
 * wire bytes used. */
int yusb_yx_decode(yusb_yx_dec_t *d, const uint8_t *wire, size_t wire_len, uint8_t *dst,
                   size_t length, size_t *consumed);
