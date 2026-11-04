# R6 PoE housing first article

**Superseded 2026-11-04: the owner judged the printed R6 too large. Do not
print R6 again.** See [R7](../r7/README.md) and the
[current housing status](../../../doc/design/USB-HOUSING.md).

R6 is designed for the ordered Radxa ZERO 3E 2GB with GPIO
([B0CZD1C54N](https://www.amazon.com/dp/B0CZD1C54N)) and Radxa PoE HAT
([B0D1KF1L7G](https://www.amazon.com/dp/B0D1KF1L7G)). It has two nesting shell
halves and two internal screw-retained PCB edge clips. Physical fit and
powered cooling are **NOT RUN** until the electronics arrive.

The native envelope is **89 × 45.5 × 46 mm**. Ethernet opens at one end;
the opposite end is closed. A single **26.2 × 8.2 mm** lower-side opening
serves the two USB-C ports. HDMI is covered. Remove the lid and internal
retainers to service microSD; no second large end aperture is needed.

Retention uses the lower PCB only. Four pads support component-free edge
regions; two clips limit lift with 0.4 mm nominal play. No printed peg enters
the conductive corner mounting holes, and no enclosure fastener substitutes
for a Radxa screw. Install the lower Radxa screws and insulated pillars before
placing the SBC in the base; install the edge clips before fitting the HAT.
The HAT and its four upper screws remain accessible with the lid removed.

Four **M3 × 16** socket-head screws enter recessed wells in the base and
engage **RX-M3×5.7** inserts in the lid. Two **M3 × 8** screws and two more
inserts secure the internal clips. Install inserts in empty plastic parts
and allow them to cool before fitting electronics. The enclosure screw heads
recess 0.6 mm into the bottom face. Case screws clamp hard plastic stops,
without loading either PCB. Actual stock, printed pilots and thread fit must
be inspected during first assembly.

The larger official board/HAT assembly is the dimensional reference. Its
topmost component has **3.5 mm roof clearance**. The earlier 14.5/18 mm
spacing discrepancy remains a source uncertainty, but neither the shell joint
nor board retention depends on HAT height. Corner hardware receives deliberately
generous diameter-8 clearance envelopes. Those envelopes and roof headroom are
design allowances, not measurements of undelivered parts. Optional accessories
must remain within the stated space; fit is checked before applying power.

Each long side has inter-board and upper vent rows; the roof is perforated.
Separate low side inlets remain open on a flat desk. Vents cut through the
inner locating skirt as well as the outer wall. The design provides passive
air paths around both boards and space for the fan present in the official
model, without assuming that the ordered HAT includes one. CAD opening area
is not a temperature or airflow measurement.

The rim has 0.5 mm per-side clearance, a 1.2 mm skirt, 2 mm engagement and
a 0.5 mm lead-in chamfer. Both shell mating edges print above the bed, away
from first-layer expansion. First close the empty halves by hand. They must
seat freely before screws are fitted; screws must never force a poor fit.

Rebuild with a Manifold-enabled OpenSCAD:

```sh
python3 hardware/usb-housing/r6/build.py --output .scratch/r6-design/export
```

The exact exports, component reference, nominal hardware and assembly paths
must be reviewed together before dispatch. This source README does not itself
release a print. The first article tests unloaded shell mating, insert/screw
fit, complete electronics installation, cable access, repeated service and
powered cooling; all physical results remain NOT RUN.
