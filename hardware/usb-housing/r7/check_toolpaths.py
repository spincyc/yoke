#!/usr/bin/env python3
"""Plot and audit the exact sliced toolpaths at all eight R7 screw-head shelves.

Reads the sliced job, finds the slicer's plate translation from first-layer
non-skirt extrusion, and draws every layer from the last head-well wall layer
to the post seat at each base screw axis: slot bridge, square bridge and the
fused full layers. Records feature type, length, speed and fan per layer
within the 6.2 mm well footprint. Printed bridge quality and shelf strength
remain NOT RUN; this audit shows toolpaths only.
"""

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
plate = args.parts / "yoke-usb-housing-r7.stl"

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
        x, y, z, e = (
            values.get(k, v) for k, v in zip("XYZE", (x, y, z, e), strict=True)
        )
    elif command in ("G0", "G1"):
        ox, oy = x, y
        feed = values.get("F", feed)
        if absolute:
            x, y, z = (values.get(k, v) for k, v in zip("XYZ", (x, y, z), strict=True))
        else:
            x, y, z = (
                values.get(k, 0) + v for k, v in zip("XYZ", (x, y, z), strict=True)
            )
        amount = values.get("E", e if extrusion_absolute else 0)
        extruded = amount - e if extrusion_absolute else amount
        e = amount if extrusion_absolute else e + amount
        if started and extruded > 1e-9 and (abs(x - ox) > 1e-9 or abs(y - oy) > 1e-9):
            segments.append((ox, oy, x, y, round(z, 3), feature, feed / 60, fan))

# Plate-to-bed translation: first-layer model extrusion versus STL footprint.
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
names = {layers[0]: "well wall (head below)", layers[1]: "slot bridge"}
names[layers[2]] = "square bridge"
for k, layer in enumerate(layers[3:], start=1):
    names[layer] = f"fused layer {k}"
well_r = parameters["head_well_diameter_mm"] / 2
cell = 300
image = Image.new("RGB", (cell * len(layers) + 20, cell * len(centres) + 60), "white")
d = ImageDraw.Draw(image)
font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 15)
d.text(
    (12, 10),
    "Exact G-code at the eight head shelves: previous layer gray; current black; "
    "bridge red. Print Z = layer top.",
    (0, 0, 0),
    font=font,
)
rows = []
for row, (name, cx, cy) in enumerate(centres):
    for col, layer in enumerate(layers):
        left, top = col * cell + 10, row * cell + 40
        d.text(
            (left + 6, top + 4),
            f"{name} {row % 4 + 1}: Z {layer:.1f} {names[layer]}",
            (0, 0, 0),
            font=font,
        )
        d.rectangle(
            (left + 2, top + 24, left + cell - 4, top + cell - 4),
            outline=(210, 210, 210),
        )

        def project(px, py, left=left, top=top, cx=cx, cy=cy):
            return left + cell / 2 + (px - cx) * 26, top + cell / 2 + 12 - (
                py - cy
            ) * 26

        def near(s, height, cx=cx, cy=cy):
            return (
                abs(s[4] - height) < 0.001
                and max(abs(s[0] - cx), abs(s[2] - cx)) < 5
                and max(abs(s[1] - cy), abs(s[3] - cy)) < 5
            )

        relevant = [s for s in segments if near(s, layer)]
        prior = [s for s in segments if near(s, round(layer - 0.2, 1))]
        for s in prior:
            d.line(
                (*project(s[0], s[1]), *project(s[2], s[3])),
                fill=(195, 195, 195),
                width=8,
            )
        for s in relevant:
            d.line(
                (*project(s[0], s[1]), *project(s[2], s[3])),
                fill=(200, 0, 0) if "Bridge" in s[5] else (0, 0, 0),
                width=3,
            )
        bridges = [s for s in relevant if "Bridge" in s[5]]
        over_well = [
            s
            for s in relevant
            if ((s[0] + s[2]) / 2 - cx) ** 2 + ((s[1] + s[3]) / 2 - cy) ** 2 < well_r**2
        ]
        features = {}
        for s in over_well:
            entry = features.setdefault(
                s[5], {"length_mm": 0.0, "speeds_mm_s": set(), "fan": set()}
            )
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
                    name: {
                        "length_mm": round(v["length_mm"], 2),
                        "speeds_mm_s": sorted(v["speeds_mm_s"]),
                        "fan": sorted(v["fan"]),
                    }
                    for name, v in features.items()
                },
                "max_bridge_segment_length_mm": round(
                    max(
                        (
                            ((s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2) ** 0.5
                            for s in bridges
                        ),
                        default=0,
                    ),
                    3,
                ),
            }
        )
image.save(args.output / "shelf-toolpaths.png")
(args.output / "toolpath-audit.json").write_text(
    json.dumps(
        {
            "gcode_sha256": hashlib.sha256(args.job.read_bytes()).hexdigest(),
            "plate_sha256": hashlib.sha256(data).hexdigest(),
            "slicer_translation_mm": [round(v, 3) for v in delta],
            "layers": rows,
            "method": "G0/G1 deposition parse respecting G90/G91, M82/M83 and G92; "
            "plate translation from first-layer non-skirt extrusion bounds versus "
            "the STL footprint; eight head axes from identity.json transforms; "
            "per-layer feature length, speed and fan inside the head-well radius.",
            "limits": "Printed bridge quality and shelf strength NOT RUN; this "
            "figure shows toolpaths only.",
        },
        indent=2,
    )
    + "\n"
)
print(
    "Wrote eight screw-shelf toolpath sections; translation",
    [round(v, 3) for v in delta],
    "; physical bridge quality NOT RUN",
)
