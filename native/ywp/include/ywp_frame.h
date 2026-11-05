#pragma once
/* YWP/1 frames: strict decode with authentication of the transmitted bytes
 * (top-level `sig` excision, ADR 0023) or the legacy canonical message,
 * and the canonical, signed encoder. Executable reference:
 * src/yoke/core/frames.py; vectors: tests/vectors/ywp1-auth.json.
 *
 * A codec holds the prepared key and the token array of the last decoded
 * datagram (about 6 KiB); it performs no allocation and no I/O. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ywp_crypto.h"
#include "ywp_json.h"
#include "ywp_types.h"

typedef struct {
    ywp_hmac_t mac;
    bool keyed;         /* false: open mode, sig must be "" */
    ywp_json_t json;    /* tokens of the last decoded datagram */
} ywp_codec_t;

/* An accepted frame. `body` indexes the body object in codec->json, valid
 * until the codec's next decode or encode. The fingerprint, filled by
 * ywp_frame_fingerprint, is HMAC-SHA256 under the site key of the frame's
 * canonical bytes, `sig` included: two frames are the same replay
 * (PROTOCOL.md Replay defense) iff their fingerprints are equal. */
typedef struct {
    ywp_frame_type_t type;
    char origin[YWP_ID_MAX + 1];
    uint64_t seq;
    double ts;
    unsigned body;
    uint8_t fingerprint[YWP_SHA256_LEN];
} ywp_frame_t;

/* psk_len == 0 selects open mode. Refusing short keys and requiring an
 * explicit open-mode choice is the configuration loader's job (ADR 0023). */
void ywp_codec_init(ywp_codec_t *codec, const ywp_crypto_ops_t *ops,
                    const uint8_t *psk, size_t psk_len);

/* Validate and authenticate one datagram. Returns YWP_OK and fills *frame,
 * or the drop counter (never STALE or DUP, which need replay state), with
 * the reference's precedence: oversize, malformed JSON, envelope keys,
 * version, other envelope fields, sig format (auth), signature (auth). */
ywp_drop_t ywp_decode(ywp_codec_t *codec, const uint8_t *raw, size_t len,
                      ywp_frame_t *frame);

/* The replay fingerprint of the frame ywp_decode just accepted (the token
 * array must still hold it). Computed on demand: receivers skip it for
 * their own looped-back and far-future frames. */
void ywp_frame_fingerprint(ywp_codec_t *codec, ywp_frame_t *frame);

/* HMAC-SHA256 under the site key of the canonical bytes of token `value` of
 * the last decoded datagram: the identity of an opaque JSON value
 * (ADR 0029). */
void ywp_codec_value_mac(ywp_codec_t *codec, unsigned value, uint8_t out[YWP_SHA256_LEN]);

/* Build, sign and serialize one frame exactly as frames.encode does for a
 * float `ts`. `body` is the JSON text of an object (any formatting; it is
 * re-serialized canonically) without non-integer numbers. Returns the
 * datagram length, or 0 when the frame would violate YWP/1 (bad id or seq,
 * nonfinite ts, invalid body, more than YWP_MAX_FRAME bytes). Reuses the
 * codec's token array. */
size_t ywp_encode(ywp_codec_t *codec, ywp_frame_type_t type, const char *origin,
                  uint64_t seq, double ts, const uint8_t *body, size_t body_len,
                  uint8_t out[YWP_MAX_FRAME]);

/* Bytes the canonical envelope of a frame from `origin` adds around its
 * body when seq, ts and sig take their longest forms: the announce budget
 * of the reference (node.py _announce_size) is overhead + body length. */
size_t ywp_frame_overhead(const char *origin);
