# Yoke Wire Protocol v1 (YWP/1)

Status: normative for v1. Changes require an ADR and a version bump.

## Transport

- UDP over IPv4 multicast.
- Control group: `239.121.111.107`, port `9753` (site-configurable; these
  are the defaults). TTL 1. Loopback enabled (snooper and agents may share
  a host).
- One frame per datagram. Maximum encoded frame size: **1200 bytes**;
  encoders MUST reject larger frames, decoders MUST drop them (count:
  `oversize`).

## Encoding

A frame is canonical JSON, UTF-8, with:

- keys sorted lexicographically at every level,
- separators `,` and `:` (no whitespace),
- encoders MUST NOT emit non-integer numbers anywhere except the `ts`
  field (see below); decoders MUST tolerate them in bodies (robustness —
  the signature binds parsed content either way).

Canonical bytes = `json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=False).encode("utf-8")`.

## Frame envelope

| Field | Type | Constraints |
|---|---|---|
| `v` | int | MUST be `1`; other values dropped (count: `version`) |
| `t` | str | one of `ann` `int` `ack` `evt` `bye` |
| `id` | str | origin node id: `[a-z0-9][a-z0-9-]{0,31}` |
| `seq` | int | per-origin, strictly increasing, ≥ 1 |
| `ts` | number | sender's Unix time, seconds; informational only |
| `body` | object | per-type schema below |
| `sig` | str | hex HMAC (see Authentication) or `""` in open mode |

Unknown envelope fields ⇒ drop (count: `malformed`). Unknown fields
*inside* `body` MUST be ignored (forward compatibility).

### Authentication

`sig = HMAC-SHA256(psk, canonical_bytes(frame_without_sig)).hexdigest()`
where `frame_without_sig` is the frame object with the `sig` key removed
entirely. The PSK is site-provisioned key bytes (from a config file,
mode 0600, at most one trailing newline stripped; hex is recommended so
no raw whitespace is ambiguous — see DEPLOYMENT.md). `sig` MUST match
`^([0-9a-f]{64})?$` — 64 lowercase hex digits, or empty in open mode;
any other string ⇒ drop (count: `auth`) before the compare (a non-hex
`sig` must never reach the constant-time comparison, which can crash on
non-ASCII input — ADR 0010). A non-string `sig` ⇒ drop (`malformed`).
In open mode (empty PSK) a receiver requires `sig == ""` and drops any
signed frame as `auth`; a keyed receiver drops an unsigned frame as
`auth`. A bad signature ⇒ drop (count: `auth`). Constant-time compare.

### Replay defense

Receivers track the highest `seq` seen per origin id. A frame with
`seq <=` that value ⇒ drop (count: `stale`), except `seq` equal AND
byte-identical canonical encoding ⇒ drop silently (count: `dup`) —
duplicates are normal on multicast.

Senders persist `seq`. On state loss, a sender MUST resume with
`seq = max(persisted + 1, int(unix_time))` so it re-enters above any
previously sent value. (`int(unix_time)` grows faster than any plausible
frame rate; see ADR 0007.)

## Versions

A pairing version is a two-element array `[n, origin]`: Lamport counter
`n` (int ≥ 1) and the intent origin's node id. Total order: compare `n`,
then `origin` lexicographically. An intent author sets
`n = latest_known_n_for_that_consumer + 1` (from announce cache; 1 if
unknown).

## Frame bodies

### `ann` — announce

Sent every `ANN_INTERVAL` (2 s) with ±25 % deterministic jitter, on
startup (after state restore), and immediately after any state change.

```
{ "role": "producer" | "consumer",
  "kind": "hdmi" | "usb",
  "name": "<human label, ≤ 64 chars>",
  "state": "idle" | "active" | "pending" | "error",
  "err": "<detail>" | null,
  "pairing": { "producer": "<id>" | null, "version": [n, "<id>"] } | null,
  "media": { ... } | null }
```

- Consumers MUST include `pairing` (null producer = unpaired; version
  `[0, ""]` when never paired). Producers set `pairing: null`.
- Producers MUST include `media`, an opaque **driver-defined** object
  that the paired consumer's driver of the same `kind` knows how to
  consume; the control plane never interprets it (ADR 0002). It MUST be
  a JSON object or null, and MUST be ≤ the frame budget. The reference
  drivers use: software HDMI (igmp) `{"group": "<ipv4>", "port": <int>}`;
  Just Add Power (jap) `{"channel": <int>, "group": "<ipv4>"}`; USB
  (usbip) `{"host": "<ipv4>", "port": <int>, "busids": ["<busid>",
  ...]}`. A consumer whose driver rejects the media shape surfaces
  `driver-error` and retries — the media contract is enforced by the
  drivers, not this envelope. Consumers set `media: null`.

### `int` — pairing intent

```
{ "op": "pair" | "unpair",
  "consumer": "<id>",
  "producer": "<id>",        # required for pair; absent for unpair
  "version": [n, "<id>"] }
```

Only the named consumer applies it. All agents cache the
highest-version intent per consumer (see Anti-entropy).

### `ack`

Multicast by the consumer after processing an intent addressed to it:

```
{ "version": [n, "<id>"],
  "applied": true | false,
  "reason": "applied" | "superseded" | "unknown-producer" | "driver-error"
            | "kind-mismatch" | "self-pair" }
```

