# ADR 0031: Interactive read/write TUI control with full CLI parity

Date: 2026-11-25. Status: accepted. Amends [ADR 0020](0020-optional-read-only-telemetry.md) for `yoke top` while keeping `yoke metrics` strictly read-only.

## Context

[ADR 0020](0020-optional-read-only-telemetry.md) introduced `yoke metrics` and
`yoke top` as strictly read-only passive observers over YWP/1. In day-to-day
bench operation and debugging, operators frequently inspect an endpoint's
declared media/pairing version/drop counters, watch recent control frames, and
pair, unpair, or re-assert routes directly from the live terminal view.

At the same time, automation, scripts, and remote troubleshooting require
complete non-interactive CLI coverage: every action available in `yoke top`
must have a deterministic, scriptable CLI command with JSON output and
standard exit codes, and no action should exist only inside curses.

## Decision

1. **Read/write `yoke top` over standard YWP/1 intents.**
   - `yoke top` becomes an interactive read/write operator console when run
     directly on a control multicast group with explicit write authorization
     (a valid site PSK via `--psk-file` or default `/etc/yoke/psk`, or
     explicit `--open` lab mode) and without `--read-only`.
   - `yoke top --read-only`, `yoke top --url <URL>`, `yoke top --demo`, and
     implicit open-mode fallback (no PSK and no `--open`) remain strictly
     read-only: write keys surface a clear refusal without opening a transmit
     path.
   - `yoke metrics` and its HTTP endpoints (`/metrics`, `/v1/snapshot`) remain
     **strictly read-only** per ADR 0020. No HTTP command API is added, and
     the YWP/1 wire protocol gains no new frame types or fields.
   - Interactive writes execute on a single bounded background worker using
     the shared control engine so the terminal refresh loop never blocks on
     network retries and never runs concurrent overlapping intents.

2. **Shared control module (`yoke.control`) preserving import boundaries.**
   - Move `IntentIO`, `IntentOutcome`, `run_intent`, `run_reapply`,
     `run_unpair_producer`, `collect`, node sorting, and node inspection
     formatting into `yoke.control`, retaining import aliases in `yoke.cli`.
   - Both `yoke.cli` and `yoke.telemetry` call the exact same control logic
     without making `yoke.telemetry` depend on `yoke.cli`.

3. **Strict 1-to-1 parity between interactive TUI actions and scriptable CLI commands.**

| Operator action | `yoke top` interactive key | Non-TUI scriptable CLI command |
| --- | --- | --- |
| Observe & sort endpoints | Main `ENDPOINTS` table + `s` | `yoke status [--sort {id,name,type,state,age}] [--watch S] [--json]` |
| Inspect full endpoint details (pairing version, media, errors, backend, last intent, drops) | `i` / `Enter` inspect overlay | `yoke inspect <NODE_ID> [--watch S] [--json]` |
| Pair consumer to producer (or switch producer) | `p` compatible peer picker | `yoke pair <PRODUCER> <CONSUMER> [--json]` |
| Unpair a consumer | `u` on selected consumer (with confirmation) | `yoke unpair <CONSUMER> [--json]` |
| Unpair all consumers of a producer | `u` on selected producer (with confirmation) | `yoke unpair --producer <PRODUCER> [--json]` |
| Re-assert current pairing intent (`n + 1`) | `r` on selected consumer (with confirmation) | `yoke reapply <CONSUMER> [--json]` |
| Stream / view live control frames and drop verdicts | `l` bounded control-log overlay | `yoke snoop [--json] [--count N] [--window S]` |
| Collector host & fleet telemetry snapshot | `COLLECTOR HOST` panels / `yoke top --once` | `yoke metrics --once --format json|prometheus` |

## Consequences

- Operators can observe, debug, inspect, snoop, pair, switch, unpair, and
  re-apply pairings from one `yoke top` terminal session without losing
  controllerless YWP/1 semantics: closing `yoke top` never alters persisted
  endpoint state.
- Scripts and CI/automation have full non-interactive JSON parity for every
  TUI capability, including bounded `yoke snoop --count/--window`, `yoke inspect`,
  `yoke reapply`, `yoke unpair --producer`, and `yoke status --sort`.
- `yoke metrics` HTTP scraping retains ADR 0020's read-only security model.
- Existing `yoke.telemetry` tests and import-boundary guarantees remain intact.
