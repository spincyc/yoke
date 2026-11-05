# 0028 — Negotiated USB/IP payload extension (YX) and latency hiding

Date: 2026-11-05. Status: accepted for software. Which levers ship enabled is
decided by `usbmon` evidence from real devices (⚠ none captured yet); until
then every lever defaults to off.

## Context

ADR 0027 (decision 3) makes compression an optional, negotiated, documented
extension to USB/IP, with plain USB/IP as the fallback and latency hiding
ordered before compression. The numbers that frame it:

- **Tier 1.5** (ESP32-P4, 10/100 Ethernet) carries about 11 MB/s of TCP
  payload, while a high-speed USB device can source about 40 MB/s. Only
  here can payload encoding raise throughput, and only on compressible data.
- **Tier 2** (ZERO 3E, GbE) carries about 110 MB/s, more than USB 2.0 high
  speed. Encoding cannot raise its throughput; it would only spend CPU. Its
  device side also runs the kernel `usbip-host` stub, which cannot speak an
  extension.
- **Round trips.** Bulk-Only Transport (BOT) mass storage is strictly
  sequential: CBW, data, CSW, and the host sends the next CBW only after the
  CSW. Every command costs at least one network round trip, and the Python
  worker forwards data-out in one 64 KiB URB at a time, so each chunk pays a
  round trip too.
- **Interoperability.** A stock `usbipd` closes the connection on an unknown
  operation code, and `usbip_net_recv_op_common()` rejects a request whose
  `status` field is non-zero. Neither can carry an offer.

## Decision

### 1. Levers, in order

| # | Lever | Where | Wire change | Works with stock peers |
|---|---|---|---|---|
| 1a | BOT write-behind | computer side (proxy) | none | yes |
| 1b | BOT read-ahead | device side (export core) | none | needs the export core |
| 2 | Zero/repeat-block elimination (`FILL`) | both ends | YX blocks | no (falls back) |
| 3 | Adaptive LZ4 with incompressible bypass (`LZ4`) | both ends | YX blocks | no (falls back) |
| 4 | Block cache with hash references (`CACHE`) | both ends | YX blocks | no (falls back) |

Control and interrupt transfers are never encoded, nor are bulk transfers
shorter than 512 bytes (`YUSB_YX_MIN_LENGTH`): their latency matters more
than their bytes, and HID reports are tiny.

### 2. Negotiation rides in bytes stock peers ignore

Both stock tools compare the busid and print the path with `strncmp`/`%s`,
so bytes after the terminating NUL are ignored by every peer we know of
(the kernel tools, `yoke.usb.client`, `tests/usbsim`).

- **Offer.** The client writes 8 bytes at offset 24 of the 32-byte busid
  field of `OP_REQ_IMPORT`: `'Y' 'X' version levers cache_blocks(u16 BE) 0 0`.
  It offers only when the busid string ends before byte 24.
- **Accept.** An exporter that understands the offer answers in the last 8
  bytes (offset 248) of the 256-byte `path` field of the `OP_REP_IMPORT`
  device record, with the same layout. The levers are the intersection of
  offer and exporter configuration. `cache_blocks` is the minimum of both
  and `CACHE` is dropped when that minimum is 0. The exporter writes an
  accept only in answer to a valid offer, and only when its path string ends
  before byte 248.
- **Fallback.** No accept, or an accept with no levers, means plain USB/IP
  for the whole connection. A stock exporter zero-fills the path, so a Yoke
  client attached to it sees no accept.
- **Framing.** The extension messages of §3 are used only when the agreed
  levers include `FILL`, `LZ4` or `CACHE`. `READAHEAD` alone changes nothing
  on the wire, so such a connection stays plain USB/IP in both directions.
- **Limits.** `cache_blocks` is at most 4096; a larger value in an offer or
  an accept is a malformed field, and the peer treats it as absent.

The version is 1. A peer that receives a higher version answers with
version 1 semantics; a client that receives an accept with a version it
does not know treats it as absent.

### 3. Two extension messages

After an accept with levers, a sender may use, per transfer:

- `YX_CMD_SUBMIT` (`0x59580001`): the 48-byte `CMD_SUBMIT` layout, OUT only,
  `transfer_buffer_length` > 0 is the decoded length, followed by a block
  stream instead of the raw OUT payload;
- `YX_RET_SUBMIT` (`0x59580003`): the `RET_SUBMIT` layout, `actual_length`
  > 0 is the decoded length, followed by a block stream instead of the raw
  IN payload.

