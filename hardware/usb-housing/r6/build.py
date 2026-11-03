#!/usr/bin/env python3
"""Export R6 native CAD, its nominal parameters and an unchanged-facet plate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PARTS = (
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
PLACEMENTS = {
    "base": (15, 16, 0),
    "lid": (15, 72, 0),
    "front-clip": (0, 115, 0),
    "right-clip": (-25, 135, 0),
}
PLATE = "yoke-usb-housing-r6"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def records(path, delta):
    data = path.read_bytes()
    result = []
    count = struct.unpack_from("<I", data, 80)[0]
    if len(data) != 84 + 50 * count:
        raise ValueError(f"Expected binary STL: {path}")
    for index in range(count):
        row = list(struct.unpack_from("<12fH", data, 84 + 50 * index))
        for start in (3, 6, 9):
            for axis in range(3):
                row[start + axis] += delta[axis]
        result.append(struct.pack("<12fH", *row))
    return result


def matrix(delta):
    return [[1, 0, 0, delta[0]], [0, 1, 0, delta[1]], [0, 0, 1, delta[2]], [0, 0, 0, 1]]


def declaration(plate, groups):
    result = {
        "schema_version": 1,
        "mesh_sha256": sha(plate),
        "job_basename": PLATE,
        "expected_component_count": 4,
        "envelope_mm": {"minimum": [0, 0, 0], "maximum": [220, 220, 220]},
        "components": {},
    }
    for name, group in groups.items():
        triangles = []
        for record in group:
            row = struct.unpack("<12fH", record)
            triangles.append([row[3:6], row[6:9], row[9:12]])
        canonical = sorted(
            tuple(sorted(tuple(round(value / 0.00001) for value in p) for p in tri))
            for tri in triangles
        )
        signature = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":")).encode()
        ).hexdigest()
        points = [p for tri in triangles for p in tri]
        result["components"]["component-" + signature[:16]] = {
            "label": "R6 " + name,
            "expected_bounds_mm": {
                "minimum": [min(p[a] for p in points) for a in range(3)],
                "maximum": [max(p[a] for p in points) for a in range(3)],
                "tolerance_mm": 0.001,
            },
            "policy": {"maximum_bridge_span_mm": 6.8},
            "bridge_authority": (
                "Nominal design allowance: two head-bearing shelves bridge the "
                "6.8 mm head well. Side vents span 3 mm. Actual toolpaths "
                "must pass supports-off review; physical bridging remains untested."
            ),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=REPO / ".scratch/r6-design/export"
    )
    parser.add_argument("--openscad", default=shutil.which("openscad"))
    args = parser.parse_args()
    if not args.openscad:
        parser.error("Manifold-enabled OpenSCAD is required")
    args.output.mkdir(parents=True, exist_ok=True)
    hardware = json.loads((HERE / "hardware.json").read_text())
    insert = hardware["insert"]
    parameters = {"revision": "R6", "physical_validation": "NOT RUN"}
    definitions = [
        "-D",
        f"insert_od={insert['outside_diameter_mm']}",
        "-D",
        f"insert_l={insert['length_mm']}",
        "-D",
        f"insert_pilot={insert['pilot_diameter_mm']}",
    ]
    for name in PARTS:
        process = subprocess.run(
            [
                args.openscad,
                "--backend",
                "Manifold",
                "--export-format",
                "binstl",
                *definitions,
                "-D",
                f'part="{name}"',
                "-D",
                f"emit_parameters={'true' if name == 'base' else 'false'}",
                "-o",
                str(args.output / f"{name}.stl"),
                str(HERE / "housing.scad"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        for line in process.stderr.splitlines():
            match = re.match(r'ECHO: "R6PARAM", "([^"]+)", (.*)', line)
            if match:
                parameters[match[1]] = json.loads(match[2])
        print(f"Exported {name}")
    if "envelope_mm" not in parameters:
        raise RuntimeError("Native CAD parameter echo missing")
    dump(args.output / "parameters.json", parameters)
    groups = {
        name: records(args.output / f"{name}.stl", delta)
        for name, delta in PLACEMENTS.items()
    }
    facets = [record for group in groups.values() for record in group]
    plate = args.output / f"{PLATE}.stl"
    plate.write_bytes(
        b"R6 FOUR-PART FIRST ARTICLE; PHYSICAL FIT NOT RUN".ljust(80, b"\0")
        + struct.pack("<I", len(facets))
        + b"".join(facets)
    )
    dump(args.output / f"{PLATE}.contact.json", declaration(plate, groups))
    dump(
        args.output / "identity.json",
        {
            "revision": "R6",
            "state": "POE_FULL_CASE_FIRST_ARTICLE_PHYSICAL_NOT_RUN",
            "hardware": hardware,
            "source_sha256": {
                name: sha(HERE / name)
                for name in (
                    "housing.scad",
                    "hardware.json",
                    "build.py",
                    "README.md",
                    "../../../site/assets/yoke-mark.svg",
                )
            },
            "parts_sha256": {
                path.name: sha(path) for path in sorted(args.output.glob("*.stl"))
            },
            "parameters_sha256": sha(args.output / "parameters.json"),
            "plate_transforms": {
                name: matrix(delta) for name, delta in PLACEMENTS.items()
            },
            "assembly_transform_lid": parameters["lid_assembly_matrix"],
            "board_matrix": parameters["board_matrix"],
            "screw_axes_mm": parameters["screw_axes_mm"],
        },
    )


if __name__ == "__main__":
    main()
