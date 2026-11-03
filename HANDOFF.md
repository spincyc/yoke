# Yoke implementation handoff

Updated 2026-11-03. **The current R5 enclosure does not fit the PoE HAT.**
It was designed and sent to print for a bare ZERO 3E, omitting the original PoE
requirement. This is a design miss, not a newly requested feature. A PoE
housing remains unfinished. Do not present or repeat R5 as the solution.

The latest request is to create the complete PoE housing with ventilation,
cold-review it and send it to the ready printer once the design is complete.
That print authorization remains unconsumed. Reference inspection found a
material inconsistency in Radxa's published dimensions; a replacement has
not been modeled, sliced or dispatched. No cancellation was sent.

## Current dimensional block

The owner identified the purchased kit as
[Amazon ASIN B0D1KF1L7G](https://www.amazon.com/dp/B0D1KF1L7G), also linked by
Radxa as a distributor listing for RA023. Its main catalog image is marked
**X1.0** and its underside image shows no fan. This resolves product identity,
but catalog images do not prove the delivered board revision. No assembled
gap, complete height or hardware dimensions were found in the listing. Do not
assume that the fan-equipped downloadable model represents this kit unchanged.

The [mechanical reference record](hardware/usb-housing/poe-reference.json)
pins the official CAD and product sources inspected on 2026-11-03. The
combined STEP places the facing PCBs 18 mm apart, while Radxa's product
specification lists 14.5 mm insulated copper pillars. Current and older HAT
STEP files are byte-identical. The model also omits the mounting screws,
pillars, fan fasteners, fan leads and photographed heatsinks. Its approximately
36.96 mm height is not a verified envelope for the complete physical module.

The owner has been asked first for the actual facing-PCB gap, complete
assembled height and presence of an underside fan. Earlier requested
screw-head and pillar dimensions are necessary only if the chosen supports
depend on them. The cold reference review considers lower-board edge retention a
possible way to avoid head-specific supports, provided the module and hardware
keepout envelopes are measured. It does not approve a complete housing or print.
Do not resolve the spacing conflict by selecting one nominal value or treating
the larger CAD stack as a proven physical upper bound.

The printer was reconciled read-only during this request: **READY, no current
file**. This establishes that R5 is not active, not that it passed physical
acceptance. The user says the printer is ready; recheck printer state before
using the remaining authorization. R5 repeat jobs are now retired in Lacuna's
`tools/print/retired-jobs.d/yoke-usb-housing-r5.json`.

Pending-task references remain in `.scratch/poe-reference/`: original STEP
archives, combined and component meshes, source inspection report and a Python
environment with CadQuery/OCP and document tools. The independent reference
review is in `.scratch/r6-cold-reference/`. These are disposable working files;
the canonical JSON above retains source URLs, hashes and the unresolved facts.

## Resume here

The immediate hardware task is a compact **Radxa ZERO 3E plus PoE HAT**
housing that slots together freely, screws together, has recessed heads,
and has a flush rectangular exterior with usable airflow. The user rejected
projecting fastener towers and repeated housings whose halves did not mate.
Use the same enclosure screw and heat-set insert stock as Lacuna; the later
heat-set requirement supersedes the earlier captive-nut choice.

[Radxa's PoE HAT assembly instructions](https://docs.radxa.com/en/zero/zero3/accessories/3e-poe-hat)
state that the metal screws and copper pillars electrically connect the
ZERO 3E and HAT, and must be installed and tightened for PoE to work. R5's
plastic PCB locating pegs and supports must therefore be reconsidered with
the complete assembly. **A taller lid alone is insufficient.** The exact
HAT variant, mechanical model, assembled stack height and mounting hardware
dimensions have not yet been verified. Do not substitute guessed values.

### Existing print and recovery

| Item | Recorded state |
| --- | --- |
| Printer | FlashForge Adventurer 5M, stock 0.4 mm nozzle |
| Job | `yoke-usb-housing-r5-fullcase-first-article.gcode` |
| Dispatch | One attempt; acknowledged 2026-11-03 at 12:17:16 UTC |
| Last read-only observation | 2026-11-03 at 13:28:39 UTC: printing, layer 26 of 68, no reported error |
| Estimate | 21.16 g PLA; 1 h 35 m 56 s plus preparation |
| Physical acceptance | Not run; no owner fit or thermal result for R5 |

The observation is a timestamped snapshot, not a promise of current state
or completion. Reconcile the existing job before any printer action. Its
single start authorization has been consumed; never replay the old start
commands or automatically reauthorize a duplicate. No cancellation was sent
after discovering the PoE omission.

The [print record](hardware/usb-housing/r5/print.json) holds the exact STL,
G-code, packet, review and manual hashes. The corresponding two dispatch
rows are committed in Lacuna's `tools/print/dispatch-log.jsonl`.

Retain these existing local resources while reconciling the job:

- Yoke `.scratch/r5-print/packet/release.json` and its entire packet directory:
  exact dispatched geometry, G-code, official bare-board reference and review
  evidence. Other R5 design/review scratch directories have been removed.
- Yoke `.scratch/r5-print/readiness.json`: historical dispatch evidence and
  commands, **not permission to start again**. Its `latest-status.json`
  sibling is an older snapshot than the table above.
- Lacuna `.scratch/r4-print-runtime/`: retained printer and geometry tools;
  its R4 directory name is historical, and it also served R5.

From the Lacuna clone, the existing read-only status command is:

```sh
.scratch/r4-print-runtime/venv/bin/python -B tools/print/ad5m.py status
```

Keep credentials and raw printer responses out of the repository. Do not
run `make clean` or `wt tidy` while this retained packet/runtime is needed;
both are disposable scratch and are not reconstructed by a Git checkout.

### What R5 establishes and what it does not

[R5 source and assembly instructions](hardware/usb-housing/r5/README.md)
describe an 84 × 35.9 × 20 mm bare-board case. It uses a vertical nesting
lid, a 1.2 mm skirt with 0.4 mm per-side clearance, four recessed M3 × 8
socket-head screws and four RX-M3×5.7 inserts. Side inlets and top outlets
are modeled. Enclosure screws do not pass through the PCB mounting holes.
Keep these enclosure fasteners distinct from Radxa's electrically required
board/HAT fasteners.

[Digital validation](hardware/usb-housing/r5/validation.json) covers native
exports, mesh and toolpath checks, sampled lid/screw/tool assembly paths,
and the official bare-board model. It does **not** cover the HAT, actual
print tolerances, usable threads, cable collars, physical fit, durability,
or powered cooling. Review passed for the modeled bare-board article and
missed the system requirement; it cannot establish PoE compatibility.

R1 and R3 full-case prints failed owner acceptance. R4's joint samples also
failed: the owner reported that the pieces did not line up or slot together;
see [the recorded result](hardware/usb-housing/r4/owner-result.json). The
physical cause is not established. R5 replaced R4's offset-and-slide joint
with vertical nesting, but has no physical fit result yet. R1/R3/R4 are
retired in Lacuna's print guards; R5 is now retired for omitting PoE.

### Next engineering steps

1. Reconcile the existing R5 print and record any owner observations without
   confusing a bare-board fit result with PoE acceptance.
2. Identify the exact PoE HAT and obtain its official drawing/model or
   measured assembly. Include both PCBs, connectors, conductive standoffs,
   screws, component heights and any cooling parts in one CAD assembly.
3. Redesign the board supports and enclosure around that stack. Preserve
   required electrical mounting connections and insulation, free lid
   engagement, recessed enclosure screws, a rectangular exterior, cable
   access, and clear inlet/outlet paths. Minimize size only after checking
   these constraints.
4. Update the canonical [USB design](doc/design/USB-ENDPOINT.md),
   [housing page](doc/design/USB-HOUSING.md), revision records, drawings and
   assembly instructions together when the replacement is complete. The
   design pages now state the PoE requirement and dimensional block; R5
   drawings remain explicitly historical and its repeat-print path is retired.
5. Use Lacuna's current acceptance and print workflow on the **complete
   board/HAT assembly**: actual-part assembly and tool access, fastener
   engagement and tip clearance, print tolerances, mesh/support checks,
   exact sliced toolpath, and independent review of the bound release
   packet. A repeat of bare-board-only checks is insufficient.
6. Before a replacement print, reconcile the printer and build plate, then
   follow the current Lacuna release workflow and applicable user print
   authorization. Record actual slot fit, screw/insert fit and powered
   thermal results after printing; digital checks cannot mark those passed.

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
- **Housing:** R5 was submitted once and is a bare-board design. The PoE
  requirement and physical acceptance remain unresolved as detailed above.

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
