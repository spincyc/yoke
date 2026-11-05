# native/ywp — YWP/1 in portable C

The controllerless pairing and control protocol (YWP/1,
[PROTOCOL.md](../../doc/design/PROTOCOL.md)) as a C11 library for the
microcontroller Yoke boxes of [ADR 0027](../../doc/decisions/0027-tiered-usb-product-line-and-native-c-data-path.md).
It is a port of `yoke.core` (`frames.py`, `node.py`) and of the runner's
driver queue. It is held to the Python reference by tests, and
[ADR 0029](../../doc/decisions/0029-native-ywp1-core-interpretations.md)
records the interpretations the port made explicit.

The library is sans-I/O: no sockets, clocks, files or threads. The node
allocates one block at creation and nothing afterwards. There are no
dependencies, and the code is permissively licensed with the repository.

| Module | Role |
|---|---|
| `ywp_json` | Strict JSON, with the reference's acceptance rules, accessors and canonical writer |
| `ywp_num` | CPython `float()`, `repr()` and canonical spelling, bit-exact and bounded in cost: Eisel–Lemire and Schubfach over one 10.7 KB table of powers of five, no libc conversion |
| `ywp_frame` | Decode: transmitted-bytes `sig` excision plus legacy canonical, then the replay fingerprint. Canonical signed encode |
| `ywp_crypto` | Platform SHA-256 seam, HMAC-SHA256, bundled portable SHA-256 |
| `ywp_node*` | The node state machine (`node.py`), its bounded tables and effect batches |
| `ywp_state` | The durable state blob, restart floor and admission bound (ADR 0023) |
| `ywp_route` | Generation-tagged driver work (`runner.py`, ADR 0013) |
| `ywp_random` | MT19937 seeded like `random.Random(seed)`, so jitter matches the reference |

## Using it

```c
ywp_config_t cfg;
ywp_config_default(&cfg);              /* limits: see Memory */
cfg.node_id = "desk-a"; cfg.role = YWP_CONSUMER; cfg.kind = YWP_USB;
cfg.psk = key; cfg.psk_len = key_len;  /* 0: open mode */
ywp_node_t *node = ywp_node_create(&cfg, &ops, &saved, mono(), wall(), &err);
ywp_node_start(node, mono());          /* then perform its effects */
/* loop: ywp_node_datagram() / ywp_node_poll() at ywp_node_next_deadline()
 *       / driver completions; after each input, perform its effects */
```

- **Inputs.** Feed them one at a time (one task, or under one lock); the
  node is not reentrant. Times are `double` seconds. `now` is monotonic and drives
  every timer. `wall` is the shell's Unix-time reading per input (NaN if
  none) and only bounds sequences.
- **Wall clock (lane C: SNTP).** Until the wall clock is near real time,
  every peer frame is dropped as far-future (`stale`), while the box's own
  frames stay admissible from its durable floor. It recovers on the first
  frame after the clock steps, with no restart. Commission only with
  synchronized time.
- **Effects.** After each input, `ywp_node_effects()` returns its batch,
  valid until the next input.
  - `SEND`: encode with `ywp_node_encode(node, fx, wall_now, buf)`, then
    multicast.
  - `PERSIST`: always the last effect of its batch. Save `*fx.persist.state`
    (`ywp_saved_encode`, then write the NVS blob), then call
    `ywp_node_persisted(id, ok, now)`.
  - `ATTACH`/`DETACH`: submit to a `ywp_route_t`.
  - `ADMISSION`: replace the data-plane allowlist.
  - `LOG`: write to the local log.
- **Configuration loader.** The loader, not the core, refuses keys shorter
  than 32 bytes and requires open mode to be chosen explicitly (ADR 0023).
  It refuses corrupt state, and it creates fresh state only when
  commissioning (ADR 0016).
- **mbedTLS binding (ESP32).** Implement `sha256_start/update/finish` with
  `mbedtls_sha256_init/starts`, `_update` and `_finish/free`, over the
  256-byte state the core lends you. Using the hardware engine needs no
  allocation. Pass `equal = NULL` to use the core's constant-time compare.
  HMAC batches message bytes into 64-byte blocks; the exceptions are each
  message's final partial block and the outer hash's 64-byte pad plus
  32-byte inner digest.
- **Flooding.** Every forged datagram costs a full parse, two HMACs and a
  canonical re-serialization before it is dropped as `auth`. The shell
  should rate-limit by source address, at least for authentication
  failures (the `auth` counter rising), so a LAN host cannot pin the node
  task; see Verification for the bounded per-frame cost.
- **Driver media.** Read the canonical media JSON with `ywp_json_parse` and
  `ywp_json_get`, `ywp_json_str_copy`, `ywp_json_uint`.

## Memory

Defaults (`ywp_limits_default`): 32 peers, 64 origins, 32 intents,
16 rejections, a 4 KiB consumer media arena, and 32 effect records over
4 KiB. Size the tables to cover the site (ADR 0029). A producer enforcing
admission needs at least as many origins as peers, because admitted
consumers' replay rows are never evicted.

| Configuration | `ywp_node_footprint` |
|---|---|
| Default | 44,496 B |
| Default producer (`media_bytes = 0`) | 40,400 B |
| Small consumer (8/16/8/4, 2.4 KiB media, minimum effects) | 26,800 B |

Within those totals the codec is 6.5 KiB (the 601-token JSON array is
6 KiB) and the MT19937 state is 2.5 KiB. Outside the node, a `ywp_route_t`
is 2.5 KiB and the shell's datagram buffers are 2 × 1200 B. Footprints are
from `make ywp-bench` on x86-64; a 32-bit target differs only by padding.
Each table has one spare row so an input can insert before the tables are
capped. In flash, the number module is about 18 KB on rv32, 10.7 KB of it
the power table.

