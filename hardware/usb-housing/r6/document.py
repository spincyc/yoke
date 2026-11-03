#!/usr/bin/env python3
"""Render the six-page R6 nominal first-article guide from its exact STL meshes.

No printer I/O. Physical stock, fit, service and powered cooling remain NOT RUN.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pymupdf as fitz
import trimesh
from PIL import Image, ImageDraw
from reportlab.lib.colors import black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def moved(mesh: trimesh.Trimesh, delta: tuple[float, float, float]):
    result = mesh.copy()
    result.apply_translation(delta)
    return result


def render(
    objects: list[trimesh.Trimesh], target: Path, anchors=None, size=(1600, 1000)
):
    """White faces with depth-tested feature outlines; never wireframe ghosts."""
    az, el = math.radians(-28), math.radians(42)
    c, s, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)
    rotation = np.array([[c, s, 0], [-s * se, c * se, ce], [s * ce, -c * ce, se]])
    width, height = size
    faces, edges, points = [], [], []
    for mesh in objects:
        vertices = mesh.vertices @ rotation.T
        normals = mesh.face_normals @ rotation.T
        facing = normals[:, 2] > 1e-7
        adjacency = {}
        for fi, face in enumerate(mesh.faces):
            for a, b in zip(face, np.roll(face, -1), strict=True):
                adjacency.setdefault(tuple(sorted((int(a), int(b)))), []).append(fi)
        features = {
            edge
            for edge, adjacent in adjacency.items()
            if len(adjacent) != 2
            or facing[adjacent[0]] != facing[adjacent[1]]
            or np.dot(normals[adjacent[0]], normals[adjacent[1]]) < 0.91
        }
        for fi, face in enumerate(mesh.faces):
            if not facing[fi]:
                continue
            polygon = vertices[face]
            faces.append(polygon)
            points.append(polygon[:, :2])
            edges.extend(
                vertices[[a, b]]
                for a, b in zip(face, np.roll(face, -1), strict=True)
                if tuple(sorted((int(a), int(b)))) in features
            )
    all_points = np.vstack(points)
    low, high = all_points.min(axis=0), all_points.max(axis=0)
    scale = min((width - 90) / (high[0] - low[0]), (height - 70) / (high[1] - low[1]))
    center = (low + high) / 2

    def project(p):
        q = p.copy()
        q[:, :2] = (p[:, :2] - center) * scale
        q[:, 0] += width / 2
        q[:, 1] = height / 2 - q[:, 1]
        return q

    depth = np.full((height, width), -np.inf, dtype=np.float32)
    for polygon in faces:
        triangle = project(polygon)
        x0, y0 = np.maximum(0, np.floor(triangle[:, :2].min(axis=0)).astype(int))
        x1, y1 = np.minimum(
            [width - 1, height - 1], np.ceil(triangle[:, :2].max(axis=0)).astype(int)
        )
        a, b, c = triangle
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-8:
            continue
        xx, yy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        u = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / denominator
        v = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / denominator
        z = u * a[2] + v * b[2] + (1 - u - v) * c[2]
        old = depth[y0 : y1 + 1, x0 : x1 + 1]
        mask = (u >= -1e-8) & (v >= -1e-8) & (u + v <= 1 + 1e-8) & (z >= old)
        old[mask] = z[mask]
    picture = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(picture)
    for edge in edges:
        a, b = project(edge)
        count = int(np.linalg.norm(b[:2] - a[:2]) * 1.5) + 2
        samples = a + np.linspace(0, 1, count)[:, None] * (b - a)
        screen = np.rint(samples[:, :2]).astype(int)
        screen[:, 0] = np.clip(screen[:, 0], 0, width - 1)
        screen[:, 1] = np.clip(screen[:, 1], 0, height - 1)
        visible = samples[:, 2] >= depth[screen[:, 1], screen[:, 0]] - 0.13
        for i in range(count - 1):
            if visible[i] and visible[i + 1]:
                draw.line([tuple(screen[i]), tuple(screen[i + 1])], fill=0, width=2)
    picture.save(target)
    return {
        name: project(np.array([point], dtype=float) @ rotation.T)[0][:2]
        for name, point in (anchors or {}).items()
    }


STEPS = [
    {
        "id": "stock",
        "page": 1,
        "kind": "handling",
        "instruction": (
            "Count four printed parts: base, lid, front clip and right clip. Check "
            "four M3 x 16 case screws, two M3 x 8 clip screws and six RX-M3x5.7 "
            "inserts. Use a caliper/depth gauge to check actual lengths, head "
            "envelopes and insert stock identity before heating."
        ),
        "stop": (
            "Stop for wrong stock, oversized heads, damaged threads or inaccessible"
            " tools."
        ),
    },
    {
        "id": "empty-fit",
        "page": 2,
        "kind": "test",
        "instruction": (
            "With no electronics, inserts or screws, match the LAN ends and lower "
            "the lid vertically. It must seat evenly by hand, then lift off freely."
            " Clear loose print debris only. Check the actual driver through all "
            "six screw wells and the heater at all six pilots."
        ),
        "stop": "Stop for binding or skew. Screws must never pull a poor fit closed.",
    },
    {
        "id": "inserts",
        "page": 2,
        "kind": "assembly",
        "instruction": (
            "Support the EMPTY lid roof-down and EMPTY base floor-down on a flat "
            "nonflammable surface. Heat-set four inserts into the lid boss mouths "
            "and two into the base clip bosses, straight down and flush with the "
            "mouth. Follow the insert maker's controlled-heater method. Cool fully;"
            " inspect flushness, threads and distortion."
        ),
        "stop": (
            "Stop for a tilted or proud insert, crack, lifted boss or blocked "
            "thread. Never heat plastic with electronics installed."
        ),
    },
    {
        "id": "factory-lower",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "Disconnect all power and cables. Outside the case, fit the four Radxa "
            "lower screws and four supplied insulated copper pillars to the SBC, "
            "following Radxa's assembly instructions. Fit microSD now. Preserve the"
            " factory electrical contact faces; do not replace these screws with "
            "case screws or add insulation at their contacts."
        ),
        "stop": (
            "Stop if the delivered SBC/HAT variant, pillars or electrical joints "
            "differ from the reference."
        ),
    },
    {
        "id": "seat-sbc",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "With HAT ABSENT, lower the SBC and attached pillars level into the "
            "base. Ethernet faces the LAN end; both USB-C sockets face the single "
            "low side opening. The PCB rests on four edge pads; all components and "
            "factory hardware must clear."
        ),
        "stop": (
            "Stop for rocking, bowed PCB, caught GPIO pins, or a component bearing "
            "on the case."
        ),
    },
    {
        "id": "clips",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "Still with HAT ABSENT, lower the front clip onto its boss by the USB-C"
            " side and the right clip onto the opposite-end boss. Their toes face "
            "inward over the PCB edges. Start both M3 x 8 screws from above and "
            "lightly snug. Each clip seats on its plastic boss, leaving nominal 0.4"
            " mm vertical PCB play."
        ),
        "stop": (
            "Stop for PCB clamping, an unseated clip, early screw bottoming or a "
            "turning insert."
        ),
    },
    {
        "id": "hat",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "Align the HAT GPIO connector and lower it level onto the SBC and "
            "supplied pillars. Tighten the four upper Radxa screws as its "
            "instructions require. Confirm full connector seating and intact "
            "conductive joints. The clips are already installed; do not try to "
            "insert them under an assembled HAT."
        ),
        "stop": (
            "Stop if HAT, fan, cable or factory screw contacts the enclosure or "
            "clip hardware."
        ),
    },
    {
        "id": "close",
        "page": 4,
        "kind": "assembly",
        "instruction": (
            "Match LAN ends and lower the lid straight down over the completed "
            "module. It must seat freely with an even seam. Hold the two closed "
            "halves securely, invert together, and start all four M3 x 16 screws "
            "from the base underside. Lightly snug opposite corners in turn, then "
            "return floor-down."
        ),
        "stop": (
            "Stop for roof contact, binding, early bottoming, cross-threading, "
            "spinning inserts or proud screw heads. Do not use screw force to close"
            " the seam."
        ),
    },
    {
        "id": "service",
        "page": 5,
        "kind": "use",
        "instruction": (
            "Disconnect power and all cables. Hold the closed case inverted, remove"
            " all four bottom M3 x 16 screws, return floor-down and lift the lid "
            "vertically. For microSD or SBC removal: undo the four upper Radxa "
            "screws, lift the HAT level off GPIO, remove both M3 x 8 clip screws "
            "and clips, then lift the SBC with its pillars. Reassemble in page 3 "
            "order."
        ),
        "stop": (
            "Do not pry on a PCB or connector. Do not pull the SBC with clips or "
            "HAT still fitted."
        ),
    },
    {
        "id": "fit-test",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Unpowered: record stock measurements; inspect the free empty seam, "
            "cooled inserts, PCB support and toe clearance. Fit actual Ethernet and"
            " both USB-C plugs and collars. Gently turn the closed case through "
            "ordinary use orientations and record play/rattle. Check cable strain "
            "and every factory fastener remains clear."
        ),
        "stop": (
            "Stop for forced plugs, cable strain, loose inserts, PCB bowing, "
            "component contact or changed support."
        ),
    },
    {
        "id": "cycles",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Unpowered and unplugged, perform ten complete service/reassembly "
            "cycles, including HAT and clip removal. Each lid must seat freely "
            "before screws. Record cycle number, seam, thread condition, insert "
            "movement and PCB play; check actual microSD access with the SBC "
            "lifted."
        ),
        "stop": (
            "Stop at the first new bind, damaged thread, loose insert or cracked "
            "plastic; return that interface to design."
        ),
    },
    {
        "id": "thermal",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Only after unpowered checks pass, use the intended PoE supply and "
            "workload on a hard flat desk, vents clear. Record ambient and CPU "
            "temperature, case surface at several points, elapsed time, throttling "
            "and resets every 5 minutes. Compare lid-open and lid-closed runs at "
            "the same ambient, workload and fan state, at least 30 minutes each and"
            " until three successive readings change by less than 1 C. Record fan "
            "presence and operation; allow cooling between runs."
        ),
        "stop": (
            "Stop for throttling, unexpected resets, odor, plastic softening, any "
            "component maker's limit, or case surface above 45 C. The 45 C "
            "threshold is a provisional PLA first-article limit, not a "
            "manufacturer-qualified rating. Lack of a temperature plateau leaves "
            "the test incomplete."
        ),
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    out = args.output
    ev = args.evidence or out
    out.mkdir(parents=True, exist_ok=True)
    ev.mkdir(parents=True, exist_ok=True)
    p = json.loads((args.parts / "parameters.json").read_text())
    names = (
        "base",
        "lid",
        "front-clip",
        "right-clip",
        "screw",
        "clip-screw",
        "insert",
        "driver",
        "heat-tool",
    )
    meshes = {
        name: trimesh.load(args.parts / f"{name}.stl", force="mesh", process=True)
        for name in names
    }
    base = meshes["base"]
    lid = meshes["lid"].copy()
    lid.apply_transform(p["lid_assembly_matrix"])
    clips = []
    for name in ("front-clip", "right-clip"):
        clip = meshes[name].copy()
        clip.apply_transform(p["clip_assembly_matrix"])
        clips.append(clip)
    screws = [
        moved(meshes["screw"], (x, y, p["head_bearing_z_mm"]))
        for x, y in p["screw_axes_mm"]
    ]
    inserts = [
        moved(meshes["insert"], (x, y, p["insert_bottom_z_mm"]))
        for x, y in p["screw_axes_mm"]
    ]
    clip_screw = meshes["clip-screw"].copy()
    clip_screw.apply_transform(
        [
            [1, 0, 0, 0],
            [0, -1, 0, 0],
            [0, 0, -1, p["clip_head_bearing_z_mm"]],
            [0, 0, 0, 1],
        ]
    )
    clip_screws = [moved(clip_screw, (x, y, 0)) for x, y in p["clip_axes_mm"]]
    clip_inserts = [
        moved(meshes["insert"], (x, y, p["clip_insert_bottom_z_mm"]))
        for x, y in p["clip_axes_mm"]
    ]
    reference = trimesh.load(args.reference, force="mesh", process=True)
    reference.apply_transform(p["board_matrix"])
    components_dir = args.reference.parent / f"{args.reference.stem}-components"
    component_paths = [components_dir / f"{i:03}.stl" for i in range(11)]
    if not all(path.exists() for path in component_paths):
        raise FileNotFoundError(
            "Official SBC/HAT component leaves 000..010 are required beside reference"
        )
    components = [
        trimesh.load(path, force="mesh", process=True) for path in component_paths
    ]
    for part in components:
        part.apply_transform(p["board_matrix"])
    sbc = trimesh.util.concatenate(components[:2])
    hat = trimesh.util.concatenate(components[2:])
    render([base, lid, *screws], out / "preview.png")
    render(
        [
            base,
            moved(sbc, (0, 0, 16)),
            *[moved(clip, (0, 0, 32)) for clip in clips],
            moved(hat, (0, 0, 46)),
            moved(lid, (0, 0, 130)),
        ],
        out / "exploded.png",
        {
            "base": (70, 28, 10),
            "sbc": (65, 25, 25),
            "front": (*p["clip_axes_mm"][0], p["clip_top_z_mm"] + 32),
            "right": (*p["clip_axes_mm"][1], p["clip_top_z_mm"] + 32),
            "hat": (63, 28, 73),
            "lid": (73, 30, 170),
        },
        size=(1000, 1300),
    )
    points = render(
        [
            base,
            moved(sbc, (0, 0, 16)),
            *[moved(clip, (0, 0, 47)) for clip in clips],
            moved(hat, (0, 0, 85)),
        ],
        ev / "installation.png",
        {
            "base": (65, 0, 7),
            "sbc": (55, 2, 25),
            "front": (*p["clip_axes_mm"][0], p["clip_top_z_mm"] + 47),
            "right": (*p["clip_axes_mm"][1], p["clip_top_z_mm"] + 47),
            "hat": (58, 10, 112),
        },
        size=(1000, 1300),
    )
    heat_points = render(
        [base, moved(meshes["lid"], (104, 0, 0))],
        ev / "empty-parts.png",
        {
            "base": (*p["clip_axes_mm"][0], p["clip_boss_top_z_mm"]),
            "lid": (99, 21.9, 32.5),
        },
    )
    render(
        [base, moved(lid, (0, 0, 24))],
        ev / "close.png",
        {"lid": (72, 28, 40), "seam": (72, 28, 13.5)},
    )
    render(
        [base, *[moved(clip, (0, 0, 12)) for clip in clips]],
        ev / "clips.png",
        {
            "front": (*p["clip_axes_mm"][0], p["clip_top_z_mm"] + 12),
            "right": (*p["clip_axes_mm"][1], p["clip_top_z_mm"] + 12),
        },
    )
    c = canvas.Canvas(str(out / "assembly.pdf"), pagesize=(612, 792), invariant=1)
    c.setTitle("Yoke R6 PoE housing - nominal first article assembly")
    c.setAuthor("Yoke")

    def text(x, y, value, size=10.5, bold=False):
        c.setFillColor(black)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y, value)

    def para(value, y, x=38, width=536, size=10.5, leading=14):
        line = ""
        for word in value.split():
            candidate = f"{line} {word}".strip()
            if c.stringWidth(candidate, "Helvetica", size) > width:
                text(x, y, line, size)
                y -= leading
                line = word
            else:
                line = candidate
        text(x, y, line, size)
        return y - leading

    def heading(number, title):
        text(38, 755, f"YOKE / R6 PoE HOUSING / {number} OF 6", 10, True)
        text(38, 726, title, 22, True)
        c.setLineWidth(0.6)
        c.line(38, 712, 574, 712)

    def footer():
        c.setLineWidth(0.4)
        c.line(38, 48, 574, 48)
        text(
            38,
            33,
            (
                "Nominal first article. Physical fit, durability and powered "
                "cooling: NOT RUN."
            ),
            8.5,
        )
        text(
            38,
            20,
            (
                "Dimensions in mm. Actual exported STL sections; nominal hardware "
                "envelopes, no threads."
            ),
            8,
        )

    def arrow(x0, y0, x1, y1):
        c.setStrokeColor(black)
        c.setLineWidth(1)
        c.line(x0, y0, x1, y1)
        angle = math.atan2(y1 - y0, x1 - x0)
        for offset in (-0.45, 0.45):
            c.line(
                x1,
                y1,
                x1 - 7 * math.cos(angle + offset),
                y1 - 7 * math.sin(angle + offset),
            )

    def step(identifier, title, y, x=38, width=536, size=10.5):
        item = next(item for item in STEPS if item["id"] == identifier)
        text(x, y, title, 11.5, True)
        y = para(item["instruction"], y - 17, x, width, size)
        y = para(item["stop"], y - 1, x, width, 9.8, 13)
        return y - 10

    def picture(path, x, y, width):
        image_width, image_height = Image.open(path).size
        c.drawImage(
            ImageReader(str(path)),
            x,
            y,
            width=width,
            height=width * image_height / image_width,
        )

    def projected(point, x, y, width, size=(1600, 1000)):
        return (
            x + point[0] * width / size[0],
            y + (size[1] - point[1]) * width / size[0],
        )

    def section(mesh, at, bounds, origin, scale, style, plane="y"):
        point = [at, 0, 0] if plane == "x" else [0, at, 0]
        normal = [1, 0, 0] if plane == "x" else [0, 1, 0]
        cut = mesh.section(plane_origin=point, plane_normal=normal)
        if cut is None:
            return
        lo, hi, zlo, zhi = bounds
        c.saveState()
        clip = c.beginPath()
        clip.rect(*origin, (hi - lo) * scale, (zhi - zlo) * scale)
        c.clipPath(clip, stroke=0, fill=0)
        path = c.beginPath()
        for contour in cut.discrete:
            axes_2d = [1, 2] if plane == "x" else [0, 2]
            points = (contour[:, axes_2d] - [lo, zlo]) * scale + origin
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
            path.close()
        c.setLineWidth(0.8)
        c.setStrokeColor(black)
        c.setFillColor(white)
        c.drawPath(path, fill=1, stroke=1, fillMode=0)
        c.saveState()
        c.clipPath(path, stroke=0, fill=0, fillMode=0)
        c.setLineWidth(0.3)
        x, y = origin
        h, w = (zhi - zlo) * scale, (hi - lo) * scale
        for k in range(-int(h) - 40, int(w + h) + 40, 7 if style < 2 else 4):
            c.line(x + k, y, x + k + (-h if style == 1 else h), y + h)
        c.restoreState()
        c.restoreState()

    heading(1, "Know the parts and hardware")
    y = para(
        (
            "For the ordered ZERO 3E 2GB WITH GPIO (Amazon B0CZD1C54N) and Radxa "
            "PoE HAT (B0D1KF1L7G). Electronics have not arrived. Print and unloaded"
            " checks can proceed; verify the delivered variant before fitting or "
            "powering."
        ),
        692,
    )
    text(
        38,
        y - 7,
        " x ".join(f"{value:g}" for value in p["envelope_mm"])
        + "  |  two shells + two removable PCB clips",
        11,
        True,
    )
    picture(out / "preview.png", 140, 395, 355)
    text(38, 460, "LAN end", 10, True)
    arrow(92, 457, 174, 472)
    text(360, 404, "Two USB-C ports / one low side opening", 9)
    text(
        38,
        379,
        "Opposite end closed. HDMI covered. microSD: open case and lift SBC.",
        10,
        True,
    )
    y = step("stock", "1  Inventory and measure owner stock", 351)
    y = para(
        (
            "Nominal screws: M3 x 16 (4) and M3 x 8 (2), measured under head; each "
            "head at most diameter 6 x height 3. Inserts: RX-M3x5.7 (6), diameter "
            "4.6 x length 5.7. Printed pilots: diameter 4.0. Do not substitute by "
            "thread label alone."
        ),
        y,
    )
    y = para(
        (
            "Separate factory hardware: 4 Radxa insulated copper pillars + 8 "
            "supplied CM2.5x4 screws. These conductive joints remain electrically "
            "functional; plastic retention touches PCB edges only."
        ),
        y - 4,
    )
    y = para(
        (
            "Tools: caliper/depth gauge, matching socket driver, correct Radxa "
            "screw driver, controlled insert heater/tip, nonflammable support, "
            "actual cables, temperature probe and CPU monitoring. Confirm tool "
            "access on empty parts."
        ),
        y - 4,
    )
    para(
        (
            "Source allowance: larger official RS109_RA023 assembly (18 mm spacing)"
            " gives 3.5 mm nominal roof clearance. Earlier 14.5/18 mm spacing "
            "discrepancy remains unresolved. Diameter-8 corner clearances and "
            "headroom are design allowances, not measurements of delivered stock."
        ),
        y - 4,
        size=10,
    )
    footer()
    c.showPage()

    heading(2, "Fit empty; heat-set six inserts")
    y = step("empty-fit", "2  Test the empty shell pair first", 692)
    y = step("inserts", "3  Heat only the empty plastic parts", y)
    picture(ev / "empty-parts.png", 96, 275, 420)
    text(45, 537, "BASE: floor down", 10, True)
    text(380, 537, "LID: roof down", 10, True)
    for point in heat_points.values():
        x, y = projected(point, 96, 275, 420)
        arrow(x, y + 38, x, y + 4)
    text(45, 277, "2 inserts / clip boss mouths", 10, True)
    text(329, 277, "4 inserts / shell boss mouths", 10, True)
    y = para(
        (
            "The illustrated lid is inverted from its assembled orientation. Its "
            "insert mouth is the seam face. Heat-set the insert end flush with that"
            " face; in assembly it occupies Z = 13.5 to 19.2. Base inserts finish "
            "flush at the clip boss top, Z = 8.0, and extend to Z = 2.3."
        ),
        248,
    )
    y = para(
        (
            "Let both parts cool completely before threading screws or installing "
            "electronics. Inspect all six bosses and threads. Test actual driver "
            "and heater clearance; the model's tool envelopes are not measurements "
            "of your tools."
        ),
        y - 4,
    )
    y = para(
        (
            "Empty nested joint: 0.5 clearance per side, 2.0 engagement, 1.2 skirt "
            "thickness and 0.5 entry chamfer. Both mating edges print above the "
            "bed. Lower and lift vertically; there is no hook or sideways latch "
            "motion."
        ),
        y - 4,
    )
    para(
        (
            "After cooling, repeat the empty fit. Check the two loose clips seat "
            "flat on their own bosses. Never drive a screw to reshape a boss, "
            "flatten a tilted insert or close an uneven seam."
        ),
        y - 4,
    )
    footer()
    c.showPage()

    heading(3, "SBC first, then clips, then HAT")
    picture(ev / "installation.png", 318, 377, 250)
    # Leader labels correspond to projected actual mesh anchor coordinates.
    for label, key, yy in [
        ("HAT last", "hat", 684),
        ("Front clip", "front", 548),
        ("Right clip", "right", 519),
        ("SBC + pillars first", "sbc", 465),
        ("BASE", "base", 398),
    ]:
        text(308, yy, label, 8.5, True)
        xx, py = projected(points[key], 318, 377, 250, (1000, 1300))
        arrow(405, yy - 3, xx, py)
    arrow(568, 605, 568, 579)
    text(532, 614, "Lower level", 8)
    y = step(
        "factory-lower",
        "4  Fit factory pillars outside the case",
        692,
        width=262,
        size=10,
    )
    y = step("seat-sbc", "5  Lower SBC with HAT absent", y, width=262, size=10)
    y = min(y, 365)
    y = step("clips", "6  Screw the two clips down from above", y, size=10)
    y = step("hat", "7  Lower HAT and fit four upper Radxa screws", y, size=10)
    para(
        (
            "Lid omitted here; see page 4. Official assembly meshes are raised "
            "apart for "
            "sequence clarity. Supplied pillars and screws must be installed as "
            "stated even where not separately represented in the source CAD. Fan "
            "shown is a reference feature; its inclusion in the ordered HAT is "
            "unconfirmed."
        ),
        y,
        size=9.5,
        leading=12,
    )
    text(
        38,
        76,
        "Factory assembly: docs.radxa.com/en/zero/zero3/accessories/3e-poe-hat",
        8,
    )
    c.linkURL(
        "https://docs.radxa.com/en/zero/zero3/accessories/3e-poe-hat",
        (38, 73, 400, 87),
        relative=0,
    )
    footer()
    c.showPage()

    heading(4, "Close freely; screw from below")
    y = step("close", "8  Close, hold together, invert, fasten", 692)
    picture(ev / "close.png", 333, 400, 238)
    arrow(560, 547, 560, 509)
    text(337, 400, "Lid lowers vertically", 9, True)
    text(38, 537, "A-A / case screw / Y = 1.6", 11, True)
    for mesh, style in [(base, 0), (lid, 1), (inserts[0], 2), (screws[0], 2)]:
        section(mesh, 1.6, (-9.5, -0.5, 0, 22), (50, 280), 10.5, style)
    text(162, 508, "21.2  blind pilot end", 10)
    text(162, 488, "19.6  screw tip", 10)
    text(162, 468, "19.2  insert top", 10)
    text(162, 448, "13.5  seam / insert mouth", 10)
    text(162, 428, "3.6    head bearing land", 10)
    text(162, 408, "0.6    head bottom / recess", 10)
    arrow(97, 245, 97, 275)
    text(43, 232, "Driver from base underside", 9.5, True)
    y = para(
        (
            "Four M3 x 16 joints: 5.7 nominal full insert overlap; screw tip "
            "extends 0.4 beyond the insert into 2.0 of blind-pilot reserve (1.6 "
            "remains). Inspect actual lengths: a long screw must not bottom out "
            "before the seam seats."
        ),
        381,
        x=162,
        width=403,
        size=10,
    )
    para(
        (
            "Load path: screw head -> base bearing land -> base/lid plastic stop at"
            " seam -> lid insert threads -> screw. Neither PCB is in this clamp "
            "stack."
        ),
        y - 5,
        x=162,
        width=403,
        size=10,
    )
    y = para(
        (
            "Section is an actual cut through the released base, lid, screw and "
            "insert STL meshes, not a schematic block. Base and lid have opposing "
            "hatches; metal has close hatching. Threads are not modeled."
        ),
        207,
        size=10,
    )
    y = para(
        (
            "The arrows show access in the assembled coordinate frame. For "
            "fastening, hold both shells together and turn the case upside down so "
            "the driver is above the bottom wells. Start all four screws before "
            "snugging opposite corners."
        ),
        y - 4,
        size=10,
    )
    para(
        (
            "Hand-tight only: no enclosure torque has been validated. Heads should "
            "lie 0.6 below the bottom surface with nominal stock. Stop for a proud "
            "head, spinning insert, bowed case or any change in PCB support."
        ),
        y - 4,
        size=10,
    )
    footer()
    c.showPage()

    heading(5, "Retention and reverse service")
    text(38, 690, "B-B / front clip screw / X = 23.5 / Y horizontal, Z up", 10.5, True)
    for mesh, style in [
        (base, 0),
        (clips[0], 1),
        (clip_inserts[0], 2),
        (clip_screws[0], 2),
        (sbc, 2),
    ]:
        section(mesh, 23.5, (-9, 3.5, 0.5, 14), (49, 478), 12, style, plane="x")
    arrow(105, 676, 105, 647)
    text(122, 666, "Driver from above; HAT absent", 9.5, True)
    y = para(
        (
            "M3 x 8 screw: head bears at Z = 10; head top 13; tip 2.0. Insert spans"
            " Z = 2.3 to 8.0. Full 5.7 insert overlap; 0.8 remains to blind pilot "
            "bottom Z = 1.2."
        ),
        636,
        x=224,
        width=350,
        size=10,
    )
    y = para(
        (
            "Clip underside seats on its plastic boss at Z = 8. The toe underside "
            "is Z = 9.2, above PCB top Z = 8.8. Nominal play 0.4; lower PCB rests "
            "on Z = 7.2 pads."
        ),
        y - 5,
        x=224,
        width=350,
        size=10,
    )
    para(
        (
            "Load path: screw head -> clip land -> base boss -> insert threads. Toe"
            " limits PCB lift; it must not clamp the board or share the factory "
            "pillar load."
        ),
        y - 5,
        x=224,
        width=350,
        size=10,
    )
    text(38, 458, "C-C / nested rear wall / X = 22", 10, True)
    for mesh, style in [(base, 0), (lid, 1)]:
        section(mesh, 22, (29.4, 35, 10.6, 16), (51, 365), 14, style, plane="x")
    para(
        (
            "Actual STL joint cut: lid skirt inside the base wall, 0.5 side gap, "
            "2.0 engagement. Matching parts lift apart vertically without screw "
            "force. Vents also pass through the inner skirt."
        ),
        430,
        x=160,
        width=414,
        size=10,
    )
    y = step("service", "9  Power off; reverse the installation order", 342)
    para(
        (
            "For lid-only inspection, the HAT and clips can remain fitted. For "
            "microSD, lift the SBC after HAT and clip removal; no outside microSD "
            "opening is provided. Keep removed factory screws separate from M3 case"
            " hardware. Do not loosen the lower conductive joints merely to open "
            "the case."
        ),
        y - 3,
        size=10,
    )
    footer()
    c.showPage()

    heading(6, "Record first-article checks")
    text(
        38,
        692,
        "All physical checks below: NOT RUN. Complete after parts arrive.",
        11,
        True,
    )
    y = step("fit-test", "A  Inspect unpowered fit and cable access", 669, size=10)
    y = step("cycles", "B  Prove the service sequence ten times", y, size=10)
    y = step("thermal", "C  Compare open and closed temperatures", y, size=10)
    y = para(
        (
            "Air paths: low side inlets, inter-board and upper side rows, and roof "
            "slots. Keep them clear on a hard desk. A fan is optional and its "
            "presence must be recorded. Vent area and 3.5 roof clearance do not "
            "establish cooling performance or a safe continuous-power rating."
        ),
        y,
        size=9.5,
        leading=12,
    )
    text(38, y - 6, "Record before deciding to use this enclosure", 11, True)
    for i, line in enumerate(
        [
            (
                "Date / printed material / measured stock: "
                "___________________________________"
            ),
            (
                "Delivered board + HAT / fan / fit + cable result: "
                "______________________________"
            ),
            (
                "Cycles / play / inserts / failures: "
                "__________________________________________"
            ),
            (
                "Ambient / workload / open vs closed temperatures / time: "
                "______________________"
            ),
            (
                "Outcome / incomplete checks / next design change: "
                "__________________________"
            ),
        ]
    ):
        text(38, y - 27 - i * 19, line, 9.5)
    footer()
    c.save()

    pdf = fitz.open(out / "assembly.pdf")
    pages = []
    for index, page in enumerate(pdf):
        path = ev / f"page-{index + 1}.png"
        page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), colorspace=fitz.csGRAY).save(path)
        pages.append({"page": index + 1, "path": str(path), "sha256": sha(path)})
    sections = []
    for index, rect in [(3, (38, 245, 574, 565)), (4, (38, 100, 574, 437))]:
        pix = pdf[index].get_pixmap(
            matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, clip=fitz.Rect(rect)
        )
        sections.append(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    combined = Image.new(
        "L",
        (max(im.width for im in sections), sum(im.height for im in sections) + 20),
        255,
    )
    offset = 0
    for im in sections:
        combined.paste(im, (0, offset))
        offset += im.height + 20
    combined.save(out / "section.png")
    sources = [
        Path(__file__),
        HERE / "housing.scad",
        HERE / "hardware.json",
        HERE / "build.py",
        args.parts / "parameters.json",
        args.reference,
        *component_paths,
    ]
    sources.extend(args.parts / f"{name}.stl" for name in names)
    actions = {
        "schema": "yoke.r6-guide-actions/1",
        "actions": [
            {
                "id": s["id"],
                "kind": s["kind"],
                "pages": [s["page"]],
                "action": s["instruction"],
                "stop": s["stop"],
            }
            for s in STEPS
        ],
        "physical_test": {
            "status": "NOT RUN",
            ("scope"): (
                "Nominal full PoE housing first article; physical and powered "
                "verification pending delivered electronics"
            ),
            "procedure": [s["instruction"] for s in STEPS if s["kind"] == "test"],
            "stop_conditions": [s["stop"] for s in STEPS],
            "observations": [
                "Actual stock and delivered electronics identity",
                "Unpowered support, clearance, cable access and repeated service",
                (
                    "Open/closed ambient, CPU and case temperatures with fan state "
                    "and workload"
                ),
            ],
        },
        "tools": {
            "caliper": "Caliper and depth gauge for stock and pilots",
            "driver": "Matching M3 socket and Radxa drivers; verify access",
            "heater": "Controlled insert heater and matching tip, maker's method",
            "support": "Flat nonflammable support",
            "cables": "Actual Ethernet and both USB-C plugs/collars",
            "monitor": "CPU telemetry and case surface temperature probe",
        },
    }
    evidence = {
        "schema": "yoke.r6-guide/1",
        "pdf_sha256": sha(out / "assembly.pdf"),
        "pages": pages,
        "sources": [{"path": str(path), "sha256": sha(path)} for path in sources],
        "actions": STEPS,
        "readability_review": "PENDING final rendered page inspection",
        "physical_validation": "NOT RUN",
    }
    for name in ("actions.json", "guide-actions.json"):
        (ev / name).write_text(json.dumps(actions, indent=2) + "\n")
    (ev / "guide-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {
                "pdf": str(out / "assembly.pdf"),
                "sha256": evidence["pdf_sha256"],
                "pages": len(pages),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
