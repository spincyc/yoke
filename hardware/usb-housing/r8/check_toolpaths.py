#!/usr/bin/env python3
"""Plot and audit the exact sliced toolpaths at all eight R8 screw-head shelves."""

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--job", type=Path, required=True)
parser.add_argument("--parts", type=Path, required=True, help="build.py export dir")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
identity = json.loads((args.parts / "identity.json").read_text())
parameters = json.loads((args.parts / "parameters.json").read_text())
plate = args.parts / "yoke-usb-housing-r8.stl"

x = y = z = e = 0.0
feed = 0.0
fan = "M107"
absolute = True
extrusion_absolute = False
feature = ""
started = False
segments = []
for line in args.job.read_text().splitlines():
    if line == ";LAYER_CHANGE":
        started = True
    if line.startswith(";TYPE:"):
        feature = line.split(":", 1)[1]
    if line.startswith(("M106", "M107")):
        fan = line.split(";", 1)[0].strip()
    code = line.split(";", 1)[0].strip()
    if not code:
        continue
    command = code.split()[0]
    values = {
        k: float(v)
        for k, v in re.findall(
            r"([XYZEF])\s*(-?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?)", code
        )
    }
    if command == "G90":
        absolute = True
    elif command == "G91":
        absolute = False
    elif command == "M82":
        extrusion_absolute = True
    elif command == "M83":
        extrusion_absolute = False
    elif command == "G92":
        x, y, z, e = (values.get(k, v) for k, v in zip("XYZE", (x, y, z, e), strict=True))
    elif command in ("G0", "G1"):
        ox, oy = x, y
        feed = values.get("F", feed)
        if absolute:
            x, y, z = (values.get(k, v) for k, v in zip("XYZ", (x, y, z), strict=True))
        else:
            x, y, z = (values.get(k, 0) + v for k, v in zip("XYZ", (x, y, z), strict=True))
        amount = values.get("E", e if extrusion_absolute else 0)
        extruded = amount - e if extrusion_absolute else amount
        e = amount if extrusion_absolute else e + amount
        if started and extruded > 1e-9 and (abs(x - ox) > 1e-9 or abs(y - oy) > 1e-9):
            segments.append((ox, oy, x, y, round(z, 3), feature, feed / 60, fan))

data = plate.read_bytes()
count = struct.unpack_from("<I", data, 80)[0]
low = [float("inf")] * 2
high = [float("-inf")] * 2
for index in range(count):
    row = struct.unpack_from("<12f", data, 84 + 50 * index)
    for start in (3, 6, 9):
        if row[start + 2] < 0.05:
            for axis in range(2):
                low[axis] = min(low[axis], row[start + axis])
                high[axis] = max(high[axis], row[start + axis])
first = [s for s in segments if abs(s[4] - 0.2) < 0.001 and "Skirt" not in s[5]]
glow = [min(min(s[0], s[2]) for s in first), min(min(s[1], s[3]) for s in first)]
ghigh = [max(max(s[0], s[2]) for s in first), max(max(s[1], s[3]) for s in first)]
delta = [((glow[a] + ghigh[a]) - (low[a] + high[a])) / 2 for a in range(2)]

centres = []
for name, mesh in identity["plate_parts"].items():
    if mesh != "base":
        continue
    tx, ty = (identity["plate_transforms"][name][r][3] for r in range(2))
    for ax, ay in parameters["screw_axes_mm"]:
        centres.append((name, ax + tx + delta[0], ay + ty + delta[1]))
bearing = parameters["head_bearing_z_mm"]
seat = parameters["post_contact_z_mm"]
count_layers = round((seat - bearing) / 0.2) + 1
layers = [round(bearing + k * 0.2, 1) for k in range(count_layers)]
names = {layers[0]: "well wall (head below)", layers[1]: "slot bridge", layers[2]: "square bridge"}
for k, layer in enumerate(layers[3:], start=1):
    names[layer] = f"fused layer {k}"
