# Yoke Wire Protocol v1 (YWP/1)

Status: normative for v1, with compatible correctness clarifications in
ADRs 0012, 0023 and 0024. The frame envelope and version remain **YWP/1**:
existing clients can still send intents and observe multicast. An
incompatible authority or wire change requires a new version; these fixes
do not implement the planned per-device/source-authorized YWP/2 security
model.

## Transport

- UDP over IPv4 multicast.
- Group configuration MUST be an IPv4 multicast address; an explicitly
  selected interface is local unicast IPv4 or `0.0.0.0`, and the port is
  1 through 65535. TTL remains 1.
- Control group: `239.121.111.107`, port `9753` (site-configurable; these
  are the defaults). TTL 1. Loopback enabled (snooper and agents may share
  a host).
- One frame per datagram. Maximum encoded frame size: **1200 bytes**;
  encoders MUST reject larger frames, decoders MUST drop them (count:
  `oversize`).
- Receivers read only datagrams addressed to the control group: the
  reference socket binds the group address, so unicast datagrams sent to
  a host's control port are never delivered to the protocol (ADR 0023).
  Datagram source addresses are transport metadata, not authenticated
  identity; their only protocol use is producer admission (see Producer
  state machine).

## Encoding

A frame is one JSON object text (RFC 8259), UTF-8, in one datagram. JSON
must contain finite numbers, unique object keys, valid Unicode and at most
32 nested containers. Invalid inputs are dropped as `malformed`, never
allowed to escape the receive loop as parser or encoding exceptions.

Receivers MUST accept any valid JSON formatting: member order,
insignificant whitespace, number spelling (`1793000000.1` or
`1.7930000000999999e9`) and string escapes (`é` or `é`) do not
affect validity, because the signature binds the transmitted bytes (see
Authentication). Encoders MUST NOT emit non-integer numbers anywhere
except the `ts` field; decoders MUST tolerate them in bodies.

The reference encoder emits **canonical** bytes: keys sorted
lexicographically at every level, separators `,` and `:` with no
whitespace, non-ASCII characters unescaped, and numbers in Python's
shortest round-trip spelling (`json.dumps(obj, sort_keys=True,
separators=(",", ":"), ensure_ascii=False, allow_nan=False)`). Canonical
form is a property of reference senders and the legacy verification path
below, not a requirement on third-party senders. Duplicate detection
(Replay defense) compares the canonical re-serialization of the whole
parsed frame, `sig` included: a byte-identical repeat is a `dup`, while a
re-formatted copy signed over its own bytes carries a different `sig` and
counts as `stale`.

## Frame envelope

| Field | Type | Constraints |
|---|---|---|
| `v` | int | MUST be `1`; other values dropped (count: `version`) |
| `t` | str | one of `ann` `int` `ack` `evt` `bye` |
| `id` | str | origin node id: `[a-z0-9][a-z0-9-]{0,31}` |
| `seq` | int | per-origin, strictly increasing, 1 through 2^63−1; bool is invalid |
| `ts` | number | finite sender Unix time, seconds; informational only |
| `body` | object | per-type schema below |
| `sig` | str | hex HMAC (see Authentication) or `""` in open mode |

Unknown envelope fields ⇒ drop (count: `malformed`). Unknown fields
*inside* `body` MUST be ignored (forward compatibility).

### Authentication

`sig = HMAC-SHA256(psk, signed_message).hexdigest()`, where the signed
message is the **transmitted datagram with its `sig` member excised**
(ADR 0023):

1. Locate the single top-level member whose key, after JSON unescaping,
   is `sig`. Let K be the byte offset of its key's opening quote and E the
   offset just past the closing quote of its string value. Members of
   nested objects are never excised.
2. If the member is not the last member of the top-level object, remove
   bytes from K through the `,` that terminates it (inclusive; the first
   non-whitespace byte after E). If it is the last member, remove bytes
   from the `,` that precedes it (the last non-whitespace byte before K)
   up to E. All other bytes, including any other whitespace, are kept.

A sender MUST sign exactly the unsigned bytes it transmits. For a
canonical frame the signed message equals the canonical bytes of the
frame object without `sig`, which is what every YWP/1 reference sender has
always signed. For compatibility, receivers MUST also accept a `sig` that
verifies over that legacy canonical re-serialization of the parsed frame
without `sig`; either message may be tried first, each compared in
constant time. Whitespace inside the excised member is not authenticated
and carries no meaning. The legacy path keeps today's property that any
re-serialization of a canonically signed frame verifies; it may be retired
only by a future protocol version. Published conformance vectors, including third-party
formatting and negative cases, are in
[`tests/vectors/ywp1-auth.json`](../../tests/vectors/ywp1-auth.json).

