# Yoke implementation handoff

Updated 2026-11-03. **R6 is the current PoE housing first article.** R5 omitted
an original requirement and remains retired; never present or repeat it as the
PoE solution. R6 passed independent cold review and was sent once to the
printer; physical fit and powered thermal acceptance are **NOT RUN**.

## Current hardware task

The owner authorized a complete housing, sufficient ventilation, independent
cold review and one print on the ready FlashForge Adventurer 5M. The ordered
[ZERO 3E 2GB with GPIO](https://www.amazon.com/dp/B0CZD1C54N) and
[Radxa PoE HAT](https://www.amazon.com/dp/B0D1KF1L7G) have not arrived; the
owner expects both Thursday and explicitly requested computed dimensions now.
The one-print authorization was consumed: `yoke-usb-housing-r6.gcode` was
submitted at **2026-11-03 15:08:19 UTC** and verified **PRINTING** with the
exact filename at 15:08:28 UTC. Estimated 46.9 g PLA and 3 h 58 m 51 s plus
preparation; four parts, supports disabled, bed levelling enabled. See the
[print record](hardware/usb-housing/r6/print.json),
[validation record](hardware/usb-housing/r6/validation.json) and
[assembly guide](hardware/usb-housing/r6/assembly.pdf). Do not resend.

[R6 source and instructions](hardware/usb-housing/r6/README.md) describe an
89 × 45.5 × 46 mm rectangular enclosure. Ethernet opens at one end; two USB-C
ports share a narrow lower-side opening. The opposite end is closed, HDMI
covered and microSD serviced with the electronics removed. Both sides and
the roof are vented, with low inlets that remain open on a desk.

Four lower-PCB edge pads and two removable clips retain the board without
using its conductive mounting holes. Fit the lower Radxa screws and insulated
pillars outside the case, seat the SBC, install the two clips, then fit the
HAT and its upper screws. Four bottom-entering M3×16 screws and four lid
inserts close the shell; two M3×8 screws and two base inserts secure the clips.
All inserts use existing RX-M3×5.7 stock. Heat-set them in empty plastic first.

## Recorded source uncertainty and disposition

The [reference record](hardware/usb-housing/poe-reference.json) preserves the
official STEP and listing evidence. The assembly model has an 18 mm facing-PCB
gap; the product lists 14.5 mm pillars. Purchased-listing photos show fanless
X1.0, while the published model includes an underside fan. Source CAD omits
conductive hardware, leads and some photographed accessories.

The earlier cold **reference-only** review blocked a claimed verified complete
envelope. Its findings remain valid. The owner's later clarification changed
the immediate scope to a computed first article before the parts arrive. R6
keeps the published assembly unchanged, retains only the lower PCB, and
reserves explicit space for unknown mounting hardware and inserted accessories.
Neither shell closure nor retention depends on the uncertain HAT gap. At the
source placement, modeled roof clearance is 3.5 mm; this is an allowance, not
a measured physical bound. Complete independent design review passed for the
frozen R6 packet; its structured reverse-service order was corrected before
release. Do not demand unavailable measurements as a prerequisite to this
authorized nominal first article, and do not invent physical passes.

Check unpowered fit, actual stock/cables, ten service cycles and sustained
powered cooling on arrival. Preserve Radxa's electrically necessary screws,
conductive pillars and insulation. Stop on interference, throttling, resets
or softened plastic; never use case screws to force parts into place.

## Print recovery and retained tools

R5 was submitted once at 2026-11-03 12:17 UTC. Before the R6 dispatch, the
printer was reconciled as **READY, no current file**. No cancellation was sent, and
no owner physical result has been recorded. Its authorization was consumed.
R1/R3/R4/R5 jobs are retired in Lacuna's print guards.

R6 used Lacuna's exact-file assembly release, independent review,
`tools.print approve` and prepared-job dispatch workflow. Its single start
was acknowledged and the exact filename verified. The ready-printer statement
and ephemeral plate-clear arm were consumed on that dispatch. Reconcile the
existing job read-only; no further start is authorized.

Working evidence is retained in Yoke `.scratch/r6-print/`, native exports in
`.scratch/r6-design/`, and original STEP/reference evidence in
`.scratch/poe-reference/`. Historical R5 exact packet remains in
`.scratch/r5-print/packet/`; its readiness commands must never be replayed.
Lacuna `.scratch/r4-print-runtime/` supplies the retained slicer, geometry and
printer tools despite its historical directory name. From the Lacuna clone:

```sh
.scratch/r4-print-runtime/venv/bin/python -B tools/print/ad5m.py status
```

Keep credentials and raw printer responses in scratch. Do not run `wt tidy`
while these retained runtime and exact-job artifacts are needed. The owning
revision's print record and Lacuna dispatch ledger must record actual dispatch
and subsequent read-only observations; physical acceptance remains separate.

## Workspace and publication

All three clones live side by side under the `thermite/ir` workspace and
use `feature/ir`. Do not change branches or work in canonical clones.
Publish authorized changes with `git push -u origin feature/ir`.

| Repository | Baseline before this handoff update |
| --- | --- |
| `spincyc/yoke` | `4f851fe5c3d2c8e814b25de91b00a92bdaae8c23` — R5 design, evidence and print record |
| `spincyc/lacuna` | `cdf5538f71fdd4c0db1ddd3b761e9570f5ff2866` — R4 retirement and R5 dispatch ledger |
| `spincyc/thermite` | `6fdbf3a5e2f29339b771a0e48062511527a5125d` — optional Yoke metrics integration |

All three baselines were clean and pushed. Yoke's
[R5 CI run](https://github.com/spincyc/yoke/actions/runs/41634885513)
passed, including Python, browser, ESP32 and Pages jobs; the deployed R5
pages and images were checked against local output. This verifies the
published software/documents, not the omitted PoE assembly. Run `make verify`
for the current checkout rather than relying on historical test counts.

## Goal and present boundary

The immediate USB product uses **two standalone Radxa ZERO 3E hardware
boxes; USB 2 is the first deliverable**:

```text
USB peripheral — USB — Yoke box ⇄ Ethernet switch ⇄ Yoke box — USB — computer
                       device side                  computer side
```

The user's computer is outside the two-box system and need not run Linux
or Yoke endpoint software. Linux inside a box is an implementation choice.
The existing Linux USB/IP adapter does not implement the required physical
USB device/gadget bridge on the computer-side box. That bridge, device
compatibility and the complete hardware path remain unfinished; see the
[agreed USB design](doc/design/USB-ENDPOINT.md).

Saved pairing must survive management closure, reboot and network loss,
and change through explicit pair/unpair commands. Thermite and third-party
management are optional. Open documented protocols, explicit state
transitions and passive multicast observation remain requirements; no
hidden management paths. These independence requirements apply to all
Yoke component types. IR routing remains a separate unfinished hardware
track; ESP32 Ethernet OTA is not an IR implementation.

The broader goal includes HDMI endpoints over switched Ethernet. The
earlier 10GbE baseline does not constrain the USB 2 prototype. The software
reference exists; **the complete endpoint hardware path has not been
bench-tested**. The proposed 4K120 DSC link, physical USB output, HDCP
ciphertext relay and competitive-gaming latency targets remain hardware
or integration work.

The September research is dated analysis. Its vendor availability,
performance estimates, and legal interpretations have not been revalidated
for publication. Its feasibility arguments are not an end-to-end
implementation result. Keep its assumptions and ⚠ bench flags visible.

## Read in this order

| Document | Purpose |
|---|---|
| `README.md` | Public software status and commands |
| `doc/design/USB-ENDPOINT.md` | Agreed two-box USB 2 product and unfinished gadget bridge; apply the PoE correction above |
| `doc/design/ARCHITECTURE.md` | Current state ownership, driver execution, and trust boundaries |
| `doc/design/PROTOCOL.md` | Normative YWP/1 contract |
| `doc/decisions/0012-*` through `0017-*` | Durable transitions, serialized effects, USB ownership, media bounds, explicit provisioning, and status evidence |
| `doc/design/SIMULATION.md` | Installed offline fault campaign and control benchmark |
| `doc/design/MEDIA.md`, `MEDIA-TESTING.md` | Synthetic media transport and verification layers |
| `doc/design/PERFORMANCE.md` | Reproducible software measurements and their limits |
| `doc/design/UPDATES.md`, `ESP32-UPDATES.md` | Operator-controlled Linux releases and the ESP32 Ethernet OTA reference |
| `doc/design/USB-HOUSING.md`, `BRANDING.md` | Enclosure revisions, assembly limits, and shared identity assets |
| `doc/design/TELEMETRY.md` | Optional passive metrics, JSON API, host sampling and terminal monitoring |
| `doc/design/EDGE-DEVICES.md`, `doc/research/` | Proposed endpoints and dated hardware research |
| `CONTRIBUTING.md`, `SECURITY.md` | Verification, contribution, and security reporting |

## Implemented software

- **Control core:** sans-I/O state machines, HMAC-authenticated multicast
  frames, bounded input validation, explicit durable-save feedback, reserved
  sequence blocks, deterministic reconciliation, and passive observation.
- **Agent shell:** serialized effects, route-generation fencing, driver
  teardown barriers, durable state files, a local single-writer state lease,
  and explicit first-install or recovery decisions.
- **Driver adapters:** simulation, IGMP membership, USB/IP host imports,
  Just Add Power channel control, and supervised external stream consumers.
  Vendor transcripts and mocks establish adapter contracts, not hardware
  compatibility. A generic `active` state is backend-confirmed readiness,
  not an independent measurement of media or physical output. USB/IP imports
  into the local host; a physical USB output to another computer requires a
  separate gadget/proxy backend.
- **Media reference:** synthetic source patterns, raw transport, ratio
  models, packetization, bounded FEC/jitter/reassembly, passive observers,
  deterministic faults, and real UDP multicast/unicast runners. `jpegxs` and
  `vc2` are model names; neither invokes a production codec. Live transport
  supports `none` and duplicate-path FEC; XOR is an offline model.
- **Reproduction tools:** an installed control fault campaign, repeated
  software benchmarks, and a local static documentation builder with link,
  anchor, asset, and deterministic-output checks.
- **Updates:** standalone Linux release staging, activation, readiness,
  rollback and crash recovery; a separate ESP32-S3/W5500 Ethernet OTA
  reference with locally provisioned mutual TLS and dual app slots. The
  ESP32 reference does not implement IR routing. Hardware acceptance for
  both update paths remains outstanding; see ADRs 0018 and 0019.
- **Telemetry:** passive control observation, cached Prometheus/JSON export,
  host sampling and a terminal monitor. Thermite's optional scrape targets
  and dashboard do not become an endpoint management dependency.
- **Housing:** R6 is the computed PoE first article. Physical fit and powered
  cooling remain NOT RUN; R5 was a bare-board design and is retired.

The agent never receives media payload. Separate reference media tools may
process unprotected synthetic content. The full HDCP boundary remains
ADR 0011; no component may decrypt, hold HDCP secrets, alter HDCP messages
or capabilities, or instruct endpoints to weaken protection.

## Verification and publication

```sh
make verify
make simulate
make benchmark-control
make benchmark-media
make docs-serve
```

The Makefile creates `.scratch/venv`; `VENV=.scratch/dev` can reuse an
existing developer environment. All generated output, caches, and test
temporaries belong under `.scratch`. Runtime code is stdlib-only, with
pinned developer tooling in the `dev` extra. Preserve any pre-existing
user environment outside `.scratch`.

CI runs the gate and an isolated wheel smoke test on Python 3.12 and 3.14,
plus a pinned ESP32 SDK target build and complete firmware host tests.
The `feature/ir` workflow publishes `.scratch/site` only after all gates
pass. GitHub Pages must use the GitHub Actions publishing source. There
are no runtime CDN assets or analytics. Canonical `doc/` Markdown feeds
the site directly; ADRs and new design pages appear automatically.

Socket tests may skip where sockets are unavailable; a passing run with
such skips does not establish the real-socket layer. Synthetic simulation
latency, Python benchmark timing, and historical real-codec timings are
separate forms of evidence. None establishes physical glass-to-glass
latency.

## Remaining engineering work

Hardware priorities are the PoE enclosure and the complete two-box USB
path above. HDMI research separately retains unresolved monitor/connector
compatibility, GPU DSC policy, accessible bridge silicon/firmware and
clock-locked transport. The roadmap's vendor questions and bench milestones
remain pending. No simulated outcome should mark those complete.

YWP/1 retains one site-wide key. Any key holder can impersonate origins and
author intents. Per-device identity, enrollment, and source authorization
need a separately designed protocol version and ADR; current parser and
DoS hardening do not provide that authority model.

For each new field failure, retain a deterministic regression with the fix.
Protocol changes begin in the normative document and an ADR. Hardware
assumptions stay isolated behind driver seams and labeled until a bench
result establishes them.
