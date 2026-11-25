#!/usr/bin/env python3
"""Render the six-page R8 nominal first-article guide from its exact STL meshes.

Two hardened compact housings on one base design: a PoE box (ZERO 3E + RA023
HAT at official 14.5 mm copper-pillar spacing, device side) and a bare box
(ZERO 3E only, computer side). No printer I/O. Physical stock, fit, service
and powered cooling remain NOT RUN.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pymupdf as fitz
import trimesh
from PIL import Image, ImageDraw
from reportlab.lib.colors import black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
SIDE_BY_SIDE = (98, 0, 0)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def moved(mesh: trimesh.Trimesh, delta: tuple[float, float, float]) -> trimesh.Trimesh:
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
        adjacency: dict[tuple[int, int], list[int]] = {}
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
        a, b, c_pt = triangle
        denominator = (b[1] - c_pt[1]) * (a[0] - c_pt[0]) + (c_pt[0] - b[0]) * (
            a[1] - c_pt[1]
        )
        if abs(denominator) < 1e-8:
            continue
        xx, yy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        u = ((b[1] - c_pt[1]) * (xx - c_pt[0]) + (c_pt[0] - b[0]) * (yy - c_pt[1])) / denominator
        v = ((c_pt[1] - a[1]) * (xx - c_pt[0]) + (a[0] - c_pt[0]) * (yy - c_pt[1])) / denominator
        z = u * a[2] + v * b[2] + (1 - u - v) * c_pt[2]
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
            "Count four printed parts: two identical bases (YOKE R8 BASE under "
            "the floor), the PoE lid (YOKE R8 POE) and the bare lid (YOKE R8 "
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
            "three inner lip bars by hand and seat flat on the four corner post "
            "rings (Z = 4.6), leaving an even 0.3 shadow gap along the seam; "
            "then lift off freely. Check the driver in all eight bottom wells "
            "and the heater tip in all eight lid pilots."
        ),
        "stop": (
            "Stop for binding, rocking, skew or corner wall splits. Screws "
            "must never pull a poor fit closed."
        ),
    },
    {
        "id": "inserts",
        "page": 2,
        "kind": "assembly",
        "instruction": (
            "Support each EMPTY lid roof-down on a flat nonflammable surface. "
            "Heat-set one RX-M3x5.7 insert into the chamfered pilot of each of "
            "the four corner posts, straight down, finishing 0.2 below the flat "
            "post face. Follow the insert maker's controlled-heater method. "
            "Cool fully; inspect squareness, threads and outer wall finish."
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
            "PoE box (device side). Disconnect all power and cables. Outside "
            "the case, fit the microSD card, then follow Radxa's instructions: "
            "four lower CM2.5x4 screws into the four M2.5x14.5 insulated copper "
            "pillars, HAT pressed fully onto GPIO, four upper CM2.5x4 screws. "
            "Tighten as Radxa requires. These conductive joints carry live "
            "48-57 V PoE and stay installed."
        ),
        "stop": (
            "Stop if measured facing PCB gap is outside 14.2-14.8 mm, or if "
            "pillars, heat-shrink insulation or screws differ from Radxa's spec."
        ),
    },
    {
        "id": "poe-seat",
        "page": 3,
        "kind": "assembly",
        "instruction": (
            "Lower the complete stack level into one base: RJ45 between the two "
            "LAN-end posts and into the low cradle, USB-C toward the open side. "
            "The SBC underside rests on three front pads and three rear-lip "
            "pads; no factory screw or pillar touches plastic. Verify solid "
            "base walls stay clear of all four corner mounts."
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
            "Its four flat post faces seat on the four base post rings (34.9 "
            "mm2 each) with an even 0.3 shadow gap; its four inner ribs stop "
            "0.4 above the HAT edges. Hold both halves together, invert, start "
            "all four M3 x 8 screws from the chamfered bottom wells, then "
            "lightly snug opposite corners. Return floor-down."
        ),
        "stop": (
            "Stop for binding, roof or rib contact, cross-threading, spinning "
            "inserts or proud heads, or if a head shelf or post ring turns, "
            "cracks or separates while snugging. Never force a gap closed."
        ),
    },
    {
        "id": "bare-seat",
        "page": 5,
        "kind": "assembly",
        "instruction": (
            "Bare box (computer side, no HAT). Disconnect all cables. Fit "
            "microSD outside the case. Lower the bare ZERO 3E level into the "
            "second base: RJ45 between the LAN-end posts, USB-C toward the open "
            "side, PCB on the six edge pads. Leave all four corner mounting "
            "holes empty."
        ),
        "stop": (
            "Stop for rocking, a bowed PCB or anything entering a mounting "
            "hole; those holes connect directly to the RJ45 PoE centre taps."
        ),
    },
    {
        "id": "bare-close",
        "page": 5,
        "kind": "assembly",
        "instruction": (
            "Match LAN ends and lower the bare lid straight down. Its four "
            "ribs stop 0.4 above component-free PCB edges; solid roof plastic "
            "shrouds the 40-pin GPIO header and all four corner mounts. Hold "
            "together, invert, fit four M3 x 8 screws from below, lightly snug "
            "opposite corners."
        ),
        "stop": (
            "Stop for binding, rib or roof contact with the board or GPIO pins, "
            "cross-threading, proud heads, or any shelf or post movement."
        ),
    },
    {
        "id": "service",
        "page": 6,
        "kind": "use",
        "instruction": (
            "Disconnect power and all cables. Never connect both PoE and "
            "USB-C power simultaneously on a PoE-equipped board. Hold the case "
            "inverted, remove the four bottom M3 x 8 screws, return floor-down "
            "and lift the lid straight up. Lift the board (or PoE stack) level "
            "out of the base; change microSD outside. Reassemble per pages 3-5."
        ),
        "stop": (
            "Do not pry on a PCB or connector. Do not loosen the Radxa "
            "conductive PoE joints merely to open the case."
        ),
    },
    {
        "id": "fit-test",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Unpowered: record stock measurements, then accept only if the seam "
            "gap is 0.15-0.45 on every side, the measured HAT gap is 14.2-14.8 "
            "(else adjust hat_gap and reprint only the PoE lid), heads sit at "
            "or below floor level, ribs clear boards, and actual Ethernet (top "
            "latch) and USB-C (USB3 + OTG) plugs seat fully."
        ),
        "stop": (
            "Stop for forced plugs, partly seated USB-C, cable strain, loose "
            "inserts, PCB bowing, exposed live hardware or component contact."
        ),
    },
    {
        "id": "cycles",
        "page": 6,
        "kind": "test",
        "instruction": (
            "Unpowered and unplugged, perform ten complete service cycles on "
            "each box, including board removal and microSD exchange. Each lid "
            "must seat freely before screws. Record seam gap, thread condition, "
            "insert stability and PCB play."
        ),
        "stop": (
            "Stop at the first new bind, damaged thread, loose insert or "
            "cracked plastic; return that interface to design."
        ),
    },
    {
        "id": "thermal",
        "page": 6,
        "kind": "test",
        "instruction": (
            "After unpowered checks pass, run each box on a hard flat desk with "
            "its real power source (active 802.3af/at PoE for PoE box, computer "
            "USB-C OTG for bare box) and workload. Every 5 minutes record "
            "ambient, CPU and case temperatures, throttling and resets; compare "
            "open vs closed for >= 30 min until three readings change < 1 C."
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
    parser.add_argument("--sbc-ref", type=Path, required=True)
    parser.add_argument("--hat-ref", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--dpi", type=int, default=150)
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
    sbc = trimesh.load(args.sbc_ref, force="mesh", process=True)
    sbc.apply_transform(p["board_matrix"])
    hat = trimesh.load(args.hat_ref, force="mesh", process=True)
    hat.apply_transform(p["board_matrix"])

    bz = p["board_top_z_mm"]
    hp, hb = p["poe_height_mm"], p["bare_height_mm"]
    tip = p["head_bearing_z_mm"] + p["screw_length_mm"]
    tip_beyond = tip - p["insert_top_z_mm"]
    tip_reserve = p["pilot_end_z_mm"] - tip
    ribs = ", ".join(f"{f[0]:g}-{f[1]:g}" for f in p["bare_fins_xy_mm"])
    body = p["body_bounds_xy_mm"]
    ax0 = p["screw_axes_mm"][0]

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
            "poe": (20, body[3], hp),
            "bare": (SIDE_BY_SIDE[0] + 40, body[3], hb),
            "lan": (body[0], 15, 9),
            "usb": (SIDE_BY_SIDE[0] + 47, body[2], 8),
        },
    )
    poe_points = render(
        [
            base,
            moved(sbc, (0, 0, 28)),
            moved(hat, (0, 0, 28)),
            moved(poe_lid, (0, 0, 96)),
        ],
        out / "exploded.png",
        {
            "base": (body[1], body[3], 3),
            "stack": (-6.4, 15, bz + 28 + 3),
            "lid": (body[0], body[3], hp + 88),
        },
        size=(1000, 1500),
    )
    bare_points = render(
        [base, moved(sbc, (0, 0, 22)), moved(bare_lid, (0, 0, 52))],
        ev / "bare-exploded.png",
        {
            "base": (body[1], body[3], 3),
            "sbc": (-6.4, 15, bz + 22 + 3),
            "lid": (body[0], body[3], hb + 45),
        },
        size=(1000, 1200),
    )
    heat_points = render(
        [
            base,
            moved(meshes["poe-lid"], (0, 48, 0)),
            moved(meshes["bare-lid"], (96, 48, 0)),
        ],
        ev / "empty-parts.png",
        {
            "poe": (ax0[0], 48 + body[2] + body[3] - ax0[1], hp - p["post_contact_z_mm"]),
            "bare": (96 + ax0[0], 48 + body[2] + body[3] - ax0[1], hb - p["post_contact_z_mm"]),
            "base": (ax0[0], ax0[1], p["post_contact_z_mm"]),
            "poe_label": (20, 48 + body[3], hp - 4.6),
            "bare_label": (96 + 30, 48 + body[3], hb - 4.6),
            "base_label": (body[0], 0, 0),
        },
    )

    c = canvas.Canvas(str(out / "assembly.pdf"), pagesize=(612, 792), invariant=1)
    c.setTitle("Yoke R8 hardened compact housings - nominal first article assembly")
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
        text(38, 754, f"YOKE / R8 COMPACT HOUSINGS / {number} OF 6", 10, True)
        text(38, 725, title, 21.5, True)
        c.setLineWidth(0.6)
        c.line(38, 711, 574, 711)

    def footer():
        c.setLineWidth(0.4)
        c.line(38, 52, 574, 52)
        text(
            38,
            37,
            "Nominal first article (R8). Physical fit, durability and powered cooling: NOT RUN.",
            8.5,
        )
        text(
            38,
            24,
            "Dimensions in mm, Z from base underside. Actual exported STL sections; nominal board/hardware envelopes.",
            8.0,
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

    def step(identifier, title, y, x=38, width=536, size=10.2):
        item = next(item for item in STEPS if item["id"] == identifier)
        text(x, y, title, 11.2, True)
        y = para(item["instruction"], y - 16, x, width, size, leading=13.5)
        y = para(item["stop"], y - 1, x, width, 9.5, leading=12.5)
        return y - 9

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
            pts = (contour[:, axes_2d] - [lo, zlo]) * scale + origin
            path.moveTo(*pts[0])
            for pt in pts[1:]:
                path.lineTo(*pt)
            if style >= 0:
                path.close()
        if style < 0:
            # Nominal reference envelopes shown as distinct dashed/thin outlines.
            c.setLineWidth(0.7)
            c.setStrokeColor(black)
            c.setDash(3, 2)
            c.drawPath(path, fill=0, stroke=1)
            c.setDash()
            c.restoreState()
            return
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

    def frame(bounds, origin, scale):
        lo, hi, zlo, zhi = bounds
        c.setLineWidth(0.4)
        c.rect(*origin, (hi - lo) * scale, (zhi - zlo) * scale)

    def callout(bounds, origin, scale, horizontal, z, label, tx, ty):
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
            "Two hardened compact boxes for Radxa ZERO 3E 2GB WITH GPIO "
            "(B0CZD1C54N). Device side: ZERO 3E + Radxa RA023 PoE HAT "
            "(B0D1KF1L7G, 14.5 mm copper pillars), powered via active 802.3af/at "
            "PoE. Computer side: bare ZERO 3E, powered from host USB-C OTG "
            "(never mix PoE and USB-C power). Physical fit remains NOT RUN."
        ),
        692,
    )
    text(38, y - 6, f"PoE box {env_poe}   |   Bare box {env_bare}", 11, True)
    pic_w = 420
    pic_x, pic_y = 95, y - 22 - pic_w * 1000 / 1600
    picture(out / "preview.png", pic_x, pic_y, pic_w)
    for key, label, sub, dx, dy in [
        ("poe", "PoE box", "device side", -10, 32),
        ("bare", "Bare box", "computer side", -10, 32),
    ]:
        px, py = projected(overview[key], pic_x, pic_y, pic_w)
        text(px + dx, py + dy, label, 10, True)
        text(px + dx, py + dy - 12, sub, 9)
        arrow(px + dx + 18, py + dy - 16, px, py + 2)
    px, py = projected(overview["lan"], pic_x, pic_y, pic_w)
    text(38, py - 32, "LAN end", 10, True)
    arrow(72, py - 24, px - 2, py - 2)
    px, py = projected(overview["usb"], pic_x, pic_y, pic_w)
    text(px - 60, pic_y + 12, "USB3 (inner) + OTG (outer) side bay", 9, True)
    arrow(px + 4, pic_y + 23, px + 1, py - 3)
    text(
        38,
        pic_y - 14,
        "Opposite end closed. HDMI covered. Roof recessed labels: USB3 / OTG / <LAN. microSD internal.",
        9.6,
        True,
    )
    y = step("stock", "1  Inventory and measure owner stock", pic_y - 36)
    y = para(
        (
            "Nominal screws: M3 x 8 socket head (8), measured under head; head "
            "at most dia 6.0 x height 3.0. Inserts: RX-M3x5.7 (8), dia 4.6 x "
            "length 5.7, printed pilots dia 4.0 with 0.46 lead-in. Four of each "
            "per box. No clips, standoffs or nylon screws are used."
        ),
        y,
        size=10,
    )
    y = para(
        (
            "PoE box only: Radxa's 4 M2.5x14.5 insulated copper pillars and 8 "
            "CM2.5x4 screws. They carry live 48-57 V PoE voltage: keep them "
            "installed, tightened and insulated. Solid plastic shrouds all four "
            "corner mounts on every printed part; vents stay >= 6.4 mm clear."
        ),
        y - 3,
        size=10,
    )
    y = para(
        (
            "Tools: caliper/depth gauge, 2.5 mm hex driver reaching 20 mm, "
            "Radxa screw driver, controlled insert heater/tip, nonflammable "
            "support, actual cables, temperature probe and CPU monitoring."
        ),
        y - 3,
        size=10,
    )
    para(
        (
            "R8 geometry notes: uniform 2.70 mm outer walls, zero corner seat "
            "recess (flat seat at Z = 4.6), solid perimeter-forced insert bosses, "
            "14.5 mm HAT spacing (3.0 mm roof clearance), 15 x 10 mm USB-C bay "
            "and +1.55 mm RJ45 latch clearance. Update hat_gap if HAT differs."
        ),
        y - 3,
        size=9.5,
        leading=12.5,
    )
    footer()
    c.showPage()

    # Page 2 ------------------------------------------------------------------
    heading(2, "Fit empty; heat-set eight inserts")
    y = step("empty-fit", "2  Test each empty lid on a base first", 692)
    y = step("inserts", "3  Heat only the empty lids, roof down", y)
    pic_w = 460
    pic_h = pic_w * 1000 / 1600
    pic_x, pic_y = 74, y - 12 - pic_h
    picture(ev / "empty-parts.png", pic_x, pic_y, pic_w)
    px, py = projected(heat_points["poe_label"], pic_x, pic_y, pic_w)
    text(px - 36, py + 24, "POE LID, roof down", 10, True)
    text(pic_x + pic_w - 128, pic_y + pic_h + 2, "BARE LID, roof down", 10, True)
    px, py = projected(heat_points["base_label"], pic_x, pic_y, pic_w)
    text(40, py + 56, "BASE, floor down", 10, True)
    for key in ("poe", "bare"):
        px, py = projected(heat_points[key], pic_x, pic_y, pic_w)
        arrow(px, py + 38, px, py + 4)
    px, py = projected(heat_points["base"], pic_x, pic_y, pic_w)
    text(40, py - 14, "flat post ring seat (1 of 4)", 9, True)
    arrow(
        40 + c.stringWidth("flat post ring seat (1 of 4)", "Helvetica-Bold", 9) + 3,
        py - 11,
        px - 2,
        py,
    )
    y = para(
        (
            "Vertical arrows: one of the four corner post pilots (dia 4.0) in "
            "each lid; set one RX-M3x5.7 insert into each. Closed, each insert "
            f"spans Z = {p['insert_bottom_z_mm']:g} to {p['insert_top_z_mm']:g} "
            f"with >= 1.6 mm nominal surrounding wall; the flat lid post face at "
            f"Z = {p['post_contact_z_mm']:g} bears directly on the base post "
            "ring. Cool fully before threading screws or adding electronics."
        ),
        pic_y - 12,
        size=10,
    )
    y = para(
        (
            f"Nesting joint: lid walls slide vertically over three {p['lip_thickness_mm']:g} "
            f"mm inner lip bars (front, rear, closed end; clear of USB-C bay "
            f"and all 4 corner mounts) with {p['lip_per_side_clearance_mm']:g} "
            f"mm clearance per side, {p['lip_engagement_mm']:g} mm engagement "
            f"and {p['lip_entry_chamfer_mm']:g} mm bevel lead-in. Lower and "
            "lift straight vertically; there is no hook, clip or sideways slide."
        ),
        y - 3,
        size=10,
    )
    para(
        (
            f"Each solid base post ring (dia {p['base_post_ring_diameter_mm']:g}, "
            "34.9 mm2 flat contact) stops the lid at Z = 4.6, leaving a clean "
            f"{p['seam_gap_mm']:g} mm outer shadow gap above the Z = {p['seam_z_mm']:g} "
            "base rim with continuous 2.70 mm corner walls (zero recess cut). "
            "Repeat the empty fit after cooling; never force a tilted insert."
        ),
        y - 3,
        size=10,
    )
    footer()
    c.showPage()

    # Page 3 ------------------------------------------------------------------
    heading(3, "PoE box: Radxa stack first, then seat")
    ex_x, ex_y, ex_w = 382, 326, 190
    picture(out / "exploded.png", ex_x, ex_y, ex_w)
    for label, key, yy in [
        ("PoE lid last", "lid", 640),
        ("Complete stack", "stack", 472),
        ("BASE", "base", 342),
    ]:
        xx, py = projected(poe_points[key], ex_x, ex_y, ex_w, (1000, 1500))
        text(ex_x - 24, yy, label, 8.5, True)
        arrow(
            ex_x - 24 + c.stringWidth(label, "Helvetica-Bold", 8.5) + 3,
            yy + 3,
            xx + 2,
            py,
        )
    y = step(
        "poe-stack",
        "4  Assemble the Radxa stack outside the case",
        692,
        width=314,
        size=9.8,
    )
    y = step("poe-seat", "5  Lower the stack into a base", y, width=314, size=9.8)
    text(38, 304, "B-B / PoE retention / X = 12.5", 10.5, True)
    text(38, 292, "Y horizontal (USB-C side left), Z up; dashed = nominal boards", 8.3)
    bounds_b = (-5.2, 34.0, 0, hp + 0.5)
    origin_b = (46, 66)
    scale_b = 5.2
    for mesh, style in [(base, 0), (poe_lid, 1), (sbc, -1), (hat, -1)]:
        section(mesh, 12.5, bounds_b, origin_b, scale_b, style, plane="x")
    frame(bounds_b, origin_b, scale_b)
    right = origin_b[0] + (bounds_b[1] - bounds_b[0]) * scale_b + 8
    callout(
        bounds_b,
        origin_b,
        scale_b,
        0.2,
        p["poe_fin_bottom_z_mm"],
        "front rib (0.4 over HAT)",
        right,
        242,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        29.2,
        p["poe_fin_bottom_z_mm"],
        "rear rib",
        right,
        218,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        2.5,
        p["pcb_support_z_mm"] - 1.0,
        "front pad",
        right,
        130,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        29.5,
        p["pcb_support_z_mm"] - 0.6,
        "rear lip pad",
        right,
        108,
    )
    callout(
        bounds_b,
        origin_b,
        scale_b,
        30.0,
        p["lip_top_z_mm"] - 0.6,
        "inner lip bar",
        right,
        86,
    )
    para(
        (
            "Actual cut through R8 base and PoE lid STLs with dashed nominal "
            "ZERO 3E + RA023 (14.5 mm gap) envelopes. Ribs bottom at Z = "
            f"{p['poe_fin_bottom_z_mm']:g} (0.4 above HAT top); SBC underside "
            f"rests on pads at Z = {p['pcb_support_z_mm']:g}; roof clears "
            f"tallest HAT envelope by {p['poe_roof_clearance_mm']:g} mm."
        ),
        292,
        x=382,
        width=192,
        size=8.8,
        leading=11.2,
    )
    para(
        (
            "Ribs limit lift only; they never clamp PCBs or load the live PoE "
            "pillars. Fan is optional inside the 14.5 mm inter-board gap. "
            "Radxa HAT assembly reference:"
        ),
        172,
        x=382,
        width=192,
        size=8.8,
        leading=11.2,
    )
    text(382, 124, "docs.radxa.com/en/zero/zero3/", 8.3)
    text(382, 113, "accessories/3e-poe-hat", 8.3)
    c.linkURL(
        "https://docs.radxa.com/en/zero/zero3/accessories/3e-poe-hat",
        (382, 110, 574, 134),
        relative=0,
    )
    footer()
    c.showPage()

    # Page 4 ------------------------------------------------------------------
    heading(4, "Close freely; screw from below")
    y = step("poe-close", "6  Close, hold together, invert, fasten", 692)
    text(38, 578, f"A-A / case screw / Y = {ax0[1]:g} / X horizontal, Z up", 11, True)
    bounds_a = (-10.2, 0.5, 0, 14)
    origin_a = (46, 298)
    scale_a = 17.2
    for mesh, style in [(base, 0), (poe_lid, 1), (inserts[0], 2), (screws[0], 2)]:
        section(mesh, ax0[1], bounds_a, origin_a, scale_a, style)
    frame(bounds_a, origin_a, scale_a)
    labels = sorted(
        [
            (p["pilot_end_z_mm"], "blind pilot end"),
            (p["insert_top_z_mm"], "insert top"),
            (p["head_bearing_z_mm"] + 8, "screw tip"),
            (p["insert_bottom_z_mm"], "insert bottom"),
            (p["post_contact_z_mm"], "flat post seat (hard stop)"),
            (p["seam_z_mm"], "base rim / 0.3 shadow gap"),
            (p["head_bearing_z_mm"], "head bearing shelves"),
            (p["head_bearing_z_mm"] - 3, "head bottom (0.4 recess)"),
        ]
    )
    edge = origin_a[0] + (bounds_a[1] - bounds_a[0]) * scale_a
    placed: list[float] = []
    for z, _label in labels:
        ty = origin_a[1] + z * scale_a - 3
        if placed and ty < placed[-1] + 12:
            ty = placed[-1] + 12
        placed.append(ty)
    shift_down = max(0.0, placed[-1] - (origin_a[1] + 14 * scale_a))
    for (z, label), ty in zip(labels, placed, strict=True):
        ty -= shift_down
        text(244, ty, f"{z:5.1f}  {label}", 8.8)
        zy = origin_a[1] + z * scale_a
        c.setLineWidth(0.8)
        c.line(edge - 6, zy, edge + 4, zy)
        c.setLineWidth(0.3)
        c.setDash(2, 2)
        c.line(edge + 4, zy, 240, ty + 3)
        c.setDash()
    arrow(138, 256, 138, 292)
    text(52, 244, "2.5 mm hex driver from base underside", 9.2, True)
    y = para(
        (
            f"Four M3 x 8 per box. Head bears at Z = {p['head_bearing_z_mm']:g} "
            "on two bridged shelves inside a chamfered dia 6.3 well (>= 1.1 mm "
            "360-deg wall, >= 1.85 mm exterior wall). Full 5.7 insert overlap; "
            f"tip ends {tip_beyond:.1f} beyond insert with {tip_reserve:.1f} "
            "blind-pilot reserve."
        ),
        552,
        x=404,
        width=170,
        size=9.3,
        leading=11.8,
    )
    y = para(
        (
            "Load path: head -> bridged shelf fused into the solid dia 8.5 "
            f"base post ring ({p['fused_layers_above_bridges']} solid layers "
            f"above two 25 mm/s bridge layers) -> flat seat at Z = "
            f"{p['post_contact_z_mm']:g} (34.9 mm2/post) -> solid lid insert "
            "boss -> insert -> screw. Neither PCB is in this clamp stack."
        ),
        y - 4,
        x=404,
        width=170,
        size=9.3,
        leading=11.8,
    )
    para(
        (
            "Hand-tight only: no torque is validated. Start all four screws "
            "before snugging opposite corners. Stop for a proud head, spinning "
            "insert, shelf or post movement, bowed case or changed PCB support."
        ),
        y - 4,
        x=404,
        width=170,
        size=9.3,
        leading=11.8,
    )
    y = para(
        (
            "Section is an actual cut through the released R8 base, PoE lid, "
            "screw and insert STLs. Base and lid use opposing hatches; metal "
            "uses close hatching. Notice the full continuous outer corner wall "
            "down to Z = 4.6. The bare box uses the identical joint."
        ),
        216,
        size=9.8,
        leading=13,
    )
    para(
        (
            "Hold both halves together and invert so the driver enters the "
            "four chamfered bottom wells vertically. Screw heads sit 0.4 mm "
            "below the flat desk-facing floor."
        ),
        y - 4,
        size=9.8,
        leading=13,
    )
    footer()
    c.showPage()

    # Page 5 ------------------------------------------------------------------
    heading(5, "Computer-side box: bare board")
    ex_x, ex_y, ex_w = 392, 384, 180
    picture(ev / "bare-exploded.png", ex_x, ex_y, ex_w)
    for label, key, yy in [
        ("Bare lid", "lid", 652),
        ("ZERO 3E", "sbc", 522),
        ("BASE", "base", 404),
    ]:
        xx, py = projected(bare_points[key], ex_x, ex_y, ex_w, (1000, 1200))
        text(ex_x - 32, yy, label, 8.5, True)
        arrow(
            ex_x - 32 + c.stringWidth(label, "Helvetica-Bold", 8.5) + 3,
            yy + 3,
            xx + 2,
            py,
        )
    y = step(
        "bare-seat",
        "7  Seat the bare board in the second base",
        692,
        width=314,
        size=9.8,
    )
    y = step("bare-close", "8  Close and fasten like page 4", y, width=314, size=9.8)
    text(38, 356, "C-C / bare retention & GPIO shroud / X = 31.2", 10.5, True)
    text(38, 344, "Y horizontal (USB-C side left), Z up; dashed = nominal ZERO 3E", 8.3)
    bounds_c = (-5.2, 34.0, 0, hb + 0.5)
    origin_c = (46, 174)
    scale_c = 7.8
    for mesh, style in [(base, 0), (bare_lid, 1), (sbc, -1)]:
        section(mesh, 31.2, bounds_c, origin_c, scale_c, style, plane="x")
    frame(bounds_c, origin_c, scale_c)
    right = origin_c[0] + (bounds_c[1] - bounds_c[0]) * scale_c + 8
    callout(
        bounds_c,
        origin_c,
        scale_c,
        26.2,
        hb - 0.8,
        "solid GPIO roof shroud",
        right,
        318,
    )
    callout(
        bounds_c,
        origin_c,
        scale_c,
        0.2,
        p["bare_fin_bottom_z_mm"],
        "front rib (0.4 over PCB)",
        right,
        290,
    )
    callout(
        bounds_c,
        origin_c,
        scale_c,
        31.6,
        12.4,
        "rear side vent",
        right,
        262,
    )
    callout(
        bounds_c,
        origin_c,
        scale_c,
        2.5,
        p["pcb_support_z_mm"] - 1.0,
        "front pad",
        right,
        228,
    )
    callout(
        bounds_c,
        origin_c,
        scale_c,
        29.5,
        p["pcb_support_z_mm"] - 0.6,
        "rear lip pad",
        right,
        204,
    )
    para(
        (
            "Actual cut through R8 base and bare lid STLs with dashed nominal "
            f"ZERO 3E envelope. Ribs at X = {ribs} all stop at Z = "
            f"{p['bare_fin_bottom_z_mm']:g} over component-free PCB edges "
            "(>= 10 mm clear of all four corner mounts). Roof slots end at "
            "Y = 22.0, keeping solid plastic over the exposed 40-pin GPIO pins."
        ),
        150,
        size=9.5,
        leading=12,
    )
    para(
        (
            "Power & data: host computer connects to the outer OTG USB-C port "
            "(USB 2.0 High-Speed gadget + 5V power; the inner USB3 port is "
            "host-only on RK3566). Corner mounting holes stay empty & shrouded."
        ),
        96,
        size=9.5,
        leading=12,
    )
    footer()
    c.showPage()

    # Page 6 ------------------------------------------------------------------
    heading(6, "Service and first-article checks")
    y = step("service", "9  Power off; reverse the installation order", 692, size=9.8)
    text(
        38,
        y - 2,
        "All physical checks below: NOT RUN. Complete and record on printed parts.",
        10.8,
        True,
    )
    y = step("fit-test", "A  Inspect unpowered fit and cable access", y - 20, size=9.8)
    y = step("cycles", "B  Prove the service sequence ten times", y, size=9.8)
    y = step("thermal", "C  Compare open and closed temperatures", y, size=9.8)
    y = para(
        (
            "Air paths: desk-open low inlets (below seam), side rows and roof "
            "slots (all >= 6.4 mm clear of live 48 V mounts; bare GPIO shrouded). "
            "Geometric open area (1238 mm2 PoE / 742 mm2 bare) is NOT measured "
            "airflow or a continuous thermal rating. Record PoE HAT fan state."
        ),
        y,
        size=9.3,
        leading=11.8,
    )
    text(38, y - 5, "Record before relying on these enclosures", 10.8, True)
    for i, line in enumerate(
        [
            "Date / material / measured stock: " + "_" * 39,
            "Board + HAT / fan / measured HAT gap / fit + cables: " + "_" * 21,
            "Cycles / seam gap / play / inserts / failures: " + "_" * 27,
            "Ambient / workload / open vs closed temps / duration: " + "_" * 17,
            "Outcome / incomplete checks / next design change: " + "_" * 23,
        ]
    ):
        text(38, y - 24 - i * 18, line, 9.3)
    footer()
    c.save()

    # Render reviewed monochrome page PNGs with exact Poppler pdftoppm matching Lacuna gate.
    for old in ev.glob("page-*.png"):
        old.unlink()
    subprocess.run(
        [
            "pdftoppm",
            "-gray",
            "-r",
            str(args.dpi),
            "-png",
            str(out / "assembly.pdf"),
            str(ev / "page"),
        ],
        check=True,
    )
    rendered = sorted(
        ev.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[1])
    )
    pages = [
        {"page": idx, "path": str(path), "sha256": sha(path)}
        for idx, path in enumerate(rendered, start=1)
    ]

    # Build section.png summary for repository docs.
    pdf = fitz.open(out / "assembly.pdf")
    sections = []
    for index, rect in [(3, (38, 200, 574, 558)), (2, (38, 474, 382, 738))]:
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
        HERE / "reference_envelopes.scad",
        args.parts / "parameters.json",
        args.sbc_ref,
        args.hat_ref,
    ]
    sources.extend(args.parts / f"{name}.stl" for name in names)
    actions = {
        "schema": "yoke.r8-guide-actions/1",
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
                "Hardened compact R8 PoE and bare housing first articles; "
                "physical fit, cable seating, service cycles and powered thermal "
                "verification remain NOT RUN"
            ),
            "procedure": [s["instruction"] for s in STEPS if s["kind"] == "test"],
            "stop_conditions": [s["stop"] for s in STEPS],
            "observations": [
                "Actual fastener/insert stock measurements and measured RA023 HAT board gap",
                "Unpowered support, rib play, USB-C/RJ45 cable seating and ten service cycles",
                "Open vs closed ambient, CPU and case surface temperatures with fan state and workload",
            ],
            "acceptance_limits": [
                "Each empty lid seats and lifts by hand with no screw force",
                "Seam shadow gap 0.15 to 0.45 mm on every side after snugging",
                "Measured SBC-top to HAT-underside gap 14.2 to 14.8 mm; otherwise update hat_gap and reprint PoE lid",
                "Screw heads sit at or below the floor face; no shelf or post movement",
                "Both USB-C plugs (USB3 + OTG) and top-latch RJ45 plug seat fully with lid closed",
                "Case surface at most 45 C (provisional PLA first-article limit)",
                "No throttling or reset under sustained workload",
            ],
        },
        "tools": {
            "caliper": "Caliper and depth gauge for stock, HAT gap and pilots",
            "driver": "2.5 mm hex driver for M3 x 8 and Radxa CM2.5 driver",
            "heater": "Controlled insert heater and matching tip, maker's method",
            "support": "Flat nonflammable support",
            "cables": "Actual Ethernet and USB-C cables with overmolds",
            "monitor": "CPU telemetry and case surface temperature probe",
        },
    }
    evidence = {
        "schema": "yoke.r8-guide/1",
        "pdf_sha256": sha(out / "assembly.pdf"),
        "dpi": args.dpi,
        "pages": pages,
        "sources": [{"path": str(path), "sha256": sha(path)} for path in sources],
        "actions": STEPS,
        "readability_review": "PENDING independent rendered page inspection",
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
