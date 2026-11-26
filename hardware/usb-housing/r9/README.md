# R9 hardened compact Yoke endpoint housings

R9 supersedes R8 after physical fit on real Radxa ZERO 3E v1.2 hardware proved
R8's three `6 × 5 mm` front base pads (`y = 0.8..5.8`) collided with underside
components omitted from Radxa's 3D STEP file (`U12` 16-pin SOIC IC, micro-HDMI
& USB-C through-hole shield legs, `ED9`–`ED12`, `C151`), propping the front side
open by several millimetres while the bare-edge rear side sat flush. Like R8,
R9 provides two boxes sharing one identical base design on a single print plate:

| Box | Electronics | Power | Outer size (mm) |
|---|---|---|---|
| PoE (device side) | ZERO 3E + Radxa RA023 PoE HAT (14.5 mm pillars) | Active 802.3af/at PoE (Mode A) | **84.55 × 39.20 × 40.20** |
| Bare (computer side) | ZERO 3E only (GPIO header fitted) | Host computer USB-C OTG | **84.55 × 39.20 × 19.20** |

Physical stock fit, cable seating, service cycles, and powered thermal
performance remain **NOT RUN** until observed on the printed hardware.

One print plate carries both housings: two identical bases, the PoE lid, and
the bare lid (`55.77 g` PLA, `178` layers, estimated `4h 43m 56s` plus prep on
FlashForge Adventurer 5M, supports disabled). Each box closes with four
recessed **M3 × 8** socket-head screws into four **RX-M3×5.7** heat-set
inserts.

## What R9 fixes over R8

| Defect in R8 / R9 v1 | R9 fix |
|---|---|
| **R8 physical fit failure — front base bumps hit ZERO 3E underside parts** | Replaced R8's three `6 × 5 mm` front bumps (`y = 0.8..5.8`) with three narrow bare-edge pads (`[11.0, 13.8] × [-0.45, 1.40]`, `[20.2, 24.8] × [-0.45, 0.85]`, `[51.0, 53.6] × [-0.35, 1.00]`) verified `0`-hit against official `radxa_zero_3e_v1200_bottom.dxf` (`>= 0.44 mm` clearance to all THT legs & SMD across full 3-DOF float/yaw), opening `4.20 mm` clear floor height underneath `U12` and all front connector pins. |
| **R9 first cold-review fix — Unconstrained X-float (`[-0.88, +0.94] mm`) & board yaw (`-1.51°..+0.86°`)** | Added **8 positive PCB-perimeter locator stops** on the base (`z = 0..7.20 mm`, all solid straight to floor): two `-X` stops (`x = -1.85..-0.25` flanking RJ45), two `+X` stops (`x = 65.25..66.85` flanking microSD), two beveled rear `+Y` stops (`y = 30.25..31.40` on rear lip), and two front `-Y` stops (`[20.6, 24.8]` beveled on front lip and `[46.9, 48.7] × [-1.25, -0.25]` in the USB3/OTG gap `0.35 mm` behind the `y <= -1.60` USB-C plug sweep). Constrains PCB travel to `dx, dy in [-0.25, +0.25] mm` and yaw to `[-0.54°, +0.48°]` even before the lid closes. |
| **R9 first cold-review fix — Real 40-pin GPIO centerline (`CON1 y = 24.135..29.215`, bottom pads `y <= 28.771`) & top-side SMD under bare ribs** | Expanded rear cavity (`body[3] = 34.50`, `inside[3] = 31.80`, rear screw axes `y = 29.50`) placing the rear lip behind `y = 30.0`, shifting `back_pads` to `y = [29.25, 30.25]` (`>= 0.234 mm` clear of real GPIO bottom pads), shifting `bare_fins` rear ribs to `y = [29.55, 31.80]` (`>= 0.090 mm` clear of real `CON1`), front bare rib to `[17.8, 20.2] × [-2.0, 0.80]` (clear of top `U6`), and closed-end bare rib to `[64.15, 72.10] × [14.5, 17.5]` (`>= 0.282 mm` clear of `C340`–`C348`, `>= 0.601 mm` worst-case PCB overlap). |
| **Upgraded digital checks (`reference_envelopes.scad` & `check_access.py`)** | Encoded the exact chamfered `BG_OUTLINE` PCB, real `CON1`, and all official v1.2 bottom & top DXF component keepouts; `check_access.py` enforces 163 checks including 13 3-DOF `(dx, dy, yaw)` poses against `base`, `bare-lid`, `CON1`, and `poe-lid` (`0.000 mm³` overlap). |

