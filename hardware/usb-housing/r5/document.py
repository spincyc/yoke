#!/usr/bin/env python3
"""Generate the Letter R5 build guide from the released component meshes.

The section paths and visible surfaces are cuts/projections of the STL files.
Hardware meshes are nominal envelopes, not measured stock. No printer I/O.
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
STEPS = [
    {
        "id": "identify",
        "page": 1,
        "instruction": (
            "Identify R5 B base and R5 L lid. Keep both LAN arrows at the "
            "Ethernet end. Use only the matched R5 pair and the bare board."
        ),
    },
    {
        "id": "verify-stock-tools",
        "page": 1,
        "instruction": (
            "Check four M3 x 8 socket-head screws and four RX-M3x5.7 inserts "
            "against the listed nominal envelopes. Confirm the insert stock label"
            " and measure stock before heating. Check driver and heater access "
            "with the actual tools."
        ),
        "stop": (
            "Stop if the stock identity, dimensions, pilot, or tool access "
            "differs; do not substitute hardware by label alone."
        ),
    },
    {
        "id": "empty-dry-fit",
        "page": 2,
        "instruction": (
            "With no board, inserts, or screws fitted, orient the two LAN ends "
            "together and lower the lid straight down. It must nest and seat "
            "smoothly by hand. Lift vertically to separate."
        ),
        "stop": (
            "Stop for a blocked, skewed, or forced fit. Do not pull the halves "
            "together with screws."
        ),
    },
    {
        "id": "heatset-inserts",
        "page": 2,
        "instruction": (
            "Support the EMPTY base flat on a nonflammable surface. Heat-set one "
            "checked insert vertically into each of the four pilots, flush with "
            "its boss top. Use the insert maker's method and a controlled heater."
            " Let the base cool fully. Check every insert is flush with its boss "
            "top, never proud, before installing the board."
        ),
        "stop": (
            "Stop for a tilted insert, lifted boss, crack, blocked thread, or "
            "distortion. Do not heat-set with the board installed."
        ),
    },
    {
        "id": "lower-board",
        "page": 3,
        "instruction": (
            "Disconnect power and all cables. If using microSD, install the "
            "card before seating the board. Put the bare board's Ethernet "
            "socket at the LAN end; the two USB-C sockets face the front "
            "openings. Lower the board vertically and level onto its pads and two"
            " locators. Check that all components and connectors clear."
        ),
        "stop": (
            "Stop if the board rocks, bows, catches, or rests on a component "
            "instead of the support pads."
        ),
    },
    {
        "id": "lower-lid",
        "page": 3,
        "instruction": (
            "Match the LAN ends. Lower the lid vertically over the base. The "
            "nested lip locates the lid; it does not slide or hook. Check an "
            "even, seated seam before adding screws."
        ),
        "stop": (
            "Stop if the lid binds or presses on the board. Screws must not fix a"
            " poor fit."
        ),
    },
    {
        "id": "install-screws",
        "page": 3,
        "instruction": (
            "Start all four M3 x 8 screws by hand from above. Lightly snug "
            "opposite corners in turn with the socket driver. Heads must remain "
            "below the lid surface; do not overtighten or bow the lid."
        ),
        "stop": (
            "Stop for early bottoming, cross-threading, an insert turning, or a "
            "head that stands proud."
        ),
    },
    {
        "id": "service",
        "page": 3,
        "instruction": (
            "Disconnect power and cables. Remove all four screws completely, lift"
            " the lid straight up, then lift the board vertically off the "
            "locators. Keep the base supported flat."
        ),
        "stop": (
            "Do not pry against the PCB or connectors, and do not slide the lid "
            "sideways."
        ),
    },
    {
        "id": "fit-and-retention",
        "page": 4,
        "instruction": (
            "Inspect the empty fit, cooled inserts, board support, seam, screw "
            "recesses, and each actual Ethernet/USB-C plug and cable collar. "
            "For the recessed microSD card, use the open/lift-board service "
            "sequence if external access is awkward. Hold the "
            "closed, unpowered case gently in its ordinary use orientations. "
            "Small board play is designed in; record actual play/rattle and "
            "any loosening."
        ),
        "stop": (
            "Stop for connector/collar contact, bowing, rocking off supports or "
            "locators, unstable support, cracks, or strained cables. Actual "
            "plugs, collars and microSD fit have not been measured by the model."
        ),
    },
    {
        "id": "cycle-test",
        "page": 4,
        "instruction": (
            "With power and cables disconnected, repeat the complete open/close "
            "sequence ten times. Each closure must seat freely before tightening."
            " Recheck the seam, locators, inserts, screw threads, and cable "
            "access."
        ),
        "stop": (
            "Stop for a new bind, crack, loose insert, damaged thread, or changed"
            " board fit. Record the cycle and return the failed interface to "
            "design."
        ),
    },
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def moved(mesh: trimesh.Trimesh, delta: tuple[float, float, float]):
    result = mesh.copy()
    result.apply_translation(delta)
    return result


def render(objects: list[trimesh.Trimesh], target: Path, anchors=None):
    """White faces with depth-tested feature outlines; never wireframe ghosts."""
    az, el = math.radians(-28), math.radians(42)
    c, s, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)
    rotation = np.array([[c, s, 0], [-s * se, c * se, ce], [s * ce, -c * ce, se]])
    width, height = 1600, 1000
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=HERE)
    parser.add_argument("--evidence", type=Path, default=REPO / ".scratch/r5-guide")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    params = json.loads((args.parts / "parameters.json").read_text())
    hardware = json.loads((HERE / "hardware.json").read_text())
    meshes = {
        name: trimesh.load(args.parts / f"{name}.stl", force="mesh", process=True)
        for name in ("base", "lid", "screw", "insert", "driver", "heat-tool")
    }
    # Assembly coordinates are the shared design frame, not the plate layout.
    lid = meshes["lid"].copy()
    lid.apply_transform(params["lid_assembly_matrix"])
    base = meshes["base"]
    board = trimesh.load(args.board, force="mesh", process=True)
    board.apply_transform(params["board_matrix"])
    axes = params["screw_axes_mm"]
    screw = meshes["screw"].copy()
    screw.apply_transform(
        [
            [1, 0, 0, 0],
            [0, -1, 0, 0],
            [0, 0, -1, params["head_bearing_z_mm"]],
            [0, 0, 0, 1],
        ]
    )
    screws = [moved(screw, (x, y, 0)) for x, y in axes]
    inserts = [
        moved(meshes["insert"], (x, y, params["insert_bottom_z_mm"])) for x, y in axes
    ]
    height = params["envelope_mm"][2]
    land = params["head_bearing_z_mm"]
    head_top = land + hardware["screw"]["head_height_envelope_mm"]
    tip = land - params["screw_length_mm"]
    render([base, lid, *screws], args.output / "preview.png")
    exploded_points = render(
        [base, moved(lid, (0, 0, 38))],
        args.output / "exploded.png",
        {str(i): [x, y, params["insert_top_z_mm"]] for i, (x, y) in enumerate(axes)},
    )
    render(
        [base, moved(board, (0, 0, 9)), *inserts], args.evidence / "board-lowering.png"
    )

    c = canvas.Canvas(
        str(args.output / "assembly.pdf"), pagesize=(612, 792), invariant=1
    )
    c.setTitle("Yoke USB housing R5 - assembly and first article checks")
    c.setAuthor("Yoke")

    def text(x, y, value, size=10.5, bold=False):
        c.setFillColor(black)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y, value)

    def para(value, y, x=38, width=536, size=10.5, leading=14):
        words = value.split()
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if c.stringWidth(candidate, "Helvetica", size) > width:
                text(x, y, line, size)
                y -= leading
                line = word
            else:
                line = candidate
        text(x, y, line, size)
        return y - leading

    def heading(number, value):
        text(38, 755, f"YOKE USB HOUSING / R5 / {number} OF 4", 10, True)
        text(38, 726, value, 22, True)
        c.setLineWidth(0.6)
        c.line(38, 712, 574, 712)

    def footer():
        c.setLineWidth(0.4)
        c.line(38, 48, 574, 48)
        text(
            38,
            33,
            (
                "Full-case first article. Physical fit, durability and thermal "
                "qualification: NOT RUN."
            ),
            8.5,
        )
        text(
            38,
            20,
            (
                "Dimensions are nominal mm. Illustrated geometry comes from this "
                "packet's exact meshes."
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

    def step(identifier, title, y):
        item = next(s for s in STEPS if s["id"] == identifier)
        text(38, y, title, 11.5, True)
        y = para(item["instruction"], y - 17)
        if item.get("stop"):
            y = para(item["stop"], y - 1, size=10)
        return y - 10

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

    heading(1, "Identify the two parts")
    y = para(
        f"R5 is a {params['envelope_mm'][0]:g} x "
        f"{params['envelope_mm'][1]:g} x {height:g} rectangular, ventilated case "
        "for a bare Radxa ZERO 3E. The lid lowers vertically onto the base; "
        "all four screws enter from above.",
        692,
    )
    text(38, y - 6, "Inventory", 12, True)
    y = para("1 x R5 B base   |   1 x R5 L lid   |   1 x bare ZERO 3E board", y - 24)
    y = para("4 x M3 x 8 socket-head screws   |   4 x RX-M3x5.7 heat-set inserts", y)
    y = para(
        f"Screw envelope: {params['screw_length_mm']:g} under head; head no "
        f"larger than diameter {hardware['screw']['head_diameter_envelope_mm']:g} "
        f"x height {hardware['screw']['head_height_envelope_mm']:g}. Insert: "
        f"diameter {params['insert_diameter_mm']:g} x length "
        f"{params['insert_length_mm']:g}; nominal printed pilot diameter "
        f"{params['pilot_diameter_mm']:.1f}.",
        y - 3,
    )
    c.drawImage(
        ImageReader(str(args.output / "exploded.png")), 48, 270, width=516, height=322.5
    )
    text(51, 568, "L / LID", 13, True)
    text(51, 291, "B / BASE", 13, True)
    arrow(70, 480, 70, 430)
    text(39, 411, "Lid straight down", 9.5)
    for point in exploded_points.values():
        x, y = 48 + point[0] * 516 / 1600, 270 + (1000 - point[1]) * 322.5 / 1000
        arrow(x, y + 18, x, y + 2)
    text(365, 283, "4 inserts into EMPTY base", 9.5, True)
    text(
        38,
        256,
        "Both LAN arrows point to Ethernet (left in these views).",
        10.5,
        True,
    )
    y = step("identify", "1  Match the R5 parts and orientation", 229)
    y = step("verify-stock-tools", "2  Check the hardware before fitting it", y)
    para(
        (
            "Tools: caliper/depth gauge, correct socket driver, controlled insert"
            " heater and tip, nonflammable support; actual Ethernet and both "
            "USB-C cables. Use a host and temperature monitoring for later "
            "powered observations."
        ),
        y,
        size=10,
    )
    footer()
    c.showPage()

    heading(2, "Fit empty, then install inserts")
    y = step("empty-dry-fit", "3  Test the empty nested joint", 692)
    y = step("heatset-inserts", "4  Heat-set four inserts in the EMPTY base", y)
    text(
        38, y - 2, "A-A / front-left screw axis / Y = 1.6 / assembled section", 10, True
    )
    origin = (53, 185)
    bounds = (-10, 0, 5.5, 22)
    scale = 14
    section(base, axes[0][1], bounds, origin, scale, 0)
    section(lid, axes[0][1], bounds, origin, scale, 1)
    section(inserts[0], axes[0][1], bounds, origin, scale, 2)
    section(screws[0], axes[0][1], bounds, origin, scale, 2)
    text(216, 405, "All four joints use the same stack", 11, True)
    values = [
        f"{height:.1f}  exterior lid surface",
        f"{head_top:.1f}  nominal head top ({height - head_top:.1f} recessed)",
        f"{land:.1f}  head bearing land",
        f"{params['insert_top_z_mm']:.1f}  insert top / base boss stop",
        f"{tip:.1f}    screw tip ({params['screw_length_mm']:g} below its head)",
        f"{params['insert_bottom_z_mm']:.1f}    insert bottom",
        f"{params['pilot_bottom_z_mm']:.1f}    blind pilot bottom",
    ]
    for index, value in enumerate(values):
        text(216, 384 - index * 17, value, 10)
    para(
        (
            "Load path: screw head -> lid bearing land -> base boss -> insert "
            "thread. The PCB is outside this clamp stack."
        ),
        250,
        x=216,
        width=358,
        size=10,
    )
    para(
        f"Nominal axial overlap {params['insert_top_z_mm'] - tip:.1f}; tip "
        f"reserve {tip - params['insert_bottom_z_mm']:.1f} to insert bottom "
        f"and {tip - params['pilot_bottom_z_mm']:.1f} to pilot bottom. "
        "Confirm actual screw and insert lengths before fitting.",
        202,
        x=216,
        width=358,
        size=10,
    )
    arrow(123, 456, 123, 425)
    text(45, 466, "Driver from above", 10)
    text(45, 164, "Plastic: opposite hatches. Metal: close hatching.", 9)
    y = para(
        (
            "Section comes from the actual base, lid, insert and screw meshes. "
            "Nominal hardware envelopes are shown; threads are not represented. "
            "Driver access is vertical through the recessed head well. Check the "
            "actual driver before closing."
        ),
        142,
        size=10,
    )
    para(
        f"The lid skirt nests with {params['lip_per_side_clearance_mm']:g} "
        "clearance per side and a chamfered entry (section B-B, page 4). "
        "Clear only loose print debris; a changed fit or damaged boss "
        "returns to design.",
        y - 5,
        size=10,
    )
    footer()
    c.showPage()

    heading(3, "Lower, close and reopen")
    c.drawImage(
        ImageReader(str(args.evidence / "board-lowering.png")),
        106,
        451,
        width=400,
        height=250,
    )
    text(38, 691, "Bare board / Ethernet at LAN arrow", 10, True)
    arrow(554, 611, 554, 563)
    text(514, 539, "Lower level", 10)
    y = step("lower-board", "5  Lower the unpowered board", 439)
    y = step("lower-lid", "6  Lower the lid straight down", y)
    y = step("install-screws", "7  Start all four screws, then snug", y)
    step("service", "8  To reopen: screws out, then lift", y)
    footer()
    c.showPage()

    heading(4, "Check the first full case")
    y = para(
        (
            "Record observations on the real parts. A digital fit check does not "
            "establish a physical fit. The first article is an unloaded fit and "
            "service trial; thermal qualification remains open."
        ),
        692,
    )
    y = step("fit-and-retention", "A  Check parts, board and actual cables", y - 12)
    y = step("cycle-test", "B  Open and close ten times", y - 1)
    text(38, y, "C  Observe heat in the intended setup", 11.5, True)
    y = para(
        (
            "Stand the base on a hard, flat desk with the lid up. Keep the low "
            "side inlets and top outlets open; do not cover the case or place it "
            "on soft fabric. Vent paths are modeled, but cooling performance is "
            "unmeasured."
        ),
        y - 17,
    )
    y = para(
        (
            "After fit checks pass, monitor the actual intended workload. Record "
            "ambient, CPU and case temperatures, elapsed time, throttling and "
            "resets. Stop for abnormal heat, distortion, odor, unstable "
            "operation, or a component maker's operating limit. No safe power or "
            "temperature rating is established by this guide."
        ),
        y - 4,
    )
    text(38, y - 9, "Record before deciding to use the case", 11.5, True)
    for index, value in enumerate(
        [
            "Date / material / printer: ______________________________________________",
            "Insert stock / measured OD x length: ___________________________________",
            "Fit / cables / cycles / issues: ________________________________________",
            "Workload / ambient / temperatures / time: _______________________________",
            "Outcome / failed interface / next action: ______________________________",
        ]
    ):
        text(38, y - 32 - index * 20, value, 10)
    text(38, 224, "B-B / rear side joint at X = 22 / Y horizontal, Z up", 10, True)
    for mesh, style in ((base, 0), (lid, 1)):
        section(mesh, 22, (28.2, 32.7, 10.5, 20.3), (52, 70), 14, style, plane="x")
    text(132, 194, "L / lid and its inner skirt", 10, True)
    text(132, 173, "B / base wall", 10, True)
    para(
        f"Nominal per-side gap {params['lip_per_side_clearance_mm']:g}; "
        f"nesting depth {params['lip_depth_mm']:g}; entry chamfer "
        f"{params['lip_entry_chamfer_mm']:g}. Opposite hatches distinguish "
        "the printed parts. No hook or sideways assembly motion.",
        148,
        x=132,
        width=442,
        size=10,
    )
    para(
        (
            "Fit and reopening must be observed on the printed pair before screws are"
            " used. Keep the same Ethernet end throughout assembly."
        ),
        95,
        x=132,
        width=442,
        size=10,
    )
    footer()
    c.save()

    pdf = fitz.open(args.output / "assembly.pdf")
    pages = []
    for index, page in enumerate(pdf):
        destination = args.evidence / f"page-{index + 1}.png"
        page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), colorspace=fitz.csGRAY).save(
            destination
        )
        pages.append(
            {"page": index + 1, "path": str(destination), "sha256": sha(destination)}
        )
    section_views = []
    for page, region in ((pdf[1], (38, 240, 574, 700)), (pdf[3], (38, 550, 574, 730))):
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, clip=fitz.Rect(region)
        )
        section_views.append(
            Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
        )
    combined = Image.new(
        "L", (section_views[0].width, sum(v.height for v in section_views) + 20), 255
    )
    offset = 0
    for view in section_views:
        combined.paste(view, (0, offset))
        offset += view.height + 20
    combined.save(args.output / "section.png")
    sources = [
        Path(__file__),
        HERE / "housing.scad",
        HERE / "hardware.json",
        HERE / "build.py",
        args.parts / "parameters.json",
        args.board,
    ]
    sources.extend(args.parts / f"{name}.stl" for name in meshes)
    evidence = {
        "schema": "yoke.r5-guide/1",
        "pdf_sha256": sha(args.output / "assembly.pdf"),
        "pages": pages,
        "sources": [{"path": str(path), "sha256": sha(path)} for path in sources],
        "actions": STEPS,
        "readability_review": "PENDING final rendered page inspection",
    }
    (args.evidence / "guide-evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    actions = {
        "actions": [
            {
                "id": item["id"],
                "kind": (
                    "assembly"
                    if item["id"]
                    in ("heatset-inserts", "lower-board", "lower-lid", "install-screws")
                    else "use"
                    if item["id"] == "service"
                    else "test"
                    if item["id"] in ("fit-and-retention", "cycle-test")
                    else "handling"
                ),
                "pages": [item["page"]],
                "action": item["instruction"],
                "stop": item.get("stop", ""),
            }
            for item in STEPS
        ],
        "tools": {
            "caliper": (
                "Caliper and depth gauge: check hardware and seated insert depths."
            ),
            "driver": (
                "Correct socket driver for actual M3 stock; nominal modeled shaft"
                " diameter 3 x length 35. Confirm actual access."
            ),
            "heater": (
                "Controlled insert heater and matching tip; nominal envelope "
                "diameter 5 x length 30. Confirm actual access and follow maker "
                "method."
            ),
            "support": "Flat nonflammable support for empty base during heating.",
            "cables": (
                "Actual Ethernet and both USB-C service cables; plugs and collars"
                " must clear."
            ),
            "monitor": "Host and temperature monitoring for powered observations.",
        },
        "physical_test": {
            "scope": (
                "Full-case unloaded fit and service first article; thermal "
                "qualification NOT RUN."
            ),
            "procedure": [
                item["instruction"]
                for item in STEPS
                if item["id"] in ("empty-dry-fit", "fit-and-retention", "cycle-test")
            ],
            "observations": [
                "Actual stock dimensions and identity",
                "Free lid seating, board support and cable clearance",
                "Ten open/close cycles and any interventions",
            ],
            "stop_conditions": [item["stop"] for item in STEPS if "stop" in item],
        },
    }
    (args.evidence / "actions.json").write_text(json.dumps(actions, indent=2) + "\n")
    print(
        json.dumps(
            {
                "pdf": str(args.output / "assembly.pdf"),
                "sha256": evidence["pdf_sha256"],
                "pages": len(pages),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