well_r = parameters["head_well_diameter_mm"] / 2
cell = 300
image = Image.new("RGB", (cell * len(layers) + 20, cell * len(centres) + 60), "white")
d = ImageDraw.Draw(image)
font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 15)
d.text((12, 10), "Exact R8 G-code at the eight head shelves: gray prior; black current; red bridge.", (0, 0, 0), font=font)
rows = []
for row, (name, cx, cy) in enumerate(centres):
    for col, layer in enumerate(layers):
        left, top = col * cell + 10, row * cell + 40
        d.text((left + 6, top + 4), f"{name} {row % 4 + 1}: Z {layer:.1f} {names[layer]}", (0, 0, 0), font=font)
        d.rectangle((left + 2, top + 24, left + cell - 4, top + cell - 4), outline=(210, 210, 210))

        def project(px, py, left=left, top=top, cx=cx, cy=cy):
            return left + cell / 2 + (px - cx) * 26, top + cell / 2 + 12 - (py - cy) * 26

        def near(s, height, cx=cx, cy=cy):
            return abs(s[4] - height) < 0.001 and max(abs(s[0] - cx), abs(s[2] - cx)) < 5 and max(abs(s[1] - cy), abs(s[3] - cy)) < 5

        relevant = [s for s in segments if near(s, layer)]
        prior = [s for s in segments if near(s, round(layer - 0.2, 1))]
        for s in prior:
            d.line((*project(s[0], s[1]), *project(s[2], s[3])), fill=(195, 195, 195), width=8)
        for s in relevant:
            d.line((*project(s[0], s[1]), *project(s[2], s[3])), fill=(200, 0, 0) if "Bridge" in s[5] else (0, 0, 0), width=3)
        bridges = [s for s in relevant if "Bridge" in s[5]]
        over_well = [s for s in relevant if ((s[0] + s[2]) / 2 - cx) ** 2 + ((s[1] + s[3]) / 2 - cy) ** 2 < well_r**2]
        features = {}
        for s in over_well:
            entry = features.setdefault(s[5], {"length_mm": 0.0, "speeds_mm_s": set(), "fan": set()})
            entry["length_mm"] += ((s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2) ** 0.5
            entry["speeds_mm_s"].add(round(s[6], 1))
            entry["fan"].add(s[7])
        rows.append(
            {
                "instance": name,
                "axis": row % 4 + 1,
                "z_mm": layer,
                "role": names[layer],
                "segments": len(relevant),
                "bridge_segments": len(bridges),
                "over_well_features": {
                    fname: {
                        "length_mm": round(v["length_mm"], 2),
                        "speeds_mm_s": sorted(v["speeds_mm_s"]),
                        "fan": sorted(v["fan"]),
                    }
                    for fname, v in features.items()
                },
                "max_bridge_segment_length_mm": round(
                    max((((s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2) ** 0.5 for s in bridges), default=0),
                    3,
                ),
            }
        )
image.save(args.output / "shelf-toolpaths.png")

# Audit ALL 8 lid insert bosses in the exact sliced G-code across the full insert
# engagement zone (Z_asm = 4.8..10.5) to verify solid printed PLA around every RX-M3x5.7
# insert (preventing hollow 15%-infill cavities behind a single 0.45 mm wall loop).
boss_rows = []
boss_pass = True
insert_r = parameters["insert_diameter_mm"] / 2.0  # 2.30 mm
req_outer_r = insert_r + 1.60                       # 3.90 mm (Ruthex >= 1.6 mm wall)
boss_img = Image.new("RGB", (300 * 4 + 20, 300 * 2 + 60), "white")
bd = ImageDraw.Draw(boss_img)
bd.text((12, 10), "Exact R8 G-code mid-insert slice (Z_asm=7.6): black=wall/solid; blue=gap fill; red=sparse; green=r2.3/r3.9", (0, 0, 0), font=font)
for lid_row, (lid_name, height_key) in enumerate((("poe-lid", "poe_height_mm"), ("bare-lid", "bare_height_mm"))):
    h_lid = parameters[height_key]
    flip_y = parameters["body_bounds_xy_mm"][2] + parameters["body_bounds_xy_mm"][3]
    tx, ty = (identity["plate_transforms"][lid_name][r][3] for r in range(2))
    # Printed z for insert_bottom (4.8) .. insert_top (10.5):
    z_lo = round(h_lid - parameters["insert_top_z_mm"], 1)
    z_hi = round(h_lid - parameters["insert_bottom_z_mm"], 1)
    z_mid = round(h_lid - 7.6, 1)
    for ax_idx, (ax, ay) in enumerate(parameters["screw_axes_mm"], start=1):
        cx = ax + tx + delta[0]
        cy = (flip_y - ay) + ty + delta[1]
        layer_zs = sorted({s[4] for s in segments if z_lo - 0.05 <= s[4] <= z_hi + 0.05})
        sparse_in_wall_layers = 0
        min_solid_wall_len_mm = float("inf")
        checked_layers = 0
        # Plot mid-insert layer (nearest available Z to z_mid)
        plot_z = min(layer_zs, key=lambda v: abs(v - z_mid))
        left, top = (ax_idx - 1) * 300 + 10, lid_row * 300 + 40
        bd.text((left + 6, top + 4), f"{lid_name} {ax_idx}: Zp={plot_z:.1f} (Za={h_lid - plot_z:.1f})", (0, 0, 0), font=font)
        bd.rectangle((left + 2, top + 24, left + 296, top + 296), outline=(210, 210, 210))
        sc = 24.0
        def bproj(px, py, left=left, top=top, cx=cx, cy=cy):
            return left + 150 + (px - cx) * sc, top + 162 - (py - cy) * sc
        for s in segments:
            if abs(s[4] - plot_z) < 0.001 and max(abs(s[0] - cx), abs(s[2] - cx)) < 5.5 and max(abs(s[1] - cy), abs(s[3] - cy)) < 5.5:
                col = (220, 0, 0) if "Sparse" in s[5] else ((0, 90, 210) if "Gap" in s[5] else (0, 0, 0))
                bd.line((*bproj(s[0], s[1]), *bproj(s[2], s[3])), fill=col, width=4)
        for rr in (insert_r, req_outer_r):
            p0 = bproj(cx - rr, cy + rr)
            p1 = bproj(cx + rr, cy - rr)
            bd.ellipse((p0[0], p0[1], p1[0], p1[1]), outline=(0, 160, 0), width=2)
        for lz in layer_zs:
            near_boss = [
                s for s in segments
                if abs(s[4] - lz) < 0.001
                and max(abs(s[0] - cx), abs(s[2] - cx)) < 5.0
                and max(abs(s[1] - cy), abs(s[3] - cy)) < 5.0
            ]
            sparse_len = 0.0
            solid_len = 0.0
            for s in near_boss:
                mx, my = (s[0] + s[2]) / 2.0, (s[1] + s[3]) / 2.0
                rr = ((mx - cx) ** 2 + (my - cy) ** 2) ** 0.5
                if insert_r <= rr <= req_outer_r:
                    seg_l = ((s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2) ** 0.5
                    if "Sparse" in s[5]:
                        sparse_len += seg_l
                    else:
                        solid_len += seg_l
            checked_layers += 1
            min_solid_wall_len_mm = min(min_solid_wall_len_mm, solid_len)
            if sparse_len > 0.1:
                sparse_in_wall_layers += 1
        ok = (checked_layers >= 25) and (sparse_in_wall_layers == 0) and (min_solid_wall_len_mm >= 55.0)
        if not ok:
            boss_pass = False
        boss_rows.append({
            "instance": lid_name,
            "axis": ax_idx,
            "z_print_range_mm": [z_lo, z_hi],
            "checked_layers": checked_layers,
            "sparse_infill_layers_in_r2p3_3p9": sparse_in_wall_layers,
            "min_solid_toolpath_mm_in_r2p3_3p9": round(min_solid_wall_len_mm, 2),
            "result": "PASS" if ok else "FAIL",
        })
boss_img.save(args.output / "insert-boss-toolpaths.png")

(args.output / "toolpath-audit.json").write_text(
    json.dumps(
        {
            "gcode_sha256": hashlib.sha256(args.job.read_bytes()).hexdigest(),
            "plate_sha256": hashlib.sha256(data).hexdigest(),
            "slicer_translation_mm": [round(v, 3) for v in delta],
            "layers": rows,
            "insert_boss_audit": {
                "result": "PASS" if boss_pass else "FAIL",
                "required_annulus_mm": [insert_r, req_outer_r],
                "rule": "Zero Sparse infill inside r=2.30..3.90 mm and >= 55 mm solid extrusion path per layer across insert length",
                "bosses": boss_rows,
            },
            "limits": "Printed bridge quality, insert pull-out and shelf strength NOT RUN; this audit checks sliced toolpaths only.",
        },
        indent=2,
    )
    + "\n"
)
print("Wrote eight R8 screw-shelf toolpath sections; translation", [round(v, 3) for v in delta])
print("Insert boss G-code audit:", "PASS" if boss_pass else "FAIL",
      [(b["instance"], b["axis"], b["sparse_infill_layers_in_r2p3_3p9"], b["min_solid_toolpath_mm_in_r2p3_3p9"]) for b in boss_rows])
if not boss_pass:
    raise SystemExit(1)