The PSK is site-provisioned key bytes (from a config file,
mode 0600, at most one trailing newline stripped, at least 32 bytes; hex
is recommended so no raw whitespace is ambiguous — see DEPLOYMENT.md).
Open mode (no key) must be selected explicitly (ADR 0023). `sig` MUST match
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

Sequence units are Unix nanoseconds (ADR 0007), not seconds. A sender
durably reserves a block before emitting frames from it: persisted `seq`
is the reserved upper bound, not merely the last sequence already emitted.
After restart it starts above that bound `S` and persists a fresh
reservation before announcing. Its floor is
`max(S, min(W, S + SEQ_FLOOR_JUMP))` where `W = int(unix_time) *
1_000_000_000`; with no durable bound (`S = 0`, first commissioning) the
floor is `W`. The wall clock can therefore advance a restart floor by at
most 60 s: a boot with a far-future clock cannot persist a reservation
that peers reject (ADR 0023). Unused reserved numbers are skipped. A
reservation failure releases no frames beyond the previous durable bound.
This prevents sequence reuse even when a process restarts in the same second
or its wall clock moves backwards while its durable state survives.

The reference implementation reserves 4096 numbers at a time. Receivers
reject (count: `stale`, without advancing the replay mark) values more
than `SEQ_FUTURE_MARGIN` (one hour) ahead of their **current** wall clock:
the shell reads wall time for each received frame and supplies it to the
core, so a clock step by time synchronization takes effect on the next
frame. Timers remain monotonic and never observe wall-clock steps. A node
reports, in its local log only, authenticated frames it rejects by this
bound and the condition where its own next sequence exceeds the bound for
its current clock. In the latter case either its clock is behind (peers
with correct clocks still admit it) or an earlier boot persisted a future
reservation; the defined recovery for the second case is the explicit
`yoke agent --rebase-sequence` operation, which with the clock confirmed
correct saves `S = (int(unix_time) + 3600) * 1_000_000_000` and leaves the
pairing unchanged. Nothing rebases automatically.

State loss, receive replay-map eviction and malicious site-key holders
remain limitations: this is not cryptographic anti-replay across arbitrary
reflashes or an ownership lease. State recovery is an explicit operator
decision.

## Versions

A pairing version is a two-element array `[n, origin]`: Lamport counter
`n` (int 1 through 2^63−1) and a syntactically valid origin node id. Total order: compare `n`,
then `origin` lexicographically. An intent author sets
`n = latest_known_n_for_that_consumer + 1` (from announce cache; 1 if
unknown).

Every new client operation MUST choose a fresh unpredictable version author
id (valid node-id syntax), distinct from prior operations, even when the
envelope origin is stable. Retries of that one operation reuse its complete
version and intent. This prevents a delayed ACK for an older operation from
being mistaken for a new request derived from a stale announce. The version
author may differ from the envelope origin; it is an operation identifier,
not an independently authenticated principal in site-PSK YWP/1.

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
  "media": { ... } | null,
  "driver_status": { "detail": "<str>" } }      # optional