`applied: false, reason: "superseded"` means a newer version already
governs. `unknown-producer` is NOT a rejection: the pairing is applied
with state `pending` (media activates when the producer announces); the
ack still says `applied: true` with reason `applied` in that case —
`unknown-producer` is reserved for malformed producer ids. `kind-mismatch`
(hdmi consumer told to pair with usb producer) and `self-pair` are
rejections and MUST NOT change state.

### `evt` — event

```
{ "event": "<slug>", "detail": { ... } }
```

Normative event slugs: `pairing-applied`, `media-up`, `media-pending`,
`media-down`, `driver-error`, `driver-retry`, `peer-lost`,
`peer-returned`, `rx-drop`, `started`, `stopping`. `rx-drop` detail
carries the drop counters `{malformed, version, auth, stale, oversize}`
and is emitted at most once per `ANN_INTERVAL` when counters changed; an
`auth` count rising is how an authentication failure surfaces (there is
no separate `auth-fail` event). `driver-error` detail is
`{"detail": "<str>"}`. A rejected intent surfaces only as its `ack`
(see below), not as an event.

### `bye`

Graceful shutdown; body `{}`. Peers mark the node departed immediately
(no `peer-lost` event storm at the timeout).

## Consumer state machine

States: `idle` (no pairing), `pending` (pairing applied, producer media
parameters unknown or media not yet confirmed), `active` (driver reports
media flowing), `error` (driver failed; retrying).

On `int` addressed to self:
1. If `version <=` current version ⇒ `ack(applied=false, superseded)`.
   (Equal versions: first application wins; re-delivery of the identical
   intent that is already applied ⇒ `ack(applied=true, "applied")` — acks
   are idempotent and re-sent so a retrying CLI always terminates.)
2. Validate (`kind`, self-pair, id syntax) ⇒ on failure `ack(false, …)`.
3. Persist `{pairing, version}` BEFORE acking (crash between apply and
   ack ⇒ CLI retries ⇒ idempotent re-ack).
4. Command driver: `pair` ⇒ attach to producer's media (from announce
   cache; else `pending` until its `ann` arrives). `unpair` ⇒ detach.
5. `ack`, emit `evt`, re-`ann` immediately.

Driver transitions `pending → active` on success, `→ error` on failure
with retry after `RETRY_BACKOFF` (1 s doubling, cap 30 s), emitting
events on every transition.

## Producer state machine

Producers are passive: announce, transmit media to their fixed group
(HDMI) or accept sessions (USB), report driver health. They apply no
intents. USB single-attachment is enforced by the media plane (usbipd
refuses a second session), not by the control plane; if that refusal
reaches the producer's driver it surfaces generically as
`evt driver-error {"detail": "<str>"}`. Two consumers pairing to one USB
producer is therefore resolved at the media layer, and yoke models each
consumer's pairing independently regardless (a losing consumer sits in
`error`/`pending` with its driver retrying). HDMI producers are
inherently shareable — multiple decoders may join one encoder's group —
so nothing enforces exclusivity there.

## Peer tracking

A node is live if an `ann` was received within `PEER_TIMEOUT` (3 ×
`ANN_INTERVAL` + 500 ms = 6.5 s). On expiry: `peer-lost` event; a
consumer paired to a lost producer transitions media to `pending`
(pairing retained). On the producer's return: `peer-returned`, media
re-attached.

## Anti-entropy

All agents cache the highest-version `int` **body** per consumer. On
receiving `ann` from consumer C whose `pairing.version <` cached
version for C: schedule a re-issue of the cached intent after a
deterministic jitter in `[0.2 s, 1.2 s]` (seeded per node); cancel if
any `int` for C with a version `>=` the cached one is heard first
(suppression). A re-issue is a **fresh frame under the relayer's own
origin and seq** carrying the cached body unchanged — the `version`
inside the body preserves authorship and total order. Re-issuing
verbatim copies is forbidden: they livelock against replay defense
whenever the original author has since sent higher-seq traffic
(ADR 0009). This converges consumers that were offline when an intent
was sent, without any coordinator. Cache entries expire after
`INTENT_CACHE_TTL` (24 h).

Agents observe `ack` frames: an ack with `applied: false` and a
rejection reason (`self-pair`, `kind-mismatch`, `unknown-producer`) for
version V of consumer C evicts a cached intent of exactly version V for
C everywhere and marks V as rejected there, so a doomed highest-version
intent is not re-offered forever. `superseded` acks evict nothing.

## Timing constants

| Name | Value |
|---|---|
| `ANN_INTERVAL` | 2.0 s (±25 % jitter) |
| `PEER_TIMEOUT` | 6.5 s |
| `RETRY_BACKOFF` | 1 s × 2ⁿ, cap 30 s |
| `CLI_RETRY` | 5 sends, 0.5 s × 2ⁿ spacing; deadline 10 s |
| `INTENT_CACHE_TTL` | 24 h |
| `SUPPRESSION_WINDOW` | 0.2–1.2 s |

All timers derive from the injected clock; nothing reads wall time
directly in `core`.

## Snooper

The snooper joins the control group, verifies signatures when it has the
PSK (reporting, never enforcing), maintains the same announce cache and
drop counters as an agent, and renders frames + inferred topology. It
MUST NOT transmit. It is therefore also the conformance oracle: a
correct implementation of this document can reconstruct global state
from the multicast stream alone. In media-multicast deployments the same
host can additionally subscribe to a producer's media group to measure
stream presence/bitrate without interpreting payload.
