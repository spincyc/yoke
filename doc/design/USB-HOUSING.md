# USB endpoint housing

The USB endpoint case is a snug, screw-closed two-piece prototype for the
**Radxa ZERO 3E V1.2**. Its body is 76.4 × 34.8 × 20 mm, with screw heads
below the base. The mounting-pad regions are designed to carry the clamp
load while avoiding contact with IC packages and GPIO pins.

**R1 is the first-article print revision. R2 is the branded CAD revision and
has not been printed.** The existing R1 job uses its original released
G-code and `YOKE R1` wordmark. The R2 files below do not replace that job.
Physical fit, cable access, retention and thermal testing remain **NOT RUN**.

![R2 branded housing, exact-mesh CAD preview](../../hardware/usb-housing/r2/preview.png)

## R2 branding

The [Yoke identity](BRANDING.md) appears as one small monochrome recess on
the lid, beside the vents. The CAD imports the canonical SVG directly.
The mark is 8 × 8.667 mm, recessed 0.45 mm, with 2 mm broad elements and
1.55 mm of roof beneath it. Its centre is (9, 15) mm in the original board
coordinate frame; the nearest vent starts 3 mm away.

![Recessed canonical mark and R2 L part ID](../../hardware/usb-housing/r2/lid-detail.png)

R2 restores the old exterior text pockets and engraves `R2 B` / `R2 L` part
IDs. Its native model imports the R1 mechanical modules. Exact mesh
comparison confines every added or removed volume to the shallow exterior
ID and mark regions. Screw bores, nut bearing seats, support posts, the
0.15 mm seam gap, cable openings and vents are unchanged. Each mesh is
watertight; the two-part plate rebuilds byte for byte. These are nominal
digital checks, not a physical qualification.

## Download and rebuild

| Revision | Native CAD | Two-part plate | Instructions and evidence |
| --- | --- | --- | --- |
| R1 — first article | [OpenSCAD](../../hardware/usb-housing/housing.scad) | [R1 STL](../../hardware/usb-housing/yoke-usb-housing-r1.stl) | [Assembly PDF](../../hardware/usb-housing/assembly.pdf), [print record](../../hardware/usb-housing/print.json) |
| R2 — NOT PRINTED | [OpenSCAD](../../hardware/usb-housing/r2/housing.scad) | [R2 STL](../../hardware/usb-housing/r2/yoke-usb-housing-r2.stl) | [Rebuild script](../../hardware/usb-housing/r2/build.py), [mesh validation](../../hardware/usb-housing/r2/validation.json) |

The R2 [base](../../hardware/usb-housing/r2/base.stl) and
[finished lid](../../hardware/usb-housing/r2/lid.stl) describe the assembled
parts. Its [manufactured lid](../../hardware/usb-housing/r2/lid-printed.stl)
includes the four 0.20 mm sacrificial bore membranes. Rebuild from the
repository root using Manifold-enabled OpenSCAD:

```sh
python3 hardware/usb-housing/r2/build.py
```

The script pins the R1 geometry and canonical brand asset hashes. Changing
either requires review. The R2 plate and component declarations are design
artifacts; they carry no print release. A future print needs a fresh slice,
exact-artifact manufacturing and assembly review, release and a clear bed.

## Assemble and test

Supply four **M2.5 × 20 mm pan-head machine screws**, four **M2.5 hex nuts**,
a matching screwdriver and a **3 mm drill bit used by hand**. Follow the
[assembly PDF](../../hardware/usb-housing/assembly.pdf); R2 uses its same
sequence with `R2 B` / `R2 L` identification.

1. With the lid alone on scrap wood, hand-clear its four thin bore membranes.
   Remove swarf; require clear 3.0–3.2 mm lid bores and intact flat nut seats.
2. Keep the board unpowered. Lower it onto the four base posts, Ethernet
   toward the open short end, then lower the matching lid without force.
3. Seat the nuts from above. Hold the case above a table edge and insert
   the 20 mm screws upward from below.
4. Lightly snug alternating corners. Preserve the intentional 0.15 mm seam
   gap. Stop for board bow, cracking, whitening, a spinning nut or rising
   torque before seating. No tightening torque has been qualified.

The thin post ligaments beside Ethernet retain the R1 light-hand-snug and
5 N prototype trial scope. Test actual Ethernet latch access and both USB-C
cables unpowered, then retention, then a 30-minute intended-load thermal
run with clear vents. Stop at 75 °C or a lower applicable manufacturer
limit. Record the actual board, filament, seam, cable and temperature results
before deployment.

The model targets the bare board. USB-C cable overmoulds are assumed to be no
larger than 12 × 7 mm. A PoE HAT, added heatsink, bracket or ingress protection
needs a separate envelope and design check. The vendor board mesh is
nonmanifold; the existing sampled assembly check and conservative component
clearance inspection do not establish continuous collision-free motion or
measured fit.
