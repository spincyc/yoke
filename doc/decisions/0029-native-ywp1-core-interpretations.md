# 0029 — Native YWP/1 core: number model, replay digest and bounded tables

Date: 2026-11-05. Status: accepted. Clarifies YWP/1 for the native C core
of ADR 0027 (`native/ywp`); the wire, the envelope and every reference
verdict are unchanged.

## Context

ADR 0027 puts YWP/1 on microcontrollers in C. Porting `yoke.core` exposed
four places where PROTOCOL.md leaned on Python without saying so:

1. **Numbers.** "Python's shortest round-trip spelling" did not say which
   literals are integers and which are floats. The legacy signed message
   and duplicate detection both depend on it: `1E2` re-serializes as
   `100.0`, the integer `-0` as `0`, the float `-0.0` as `-0.0`, and a
   1200-digit integer is valid where a float that rounds to infinity is not.
2. **Replay identity.** The reference keeps each origin's last canonical
   frame for the `dup`/`stale` decision. A canonical re-serialization can be
   about four times longer than the datagram (`1e15` becomes
   `1000000000000000.0`), so the bytes do not suit a microcontroller.
3. **Table bounds.** ADR 0010 caps every per-origin map at `MAX_TRACKED`
   (4096). That is a Python process limit, not something a 512 KiB part
   should hold.
4. **"Changed" media.** A consumer reattaches when its producer's
   description changes. Python compared the parsed objects, and in Python
   `1`, `1.0` and `true` are equal; a C implementation cannot reproduce
   that cheaply, and a description is opaque, not numeric.

## Decision

- **Number model (normative clarification).** A literal with a fraction or
  exponent is a binary64 float, rounded to nearest with ties to even. If it
  rounds to infinity the frame is `malformed`. Its canonical spelling is the
  shortest round-trip digits in Python `repr` layout. A literal with neither
  is an integer of any size. Its canonical spelling is its decimal digits,
  `-0` written as `0`. The native core reproduces CPython bit for bit:
  correctly rounded parsing and a shortest-digit `repr`, with no libc
  number conversion. Each float is converted at most once per canonical
  re-serialization and finiteness is read from the digits, so a forged
  frame packed with costly numbers costs no more than one packed with
  integers (`tests/native_ywp/test_ywp_cost.py`).
- **Replay digest (implementation latitude).** The replay mark may hold
  HMAC-SHA256, under the site key, of the canonical bytes instead of the
  bytes themselves. In open mode the key is empty. Equal digests mean equal
  bytes, short of an HMAC collision, so verdicts are unchanged. The native
  core stores 32 bytes per origin.
- **Bounded tables (interpretation of ADR 0010).** Each per-origin table
  (peers, replay marks with admission sources, cached intents, rejected
  intents) has a fixed implementation capacity and keeps the reference's
  rule: FIFO in insertion order, never evicting the paired producer, and
  capped when an input ends, so a row inserted and removed by the same
  input never pushes out an older one. The reference keeps 4096 entries.
  The native default is 32 peers, 64 origins, 32 intents and 16
  rejections, set at node creation, and should cover every origin active
  on the site, CLI hosts included. Overflow degrades as it does at 4096:
  - An evicted peer is learned again from its next announce.
  - An origin whose mark was evicted can have captured authentic frames
    replayed. A replayed announce or `bye` is undone by that origin's next
    announce, within one interval; intents stay ordered by version, so a
    replayed intent is superseded.
  - An evicted cached intent takes its pending re-issue with it. The
    reference keeps a timer that does nothing.
- **Admitted consumers keep their replay rows.** A producer
  enforcing data-plane admission (ADR 0024) never evicts the replay and
  source row of a currently admitted consumer, so its origin table holds at
  least as many rows as its peer table. Otherwise a flood of other authentic
  origins could evict that row, and an attacker could replay two of the
  consumer's captured frames from its own address. That rebinds the
  admitted address to the attacker until the consumer's next two frames,
  about one announce interval later. Both implementations follow the rule:
  the reference gets it from this ADR (`_bound_maps` in
  `src/yoke/core/node.py`), and its single `MAX_TRACKED` already keeps
  origins >= peers. A lost or departed peer's row is still forgotten after
  `PEER_FORGET`, because by then it is no longer admitted.
- **Description changes (normative clarification).** A producer description
  changes, and a rejected intent's non-id `producer` value matches, when
  canonical bytes differ or match. The reference node compares the same way
  (`_same_json` in `src/yoke/core/node.py`), so `1`, `1.0` and `true` are
  different descriptions in every implementation.
- **Local differences, outside the protocol.** The native node documents
  these in `native/ywp/README.md`:
  - Its durable state is a CRC-checked binary blob for NVS, not
    `state.json`; fields and validation are the same.
  - A timer poll may stop early and ask to be polled again when its bounded
    effect space is nearly full. Frame order is unchanged; only the batches
    that a sequence reservation holds are smaller.
  - It drops peer names and error text, which no rule reads, and truncates
    local log lines.

## Consequences

Third-party implementers get an exact number model to test against
`tests/vectors/ywp1-auth.json`. The C core is held to the reference in
tests:
- differential decode over random, mutated and fuzzer-grown frames;
- a lockstep comparison of every effect against `yoke.core.node.Node`, with
  the reference's `MAX_TRACKED` patched down to the native capacity;
- every `tests/test_simnet_scenarios.py` scenario with native nodes, whose
  bus traffic is byte-identical to the Python run;
- interop with `yoke agent` and `yoke pair` over loopback multicast.

Capacity becomes a commissioning parameter for microcontroller sites. A
site with more active origins than a box's tables churns its bookkeeping:
peers are learned again from announces, the paired producer is always
kept, and pairing safety still rests on versions. The digest and the bounds
add no wire field and need no coordinated upgrade.