Stack use is bounded and does not depend on nesting. The canonical writer
is iterative, and number conversion uses fixed bignums of 340 B. The
deepest path, `ywp_node_datagram` then decode, the canonical writer and an
exact midpoint comparison, measures about 2.8 KiB with
`gcc -O2 -fstack-usage` on x86-64. Give the task that runs the node at
least 6 KiB until this is measured on target.

## Deliberate subset and differences

Wire behavior is the reference's. Every scenario's simulated traffic is
byte-identical (`tests/native_ywp/test_ywp_simnet.py`). Local differences:

- Tables have fixed capacities instead of 4096 entries (ADR 0029). Like the
  reference, they are capped when each input ends and never evict an
  admitted consumer's replay row. An evicted intent drops its pending
  re-issue along with it.
- A consumer keeps every producer's media in one arena. Under memory
  pressure it drops other producers' copies, never the paired producer's,
  and a dropped producer's media counts as unknown until that producer next
  announces. Media longer than 1200 canonical bytes counts as null; only
  non-integer numbers can expand it that far.
- A timer poll that would overflow the effect space stops early.
  `next_deadline` then returns `now`, and frame order is unchanged.
- Peer records drop name and `err` text, which no rule reads. Logs are
  truncated at 192 bytes, and the start barrier's logs precede its
  `PERSIST`.
- Driver detail is taken as UTF-8, with invalid bytes replaced by `?`.
  Producer media must be a JSON object or null. The node decodes its own
  datagrams, so it holds the key the reference keeps in the shell.

## Verification (software only)

The tests are in `tests/native_ywp`, built by pytest with
`-Wall -Wextra -Werror`, ASan and UBSan; the ctypes library uses UBSan only.

- **Unit tests.** `test_ywp_unit.py` (`c/unit.c`) covers crypto known
  answers, the state blob, JSON accessors, the RNG, the route queue (with
  a submission during a failing teardown) and API contracts. It includes a
  poll that stops early with no frame lost, and a minimum-limits node with
  worst-case name, media and all-control detail that drops no effect.
- **Vectors.** `test_ywp_vectors.py` checks all 27 vectors in
  `tests/vectors/ywp1-auth.json`, and that reference datagrams re-encode
  byte for byte.
- **Frame differential.** `test_ywp_frames.py` compares C and Python
  verdicts and parsed fields over 20,000 random and mutated frames, plus
  edge cases. It also checks the encoder and canonical writer byte for byte,
  and `test_ywp_num.py` checks about 630,000 conversions (parse,
  finiteness, canonical spelling and repr) against CPython, adversarial
  classes included, plus the table against its generator
  (`gen_num_tables.py`).
- **Fuzzing.** `test_ywp_fuzz.py` runs bounded libFuzzer passes, then the
  Python differential over the grown corpus. One pass checks decode and
  canonical-writer invariants. The other drives two nodes with arbitrary
  programs (bodies up to about 1100 B, each node receiving the other's
  frames) and checks that no effect overruns and no deadline lies in the
  past. `make ywp-fuzz FUZZ_SECONDS=N` runs longer.
- **Lockstep.** `test_ywp_lockstep.py` compares every effect and the
  observable state, rejected intents included, against `Node` over scripted
  inputs biased toward self-pair and kind-mismatch rejections. Saves fail,
  every table fills and evicts (capped when each input ends), and sequence
  reservations are crossed. A separate test checks the admitted-row rule
  against a flood of other origins.
- **Simulated network.** The `tests/test_simnet_scenarios.py` scenarios run
  with all-C, C-consumer and C-producer mixes. `test_ywp_simnet.py` requires
  their bus traffic to be byte-identical to the Python run.
- **Real sockets.** `test_ywp_interop.py` puts a C producer and consumer
  (`c/host_node.c`) with `yoke agent` producer and consumer on loopback
  multicast and drives them with `yoke pair`, `unpair` and `status`.
- **Build.** `test_ywp_build.py` builds the plain CMake library and checks
  that the ESP-IDF component branch registers every source.

Host cost (`make ywp-bench`, Intel Core Ultra 9 285K, `-O2`; **host
numbers**):

| Operation | Time |
|---|---|
| Decode and authenticate a canonical signed announce | 1.8 µs |
| The same over the legacy canonical path | 3.2 µs |
| Datagram into a node (decode, fingerprint, handling) | 3.7 µs |
| Encode an announce | 1.5 µs |
| Poll with 32 peers | 0.1 µs |
| **Worst forged datagram:** 1200 B of `[0,0,…]`, rejected only after both messages are hashed | 14.5 µs |
| Forged 1200 B arrays of the costliest floats (`1e308`, `5e-324`, 17-digit extremes, 20- to 40-digit midpoint straddles that force the exact comparison) | 8.7–15.0 µs |

A float-packed forged frame used to cost up to 2.2 ms, because every number
was converted twice and exact conversion shifted about 1000 bits of
decimals. Each float is now converted at most once, and only to emit its
canonical spelling: finiteness is decided from its digits, a literal of up
to 15 digits is copied, and other literals go through the table-driven
conversions. `test_ywp_cost.py` fails if any float-packed frame costs more
than 3× the integer-packed one. A 240 MHz 32-bit core is estimated, from
instruction counts and not measured, at about 0.35M cycles (1.4 ms) of
number conversion for the worst 1200 B frame. Hashing and parsing add to
that, so rate-limit authentication failures in the shell.

⚠ **Unverified:** the ESP-IDF component build, target timing and stack
depth on ESP32-S3/P4, and the mbedTLS binding. These belong to the firmware
lane and need the toolchain and the bench.
