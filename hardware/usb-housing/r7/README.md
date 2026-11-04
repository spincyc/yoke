# R7 compact Yoke endpoint housings

R7 replaces R6, which the owner judged too large as printed (2026-11-04). It
provides two boxes that share one base design:

| Box | Electronics | Power | Outer size (mm) |
|---|---|---|---|
| PoE (device side) | ZERO 3E + Radxa RA023 PoE HAT | PoE through Ethernet | **81.4 × 36.25 × 41.7** |
| Bare (computer side) | ZERO 3E only (GPIO header fitted) | Computer's USB-C | **81.4 × 36.25 × 18.5** |

The PoE box is 8.5% shorter, 20% narrower and 9% lower than R6 (89 × 45.5 ×
46). Its volume is 34% smaller. Physical fit and powered cooling are **NOT
RUN** until the electronics arrive.

The parts are designed for the ordered ZERO 3E 2GB with GPIO
([B0CZD1C54N](https://www.amazon.com/dp/B0CZD1C54N)) and the Radxa PoE HAT
([B0D1KF1L7G](https://www.amazon.com/dp/B0D1KF1L7G)). One print plate carries
both housings: two identical bases, the PoE lid and the bare lid. Each box uses
four **M3 × 8** socket-head screws and four **RX-M3×5.7** inserts. No clips,
standoffs or extra hardware are needed.

## Where the space went

R6's internal clearances are removed or reduced as follows. All values are
measured from the official RS109_RA023 CAD.

| R6 allowance | R7 |
|---|---|
| 12.7 mm closed-end gap for two screw bosses and a clip boss | Clip removed. The two closed-end posts sit at x = 69.3, merged into the end and side walls. |
| 7.5 mm USB-side gap for a clip boss; USB-C faces 9.5 mm inside the wall | Front wall is 0.4 mm clear of the HDMI face. The **USB-C receptacle faces are 2.15 mm** from the outer wall. |
| 1.5 mm below the board, 3.5 mm above the HAT | 0.54 mm below the lowest underside part; 1.0 mm above the HAT's tallest part |
| 2 mm walls plus a separate 1.2 mm skirt | 2 mm lid walls nest over a 1.2 mm base lip that sits below the PCB |

## Design decisions

**Closure.** The lid lowers vertically over a 1.2 mm inner lip on the base,
with 0.4 mm clearance per side, 1.5 mm engagement and a 0.5 mm lead-in. Each
base post stays a solid 7.8 mm ring from the floor to a seat at z = 4.6. The
ring rises into an 8.6 mm recess in the lid, and the lid post bears on it.
These four seats are the only hard stops, each about 29 mm² of contact. They
leave a 0.3 mm shadow gap at the outer seam by design. There is no hook,
slide or snap.

Each screw enters a bottom well and the head sits 0.4 mm below the floor. The
head bears on two bridged shelves at z = 3.4. The next layer is a square
bridge over the slot. Four full, solid layers then fuse the shelf into the
post wall up to the seat; the toolpath audit shows the shelf and square
layers printed as slicer bridges at 25 mm/s with full fan.

The screw continues into an insert in the lid post, which spans
z = 4.8–10.5. The screw engages the full 5.7 mm of the insert, and its tip
ends 0.9 mm beyond it with a 0.7 mm blind-pilot reserve. The clamp runs from
the head through the fused shelf and post ring to the seat, then the lid post,
insert and screw. Neither PCB is in that load path.

R7's first review found the earlier spigot design held by a 0.2 mm layer over
the head well. That design was replaced before release.

**Screw placement (coordinator idea 1).** Two screws sit beside the RJ45 at
the Ethernet end, in space that is otherwise unused. The closed end cannot go
without screws. Without them, a screw-free closed end needs a hook or tilt
tab, and the owner rejected hooks and sideways joints in R4. Its posts also
cannot sit just past the card allowance. An insert boss must lie entirely
outside the vertical path along which the board stack is lowered. It needs a
1.6 mm wall around the 4.6 mm insert, which is a 7.8 mm column. The nearest
legal axis to the HAT edge (x = 65.06) is therefore x ≈ 69.2. A search over
the official meshes confirmed this.

Placing the insert under the board or above the HAT would need screws of 30 mm
or more; the owner's longest screw is 20 mm. The corner posts are merged into
the end and side walls, so no towers or ears project from the case. The posts
add 3.3 mm of length compared with a case limited only by the card allowance.

**Board retention (coordinator idea 2, adopted).** The SBC underside rests on
three front pads (y = 0.8–5.8) and three raised rear-lip pads (y = 29.0–30.2).
These pads stand at x = 10–16, 27–33 and 46–52, on component-free edges. They
avoid the header pins and all Radxa hardware reservations.

The lid ribs stop 0.4 mm above component-free top edges, so they limit lift
without clamping. On the PoE lid, the ribs sit over the HAT at x = 12–14.4 and
48–50.4. On the bare lid, they sit over bare SBC edges at x = 16.2–18.2,
29–31, 5.95–6.85 and 58.15–59.05, clear of the mounting-hole pads. The RJ45
sits between the two Ethernet-end posts above a
low guard and cradle. The lid walls and posts locate the board within about
0.4 mm.

The entire Radxa stack is assembled outside the case and lowered as one unit.
Its conductive joints are never disturbed to install retention.

**Brass standoffs and nylon screws (coordinator idea 3, rejected).** These
would need hardware the owner may not have. They would also extend live PoE
metal toward the floor and add a second electrical joint. The rib and pad
scheme needs none of that.

**Electrical rule.** Radxa's four insulated copper pillars and eight CM2.5×4
screws stay installed and tightened. No case fastener substitutes for them,
and no metal path reaches the outside. Every printed part keeps clear of
diameter-8 reservations around the four mounting axes:

- below the SBC (4 mm);
- between the boards (18 mm source gap);
- above the HAT (4 mm).

The base also keeps a diameter-8.2 keepout above its floor. The bare box
treats its mounting holes the same way, because they are still wired to the
RJ45 centre taps. Nothing enters or touches them.

**Apertures.**

- **Ethernet:** an end window with the jack face outline plus 0.5 mm (y 6.43–23.63,
  z 2.35–14.75); the jack face is 1.7 mm inside the outer end face.
- **USB-C:** one 26 × 9.5 mm opening open to the seam serves both ports. It
  passes a 12.5 × 7.5 mm overmold up to each receptacle face.
- **HDMI:** covered.
- **Closed end:** solid.
- **microSD:** sits under the closed end with 2.5–3 mm card projection allowed.
  Change it with the lid off and the board lifted out.

**Ventilation.** Ten low inlets per long side, z = 2.1–5.6, cross the seam
and stay open on a desk. They sit between the edge pads, so no pad face blocks
them. The PoE box has side rows between and above the boards; the bare box has
one row above its board. Each roof has 13 slots. The HAT's female GPIO header
stands 1.4 mm behind the PoE rear wall between the boards, so that rear row
keeps only its two header-free end slots. Geometric open area is 2,270 mm² for
PoE and 1,511 mm² for bare. These areas are not measured airflow.

**HAT-height uncertainty.** Radxa's CAD gap is 18 mm; the listing says 14.5 mm
pillars. The design follows the 18 mm CAD with 1.0 mm roof clearance. The base
has no HAT-dependent feature. If the measured gap differs, set `hat_gap` and
rebuild: only the PoE lid changes. Its height, rib depth and vent rows follow
`hat_gap`; at 14.5 mm the lid is 38.2 mm tall.

## Rebuild and check

```sh
python3 hardware/usb-housing/r7/build.py --output .scratch/r7-design/export
python3 hardware/usb-housing/r7/check_access.py \
    --parts .scratch/r7-design/export --output .scratch/r7-design/evidence/access.json
python3 hardware/usb-housing/r7/document.py --parts .scratch/r7-design/export \
    --reference <poe-reference>/rs109_ra023.stl --output hardware/usb-housing/r7
```

These commands need Manifold-enabled OpenSCAD, trimesh, manifold3d,
reportlab and PyMuPDF.

- `check_joint.py --parts <export> --output <json>` checks each screw post.
  It needs at least two fused full layers above the bridge layers and at
  least 10 mm² of seat.
- `check_toolpaths.py --job <gcode> --parts <export>` audits the sliced shelf
  layers: feature type, length, speed and fan.

[validation.json](validation.json) records the digital checks. They are:

- the case-screw joint: fused layers and seat area;
- exact OCCT intersection and continuous 60 mm insertion sweeps against the
  official STEP;
- cable, card, reserve, insert-wall and vent checks;
- Lacuna mesh, layer-support and G-code preflight.

They do not establish printed fit, thread strength, cable seating or cooling.
The [assembly guide](assembly.pdf) gives the sequence and the physical tests.
This README does not release a print; that requires the reviewed Lacuna packet.
