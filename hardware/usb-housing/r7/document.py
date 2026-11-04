#!/usr/bin/env python3
"""Render the six-page R7 nominal first-article guide from its exact STL meshes.

Two compact housings: a PoE box (ZERO 3E + RA023 HAT, device side) and a bare
box (ZERO 3E only, computer side). No printer I/O. Physical stock, fit,
service and powered cooling remain NOT RUN.
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
BARE_OFFSET = (0, 60, 0)
SIDE_BY_SIDE = (96, 0, 0)


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
            "Count four printed parts: two identical bases (YOKE R7 BASE under "
            "the floor), the PoE lid (YOKE R7 POE) and the bare lid (YOKE R7 "
            "USB). Count eight M3 x 8 socket-head screws and eight RX-M3x5.7 "
            "inserts. Measure screw length, head envelope and insert stock "
            "before heating."
        ),
        "stop": (
            "Stop for wrong stock, heads over 6.0 x 3.0, damaged threads or "
            "inaccessible tools. M3 x 16 and M3 x 20 are too long here."
        ),
    },
    {
        "id": "empty-fit",
        "page": 2,
        "kind": "test",
        "instruction": (
            "With no electronics, inserts or screws, match the LAN ends and "
            "lower each lid vertically onto a base. It must slide over the "
            "inner lip by hand and seat on the four corner post rings, leaving "
            "an even 0.3 shadow gap along the seam; then lift off freely. Check "
            "the actual driver in all eight bottom wells and the heater in all "
            "eight lid post pilots."
        ),
        "stop": (
            "Stop for binding, rocking or skew. Screws must never pull a poor "
            "fit closed."
        ),
    },
    {
        "id": "inserts",
        "page": 2,
        "kind": "assembly",
        "instruction": (
            "Support each EMPTY lid roof-down on a flat nonflammable surface. "
            "Heat-set one insert into the pilot at the bottom face of each of the"
            " four corner posts, straight down, finishing 0.2 below that face. "
            "Follow the insert maker's controlled-heater method. Cool fully; "
            "inspect squareness, threads and distortion."
        ),
        "stop": (
            "Stop for a tilted insert, an insert proud of the post face, crack, "
            "bulged post or blocked thread. Never heat plastic with electronics "
            "installed."
        ),
    },
    {
        "id": "poe-stack",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "PoE box. Disconnect all power and cables. Outside the case, fit the "
            "microSD card, then follow Radxa's instructions: four lower CM2.5 "
            "screws into the four insulated copper pillars, HAT pressed fully "
            "onto GPIO, four upper CM2.5 screws. Tighten as Radxa requires. "
            "These conductive joints carry PoE voltage and stay installed."
        ),
        "stop": (
            "Stop if the delivered SBC/HAT, pillars, insulation or electrical "
            "joints differ from Radxa's assembly or the reference."
        ),
    },
    {
        "id": "poe-seat",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "Lower the complete stack level into one base: RJ45 between the two "
            "LAN-end posts and into the low cradle, USB-C toward the long open "
            "side. The SBC underside rests on three front pads and three raised "
            "rear-lip pads; no factory screw or pillar touches plastic."
        ),
        "stop": (
            "Stop for rocking, a bowed PCB, a component bearing on the case or "
            "plastic touching Radxa hardware."
        ),
    },
    {
        "id": "poe-close",
        "page": 4,
        "kind": "assembly",
        "instruction": (
            "Match LAN ends and lower the PoE lid straight down over the stack. "
            "It must seat on the four post rings with the even shadow gap; its four "
            "inner ribs stop 0.4 above the HAT edges. Hold both halves together, "
            "invert, start all four M3 x 8 screws from the bottom wells, then "
            "lightly snug opposite corners. Return floor-down."
        ),
        "stop": (
            "Stop for binding, roof or rib contact, cross-threading, spinning "
            "inserts or proud heads, or if a head shelf or post ring turns, "
            "cracks or separates while snugging. Never use screw force to close "
            "a gap."
        ),
    },
    {
        "id": "bare-seat",
        "page": 5,
        "kind": "assembly",
        "instruction": (
            "Computer-side box, no HAT. Fit microSD outside the case. Lower the "
            "bare ZERO 3E level into the second base: RJ45 between the LAN-end "
            "posts, USB-C toward the long open side, PCB on the six edge pads. "
            "Leave all four mounting holes empty."
        ),
        "stop": (
            "Stop for rocking, a bowed PCB or anything entering a mounting hole;"
            " those holes connect to the RJ45 PoE centre taps."
        ),
    },
    {
        "id": "bare-close",
        "page": 5,
        "kind": "assembly",
        "instruction": (
            "Match LAN ends and lower the bare lid straight down. Its four ribs "
            "stop 0.4 above the PCB edges. Hold together, invert, fit four M3 x 8"
            " screws from below, lightly snug opposite corners."
        ),
        "stop": (
            "Stop for binding, rib or roof contact with the board, cross-"
            "threading, proud heads, or a head shelf or post ring that turns, "
            "cracks or separates while snugging."
        ),
    },
    {
        "id": "service",
        "page": 6,
        "kind": "use",
        "instruction": (
            "Disconnect power and all cables. Hold the case inverted, remove the "
            "four bottom M3 x 8 screws, return floor-down and lift the lid "
            "straight up. Lift the complete board (or PoE stack) level out of the"
            " base; change microSD outside the case. Reassemble in page 3-5 "
            "order."
        ),
        "stop": (
            "Do not pry on a PCB or connector. Do not loosen the Radxa conductive"
            " joints merely to open the case."
        ),
    },
    {
        "id": "fit-test",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Unpowered: record stock measurements, then accept only if the seam "
            "gap is 0.15-0.45 on every side, the measured HAT gap is 17.7-18.3 "
            "(otherwise set hat_gap and reprint only the PoE lid), heads sit at "
            "or below the floor and ribs clear the boards. Seat the actual "
            "Ethernet and both USB-C plugs; record play/rattle in use positions."
        ),
        "stop": (
            "Stop for forced plugs, partly seated USB-C, cable strain, loose "
            "inserts, PCB bowing or component contact."
        ),
    },
    {
        "id": "cycles",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Unpowered and unplugged, perform ten complete service cycles on each"
            " box, including board removal and microSD exchange. Each lid must "
            "seat freely before screws. Record seam gap, thread condition, "
            "insert movement and PCB play."
        ),
        "stop": (
            "Stop at the first new bind, damaged thread, loose insert or cracked"
            " plastic; return that interface to design."
        ),
    },
    {
        "id": "thermal",
        "page": 6,
        "kind": "test",
        "instruction": (
            "After unpowered checks pass, run each box on a hard flat desk with "
            "its real power source (PoE switch or computer USB-C) and workload. "
            "Every 5 minutes record ambient, CPU and case temperatures, "
            "throttling and resets. Compare lid-open and lid-closed runs at "
            "least 30 minutes each, until three readings change under 1 C."
        ),
        "stop": (
            "Stop for throttling, resets, odor, softening, any maker's limit or "
            "case surface above 45 C (provisional PLA limit, not a rating). "
            "No plateau leaves the test incomplete."
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
    names = ("base", "poe-lid", "bare-lid", "screw", "insert", "driver", "heat-tool")
    meshes = {
        name: trimesh.load(args.parts / f"{name}.stl", force="mesh", process=True)
        for name in names
    }
    base = meshes["base"]
    poe_lid = meshes["poe-lid"].copy()
    poe_lid.apply_transform(p["poe_lid_assembly_matrix"])
    bare_lid = meshes["bare-lid"].copy()
    bare_lid.apply_transform(p["bare_lid_assembly_matrix"])
    screws = [
        moved(meshes["screw"], (x, y, p["head_bearing_z_mm"]))
        for x, y in p["screw_axes_mm"]
    ]
    inserts = [
        moved(meshes["insert"], (x, y, p["insert_bottom_z_mm"]))
        for x, y in p["screw_axes_mm"]
    ]
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
    bz = p["board_top_z_mm"]
    hp, hb = p["poe_height_mm"], p["bare_height_mm"]
    tip = p["head_bearing_z_mm"] + p["screw_length_mm"]
    tip_beyond = tip - p["insert_top_z_mm"]
    tip_reserve = p["pilot_end_z_mm"] - tip
    ribs = ", ".join(f"{f[0]:g}-{f[1]:g}" for f in p["bare_fins_xy_mm"])
    # Overview: both closed housings side by side with their screws.
    overview = render(
        [
            base,
            poe_lid,
            *screws,
            moved(base, SIDE_BY_SIDE),
            moved(bare_lid, SIDE_BY_SIDE),
            *[moved(s, SIDE_BY_SIDE) for s in screws],
        ],
        out / "preview.png",
        {
            "poe": (20, 32, hp),
            "bare": (SIDE_BY_SIDE[0] + 40, 32, hb),
            "lan": (-8.1, 15, 9),
            "usb": (SIDE_BY_SIDE[0] + 47, -3.65, 8),
        },
    )
    poe_points = render(
        [
            base,
            moved(sbc, (0, 0, 30)),
            moved(hat, (0, 0, 30)),
            moved(poe_lid, (0, 0, 105)),
        ],
        out / "exploded.png",
        {
            "base": (73.3, 32.6, 3),
            "stack": (-6.4, 15, bz + 30 + 3),
            "lid": (-8.1, 32.6, hp + 95),
        },
        size=(1000, 1500),
    )
    bare_points = render(
        [base, moved(sbc, (0, 0, 22)), moved(bare_lid, (0, 0, 52))],
        ev / "bare-exploded.png",
        {
            "base": (73.3, 32.6, 3),
            "sbc": (-6.4, 15, bz + 22 + 3),
            "lid": (-8.1, 32.6, hb + 45),
        },
        size=(1000, 1200),
    )
    heat_points = render(
        [
            base,
            moved(meshes["poe-lid"], (0, 46, 0)),
            moved(meshes["bare-lid"], (92, 46, 0)),
        ],
        ev / "empty-parts.png",
        {
            "poe": (-4.2, 46 + 28.95 - 1.2, hp - p["post_contact_z_mm"]),
            "bare": (92 - 4.2, 46 + 28.95 - 1.2, hb - p["post_contact_z_mm"]),
            "base": (-4.2, 1.2, p["post_contact_z_mm"]),
            "poe_label": (20, 46 + 32.6, hp - 3.9),
            "bare_label": (92 + 30, 46 + 32.6, hb - 3.9),
            "base_label": (-8.1, 0, 0),
        },
    )
    c = canvas.Canvas(str(out / "assembly.pdf"), pagesize=(612, 792), invariant=1)
    c.setTitle("Yoke R7 compact housings - nominal first article assembly")
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
        text(38, 755, f"YOKE / R7 COMPACT HOUSINGS / {number} OF 6", 10, True)
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
                "Dimensions in mm, Z from the base underside. Actual exported STL "
                "sections; nominal hardware envelopes, no threads."
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
            if style >= 0:
                path.close()
        if style < 0:
            # Official board meshes are surface tessellations, not closed
            # solids: draw their cut lines only, never an invented fill.
            c.setLineWidth(0.6)
            c.setStrokeColor(black)
            c.drawPath(path, fill=0, stroke=1)
            c.restoreState()
            return
        c.setLineWidth(0.8)
        c.setStrokeColor(black)
        c.setFillColor(white)
        c.drawPath(path, fill=1, stroke=1, fillMode=0)
        if style >= 0:
            c.saveState()
            c.clipPath(path, stroke=0, fill=0, fillMode=0)
            c.setLineWidth(0.3)
            x, y = origin
            h, w = (zhi - zlo) * scale, (hi - lo) * scale
            for k in range(-int(h) - 40, int(w + h) + 40, 7 if style < 2 else 4):
                c.line(x + k, y, x + k + (-h if style == 1 else h), y + h)
            c.restoreState()
        c.restoreState()

    def frame(bounds, origin, scale):
        lo, hi, zlo, zhi = bounds
        c.setLineWidth(0.4)
        c.rect(*origin, (hi - lo) * scale, (zhi - zlo) * scale)

    def callout(bounds, origin, scale, horizontal, z, label, tx, ty):
        """Label outside a section frame with a leader to an exact point."""
        lo, _hi, zlo, _zhi = bounds
        x = origin[0] + (horizontal - lo) * scale
        y = origin[1] + (z - zlo) * scale
        text(tx, ty, label, 8.5, True)
        start = (
            tx - 3 if tx > x else tx + c.stringWidth(label, "Helvetica-Bold", 8.5) + 3
        )
        arrow(start, ty + 3, x, y)

    env_poe = " x ".join(f"{v:g}" for v in p["poe_envelope_mm"])
    env_bare = " x ".join(f"{v:g}" for v in p["bare_envelope_mm"])

    # Page 1 ------------------------------------------------------------------
    heading(1, "Know the parts and hardware")
    y = para(
        (
            "Two boxes for the ordered Radxa ZERO 3E 2GB WITH GPIO (Amazon "
            "B0CZD1C54N). Device side: ZERO 3E + Radxa PoE HAT (B0D1KF1L7G), "
            "powered over Ethernet. Computer side: bare ZERO 3E, powered by the "
            "computer through USB-C. Electronics have not arrived; verify them "
            "before fitting or powering."
        ),
        692,
    )
    text(38, y - 6, f"PoE box {env_poe}   |   Bare box {env_bare}", 11, True)
    pic_w = 430
    pic_x, pic_y = 90, y - 22 - pic_w * 1000 / 1600
    picture(out / "preview.png", pic_x, pic_y, pic_w)
    for key, label, sub, dx, dy in [
        ("poe", "PoE box", "device side", -10, 34),
        ("bare", "Bare box", "computer side", -10, 34),
    ]:
        px, py = projected(overview[key], pic_x, pic_y, pic_w)
        text(px + dx, py + dy, label, 10, True)
        text(px + dx, py + dy - 12, sub, 9)
        arrow(px + dx + 18, py + dy - 16, px, py + 2)
    px, py = projected(overview["lan"], pic_x, pic_y, pic_w)
    text(38, py - 34, "LAN end", 10, True)
    arrow(66, py - 26, px - 2, py - 2)
    px, py = projected(overview["usb"], pic_x, pic_y, pic_w)
    text(px - 40, pic_y + 18, "Both USB-C: one long side opening", 9, True)
    arrow(px + 4, pic_y + 29, px + 1, py - 3)
    text(
        38,
        pic_y - 14,
        "Opposite end closed. HDMI covered. microSD: lid off, lift board, change card.",
        10,
        True,
    )
    y = step("stock", "1  Inventory and measure owner stock", pic_y - 38)
    y = para(
        (
            "Nominal screws: M3 x 8 socket head (8), measured under head; head at "
            "most diameter 6 x height 3. Inserts: RX-M3x5.7 (8), diameter 4.6 x "
            "length 5.7, printed pilots diameter 4.0. Four of each per box. No "
            "clips, standoffs or nylon screws are used."
        ),
        y,
    )
    y = para(
        (
            "PoE box only: Radxa's 4 insulated copper pillars and 8 CM2.5x4 "
            "screws. They carry PoE voltage (up to 57 V): keep them installed, "
            "tightened and insulated. No case fastener replaces or touches them."
        ),
        y - 4,
    )
    y = para(
        (
            "Tools: caliper/depth gauge, 2.5 mm hex driver reaching 20 mm, "
            "Radxa screw driver, controlled insert heater/tip, nonflammable "
            "support, actual cables, temperature probe and CPU monitoring."
        ),
        y - 4,
    )
    para(
        (
            "Source allowance: official RS109_RA023 CAD, 18 mm board gap, 1.0 "
            "roof clearance. Radxa's 14.5 mm pillar listing is unresolved; if the"
            " measured gap differs, only the PoE lid is regenerated and reprinted."
        ),
        y - 4,
        size=10,
    )
    footer()
    c.showPage()

    # Page 2 ------------------------------------------------------------------
    heading(2, "Fit empty; heat-set eight inserts")
    y = step("empty-fit", "2  Test each empty lid on a base first", 692)
    y = step("inserts", "3  Heat only the empty lids, roof down", y)
    pic_w = 470
    pic_h = pic_w * 1000 / 1600
    pic_x, pic_y = 70, y - 16 - pic_h
    picture(ev / "empty-parts.png", pic_x, pic_y, pic_w)
    px, py = projected(heat_points["poe_label"], pic_x, pic_y, pic_w)
    text(px - 40, py + 26, "POE LID, roof down", 10, True)
    text(pic_x + pic_w - 128, pic_y + pic_h + 2, "BARE LID, roof down", 10, True)
    px, py = projected(heat_points["base_label"], pic_x, pic_y, pic_w)
    text(40, py + 58, "BASE, floor down", 10, True)
    for key in ("poe", "bare"):
        px, py = projected(heat_points[key], pic_x, pic_y, pic_w)
        arrow(px, py + 40, px, py + 4)
    px, py = projected(heat_points["base"], pic_x, pic_y, pic_w)
    text(40, py - 14, "post ring seat (one of 4)", 9, True)
    arrow(
        40 + c.stringWidth("post ring seat (one of 4)", "Helvetica-Bold", 9) + 3,
        py - 11,
        px - 2,
        py,
    )
    y = para(
        (
            "Vertical arrows: one of the four corner post pilots in each lid; set"
            " an insert into each. In the closed box each insert spans Z = "
            f"{p['insert_bottom_z_mm']:g} to {p['insert_top_z_mm']:g}; the lid "
            f"post face at Z = {p['post_contact_z_mm']:g} seats on the base post "
            "ring. Let parts cool before threading screws or adding electronics."
        ),
        pic_y - 14,
    )
    y = para(
        (
            f"Nesting joint: lid walls slide over a {p['lip_thickness_mm']:g} "
            f"inner lip with {p['lip_per_side_clearance_mm']:g} clearance per "
            f"side, {p['lip_engagement_mm']:g} engagement and a "
            f"{p['lip_entry_chamfer_mm']:g} lead-in. Each solid base post ring "
            f"(diameter {p['base_post_ring_diameter_mm']:g}) rises into a "
            f"{p['lid_seat_recess_diameter_mm']:g} seat recess in the lid. Lower "
            "and lift vertically; there is no hook, clip or sideways motion."
        ),
        y - 4,
    )
    para(
        (
            f"The lid rests on the post rings, so a {p['seam_gap_mm']:g} shadow"
            " gap stays at the outer seam by design. After cooling, repeat the "
            "empty fit. Never drive a screw to close the gap, flatten a tilted "
            "insert or reshape a post."
        ),
        y - 4,
    )
    footer()
    c.showPage()

    # Page 3 ------------------------------------------------------------------
    heading(3, "PoE box: Radxa stack first, then seat")
    ex_x, ex_y, ex_w = 380, 318, 194
    picture(out / "exploded.png", ex_x, ex_y, ex_w)
    for label, key, yy in [
        ("PoE lid last", "lid", 640),
        ("Complete stack", "stack", 470),
        ("BASE", "base", 336),
    ]:
        xx, py = projected(poe_points[key], ex_x, ex_y, ex_w, (1000, 1500))
        text(ex_x - 20, yy, label, 8.5, True)
        arrow(
            ex_x - 20 + c.stringWidth(label, "Helvetica-Bold", 8.5) + 3,
            yy + 3,
            xx + 2,
            py,
        )
    y = step(
        "poe-stack",
        "4  Assemble the Radxa stack outside the case",
        692,
        width=316,
        size=10,
    )
    y = step("poe-seat", "5  Lower the stack into a base", y, width=316, size=10)
    text(38, 300, "B-B / PoE retention / X = 14", 10.5, True)
    text(38, 288, "Y horizontal (USB-C side left), Z up", 8.5)
    bounds_b = (-4.5, 33.5, 0, hp + 0.5)
    origin_b = (46, 60)
    scale_b = 5.3
    for mesh, style in [(base, 0), (poe_lid, 1), (sbc, -1), (hat, -1)]:
        section(mesh, 14.0, bounds_b, origin_b, scale_b, style, plane="x")
    frame(bounds_b, origin_b, scale_b)
    right = origin_b[0] + (bounds_b[1] - bounds_b[0]) * scale_b + 8
    callout(
        bounds_b,
        origin_b,
        scale_b,
        0.2,
        p["poe_fin_bottom_z_mm"],
        "rib, 0.4 over HAT",
        right,
        236,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        29.3,
        p["poe_fin_bottom_z_mm"],
        "rear rib",
        right,
        214,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        2.0,
        p["pcb_support_z_mm"] - 1.0,
        "front pad",
        right,
        122,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        29.6,
        p["pcb_support_z_mm"] - 0.6,
        "rear lip pad",
        right,
        100,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        30.4,
        p["lip_top_z_mm"] - 1.0,
        "lip under lid wall",
        right,
        78,
    )
    para(
        (
            "Actual cut through base, PoE lid, SBC and HAT meshes; board meshes "
            "are drawn as cut lines. Ribs bottom at Z = "
            f"{p['poe_fin_bottom_z_mm']:g}, 0.4 above the HAT top edge; the SBC "
            f"underside rests on pads at Z = {p['pcb_support_z_mm']:g}. Ribs limit"
            " lift only; they do not clamp the boards or load the pillars."
        ),
        290,
        x=380,
        width=194,
        size=9,
        leading=11.5,
    )
    para(
        "Fan is a reference feature; the ordered HAT may be fanless. Radxa assembly:",
        168,
        x=380,
        width=194,
        size=9,
        leading=11.5,
    )
    text(380, 145, "docs.radxa.com/en/zero/zero3/", 8.5)
    text(380, 134, "accessories/3e-poe-hat", 8.5)
    c.linkURL(
        "https://docs.radxa.com/en/zero/zero3/accessories/3e-poe-hat",
        (380, 140, 574, 160),
        relative=0,
    )
    footer()
    c.showPage()

    # Page 4 ------------------------------------------------------------------
    heading(4, "Close freely; screw from below")
    y = step("poe-close", "6  Close, hold together, invert, fasten", 692)
    text(38, 580, "A-A / case screw / Y = 1.2 / X horizontal, Z up", 11, True)
    bounds_a = (-9, 0.5, 0, 14)
    origin_a = (50, 300)
    scale_a = 18
    for mesh, style in [(base, 0), (poe_lid, 1), (inserts[0], 2), (screws[0], 2)]:
        section(mesh, 1.2, bounds_a, origin_a, scale_a, style)
    frame(bounds_a, origin_a, scale_a)
    labels = sorted(
        [
            (p["pilot_end_z_mm"], "blind pilot end"),
            (p["insert_top_z_mm"], "insert top"),
            (p["head_bearing_z_mm"] + 8, "screw tip"),
            (p["insert_bottom_z_mm"], "insert bottom"),
            (p["post_contact_z_mm"], "post seat (hard stop)"),
            (p["seam_z_mm"], "base rim / seam"),
            (p["head_bearing_z_mm"], "head bearing shelves"),
            (p["head_bearing_z_mm"] - 3, "head bottom (0.4 recess)"),
        ]
    )
    edge = origin_a[0] + (bounds_a[1] - bounds_a[0]) * scale_a
    placed = []
    for z, _label in labels:
        ty = origin_a[1] + z * scale_a - 3
        if placed and ty < placed[-1] + 12:
            ty = placed[-1] + 12
        placed.append(ty)
    shift_down = max(0, placed[-1] - (origin_a[1] + 14 * scale_a))
    for (z, label), ty in zip(labels, placed, strict=True):
        ty -= shift_down
        text(244, ty, f"{z:5.1f}  {label}", 9)
        zy = origin_a[1] + z * scale_a
        c.setLineWidth(0.8)
        c.line(edge - 6, zy, edge + 4, zy)
        c.setLineWidth(0.3)
        c.setDash(2, 2)
        c.line(edge + 4, zy, 240, ty + 3)
        c.setDash()
    arrow(136, 258, 136, 294)
    text(56, 246, "Driver from base underside", 9.5, True)
    y = para(
        (
            f"Four M3 x 8 per box. Head bears at Z = {p['head_bearing_z_mm']:g} on"
            " two bridged shelves, 0.4 above the bottom face. Full 5.7 insert "
            f"overlap; the tip ends {tip_beyond:.1f} beyond the insert with "
            f"{tip_reserve:.1f} blind-pilot reserve."
        ),
        552,
        x=404,
        width=170,
        size=9.5,
        leading=12,
    )
    y = para(
        (
            "Load path: head -> bridged shelf fused into the solid base post "
            f"ring ({p['fused_layers_above_bridges']} full layers above two "
            f"bridge layers) -> post seat at Z = {p['post_contact_z_mm']:g}, "
            "about 29 mm2 -> lid post -> insert -> screw. The outer seam keeps "
            "its shadow gap; neither PCB is in this clamp stack."
        ),
        y - 4,
        x=404,
        width=170,
        size=9.5,
        leading=12,
    )
    para(
        (
            "Hand-tight only: no torque is validated. Start all four screws "
            "before snugging opposite corners. Stop for a proud head, spinning "
            "insert, a shelf or post ring that turns or separates, a bowed case"
            " or changed PCB support."
        ),
        y - 4,
        x=404,
        width=170,
        size=9.5,
        leading=12,
    )
    y = para(
        (
            "Section is an actual cut through the released base, PoE lid, screw "
            "and insert STL meshes. Base and lid have opposing hatches; metal "
            "has close hatching. The bare box uses the identical joint."
        ),
        218,
        size=10,
    )
    para(
        (
            "Hold both halves together and turn the case upside down so the "
            "driver is above the four bottom wells. The bottom face is flat; "
            "screw heads sit below it."
        ),
        y - 4,
        size=10,
    )
    footer()
    c.showPage()

    # Page 5 ------------------------------------------------------------------
    heading(5, "Computer-side box: bare board")
    ex_x, ex_y, ex_w = 392, 380, 182
    picture(ev / "bare-exploded.png", ex_x, ex_y, ex_w)
    for label, key, yy in [
        ("Bare lid", "lid", 650),
        ("ZERO 3E", "sbc", 520),
        ("BASE", "base", 400),
    ]:
        xx, py = projected(bare_points[key], ex_x, ex_y, ex_w, (1000, 1200))
        text(ex_x - 30, yy, label, 8.5, True)
        arrow(
            ex_x - 30 + c.stringWidth(label, "Helvetica-Bold", 8.5) + 3,
            yy + 3,
            xx + 2,
            py,
        )
    y = step(
        "bare-seat",
        "7  Seat the bare board in the second base",
        692,
        width=316,
        size=10,
    )
    y = step("bare-close", "8  Close and fasten like page 4", y, width=316, size=10)
    text(38, 352, "C-C / bare retention / X = 30.5", 10.5, True)
    text(38, 340, "Y horizontal (USB-C side left), Z up", 8.5)
    bounds_c = (-4.5, 33.5, 0, hb + 0.5)
    origin_c = (46, 172)
    scale_c = 8
    for mesh, style in [(base, 0), (bare_lid, 1), (sbc, -1)]:
        section(mesh, 30.5, bounds_c, origin_c, scale_c, style, plane="x")
    frame(bounds_c, origin_c, scale_c)
    right = origin_c[0] + (bounds_c[1] - bounds_c[0]) * scale_c + 8
    callout(
        bounds_c,
        origin_c,
        scale_c,
        0.2,
        p["bare_fin_bottom_z_mm"],
        "rib, 0.4 over PCB",
        right,
        300,
    )
    callout(
        bounds_c,
        origin_c,
        scale_c,
        2.0,
        p["pcb_support_z_mm"] - 1.0,
        "front pad",
        right,
        230,
    )
    callout(
        bounds_c,
        origin_c,
        scale_c,
        29.6,
        p["pcb_support_z_mm"] - 0.6,
        "rear lip pad",
        right,
        208,
    )
    callout(bounds_c, origin_c, scale_c, 31.5, 12.0, "vent slot", right, 270)
    para(
        (
            "Actual cut through base, bare lid and official SBC mesh. Ribs at X ="
            f" {ribs} all stop at Z = {p['bare_fin_bottom_z_mm']:g} over "
            "component-free bare PCB edges, clear of the mounting-hole pads. The "
            "base is identical to the PoE box base."
        ),
        150,
        size=9.5,
        leading=12,
    )
    para(
        (
            "Power and data: the computer's USB-C cable powers this box. Mounting"
            " holes stay empty; no peg, screw or metal reaches them."
        ),
        98,
        size=9.5,
        leading=12,
    )
    footer()
    c.showPage()

    # Page 6 ------------------------------------------------------------------
    heading(6, "Service and first-article checks")
    y = step("service", "9  Power off; reverse the installation order", 692, size=10)
    text(
        38,
        y - 2,
        "All physical checks below: NOT RUN. Complete after parts arrive.",
        11,
        True,
    )
    y = step("fit-test", "A  Inspect unpowered fit and cable access", y - 22, size=10)
    y = step("cycles", "B  Prove the service sequence ten times", y, size=10)
    y = step("thermal", "C  Compare open and closed temperatures", y, size=10)
    y = para(
        (
            "Air paths: low side inlets that stay open on a desk, side rows "
            "between and above the boards, and roof slots. Vent area and roof "
            "clearance do not establish cooling performance or a continuous "
            "power rating. Record fan presence on the PoE HAT."
        ),
        y,
        size=9.5,
        leading=12,
    )
    text(38, y - 6, "Record before deciding to use these enclosures", 11, True)
    for i, line in enumerate(
        [
            "Date / material / measured stock: " + "_" * 39,
            "Delivered board + HAT / fan / board gap / fit + cables: " + "_" * 20,
            "Cycles / seam gap / play / inserts / failures: " + "_" * 28,
            "Ambient / workload / open vs closed temperatures / time: " + "_" * 16,
            "Outcome / incomplete checks / next design change: " + "_" * 23,
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
    for index, rect in [(3, (38, 200, 574, 556)), (2, (38, 476, 380, 740))]:
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
        "schema": "yoke.r7-guide-actions/1",
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
            "scope": (
                "Nominal compact PoE and bare housing first articles; physical "
                "and powered verification pending delivered electronics"
            ),
            "procedure": [s["instruction"] for s in STEPS if s["kind"] == "test"],
            "stop_conditions": [s["stop"] for s in STEPS],
            "observations": [
                "Actual stock, delivered electronics identity and measured HAT gap",
                "Unpowered support, rib play, cable seating and repeated service",
                (
                    "Open/closed ambient, CPU and case temperatures with fan state, "
                    "power source and workload"
                ),
            ],
            "acceptance_limits": [
                "Each empty lid seats and lifts by hand with no screw force",
                "Seam shadow gap 0.15 to 0.45 mm on every side after snugging",
                (
                    "Measured SBC-top to HAT-underside gap 17.7 to 18.3 mm; "
                    "otherwise set hat_gap and regenerate only the PoE lid"
                ),
                "Screw heads at or below the floor face; no shelf or post movement",
                "Both USB-C plugs and the RJ45 plug seat fully with the lid fastened",
                "Case surface at most 45 C (provisional PLA first-article limit)",
                "No throttling or reset under the stated workload",
            ],
        },
        "tools": {
            "caliper": "Caliper and depth gauge for stock and pilots",
            "driver": "2.5 mm hex driver for M3 x 8 and the Radxa driver",
            "heater": "Controlled insert heater and matching tip, maker's method",
            "support": "Flat nonflammable support",
            "cables": "Actual Ethernet and USB-C cables with overmolds",
            "monitor": "CPU telemetry and case surface temperature probe",
        },
    }
    evidence = {
        "schema": "yoke.r7-guide/1",
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
