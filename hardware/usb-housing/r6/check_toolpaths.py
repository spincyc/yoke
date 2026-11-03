import argparse
import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

parser = argparse.ArgumentParser(
    description="Plot screw-seat toolpaths; physical bridge quality remains untested"
)
parser.add_argument("--job", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
HERE = args.output
HERE.mkdir(parents=True, exist_ok=True)
JOB = args.job
x = y = z = e = 0.0
absolute = True
extrusion_absolute = False
feature = ""
started = False
segments = []
for line in JOB.read_text().splitlines():
    if line == ";LAYER_CHANGE":
        started = True
    if line.startswith(";TYPE:"):
        feature = line.split(":", 1)[1]
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
            segments.append((ox, oy, x, y, z, feature))

# Native plate is centered by Orca without rotation: delta(-50.3,-64.4).
centers = [
    (-40.3, -46.8),
    (-40.3, -20.4),
    (40.3, -46.8),
    (40.3, -20.4),
    (-26.8, 54.9),
    (-2.8, 55.2),
]
image = Image.new("RGB", (1440, 2220), "white")
d = ImageDraw.Draw(image)
font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 17)
d.text(
    (20, 10),
    "Exact G-code screw-head shelves: previous layer gray; current black; bridge red",
    (0, 0, 0),
    font=font,
)
rows = []
for row, (cx, cy) in enumerate(centers):
    layers = [3.4, 3.6, 3.8, 4.0] if row < 4 else [0.8, 1.0, 1.2, 1.4]
    for col, layer in enumerate(layers):
        left, top = col * 360, row * 360 + 50
        d.text(
            (left + 8, top + 6),
            f"Head {row + 1}, print Z {layer:.1f} mm",
            (0, 0, 0),
            font=font,
        )
        d.rectangle(
            (left + 4, top + 32, left + 356, top + 354), outline=(210, 210, 210)
        )

        def project(x, y, left=left, top=top, cx=cx, cy=cy):
            return left + 180 + (x - cx) * 31, top + 190 - (y - cy) * 31

        relevant = [
            s
            for s in segments
            if abs(s[4] - layer) < 0.001
            and max(abs(s[0] - cx), abs(s[2] - cx)) < 5
            and max(abs(s[1] - cy), abs(s[3] - cy)) < 5
        ]
        prior = [
            s
            for s in segments
            if abs(s[4] - (layer - 0.2)) < 0.001
            and max(abs(s[0] - cx), abs(s[2] - cx)) < 5
            and max(abs(s[1] - cy), abs(s[3] - cy)) < 5
        ]
        for s in prior:
            d.line(
                (*project(s[0], s[1]), *project(s[2], s[3])),
                fill=(195, 195, 195),
                width=9,
            )
        for s in relevant:
            d.line(
                (*project(s[0], s[1]), *project(s[2], s[3])),
                fill=(200, 0, 0) if "Bridge" in s[5] else (0, 0, 0),
                width=3,
            )
        bridges = [s for s in relevant if "Bridge" in s[5]]
        rows.append(
            {
                "head": row + 1,
                "z_mm": layer,
                "segments": len(relevant),
                "bridge_segments": len(bridges),
                "max_bridge_segment_length_mm": max(
                    (((s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2) ** 0.5 for s in bridges),
                    default=0,
                ),
            }
        )
image.save(HERE / "shelf-toolpaths.png")
(HERE / "toolpath-audit.json").write_text(
    json.dumps(
        {
            "gcode_sha256": hashlib.sha256(JOB.read_bytes()).hexdigest(),
            "layers": rows,
            "method": "G0/G1 deposition parse respecting G90/G91,M82/M83,G92; "
            "six native head centers after slicer centering; base shelf "
            "layers3.4/3.6/3.8/4.0, clip shelf layers0.8/1.0/1.2/1.4. "
            "Prior-layer extrusion is gray/current black/bridge red.",
            "limits": "Printed bridge quality and shelf strength NOT RUN; "
            "this figure shows toolpaths only.",
        },
        indent=2,
    )
    + "\n"
)
print(
    "Wrote six screw-seat sections from exact G-code; physical bridge quality NOT RUN"
)
