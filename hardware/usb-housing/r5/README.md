# R5 straight-nesting ZERO 3E housing

R5 is a full two-part first article after the failed R4 joint samples. The
lid lowers vertically into an open rim and fastens with **four M3 × 8
socket-head screws and four RX-M3×5.7 heat-set inserts** from the same owner
stock profile. There are no hooks, sideways slides, separate fixtures or
projecting fastener towers. Physical fit, strength and thermal
performance remain **NOT RUN** until observations are recorded.

The [first print record](print.json) records the exact two-part job.
[Digital validation](validation.json) records its checked geometry and limits.

Use the [assembly manual](assembly.pdf) for the illustrated installation and
first-article checks. The [assembled view](preview.png),
[exploded view](exploded.png) and [joint sections](section.png) render the
actual exported parts.

The outside is one **84.0 × 35.9 × 20.0 mm rectangle**. The official loaded
board spans x −6.4…65.00165, y −1.5…30.00553 mm. The case spans
x −9.2…74.8, y −3.7…32.2 mm. Four internal fastener bays at
(−5, 1.6), (−5, 28), (70.4, 1.6), (70.4, 28) require the extra rear length:
their nearest edge is x 66.2, about 1.20 mm beyond the loaded board. No M3
screw enters the PCB's Ø2.75 mm mounting holes. No HAT or extra heatsink is
included in this envelope.

The open nesting rim has a **1.2 mm thick, 2.0 mm deep** lid skirt, with
**0.4 mm per-side clearance** and a **0.5 mm entry chamfer**. Connector
windows and screw bays interrupt that skirt where needed. Its mating faces
print above the bed on both parts, away from first-layer expansion. The
outside base and lid edges meet at z 13.5 mm; the skirt is a locating feature,
not a snap latch. Engraved **R5 B / R5 L** and **LAN arrows** permanently
identify each part and the Ethernet end.
Both LAN arrows point toward Ethernet when the parts are assembled.

| Interface | Nominal geometry |
| --- | --- |
| Boss / insert outside diameter | 8.4 / 4.6 mm |
| Insert pilot / nominal insert length | 4.0 / 5.7 mm |
| Insert top / insert bottom / blind pilot bottom | z 13.5 / 7.8 / 6.8 mm |
| Head well / rectangular shank slot | Ø6.8 / 4.0 × 6.8 mm |
| Head bearing plane / nominal head top | z 16.5 / 19.5 mm |
| M3 × 8 tip / nominal axial metal overlap | z 8.5 / 5.0 mm |
| Insert-end / pilot-bottom tip reserve | 0.70 / 1.70 mm |
| Nominal head recess | 0.50 mm |
| PCB support / lid capture | z 6.88 / 9.00 mm |
| Nominal PCB top / capture play | z 8.52 / 0.48 mm |

Two flat bridge shelves carry each head. The screw load runs through the
lid and base boss stops into the inserts, bypassing the PCB. Smooth hardware
meshes establish nominal overlap, not usable thread engagement, torque,
pullout strength or measured stock tolerances. [hardware.json](hardware.json)
records the owner-selected kit and manufacturer's nominal insert profile;
actual hardware dimensions and heat setting still require observation.

Eight **3 × 2.4 mm** low inlets on each long side provide **115.2 mm²**
of dedicated inlet area, beginning 2.8 mm above the bottom. Seven
**3 × 13 mm** top outlets provide **273 mm²**. The solid floor can rest on
a flat surface without blocking these vents. These are geometric open
areas, excluding connector windows; they are not measured airflow or cooling
performance. The rear **17.6 × 13.3 mm** access opening reaches the recessed
microSD end; actual finger/card access must be checked on the first article.
If a fingertip cannot operate it through the opening, disconnect power,
remove the lid and lift the board to service the card.

Set all four inserts in the empty base, flush with the boss faces, and let
them cool. Lower the bare board vertically onto its four support pads and
two relieved locating pegs. Place the lid with both LAN marks pointing to
the Ethernet end and lower it straight down. It must seat freely before
fitting screws. Start all four screws by hand, then lightly snug them in
opposite pairs. No qualified tightening torque is specified. For service,
disconnect cables, remove all four screws and lift the lid straight up.
Do not use screws to force a gap closed or bend the PCB.

Both pieces print exterior-face down with a stock 0.4 mm nozzle and
0.2 mm layers; no supports or removable bore skins are designed in. The
6.8 mm bridge allowance covers the two head-bearing shelves. Exact sliced
toolpaths must still be reviewed before dispatch. The full-case first
article is the physical test; there is no new coupon prerequisite.

```sh
python3 hardware/usb-housing/r5/build.py \
  --output .scratch/r5-design/export
```

[housing.scad](housing.scad) is the native dimensional authority.
[build.py](build.py) exports six part/hardware/tool meshes, native echoed
`parameters.json`, source identities, an exact facet-concatenated two-part
plate and its Lacuna contact declaration. The lid assembly matrix is
diag(1, −1, −1) with translation (0, 28.5, 20); the official board uses
+8.5 mm Z. The reference mesh and its pinned source/tessellation are
documented in [R4 reference instructions](../r4/README.md#rebuild-and-check).
The exact reviewed board STL SHA-256 is
`1c1268c8af4b1a6c0b07fec07b3b28c6662e5d10c0c1b794c1f1083f31af8f27`.
Digital inspection does not substitute for first-article fit, cable access,
usable threads, repeated service, strength or thermal observations.