Plain `CMD_SUBMIT`/`RET_SUBMIT` stay valid at any time; the sender chooses.
Unlink, sequence numbers, devid checks and every other rule are unchanged.
A `YX_CMD_SUBMIT` whose decoded length exceeds what the receiver accepts
for a plain `CMD_SUBMIT` (its URB size limit) is a violation, as is a YX
message for an ineligible transfer (not bulk, or the wrong direction).

### 4. Block stream

The decoded payload is cut into 4096-byte blocks (the last holds the
remainder). Each block is a 4-byte header `{kind u8, aux u8 = 0, wire_len
u16 BE}` followed by `wire_len` bytes:

| Kind | Body | `wire_len` |
|---|---|---|
| 0 RAW | the block | the block length |
| 1 FILL | a unit tiled over the block (zeros, erased flash, repeated sectors) | 1–512 and ≤ the block length |
| 2 LZ4 | an LZ4 raw block that decodes to exactly the block length | 1 to block length − 1 |
| 3 REF | `slot u32 BE`, `xxh32 u32 BE` of a cached full block | 8 |

Blocks are independent: each LZ4 body is a raw LZ4 block compressed on its
own, with no dictionary and no reference to earlier blocks, so it decodes
with `LZ4_decompress_safe` into exactly the block length. The REF check is
xxh32 with seed 0 over the 4096 cached bytes.

A receiver closes the connection on any violation: an unknown kind, non-zero
`aux`, a length outside the table, a kind whose lever was not agreed, an LZ4
body that does not decode to exactly the block length, or a reference to an
empty slot or with a wrong check.

### 5. Block cache

Each direction has its own cache of `cache_blocks` slots of 4096 bytes. After
encoding (sender) or decoding (receiver) a full-size RAW or LZ4 block, both
ends store it in slot `inserted % cache_blocks` and increment `inserted`.
FILL, REF and partial blocks are never stored. TCP preserves order, so both
ends hold identical caches, provided two rules hold:

- a receiver decodes every YX stream it receives, including the payload of
  a `YX_RET_SUBMIT` for a sequence number it no longer knows (an unlinked or
  timed-out URB, a tombstone) or no longer wants; discarding the bytes
  undecoded would skip cache insertions;
- a sender updates its mirror only for bytes it actually sends: encoding is
  the commit point, and every encoded stream is transmitted in full (a
  stream cannot be withdrawn once encoded; an unlink is sent after it). The sender emits REF only when its own mirror
slot holds byte-identical data. The hash only finds candidates and checks
synchronization, so a collision can never corrupt data. Caches per
direction avoid the race a shared cache would have, where blocks cross in
flight.

### 6. Adaptive LZ4 (sender policy, not wire)

A sender tries FILL first, then REF, then LZ4. An LZ4 result that saves less
than one eighth of the block is sent RAW (incompressible bypass). After 8
consecutive bypasses the sender skips LZ4 for 128 blocks, then probes again;
a probe that bypasses again starts another skip. After the first 8 misses,
encrypted, JPEG and video payloads therefore cost one failed attempt per
129 blocks.

### 7. BOT write-behind (computer side)

Inside a BOT data-out phase, whose length the CBW announces, the proxy keeps
up to `bot_out_window` data-out URBs in flight instead of one. The host's
data is acknowledged by the UDC at local speed, and the round trip is paid
once per command rather than once per 64 KiB.

Error semantics are preserved at the command level:

- the device's CSW, which carries status and residue, is still the only
  completion the host sees;
- a device STALL during the data phase halts the local endpoint while the
  host is still sending that phase's data, as with `out_window`;
- once the host has sent all of the phase's data, the proxy does not halt.
  BOT forbids a STALL on the next CBW. It clears the device's halt instead
  and counts `late_out_stalls`, and the CSW reports the failure.

### 8. BOT read-ahead (device side)

After the host's `READ(10)` of `n` blocks at LBA `l` completes with a good
CSW, an exporter with `READAHEAD` agreed may issue its own `READ(10)` of `n`
blocks at `l + n` to the device. It may use that data only when the host's
next command is exactly that read (same LUN, LBA and length) and the
speculative command completed with CSW status 0 and residue 0. The host then
receives the data and a CSW carrying its own tag. Any other command waits
for the speculation to finish, discards it and runs normally.

A failed speculation must not hide a condition from the host. A
speculative CHECK CONDITION can consume a UNIT ATTENTION, such as a media
change. The exporter therefore issues REQUEST SENSE itself. If the key is
UNIT ATTENTION or NOT READY, it answers the host's next command with CHECK
CONDITION and that command's REQUEST SENSE with the saved sense data, which
is exactly what the device would have reported. Any other failure is
discarded, because the host never issued that command.

