#!/usr/bin/env python3
"""Draw the R4 engineering assembly packet from exact candidate STL sections."""

from __future__ import annotations

import argparse
import runpy
import subprocess
from pathlib import Path

import trimesh
from reportlab.lib.colors import black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    out = a.output
    render = runpy.run_path(str(HERE / "render.py"))["render"]
    render(out, "preview.png", False)
    render(out, "exploded.png", False, 25)
    render(out, "exploded-ink.png", False, 25, True)
    b = trimesh.load(out / "base.stl")
    lid_mesh = trimesh.load(out / "lid.stl")
    lid_mesh.apply_transform(
        [[1, 0, 0, 0], [0, -1, 0, 28.5], [0, 0, -1, 20], [0, 0, 0, 1]]
    )
    c = canvas.Canvas(str(out / "assembly.pdf"), pagesize=(612, 792), invariant=1)
    c.setTitle("Yoke R4 - provisional assembly layout - NOT FOR PRINT")

    def text(x, y, s, size=10):
        c.setFillColor(black)
        c.setFont("Helvetica", size)
        c.drawString(x, y, s)

    def lines(items, y, x=34, size=10, gap=15):
        for s in items:
            text(x, y, s, size)
            y -= gap

    def title(n, s):
        text(34, 755, f"YOKE ZERO 3E / R4 / {n} OF 5", 11)
        text(34, 729, s, 19)

    def footer():
        text(
            34,
            36,
            (
                "PROVISIONAL LAYOUT / catalogue profile / stock identity + fit "
                "pending / NOT FOR PRINT"
            ),
            9,
        )
        text(
            34,
            23,
            "Physical fit, strength, thermal tests, slicing and print release: NOT RUN",
            8,
        )

    def section(m, plane, at, bounds, origin, scale, reverse=False):
        axis = {"x": 0, "y": 1}[plane]
        normal = [0, 0, 0]
        normal[axis] = 1
        point = [0, 0, 0]
        point[axis] = at
        cut = m.section(plane_origin=point, plane_normal=normal)
        if cut is None:
            return
        axes = [1, 2] if axis == 0 else [0, 2]
        lo, hi, zlo, zhi = bounds
        c.saveState()
        clip = c.beginPath()
        clip.rect(*origin, (hi - lo) * scale, (zhi - zlo) * scale)
        c.clipPath(clip, stroke=0, fill=0)
        path = c.beginPath()
        for contour in cut.discrete:
            q = (contour[:, axes] - [lo, zlo]) * scale + origin
            path.moveTo(*q[0])
            for v in q[1:]:
                path.lineTo(*v)
            path.close()
        c.setFillColor(white)
        c.setStrokeColor(black)
        c.setLineWidth(0.65)
        c.drawPath(path, fill=1, stroke=1, fillMode=0)
        c.saveState()
        c.clipPath(path, stroke=0, fill=0, fillMode=0)
        c.setLineWidth(0.25)
        x, y = origin
        h = (zhi - zlo) * scale
        w = (hi - lo) * scale
        for k in range(-int(h) - 20, int(w) + int(h) + 20, 7):
            c.line(x + k, y, x + k + (-h if reverse else h), y + h)
        c.restoreState()
        c.restoreState()

    def arrow(x0, y0, x1, y1):
        c.setStrokeColor(black)
        c.setLineWidth(1)
        c.line(x0, y0, x1, y1)
        import math

        a = math.atan2(y1 - y0, x1 - x0)
        for d in [-0.5, 0.5]:
            c.line(x1, y1, x1 - 8 * math.cos(a + d), y1 - 8 * math.sin(a + d))

    title(1, "Inventory, orientation and board placement")
    lines(
        [
            "R4 B base + R4 L lid. Flat rectangle: 76.8 x 35.9 x 20.0 mm.",
            "Two M3 x 8 socket-head screws from the same FGRUH kit used by Lacuna.",
            (
                "Two RX M3 x 5.7 heat-set inserts: catalogue OD 4.6 / length 5.7 "
                "/ pilot 4.0 mm."
            ),
            (
                "Stock identity is unconfirmed. Use the maker profile only after "
                "checking the label."
            ),
            (
                "Nominal screw envelope: head <= OD 6 x height 3 mm; check actual"
                " hardware."
            ),
            (
                "Tools: caliper/depth gauge, M3 socket key, controlled insert "
                "heater, padded mat,"
            ),
            (
                "actual service Ethernet + both USB-C cables, 0-10 N gauge, "
                "timer/test host."
            ),
        ],
        698,
    )
    c.drawImage(
        ImageReader(str(out / "exploded-ink.png")),
        66,
        282,
        width=480,
        height=308,
        mask="auto",
    )
    text(46, 272, "BASE: two insert bosses beside RJ45; round + relieved PCB pegs", 9)
    text(
        46,
        597,
        "LID: three guide slots, two rear retaining tabs, two recessed heads",
        9,
    )
    lines(
        [
            (
                "1. Confirm actual RX stock identity, then review the small "
                "coupon job first."
            ),
            (
                "2. Heat-set inserts in the EMPTY base before any board or lid is"
                " installed (page 2)."
            ),
            (
                "3. Unpowered bare ZERO 3E V1.2: RJ45 faces -X / left, USB-C "
                "faces -Y / front."
            ),
            (
                "   Lower board vertically and parallel onto four pads. Align the"
                " round peg first,"
            ),
            (
                "   then the diagonal relieved peg. Never force a connector, pin,"
                " or component."
            ),
            (
                "4. Test lid closure without screws as on page 3; it must seat "
                "and slide by hand."
            ),
            "No screw passes through a PCB hole. Original PCB bores are OD 2.75 mm.",
            (
                "Both parts print exterior face down. Open bores; zero "
                "sacrificial membranes."
            ),
        ],
        250,
    )
    footer()
    c.showPage()
    title(2, "Insert installation and exact screw section")
    lines(
        [
            "A-A at Y = 1.6 mm: actual base/lid section; X horizontal, Z up.",
            (
                "Opposite hatching distinguishes printed parts; gray is nominal "
                "metal envelope."
            ),
        ],
        698,
    )
    bounds = (-9.4, -0.4, 0, 20.5)
    origin = (58, 350)
    scale = 13
    section(b, "y", 1.6, bounds, origin, scale)
    section(lid_mesh, "y", 1.6, bounds, origin, scale, True)

    def rect(x, z, w, h, shade=0.82):
        c.setFillGray(shade)
        c.setStrokeColor(black)
        c.rect(
            origin[0] + (x - bounds[0]) * scale,
            origin[1] + z * scale,
            w * scale,
            h * scale,
            fill=1,
            stroke=1,
        )

    rect(-7.3, 7.8, 4.6, 5.7)
    rect(-6.5, 7.8, 3, 5.7, 1)
    rect(-6.5, 8.5, 3, 8, 0.65)
    rect(-8, 16.5, 6, 3)
    lines(
        [
            "z 20.0   exterior top",
            "z 19.5   nominal head top: recess 0.50",
            "z 16.5   head bearing land",
            "z 13.5   boss hard stop / insert top",
            "z 8.5     8 mm screw tip",
            "z 7.8     provisional insert bottom",
            "z 6.8     blind pilot bottom",
            "Nominal grip 3.0; axial overlap 5.0 mm.",
            "Tip-to-insert-bottom reserve 0.70 mm.",
            "Tip-to-plastic-bottom reserve 1.70 mm.",
            "OD 8.4 boss: 1.9 mm around OD 4.6 insert.",
            "OD 6.4 head well: 1.0 mm radial wall.",
            "Axes: (-5, 1.6) and (-5, 28) mm.",
        ],
        626,
        x=220,
        size=10,
        gap=19,
    )
    lines(
        [
            (
                "HEAT-SET ACTION (before board): support base flat, hold heater "
                "normal to boss."
            ),
            (
                "Set each insert flush at Z 13.5, keep its bore coaxial, let it "
                "cool fully before loading."
            ),
            (
                "Tool envelope: OD 2.4 pilot x 1, OD 4.2 shoulder x 2, then OD 5 "
                "body. Heater"
            ),
            ("must fit with pilot 1 mm engaged; use the maker heat process."),
            (
                "Do not install into a guessed pilot, press cold, or use the PCB "
                "as a support."
            ),
            (
                "SCREW ACTION (after free slot closure): insert two M3 x 8 "
                "downward from top."
            ),
            (
                "OD 3 driver shaft reaches each head vertically through the OD "
                "6.4 open well."
            ),
            (
                "Start both threads by hand; lightly snug alternately. No "
                "qualified torque exists."
            ),
            (
                "Load path: head -> lid boss -> base boss hard stop -> embedded "
                "metal insert."
            ),
            (
                "Screw preload bypasses the PCB; upper pads leave nominal 0.18 mm"
                " capture play."
            ),
            (
                "Measured acceptance: nominal axial overlap >= 4.5 mm; tip "
                "reserve >= 0.4 mm; head"
            ),
            (
                "recess >= 0.2 mm. Do not substitute a longer screw or tighten "
                "away interference."
            ),
        ],
        316,
        gap=16,
    )
    footer()
    c.showPage()
    title(3, "Slot together, then screw together")
    lines(
        [
            (
                "5. Hold lid level 1.2 mm LEFT (-X) of the base. Keep all cables "
                "disconnected."
            ),
            (
                "   Lower vertically: align three chamfered tongues with their "
                "elongated slots."
            ),
            (
                "6. Slide lid RIGHT (+X) 1.2 mm until all outside faces align. Do"
                " not force it."
            ),
            (
                "   Both rear tabs now sit under the receiver roofs; only then "
                "fit the screws."
            ),
        ],
        698,
    )
    # One action per sketch: insertion then sliding.
    for x, label, dx, dz in [
        (75, "5  LOWER AT X OFFSET -1.2", -1.2, 16),
        (340, "6  SLIDE +1.2 TO FLUSH", 0, 0),
    ]:
        c.setLineWidth(0.9)
        c.rect(x, 525, 160, 40)
        c.rect(x - 10 if dx else x, 575 if dz else 567, 160, 12)
        if dz:
            arrow(x + 80, 619, x + 80, 590)
        else:
            arrow(x - 35, 590, x + 15, 590)
        text(x - 15, 505, label, 9)
    text(
        34,
        476,
        "B-B rear tab at Y = 3.0 mm (X / Z); exact STL contours, 24x scale.",
        10,
    )
    bounds = (63.4, 67.8, 13, 20.2)
    origin = (58, 278)
    scale = 24
    section(b, "y", 3, bounds, origin, scale)
    section(lid_mesh, "y", 3, bounds, origin, scale, True)
    lines(
        [
            "Tab overlap under roof: 0.75 mm.",
            "Tab root / tip: 1.0 / 0.5 mm.",
            "Pocket roof thickness: 1.5 mm.",
            "Remaining rear wall: 1.2 mm.",
            "Matching roof chamfer: 0.30 vertical clearance.",
            "Pocket width 4.4; tab 3.9; side gap 0.25.",
            "Rear slots prevent straight vertical lift.",
            "Screws prevent the reverse slide.",
            "Physical retention strength: NOT RUN.",
        ],
        449,
        x=220,
        gap=19,
    )
    text(
        34,
        246,
        "C-C guide at X = 16 mm (Y / Z): tongue 1.0 wide x 1.2 high, 0.3 lead-in.",
        10,
    )
    bounds = (28.8, 32.4, 13, 16)
    origin = (70, 141)
    scale = 28
    section(b, "x", 16, bounds, origin, scale)
    section(lid_mesh, "x", 16, bounds, origin, scale, True)
    lines(
        [
            "Slot width 1.5; tongue side clearance 0.25.",
            "Guide slot has 0.85 mm material each side.",
            "Extra X length permits the 1.2 mm slide.",
            "No snap flexure or glue is required.",
            "Front-right board pad is relieved to clear",
            "the USB-C shell during that slide.",
        ],
        222,
        x=220,
        gap=16,
    )
    lines(
        [
            (
                "The three guides carry XY location. Rear tabs carry opening load"
                " at the rear."
            ),
            "Front screws clamp the hard stops; no external ear or turret exists.",
        ],
        105,
    )
    footer()
    c.showPage()
    title(4, "Fit tests, service and first-article decisions")
    lines(
        [
            (
                "STATUS: every physical observation below is NOT RUN. Digital fit"
                " is provisional."
            ),
            (
                "Before release: confirm actual insert label/OD/length; measure "
                "both screws"
            ),
            (
                "(length, head OD/height), actual board thickness and actual "
                "heater/driver envelope."
            ),
            (
                "Regenerate exact parts/plate/PDF, review every page and joint "
                "independently, then"
            ),
            (
                "perform the normal Lacuna exact-job release. No R1, R2 or R3 "
                "approval transfers."
            ),
        ],
        698,
    )
    text(34, 598, "Unpowered fit and retention", 13)
    lines(
        [
            (
                "A. Measure empty parts. Require clear bores, intact flat seats "
                "and no cracks/sag."
            ),
            (
                "   Fit inserts only after pilot confirmation. Reject any boss "
                "whitening or looseness."
            ),
            (
                "B. Close empty case WITHOUT screws: lower at -1.2 X, slide +1.2 "
                "X by hand."
            ),
            (
                "   Require flush exterior <= 0.2 mm mismatch, no forced bend, "
                "all three guides"
            ),
            (
                "   seated and rear hooks preventing a straight lift. Reopen by "
                "reversing the slide."
            ),
            (
                "C. Fit the board without cables or screws. It must sit flat on "
                "all four pads."
            ),
            (
                "   Require no contact with components, no bowed PCB, and free "
                "repeat closure."
            ),
            "D. Check measured stack: >= 4.5 mm axial overlap, >= 0.4 mm tip reserve,",
            (
                "   >= 0.2 mm head recess. These limits include print error; "
                "never tighten to fix fit."
            ),
            (
                "E. Start both M3 x 8 screws and lightly snug. Rotate over a "
                "padded mat through"
            ),
            ("   six orientations for 10 s: no release; board play <=0.3 mm."),
            (
                "F. Use actual Ethernet and both USB-C cables. Require full "
                "engagement, operable"
            ),
            (
                "   latches and straight removal without touching plastic. Hold "
                "case, not PCB/ports."
            ),
            (
                "G. With board unpowered and cables removed, apply 5 N separating"
                " load between"
            ),
            (
                "   halves for 10 s each at front/rear, over padded mat. Require "
                "no release, crack"
            ),
            (
                "   or permanent gap > 0.2 mm. Repeat assembly/service 10 cycles "
                "and reinspect."
            ),
        ],
        572,
        gap=16,
    )
    text(34, 294, "Powered observation and reverse service", 13)
    lines(
        [
            (
                "H. After unpowered checks pass, run intended workload for 30 min"
                " at measured"
            ),
            (
                "   ambient <= 30 C. Record CPU temperature once/minute, "
                "throttling and resets."
            ),
            (
                "   Require no reset, thermal throttling, plastic distortion or "
                "CPU reading >= 75 C."
            ),
            "   This is a trial limit, not a rated thermal envelope.",
            (
                "SERVICE: unplug all power/cables; support base on mat. Remove "
                "both screws."
            ),
            (
                "Slide lid LEFT 1.2 mm before lifting. Lift level; lift PCB "
                "vertically off pegs by edges."
            ),
            (
                "Inserts stay in base. If an insert turns, cracks or withdraws, "
                "stop and revise boss."
            ),
            (
                "STOP for forced fit, bowed PCB, damaged connector, stalled "
                "screw, bottoming,"
            ),
            (
                "insert motion, crack, heat, smoke, smell, or any failed limit. "
                "Record dimensions"
            ),
            (
                "and the failed interface; return to design. Physical acceptance "
                "needs observations."
            ),
            (
                "Limits: bare board only; no PoE HAT/heatsink; no ingress or "
                "torque rating."
            ),
            (
                "Board surface-edge and sampled checks can miss collisions "
                "between samples."
            ),
        ],
        269,
        gap=17,
    )
    footer()
    c.showPage()
    title(5, "Small coupon trials before a full housing")
    lines(
        [
            (
                "Print preparation remains BLOCKED until stock identity and job "
                "review are complete."
            ),
            (
                "Five native parts: front-base + front-lid; rear-base + rear-lid; "
                "rear-jig fixture."
            ),
            (
                "They crop the actual housing, preserving its screw lands, guide and "
                "hook profiles."
            ),
            (
                "Use ONE M3 x 8 and ONE confirmed RX insert for the front trial, not "
                "new stock claims."
            ),
            "The rear jig is a test fixture, not part of the final enclosure.",
        ],
        698,
    )
    text(34, 596, "Front coupon: heat-set, slot, screw, service", 13)
    lines(
        [
            (
                "1. Verify actual insert matches RX-M3x5.7 catalogue; measure "
                "OD/length and screw."
            ),
            (
                "2. Heat-set in front-base only: use maker pilot 4.0, length 5.7 and "
                "min wall 1.6."
            ),
            "   Boss wall 1.9; pilot depth 6.7 is our length+1.0 design allowance.",
            (
                "3. With no screw, lower front-lid at -1.2 X; slide +1.2 X to aligned"
                " outside edges."
            ),
            (
                "   Require easy hand seating and real guide restraint, no whitening "
                "or forced flex."
            ),
            "4. Measure stack: nominal grip 3.0 / axial overlap 5.0 mm;",
            "   Tip reserves: insert 0.70 / plastic 1.70; head recess 0.50 mm.",
            (
                "   Smooth meshes do not establish usable threads: count and record "
                "actual turns."
            ),
            "5. Seat M3 x 8 by hand; reject bottoming or a turning insert.",
            (
                "   Confirm driver reaches the head, head remains recessed, and the "
                "screw blocks"
            ),
            "   reverse X slide. KEEP SCREW FITTED for the load trial below.",
            "   Hold base floor; separate halves to 5 N for 10 s, three times.",
            "   Require no release, insert motion, crack or residual gap >0.2 mm.",
            "   Remove screw, reverse slide, lift. Cycle 10 times; reinspect.",
        ],
        570,
        gap=14,
    )
    text(34, 362, "Rear coupon: hard X stops before opening load", 13)
    lines(
        [
            (
                "6. Close rear-base + rear-lid with the same lower/slide sequence "
                "OUTSIDE the jig."
            ),
            (
                "7. Lower the closed pair into rear-jig; base rests on its 2 mm "
                "floor. Hard faces"
            ),
            (
                "   at X 55.5 / 67.7 leave 0.1 mm at each coupon end, limiting reverse "
                "travel to 0.2."
            ),
            (
                "   Clamp the JIG FLOOR to a bench. Use padded jaws/gauge on the lid "
                "roof edge."
            ),
            ("8. Require free vertical pair motion <0.5 N. Grip lid edge at Y 9.1."),
            "   Pull +Z to 5 N for 10 s, three times; hard X stops carry the reaction.",
            "   Require stop deflection <=0.1 mm and relative X travel <=0.3 mm.",
            "   Greater travel invalidates fixture: stop and repair before retrying.",
            "   Require no joint release, crack, whitening or permanent distortion.",
            (
                "   Record loaded gap and residual gap; require residual gap <=0.2 mm"
                " after load."
            ),
            (
                "9. Remove pair vertically from jig before reversing slide. Cycle 10 "
                "times and inspect."
            ),
            (
                "The matched sloping faces carry rear opening load; front screws "
                "restrain X in the"
            ),
            (
                "full case. Rear-coupon PASS cannot prove full-board, cable, strength"
                " or thermal fit."
            ),
            (
                "STOP on any failed limit. Revise the failing interface before a "
                "full-case trial."
            ),
            (
                "All actual coupon observations, insertion force, screw turns and "
                "loads: NOT RUN."
            ),
        ],
        350,
        gap=16,
    )
    footer()
    c.save()
    subprocess.run(
        [
            "pdftoppm",
            "-gray",
            "-r",
            "100",
            "-png",
            str(out / "assembly.pdf"),
            str(out / "page"),
        ],
        check=True,
    )
    # Dedicated section preview comes directly from page 2, retaining its status legend.
    from PIL import Image

    Image.open(out / "page-2.png").save(out / "section.png")


if __name__ == "__main__":
    main()