## Retained R8 hardened features

- **Closure & solid insert bosses.** Uniform `2.70 mm` outer walls (`5.00 mm`
  corner inset), zero lid corner seat recess (`seam = 4.3`,
  `lid_bottom = contact_z = 4.6`, `>= 34.94 mm²` flat post-ring seat, `0.3 mm`
  outer shadow gap). Three `60° × 0.12 mm` open-top vertical perimeter-forcing
  seams at `r = 3.92..4.04 mm` (`z = 4.6..12.6`) force OrcaSlicer's frozen
  `wall_loops = 2 / 15%` profile to print four continuous concentric solid wall
  rings (`0` sparse-infill layers inside `r = 2.30..3.90 mm`, `>= 60.18 mm`/layer
  solid extrusion across all 8 insert bosses). Base M3 × 8 head wells keep `2`
  slow (`25 mm/s`) bridge layers + `4` fused solid layers (`z = 3.4..4.6`), full
  `5.7 mm` RX-M3×5.7 insert engagement (`z = 4.8..10.5`), `0.9 mm` tip beyond,
  and `0.7 mm` blind-pilot reserve. Neither PCB is in the clamp stack.
- **Electrical safety & openings.** Full-height Ø`8.0 mm` keepouts clear all 4
  live `48–57 V` corner mounts across all 3 printed parts (`0.000 mm³` overlap),
  all roof and side vents stay `>= 6.4 mm` from mounts, bare roof slots stop at
  `y = 22.0` to shroud the exposed 40-pin GPIO header, expanded USB-C bay
  (`x = 33.8..61.8, z = 3.7..14.2`) clears `15 × 10 mm` overmolded plugs, and
  RJ45 window (`z = 2.35..15.80`) provides `+1.55 mm` top-latch margin.
  Geometric open area is `1,238.1 mm²` (PoE) and `742.2 mm²` (bare); these areas
  are not measured airflow or a thermal rating.

## Rebuild and check

```sh
python3 hardware/usb-housing/r9/build.py --output .scratch/r9-design/export
openscad --backend=manifold -D 'part="sbc-ref"' -o .scratch/r9-design/export/sbc-ref.stl hardware/usb-housing/r9/reference_envelopes.scad
openscad --backend=manifold -D 'part="hat-ref"' -o .scratch/r9-design/export/hat-ref.stl hardware/usb-housing/r9/reference_envelopes.scad
python3 hardware/usb-housing/r9/check_access.py \
    --parts .scratch/r9-design/export --output .scratch/r9-design/evidence/access.json
python3 hardware/usb-housing/r9/check_joint.py \
    --parts .scratch/r9-design/export --output .scratch/r9-design/evidence/joint.json
python3 hardware/usb-housing/r9/document.py --parts .scratch/r9-design/export \
    --sbc-ref .scratch/r9-design/export/sbc-ref.stl \
    --hat-ref .scratch/r9-design/export/hat-ref.stl \
    --output hardware/usb-housing/r9 --evidence .scratch/r9-design/evidence
```

[validation.json](validation.json) records all digital checks;
[print.json](print.json) records the verified 2026-11-26 Adventurer 5M dispatch;
[assembly.pdf](assembly.pdf) gives the full monochrome build sequence and
unpowered/powered physical test protocol. Printing requires the independently
reviewed and approved Lacuna assembly release packet.