### 9. Evidence decides the defaults

- **Capture.** `tools/usbtrace` captures real device traffic through
  `usbmon` into pcap with `LINKTYPE_USB_LINUX_MMAPPED` (220), a documented
  format that Wireshark reads.
- **Replay.** The replay benchmark reports achievable throughput over
  100 Mbit and 1 Gbit links for each lever, from real captures or from
  synthetic ones (a zero-heavy disk, compressible files, JPEG/video, HID,
  audio).
- **Defaults.** Every lever stays off by default (`yx_levers = 0`,
  `bot_out_window = 1`) until captures from the bench devices justify
  enabling one. The Python reference stays plain USB/IP.

## Consequences

- Tier 1.5 can exceed its 10/100 ceiling on compressible data. Its claim
  stays "about 11 MB/s floor, faster on compressible data" (ADR 0027).
- Tier 2 gains only from write-behind, which needs no peer support. YX would
  need the export core on the device side with a Linux host backend
  (usbdevfs), which is follow-up work.
- An encoding peer spends memory per direction: up to 16 KiB of LZ4 state
  and `2 × cache_blocks × 4 KiB` with the cache (the sender mirrors its
  receiver), plus a 4 KiB decoder staging block. Measured on the proxy at
  ESP32-S3 sizing (`tests/native_usb/test_native_footprint.py`): 51.9 KB
  plain, 60.5 KB with FILL, 77.0 KB with FILL and LZ4, 208 KB with a
  16-block cache; on that target CACHE is out of reach.
- The extension command codes and the trailing-byte offer are Yoke-defined.
  Wireshark's USB/IP dissector shows `YX_*` messages as unknown commands.

## Alternatives considered

- **A new operation code before `OP_REQ_IMPORT`.** A stock `usbipd` closes
  the connection, so every fallback costs a reconnect. Rejected.
- **Offer in the request `status` field.** `usbip_net_recv_op_common()`
  rejects a non-zero status. Rejected.
- **Repurposed header fields** (`start_frame`, `devid` in replies) instead
  of new command codes. More compact, but they are invisible to tools and
  easy to misparse. Rejected for explicit codes.
- **Compression below USB/IP** (TLS with compression, a compressing tunnel).
  It cannot skip tiny interrupt transfers or reference cached blocks, and it
  adds a second stream layer. Rejected.
- **zstd.** It gives better ratios but costs several times more CPU per byte
  on a Cortex-A55 or ESP32-P4, and it is a larger dependency. LZ4 is BSD-2
  and fast enough to beat 100 Mbit on both. Revisit with captures.

## Implementation status (2026-11-05)

| Part | Status | Where |
|---|---|---|
| Negotiation, YX messages, block stream | Implemented at both ends | `native/yusb/src/yx.c`, proxy (`client.c`), export core (`export.c`) |
| FILL, adaptive LZ4 with bypass, CACHE | Implemented; off by default | `--yx fill,lz4,cache --yx-cache N` |
| BOT write-behind | Implemented in the proxy; off by default | `--bot-write-behind N` |
| BOT read-ahead (§8) | Specified, **not implemented** | — |
| Trace capture and replay | Implemented | `tools/usbtrace` |

**Verification:**

- **Format oracle.** An independent Python decoder written from this ADR checks the encoder's output.
- **Fuzzing.** The block stream, the offer and accept fields, and both endpoints' receive paths are fuzzed (`make fuzz-usb`).
- **End to end.** The native worker and the export core negotiate every lever set over TCP and check data byte for byte (`tests/native_usb/test_native_export.py`). A stock exporter model ignores the offer.
- **Late payloads.** `fuzz_proxyyx` answers timed-out and unlinked URBs with YX streams; the client decodes them into a scratch buffer, so its cache stays in step.
- **No copies.** Both ends send RAW block bodies straight from the transfer buffer (`yusb_yx_encode_iov`); only headers and coded bodies are written.

**Why read-ahead waits.** On the synthetic traces, the replay model shows it helps long sequential reads: about +11% at 0.2 ms RTT and +54% at 1 ms on 1 Gbit. It costs about 6% on file workloads at 0.2 ms RTT, because exact-length speculation misses every short tail read. It also needs the exporter to emulate a CHECK CONDITION and REQUEST SENSE (§8). Captures from real devices will decide whether that complexity is worth it.
