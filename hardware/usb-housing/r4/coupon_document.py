#!/usr/bin/env python3
"""Draw the R4 coupon-only first-fit guide from the exact native meshes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import runpy
import shutil
import subprocess
from pathlib import Path

import numpy as np
import trimesh
from reportlab.lib.colors import black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LID = np.array(
    [[1, 0, 0, 0], [0, -1, 0, 28.5], [0, 0, -1, 20], [0, 0, 0, 1]],
    dtype=float,
)
PLACEMENTS = [
    ("FB", "front-base", (15, 8, 0)),
    ("FL", "front-lid", (15, -2, 0)),
    ("RB", "rear-base", (-10, 8, 0)),
    ("RL", "rear-lid", (-10, 4, 0)),
    ("J", "rear-jig", (15, 22, 0)),
]
INPUTS = [row[1] for row in PLACEMENTS] + ["screw", "insert", "driver", "heat-tool"]

TOOL_INPUTS = [
    "front-clamp-jaw",
    "rear-base-clamp-jaw",
    "front-hook-envelope",
    "rear-hook-envelope",
]


def box(bounds):
    low, high = np.asarray(bounds, dtype=float)
    mesh = trimesh.creation.box(extents=high - low)
    mesh.apply_translation((low + high) / 2)
    return mesh


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def translated(mesh: trimesh.Trimesh, delta: tuple) -> trimesh.Trimesh:
    result = mesh.copy()
    result.apply_translation(delta)
    return result


class Packet:
    """Letter-size monochrome drawing helpers; all coordinates are points."""

    def __init__(self, output: Path):
        self.pdf = canvas.Canvas(str(output), pagesize=(612, 792), invariant=1)
        self.pdf.setTitle("Yoke R4 - five-part coupon assembly and first-fit trial")

    def text(self, x, y, value, size=10):
        self.pdf.setFillColor(black)
        self.pdf.setFont("Helvetica", size)
        self.pdf.drawString(x, y, value)

    def lines(self, rows, y, x=34, gap=16, size=10):
        for value in rows:
            self.text(x, y, value, size)
            y -= gap

    def title(self, number, value):
        self.text(34, 755, f"YOKE / R4 COUPON TRIAL / {number} OF 4", 11)
        self.text(34, 729, value, 19)

    def footer(self):
        self.text(34, 36, "COUPON TRIAL ONLY / actual stock and fit unconfirmed", 9)
        self.text(34, 23, "All physical measurements and tests: NOT RUN", 8)

    def page(self):
        self.footer()
        self.pdf.showPage()

    def arrow(self, x0, y0, x1, y1):
        self.pdf.setStrokeColor(black)
        self.pdf.setLineWidth(0.8)
        self.pdf.line(x0, y0, x1, y1)
        angle = math.atan2(y1 - y0, x1 - x0)
        for offset in [-0.5, 0.5]:
            self.pdf.line(
                x1,
                y1,
                x1 - 7 * math.cos(angle + offset),
                y1 - 7 * math.sin(angle + offset),
            )

    def section(self, mesh, axis, at, bounds, origin, scale, *, hatch=1, gray=None):
        normal = [0, 0, 0]
        normal[axis] = 1
        point = [0, 0, 0]
        point[axis] = at
        cut = mesh.section(plane_origin=point, plane_normal=normal)
        if cut is None:
            return
        axes = [1, 2] if axis == 0 else [0, 2]
        low, high, zlow, zhigh = bounds
        pdf = self.pdf
        pdf.saveState()
        clip = pdf.beginPath()
        clip.rect(*origin, (high - low) * scale, (zhigh - zlow) * scale)
        pdf.clipPath(clip, stroke=0, fill=0)
        path = pdf.beginPath()
        for contour in cut.discrete:
            points = (contour[:, axes] - [low, zlow]) * scale + origin
            path.moveTo(*points[0])
            for vertex in points[1:]:
                path.lineTo(*vertex)
            path.close()
        pdf.setFillColor(white)
        if gray is not None:
            pdf.setFillGray(gray)
        pdf.setStrokeColor(black)
        pdf.setLineWidth(0.65)
        pdf.drawPath(path, fill=1, stroke=1, fillMode=0)
        if hatch:
            pdf.saveState()
            pdf.clipPath(path, stroke=0, fill=0, fillMode=0)
            pdf.setLineWidth(0.25)
            x, y = origin
            width = (high - low) * scale
            height = (zhigh - zlow) * scale
            for k in range(-int(height) - 20, int(width + height) + 20, 8):
                pdf.line(x + k, y, x + k + hatch * height, y + height)
            pdf.restoreState()
        pdf.restoreState()


def inventory(packet: Packet, meshes: dict):
    packet.title(1, "Identify five parts and the trial hardware")
    packet.lines(
        [
            "This packet covers ONE five-piece coupon plate and ONE metal joint.",
            "FB front-base; FL front-lid; RB rear-base; RL rear-lid; J rear-jig.",
            "One M3 x 8 socket-head screw and one nominal RX-M3x5.7 insert.",
            "Printing tests the catalogue profile; actual insert stock is unconfirmed.",
            ("After printing, check label/OD/length BEFORE heat-setting (page 2)."),
        ],
        695,
    )
    packet.text(34, 598, "Exact bed sections at0.5 mm; +Y up / +X right", 11)
    packet.text(
        34,
        580,
        "Dimensions in mm; mark part codes before removing the plate layout.",
        8,
    )
    placed = [translated(meshes[name], delta) for _, name, delta in PLACEMENTS]
    low = np.min([mesh.bounds[0, :2] for mesh in placed], axis=0)
    high = np.max([mesh.bounds[1, :2] for mesh in placed], axis=0)
    origin = np.array([44, 347])
    scale = min(520 / (high[0] - low[0]), 220 / (high[1] - low[1]))
    for code, name, delta in PLACEMENTS:
        mesh = translated(meshes[name], delta)
        cut = mesh.section(plane_origin=[0, 0, 0.5], plane_normal=[0, 0, 1])
        packet.pdf.setStrokeColor(black)
        packet.pdf.setLineWidth(0.65)
        for contour in cut.discrete:
            points = (contour[:, :2] - low) * scale + origin
            path = packet.pdf.beginPath()
            path.moveTo(*points[0])
            for vertex in points[1:]:
                path.lineTo(*vertex)
            path.close()
            packet.pdf.drawPath(path)
        center = (mesh.bounds[0, :2] + mesh.bounds[1, :2]) / 2
        screen = (center - low) * scale + origin
        packet.pdf.setFillColor(white)
        packet.pdf.rect(screen[0] - 12, screen[1] - 7, 24, 17, fill=1)
        packet.text(screen[0] - (3 if code == "J" else 7), screen[1] - 2, code, 11)
    packet.lines(
        [
            "Keep this orientation while removing parts. Mark FB / FL / RB / RL / J",
            (
                "on noncontact outside faces with a fine marker; keep lands and"
                " slots clean."
            ),
            "FB/FL: screw corner and guide. RB/RL: hook and pocket. J: tall stop wall.",
            "Base floors and jig floor print down; lid exterior faces print down.",
        ],
        320,
    )
    packet.text(34, 236, "Tools and handling", 12)
    packet.lines(
        [
            "Caliper/depth gauge (tip <=1 mm), controlled M3 insert heater, socket key",
            "matching the real screw (shaft <=3 mm), ONE padded bench clamp reused",
            "front to rear: upper pad <=8 wide x5 deep x5 high; opening >= bench T+4.",
            "Use a 0-10 N gauge/grip (pages3-4), timer, bench and mat.",
            "Keep the bench under the complete base/jig floor. Actual tools must clear",
            "every part. Mark only noncontact outer faces; keep lands and slots clean.",
            "Cool parts, remove loose strings/swarf; reject cracks or sagged lands.",
            "Record caliper resolution for the 0.1 mm stop and 0.2 mm gap readings.",
            "Do not drill out a trial fit or force a part to hide interference.",
        ],
        211,
    )
    packet.page()


def front_insert(packet: Packet, meshes: dict, scratch: Path):
    packet.title(2, "Check stock, set one insert, then slot together")
    packet.lines(
        [
            "1. Measure actual hardware before applying heat. Match RX-M3x5.7 label:",
            (
                "   catalogue OD4.6 x length5.7, lead-in3.9, pilot4.0 mm; brass"
                " RX profile."
            ),
            "   If stock differs, STOP heat-setting and record the measurements.",
        ],
        695,
    )
    packet.text(34, 630, "A-A: FB + insert + engaged heater; Y1.6 / X-Z", 10)
    base = meshes["front-base"]
    insert = translated(meshes["insert"], (-5, 1.6, 7.8))
    heater = translated(meshes["heat-tool"], (-5, 1.6, 12.5))
    bounds, origin, scale = (-9.5, -0.5, 6.5, 26), (49, 365), 11
    packet.section(base, 1, 1.6, bounds, origin, scale)
    packet.section(insert, 1, 1.6, bounds, origin, scale, hatch=0, gray=0.82)
    packet.section(heater, 1, 1.6, bounds, origin, scale, hatch=0, gray=0.65)
    packet.arrow(169, 582, 169, 474)
    packet.text(158, 595, "-Z set", 8)
    packet.lines(
        [
            "2. Support FB floor flat, with its pilot upright.",
            "   Use maker heat-setting instructions for the",
            "   actual plastic/insert. No temperature is assumed.",
            "   Seat ONE insert coaxially and flush with FB's",
            "   Z13.5 boss face. Let it cool fully before loading.",
            "Tool envelope: OD2.4 pilot x1; OD4.2 shoulder",
            "x2 above it; OD5 body. Pilot is shown 1 mm",
            "engaged. Actual heater must fit that envelope.",
            "Nominal pilot depth6.7 is our length+1.0 allowance.",
            "Boss wall1.9 exceeds catalogue minimum1.6 mm.",
            "Require no crack, whitening, tilt or loose insert.",
            "Hatched: plastic. Gray: nominal metal/tool.",
        ],
        605,
        x=205,
        gap=17,
    )
    packet.lines(
        [
            "3. With NO screw fitted, hold FL level at X=-1.2 mm from aligned faces.",
            "   Lower over FB's guide; slide RIGHT +1.2 mm until outside faces align.",
            (
                "   Require easy hand seating and real guide restraint, without"
                " forced flex."
            ),
        ],
        334,
    )
    packet.text(34, 270, "B-B: actual front guide at X1.5; Y-Z", 10)
    lid = meshes["front-lid"].copy()
    lid.apply_transform(LID)
    guide_bounds = (-3.9, -0.3, 13, 16.2)
    packet.section(base, 0, 1.5, guide_bounds, (65, 137), 29)
    packet.section(lid, 0, 1.5, guide_bounds, (65, 137), 29, hatch=-1)
    packet.text(38, 112, "1.0 tongue /1.5 slot /0.85 walls (mm)", 9)
    packet.pdf.drawImage(
        ImageReader(str(scratch / "front-exploded.png")),
        295,
        103,
        width=270,
        height=174,
        mask="auto",
    )
    packet.text(317, 92, "FL above / FB below; exact cropped meshes", 9)
    packet.page()


def front_screw(packet: Packet, meshes: dict, tools: dict):
    packet.title(3, "Clamp the front floor; keep the screw fitted")
    packet.text(34, 695, "C-C at Y1.6: actual screw/driver and clamp ledge in X-Z", 10)
    packet.text(
        34, 677, "Front grip at Y0 is projected; gray shapes are tool envelopes.", 9
    )
    base = meshes["front-base"]
    lid = meshes["front-lid"].copy()
    lid.apply_transform(LID)
    screw = meshes["screw"].copy()
    screw.apply_transform(
        [[1, 0, 0, -5], [0, -1, 0, 1.6], [0, 0, -1, 16.5], [0, 0, 0, 1]]
    )
    insert = translated(meshes["insert"], (-5, 1.6, 7.8))
    driver = translated(meshes["driver"], (-5, 1.6, 19.5))
    bounds, origin, scale = (-9.5, 21, -3, 25), (43, 388), 7
    bench = box([[-9.2, -5, -3], [20, 10, 0]])
    packet.section(bench, 1, 1.6, bounds, origin, scale, hatch=-1)
    packet.section(base, 1, 1.6, bounds, origin, scale)
    packet.section(lid, 1, 1.6, bounds, origin, scale, hatch=-1)
    for metal, gray in [(insert, 0.82), (screw, 0.65), (driver, 0.9)]:
        packet.section(metal, 1, 1.6, bounds, origin, scale, hatch=0, gray=gray)
    packet.section(
        tools["front-clamp-jaw"], 1, 1.6, bounds, origin, scale, hatch=0, gray=0.82
    )
    packet.section(
        tools["front-hook-envelope"], 1, 0, bounds, origin, scale, hatch=0, gray=0.65
    )
    packet.arrow(165, 585, 165, 622)
    packet.text(171, 610, "grip +Z", 8)
    packet.text(44, 375, "Bench fully supports FB; clamp body outside X20.", 8)
    packet.lines(
        [
            "4. Measure before tightening; nominal mm:",
            "head land16.5 / insert top13.5 => G=3.0",
            "screw L=8 => axial overlap L-G=5.0",
            "insert-end reserve5.7-(L-G)=0.70",
            "plastic reserve6.7-(L-G)=1.70",
            "well3.5 deep / head<=3 => recess0.50.",
            "Require measured overlap>=4.5,",
            "insert-end reserve>=0.4 and recess>=0.2.",
            "Smooth envelopes do not prove usable",
            "threads: record thread-start/seat turns.",
            "Head -> FL boss -> FB stop -> insert.",
            "5. Fit ONE M3 x8 only after free seating.",
            "Start by hand and lightly seat. Reject",
            "bottoming, binding or insert rotation.",
        ],
        650,
        x=280,
        gap=16,
    )
    packet.lines(
        [
            "6. KEEP SCREW FITTED. Put FB's complete floor on the bench. Clamp its",
            "   exposed ledge, X10..20/Y-0.5..7.5, from +X; upper jaw height<=5 mm.",
            "   The reused small pad fits this maximum 10-deep x8-wide envelope.",
            "   Open beyond bench T+4; enter with jaw +0.2 Z, then lower 0.2 to seat.",
            "7. Grip FL at X8/Y0. Approach from +X, 0.2 mm below its roof, then raise",
            (
                "   0.2 to contact. Hook intrusion under roof<=1.5; below-roof "
                "reach<=5 mm."
            ),
            "   Pull +Z to 5 N for 10 s, three trials. Do not pull on FB or the clamp.",
            (
                "   Require no release, crack, whitening, insert motion or "
                "residual gap>0.2."
            ),
            "8. Remove screw, slide FL LEFT -1.2, then lift. Repeat assembly/service",
            "   ten times. Record actual turns, dimensions, loads, gaps and damage.",
            "   No torque is qualified. Stop for forced fit or any failed limit.",
        ],
        342,
        gap=18,
    )
    packet.page()


def rear_trial(packet: Packet, meshes: dict, tools: dict):
    packet.title(4, "Hold RB down; leave RL free for the pull")
    packet.lines(
        [
            "9. OUTSIDE J, lower RL at -1.2 X relative to RB; slide +1.2 to align.",
            "   Lower the CLOSED pair into J; RB rests on J's 2 mm floor.",
        ],
        695,
    )
    rear_base = translated(meshes["rear-base"], (0, 0, 2))
    rear_lid = meshes["rear-lid"].copy()
    rear_lid.apply_transform(LID)
    rear_lid.apply_translation([0, 0, 2])
    jig = meshes["rear-jig"]
    packet.text(34, 643, "D-D at Y3: exact RB / RL / J; X-Z", 10)
    bounds, origin, scale = (50, 71, 0, 25), (43, 431), 7.5
    packet.section(jig, 1, 3, bounds, origin, scale)
    packet.section(rear_base, 1, 3, bounds, origin, scale, hatch=-1)
    packet.section(rear_lid, 1, 3, bounds, origin, scale)
    packet.arrow(211, 573, 211, 614)
    packet.text(208, 622, "+Z", 9)
    packet.text(43, 417, "J floor / RB middle / RL upper. Stop wall5 mm.", 8)
    packet.lines(
        [
            "Hard X faces55.5 /67.7; nominal end",
            "gaps0.1 each (0.2 reverse travel).",
            "10. Hand-hold J FLOOR for the unloaded",
            "free-pair check: vertical drag<0.5 N.",
            "11. THEN clamp RB+J to the bench through",
            "the rear window as E-E below. RL is free.",
            "Upper pad<=8 wide x5 deep x5 high,",
            "X55.9..63.9/Y6.2..11.2/Z4..9.",
            "Require RB stays seated on J; lifting RB",
            "invalidates the fixture, not the joint.",
            "12. Grip RL at X57.2/Y9.1 from +Y:",
            "0.2 below roof, then+0.2 to contact.",
            "Grip width<=2; below-roof reach<=8 mm.",
        ],
        621,
        x=247,
        gap=15,
    )
    packet.text(34, 405, "E-E at X60.55: floors/jaw; grip at X57.2 projected (Y-Z)", 10)
    # T is drawn schematically; the actual opening is measured against the bench.
    t = 6
    clamp_bounds, clamp_origin, clamp_scale = (-19, 20, -10, 25), (43, 289), 3.0
    bench = box([[49, -18.5, -t], [72, 11.5, 0]])
    for part, hatch in [(bench, -1), (jig, 1), (rear_base, -1), (rear_lid, 1)]:
        packet.section(
            part, 0, 60.55, clamp_bounds, clamp_origin, clamp_scale, hatch=hatch
        )
    packet.section(
        tools["rear-base-clamp-jaw"],
        0,
        60.55,
        clamp_bounds,
        clamp_origin,
        clamp_scale,
        hatch=0,
        gray=0.65,
    )
    # C-frame symbol is outside the pair; the checked upper pad is exact above.
    frame_parts = [
        box([[55.9, 6.2, -t - 3], [63.9, 11.2, -t]]),
        box([[55.9, 11.2, 7], [63.9, 18, 9]]),
        box([[55.9, 11.2, -t - 3], [63.9, 18, -t]]),
        box([[55.9, 18, -t - 3], [63.9, 20, 9]]),
    ]
    for part in frame_parts:
        packet.section(
            part, 0, 60.55, clamp_bounds, clamp_origin, clamp_scale, hatch=0, gray=0.82
        )
    packet.section(
        tools["rear-hook-envelope"],
        0,
        57.2,
        clamp_bounds,
        clamp_origin,
        clamp_scale,
        hatch=0,
        gray=0.65,
    )
    packet.pdf.setFillColor(white)
    packet.pdf.rect(44, 303, 46, 12, fill=1, stroke=0)
    packet.text(46, 306, "bench (T)", 8)
    packet.arrow(151, 365, 151, 393)
    packet.text(157, 379, "+Z", 8)
    packet.lines(
        [
            "Upper pad touches ONLY RB's floor.",
            "J lies between RB and the bench.",
            "Open beyond T+4; enter from +Y with",
            "upper pad +0.2 Z, then lower 0.2 to seat.",
            "Lower jaw below bench; frame outside",
            "the pair. Entire J floor is supported.",
            "T=actual bench thickness. C-frame is",
            "schematic: verify actual tool clearance.",
            "Never clamp RL. Dark grip is projected.",
        ],
        385,
        x=200,
        gap=12,
        size=9.5,
    )
    packet.lines(
        [
            (
                "13. Pull RL +Z to 5 N for 10 s, three trials. Require stop "
                "deflection<=0.1"
            ),
            "    and relative reverse X travel<=0.3 mm under load. Greater motion or",
            (
                "    RB lifting INVALIDATES THE FIXTURE; repair it before "
                "judging the hook."
            ),
            "    Require no release, crack, whitening or permanent distortion. Record",
            "    loaded gap; require residual gap<=0.2 mm after unloading.",
            "14. Remove the clamp, lift the CLOSED pair out of J, then slide RL -1.2 X",
            (
                "    before lifting it from RB. Repeat assembly/service ten "
                "times; inspect."
            ),
            (
                "STOP on any failed limit. Record actual tool fit, forces and "
                "observations."
            ),
            "This trial answers joint fit/retention; it does not establish long-term",
            "service strength or usable thread engagement. All physical tests NOT RUN.",
        ],
        267,
        gap=17,
    )
    packet.footer()
    packet.pdf.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=HERE / "coupon-assembly.pdf")
    parser.add_argument("--scratch", type=Path, default=REPO / ".scratch/r4-coupon-doc")
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    if not scratch.is_relative_to((REPO / ".scratch").resolve()):
        parser.error("Transient drawing files must stay in repository .scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    parts = args.parts.resolve()
    meshes = {
        name: trimesh.load(parts / f"{name}.stl", force="mesh") for name in INPUTS
    }
    tool_dir = args.tools.resolve()
    tools = {
        name: trimesh.load(tool_dir / f"{name}.stl", force="mesh")
        for name in TOOL_INPUTS
    }
    front = scratch / "front"
    front.mkdir(exist_ok=True)
    for role in ["base", "lid"]:
        shutil.copyfile(parts / f"front-{role}.stl", front / f"{role}.stl")
    render = runpy.run_path(str(HERE / "render.py"))["render"]
    render(front, "", False, 12, True, scratch / "front-exploded.png")
    packet = Packet(args.output)
    inventory(packet, meshes)
    front_insert(packet, meshes, scratch)
    front_screw(packet, meshes, tools)
    rear_trial(packet, meshes, tools)
    subprocess.run(
        [
            "pdftoppm",
            "-gray",
            "-r",
            "150",
            "-png",
            str(args.output),
            str(scratch / "page"),
        ],
        check=True,
    )
    manifest = {
        "scope": "Five plastic coupons/fixture plus one M3x8 and one RX insert",
        "physical_result": "NOT RUN",
        "stock_identity": "UNCONFIRMED; match before any heat-setting",
        "sources": {
            "coupon_document.py": sha(Path(__file__)),
            "render.py": sha(HERE / "render.py"),
        },
        "meshes": {f"{name}.stl": sha(parts / f"{name}.stl") for name in INPUTS},
        "tool_meshes": {
            f"{name}.stl": sha(tool_dir / f"{name}.stl") for name in TOOL_INPUTS
        },
        "pdf_sha256": sha(args.output),
        "pages": {path.name: sha(path) for path in sorted(scratch.glob("page-*.png"))},
    }
    (scratch / "coupon-document-inputs.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps({"pdf": str(args.output), "sha256": sha(args.output)}))


if __name__ == "__main__":
    main()