```

- Consumers MUST include `pairing` (null producer = unpaired; version
  `[0, ""]` when never paired). Producers set `pairing: null`.
- `driver_status` is optional evidence from the local backend: `detail` is
  a non-empty human string of at most 128 UTF-8 bytes (truncated at a
  character boundary). Senders omit the member when there is no detail and
  omit it first when the frame budget is tight; receivers MUST NOT require
  it and ignore other members inside it. `err` is likewise bounded human
  text, shortened to fit the frame budget.
- Producers MUST include `media`, an opaque **driver-defined** object
  that the paired consumer's driver of the same `kind` knows how to
  consume; the control plane never interprets it (ADR 0002). It MUST be
  a JSON object or null, and MUST be ≤ the frame budget. The reference
  drivers use: software HDMI (igmp) `{"group": "<ipv4>", "port": <int>}`;
  Just Add Power (jap) `{"channel": <int>, "group": "<ipv4>"}`; USB
  (usbip) `{"host": "<host>", "port": <int>, "busids": ["<busid>",
  ...]}`. The USB/IP `host` is an IPv4 literal, an IPv6 literal (bare or
  bracketed, with an optional `%zone` of letters, digits, `_ . -`), or a
  DNS name of LDH labels (≤ 63 bytes each, ≤ 253 bytes total, optional
  trailing dot); `port` is 1–65535; `busids` are 1–64 unique
  `<bus>-<port>[.<port>…]` strings of ≤ 31 bytes; each
  `"<host> <port> <busid>\n"` connection record must fit 99 bytes. A
  producer that enforces data-plane admission advertises an IPv4 literal
  (ADR 0024). A consumer whose driver rejects the media shape surfaces
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

An applied ACK confirms the saved pairing, not successful media activation
or completed backend release after unpair.
Clients distinguish that result from a subsequent matching consumer announce
reporting active media. Open-PSK lab operation must be selected explicitly
(`open_mode = true` for agents, `--open` for intent senders) and is
identified by tools; it is not authenticated deployment.

### `evt` — event

```
{ "event": "<slug>", "detail": { ... } }
```

Normative event slugs and their `detail` objects:

| Event | `detail` |
|---|---|
| `started` | `{}` |
| `stopping` | `{}` |
| `pairing-applied` | `{"producer": P, "version": [n, "<id>"]}`; P is `"<id>"` or `null` |
| `media-up` | `{}` or `{"detail": "<str>"}` (driver evidence) |
| `media-pending` | `{}` or `{"producer": "<id>"}` |
| `media-down` | `{}` |
| `driver-error` | `{"detail": "<str>"}` (may be `""`) |
| `driver-retry` | `{"producer": "<id>"}` (attach) or `{"operation": "detach"}` |
| `peer-lost` | `{"peer": "<id>", "reason": R}`; R is `"timeout"` or `"bye"` |
| `peer-returned` | `{"peer": "<id>"}` |
| `rx-drop` | `{"malformed": n, "version": n, "auth": n, "stale": n, "oversize": n}` |

Detail strings are human text of at most 128 UTF-8 bytes, truncated at a
character boundary; the full text belongs in the sender's local log
(ADR 0023). Counters are cumulative non-negative integers since the
sender started. Receivers MUST ignore unknown slugs and unknown `detail`
members. `rx-drop` is emitted at most once per `ANN_INTERVAL` when
counters changed; an `auth` count rising is how an authentication
failure surfaces (there is no separate `auth-fail` event). A rejected
intent surfaces only as its `ack` (see below), not as an event.

### `bye`

Graceful shutdown; body `{}`. Peers mark the node departed immediately
(no `peer-lost` event storm at the timeout).

## Consumer state machine

States: `idle` (unpaired and backend release confirmed), `pending` (producer
parameters unknown, attachment not ready or teardown awaiting confirmation),
`active` (the backend confirms requested
attachment readiness), `error` (driver failed; retrying). Active does not
prove payload flow, application behavior or a physical target's response.
Driver evidence (group membership, process running, selected channel or
owned USB import) is described in bounded `driver_status.detail` announce
metadata and `media-up` event detail when available.
A changed non-null producer description invalidates prior attachment
readiness: clear old status evidence and announce pending before starting
the replacement attach. Only current-generation backend success restores
active readiness.

On `int` addressed to self:
1. If `version <=` current version ⇒ `ack(applied=false, superseded)`.
   (Equal versions: first application wins; re-delivery of the identical
   intent that is already applied ⇒ `ack(applied=true, "applied")` — acks
   are idempotent and re-sent so a retrying CLI always terminates.)
2. Validate (`kind`, self-pair, id syntax) ⇒ on failure `ack(false, …)`.
3. Stage, but do not apply in memory, `{pairing, version}`. Emit a
   `Persist(state, request_id)` effect. The shell performs the durable save
   and feeds `on_persist_result(request_id, success, now)` back to the core.
   Only matching successful feedback commits the pairing and allows the
   remaining effects. Failure retains the previous committed pairing and
   MUST NOT produce an applied acknowledgment, including on duplicate
   delivery of the failed intent. Unknown/duplicate feedback is ignored.
4. Command driver: `pair` ⇒ attach to producer's media (from announce
   cache; else `pending` until its `ann` arrives). `unpair` ⇒ detach.
5. `ack`, emit `evt`, re-`ann` immediately.

Driver transitions `pending → active` on success, `→ error` on failure
with retry after `RETRY_BACKOFF` (1 s doubling, cap 30 s), emitting
events on every transition.
Repeated error or pending polls MUST NOT postpone an already scheduled
retry. Retry deadlines advance only after an attempted reconciliation;
successful attachment resets backoff. A backend reporting pending drift
from a live configured producer schedules bounded reattachment as well.
When the paired producer's announced `state` changes from anything else
to `active` while the consumer is `error` or `pending` with no teardown
outstanding, the consumer resets its backoff and reattaches immediately
(a replugged device or newly applied admission must not wait out a 30 s
backoff). The producer's `state` is otherwise advisory and never causes
a detach (ADR 0023).

Unpaired configuration does not prove that the driver has released its old
resources. After explicit unpair, or an unknown-producer replacement, track
teardown until the backend confirms idle. Failed teardown remains visible as
error and retries detach independently of producer liveness; no successful
media-up is inferred from a stale active report. On restart a consumer with
any nonzero saved version requests idempotent owned-resource teardown before
reattachment, so a crash between a durable replacement/unpair and driver
cleanup is recoverable. Pending/uncertain resources
reported by an unpaired backend also trigger this reconciliation.

Startup uses the same persistence barrier. While a save is pending the
shell serializes further core inputs; the core also refuses to start an
overlapping save. An explicit replacement first detaches the old route,
including when the new producer is unknown. Driver completion is tagged by
the shell's route generation; stale completions cannot activate a newer
route. A control-plane peer timeout alone does not unpair or require tearing
down a healthy underlying media session. Orderly agent shutdown quiesces its
local driver but retains the saved pairing for restart.
The shell bounds queued driver work to the actual in-flight call and the
latest desired route, retaining a detach barrier across coalesced changes.
Superseded pending routes do not accumulate tasks or run during shutdown.

Detach is idempotent but teardown uncertainty is a driver error: retain
owned resource bookkeeping until release is confirmed. Replacement cannot
begin before previous teardown completes. IGMP membership, stream process
liveness and an appliance channel selection are attachment evidence only.

The Linux USB/IP reference driver reconciles exact host, advertised TCP
port and busids. It detaches only owned imports, cleans newly created ports
after partial failure and never preempts unrelated importers. Ownership
journaling is separate from pairing configuration so restart can reconcile
surviving imports. Producer readiness requires configured devices actually
bound to `usbip-host` and visible through local `usbipd`, not merely present
in the physical inventory. No physical OTG presentation is implied.
Ownership intent alone cannot identify an import whose upstream connection
record was never written: ports lacking corroborating tuple metadata remain
untouched and report manual recovery required, without blocking unrelated
routes. A missing owned import is attributed only to record-less ports
that were absent when that import was last observed with its record (or,
before the first observation, absent before the import was attempted);
when none exists the import is gone and re-attach proceeds (ADR 0024).
Reject targets that cannot fit the upstream connection-record limit
before side effects. Producer unbind releases only bindings this driver
established; an already-bound export is not automatically owned.
Producer polls may reconcile startup bind failure or a returned configured
device; explicit unbind disables automatic repair. With externally
provisioned bindings (ADR 0024) the producer driver never binds or unbinds
and reports unbound devices as errors.

Announcements must remain encodable during error and readiness transitions.
Optional evidence is omitted first, then human error text is shortened to
the available wire budget. Opaque media parameters are never truncated.
Reject local descriptions whose mandatory fields cannot fit in the worst
state/envelope size before publishing them.

The state file has the existing shape
`{"seq": int, "pairing": {"producer": str|null, "version": [int,str]}}`.
Integers are non-bool, bounded by 2^63−1; node ids are validated. The zero
version is exactly `[0, ""]` and implies an unpaired consumer. Saves use an
exclusive unique temporary file in the destination directory, complete
writes, file synchronization, atomic replacement and parent-directory
synchronization, with private permissions. Symbolic links and nonregular
state files are refused. Corrupt state never becomes fresh state. A missing
file on a commissioned installation must require explicit recovery; fresh
installation permission belongs to the provisioning shell (`yoke agent
--commission`). State input is limited to 4096 bytes, finite unique-key UTF-8
JSON with bounded nesting; invalid input is a recovery error.
A POSIX exclusive nonblocking advisory lease on the stable private
`<state>.lock` file is held for the agent lifetime. A second writer using
that state path is refused; the lock file is never unlinked on release.
It is not a pairing lease and its lifetime never changes saved pairing.
Duplicate node ids configured with different state files remain an operator
error that this local lock cannot prevent.

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

**Data-plane admission (USB/IP reference backend, ADR 0024).** A producer
configured for admission derives its admission set locally from accepted
frames; nothing is added to the wire. A consumer is admitted when all
hold: its latest accepted announce has `role: "consumer"`, this
producer's `kind` and `pairing.producer` equal to this producer's id; it
is neither timed out (`PEER_TIMEOUT`) nor departed (`bye`); and its
admitted address is confirmed. The address is the IPv4 source of the
consumer's accepted frames (any type); it is set or changed only after
`ADMISSION_CONFIRMATIONS` (2) consecutive accepted frames from the same
source. Frames dropped for any reason — including `dup` and `stale`
replays — never bind or confirm an address. The producer publishes the
complete set, as (consumer id, address) pairs, to its backend whenever it
changes and publishes the empty set when it starts, so a restarted
producer never inherits stale permission. The backend restricts new
media-plane sessions to those addresses and loopback; established
sessions are not torn down by the control plane. Addresses are
unauthenticated transport metadata: admission raises the bar against
off-path LAN hosts, not against on-path attackers or site-key holders.

## Peer tracking

A node is live if an `ann` was received within `PEER_TIMEOUT` (3 ×
`ANN_INTERVAL` + 500 ms = 6.5 s). On expiry: `peer-lost` event; a
consumer paired to a lost producer transitions media to `pending`
(pairing retained). On the producer's return: `peer-returned`, media
re-attached.

## Anti-entropy

All agents cache the highest-version normalized `int` per consumer. Retain
only `op`, `consumer`, `version`, and `producer` for pair operations. Unknown
extension fields are ignored, not copied into re-issued frames; malformed
producer fields are not cached. This guarantees relays can encode the known
semantic intent despite tolerated inbound float extensions or padding that
nearly filled the original sender's frame budget. On
receiving `ann` from consumer C whose `pairing.version <` cached
version for C: schedule a re-issue of the cached intent after a
deterministic jitter in `[0.2 s, 1.2 s]` (seeded per node); cancel if
any `int` for C with a version `>=` the cached one is heard first
(suppression). A re-issue is a **fresh frame under the relayer's own
origin and seq** carrying those normalized fields unchanged — the `version`
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
| `SEQ_FUTURE_MARGIN` | 3600 s (admission bound ahead of current wall time) |
| `SEQ_FLOOR_JUMP` | 60 s (maximum wall-clock advance of a restart floor) |
| `ADMISSION_CONFIRMATIONS` | 2 consecutive accepted frames |

All timers derive from the injected monotonic clock; nothing reads a
clock directly in `core`. Wall time enters the core only as a value the
shell reads with each input, for the sequence floor and the future
admission bound.

## Snooper

The snooper joins the control group, verifies signatures when it has the
PSK (reporting, never enforcing), maintains the same announce cache and
drop counters as an agent, and renders frames + inferred topology. It
MUST NOT transmit. It can check protocol conformance and reconstruct
recently observed control state from the multicast stream, within the
retention and loss limits below. In media-multicast deployments the same
host can additionally subscribe to a producer's media group to measure
stream presence/bitrate without interpreting payload.

Observer caches are bounded by `MAX_TRACKED`, `PEER_FORGET` and
`INTENT_CACHE_TTL`, as applicable to their record type. A bounded early-ACK
cache permits an ACK arriving before its intent to be associated later.
Bad-signature frames may be displayed for diagnosis but MUST NOT update
trusted topology, replay or operation caches. Retention limits mean a
late-starting or lossy passive observer cannot prove absent history.
Callers computing current observer liveness supply their current monotonic
time; the observer reads no clock and packet silence never refreshes a
peer's last announcement time.
Text renderers escape terminal control characters (ASCII/C1 and Unicode
format/separator controls); JSON diagnostic output retains the original data.
Observers apply the same future-sequence admission bound as nodes, using
the wall time their shell reads for each observed datagram (or the last
supplied reading) before updating replay high-water.
If distinct normalized intents reuse one consumer/version, a version-only
ACK cannot establish which body it acknowledges: report ambiguity rather
than attributing success to one body. A fresh operation version resolves
that ambiguity; observing a corrected legacy retry is not proof of intent
identity.

## Optional telemetry

Read-only telemetry observers may derive metrics from accepted existing
frames. They MUST NOT send control frames or make pairing depend on a
collector, scrape, dashboard or management heartbeat. The optional HTTP
metrics/JSON interface in [ADR 0020](../decisions/0020-optional-read-only-telemetry.md)
is separate from YWP/1 and adds no envelope or body fields. Observed event
counts are observation-relative, not guaranteed device lifetime totals.
