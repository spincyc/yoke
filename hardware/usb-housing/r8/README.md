# R8 hardened compact Yoke endpoint housings

R8 replaces R7 after a comprehensive pre-print mechanical, electrical-safety,
and printability audit identified three blocking defects and eight major defects
in R7. Like R7, R8 provides two boxes sharing one identical base design on a
single print plate:

| Box | Electronics | Power | Outer size (mm) |
|---|---|---|---|
| PoE (device side) | ZERO 3E + Radxa RA023 PoE HAT (14.5 mm pillars) | Active 802.3af/at PoE (Mode A) | **84.55 × 38.00 × 40.20** |
| Bare (computer side) | ZERO 3E only (GPIO header fitted) | Host computer USB-C OTG | **84.55 × 38.00 × 19.20** |

Physical stock fit, cable seating, service cycles, and powered thermal
performance remain **NOT RUN** until observed on the printed hardware.

One print plate carries both housings: two identical bases, the PoE lid, and
the bare lid (`54.54 g` PLA, `178` layers, estimated `4h 36m 54s` plus prep on
FlashForge Adventurer 5M, supports disabled). Each box closes with four
recessed **M3 × 8** socket-head screws into four **RX-M3×5.7** heat-set
inserts.

## What R8 fixes over R7

| Defect in R7 | R8 fix |
|---|---|
| **Blocker 1 — Wrong PoE HAT gap (`18.0 mm` vs shipped `14.5 mm` pillars) & tight `1.0 mm` roof clearance** | `hat_gap = 14.5 mm` matches Radxa's shipped `4 × M2.5 × 14.5 mm` insulated copper pillars, with `3.0 mm` PoE roof clearance (`40.2 mm` height, exact 178 lid layers) and `1.7 mm` bare roof clearance (`19.2 mm` height, exact 73 lid layers). |
| **Blocker 2 — Exterior corner wall breakthroughs on both lids** | Eliminated R7's `r = 4.3 mm` lid seat recess completely (`seam = 4.3`, `lid_bottom = post_contact_z = 4.6`). Both lids now have continuous `2.70 mm` outer walls (`>= 0.96 mm` unbroken 2-loop exterior corner skin outside internal perimeter seams; `check_access.py` verifies zero breakthrough). |
| **Blocker 3 — Live 48–57 V PoE corner mounts & bare 40-pin GPIO exposed through vents** | All roof and side vents stay strictly within `x = 10..53` (`>= 6.4 mm` solid plastic shroud over all 4 live corner mounts); bare lid roof slots end at `y = 22.0` to shroud the live `+5V`/GND 40-pin GPIO pins. Full-height Ø8 mm keepouts checked across all 3 printed parts (`base`, `poe-lid`, `bare-lid`). |
| **R8 first cold-review fix — Hollow insert bosses under `wall_loops=2, 15%` infill** | Screw axes inset `5.00 mm` from outer walls (`lid post_r = 4.05 mm`, `base_post_r = 4.25 mm`) with `0.12 mm` open-top vertical perimeter-forcing micro-seams at `r = 3.92..4.04 mm` (`z = 4.6..12.6`). Forces OrcaSlicer to print solid concentric walls + gap fill through the entire `r = 2.00..3.92 mm` (`>= 1.62 mm` outside Ø4.6 mm insert) across all 8 insert bosses, verified directly in G-code by `check_toolpaths.py`, while preserving `>= 34.9 mm²` flat seat contact and `>= 1.85 mm` exterior head-well wall. |
| **R8 first cold-review fix — Float-proof bare retention ribs** | Front bare ribs shifted to `x = 20.0..22.4` and `30.0..32.4` (`>= 4.3 mm` clear of micro-HDMI and USB3); rear bare ribs placed at `y = 29.50..30.60` with RJ45 cradle tightened to `±0.27 mm` so worst-case `5.08 mm` 2×20 GPIO (`y <= 29.04`) clears ribs across full base/case XY float (`27` float checks in `check_access.py`). |
| **Major — Fragile USB-C bay lip teeth, rear keepout slivers & thin RJ45 cradle walls** | `low_inlets` (`z = 1.8..3.7`) sit cleanly below the continuous `seam = 4.3` base rim; segmented `lip_ring` stays strictly inside mount-free and USB-free spans; RJ45 cradle walls widened to `1.2 mm` ending at `seam = 4.3`. |
| **Major — Tight USB-C (`12.5×7.5`) & RJ45 top-latch windows** | Expanded USB-C opening (`x = 33.8..61.8, z = 3.7..14.2`) clears `15.0 × 10.0 mm` overmolded plugs on both ports; RJ45 window (`z = 2.35..15.80`) adds `+1.55 mm` top-latch/boot clearance. |
| **Major — Fragile `0.9 mm` bare rear ribs & unlabelled USB-C ports** | Sturdy `2.4 mm` front and `4.0 mm` rear bare ribs (`>= 10 mm` from all live corner mounts); roof recessed labels `YOKE R8 POE/USB <LAN`, `USB3` (inner host port, `x = 41.6`), and `OTG` (outer gadget/power port, `x = 54.0`), inset from front edge and vents. |

## Design summary

- **Closure & clamp path.** Lid walls lower vertically over three `1.2 mm`
  beveled inner lip bars (`0.4 mm` clearance/side, `1.0 mm` engagement, `0.5 mm`
  lead-in). Four solid Ø`8.50 mm` base post rings stop the flat Ø`8.10 mm` lid
  post faces at `z = 4.6` (`>= 34.94 mm²` per post), leaving an even `0.3 mm`
  outer shadow gap above `seam = 4.3`. Each M3 × 8 screw enters a chamfered
  bottom well (`head_d = 6.3`, recessed `0.4 mm`), bears on two `25 mm/s`
  bridged shelves at `z = 3.4` (`3.4 × 3.4 mm` square bridge layer) fused
  through `4` full solid layers (`z = 4.0..4.6`), and engages the full `5.7 mm`
  RX-M3×5.7 insert (`z = 4.8..10.5`) in a solid perimeter-forced boss with
  `0.9 mm` tip beyond and `0.7 mm` blind-pilot reserve. Neither PCB is in the
  clamp stack.
- **Board retention & electrical safety.** The SBC underside rests on six
  component-free edge pads (`z = 5.8`). Lid ribs stop `0.4 mm` above
  component-free top edges (HAT edges in PoE box, SBC edges in bare box) without
  clamping or loading Radxa's conductive copper pillars. Radxa's four
  `M2.5 × 14.5 mm` insulated copper pillars and eight `CM2.5 × 4` screws remain
  installed and untouched in the PoE box; mounting holes stay empty and shrouded
  in the bare box. Never connect both PoE and USB-C power simultaneously.
- **Ventilation.** Desk-open low inlets (`z = 1.8..3.7`), side vent rows
  (`3.0 mm` slots on `5.0 mm` pitch with `2.0 mm` struts), and 9 roof slots
  (`x = 10..53`). Geometric open area is `1,238.1 mm²` (PoE) and `742.2 mm²`
  (bare); these areas are not measured airflow or a thermal rating.

## Rebuild and check

```sh
python3 hardware/usb-housing/r8/build.py --output .scratch/r8-design/export
python3 hardware/usb-housing/r8/check_access.py \
    --parts .scratch/r8-design/export --output .scratch/r8-design/evidence/access.json
python3 hardware/usb-housing/r8/check_joint.py \
    --parts .scratch/r8-design/export --output .scratch/r8-design/evidence/joint.json
openscad --backend=manifold -D 'part="sbc-ref"' -o .scratch/r8-design/export/sbc-ref.stl hardware/usb-housing/r8/reference_envelopes.scad
openscad --backend=manifold -D 'part="hat-ref"' -o .scratch/r8-design/export/hat-ref.stl hardware/usb-housing/r8/reference_envelopes.scad
python3 hardware/usb-housing/r8/document.py --parts .scratch/r8-design/export \
    --sbc-ref .scratch/r8-design/export/sbc-ref.stl \
    --hat-ref .scratch/r8-design/export/hat-ref.stl \
    --output hardware/usb-housing/r8 --evidence .scratch/r8-design/evidence
```

[validation.json](validation.json) records all digital checks; [assembly.pdf](assembly.pdf)
gives the full monochrome build sequence and unpowered/powered physical test
protocol. Printing requires the independently reviewed and approved Lacuna
assembly release packet.
