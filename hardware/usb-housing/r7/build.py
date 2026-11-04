#!/usr/bin/env python3
"""Export R7 native CAD, its nominal parameters and an unchanged-facet plate.

One plate carries both compact housings: two identical bases, the PoE lid
(device side, ZERO 3E + RA023 HAT) and the bare lid (computer side, ZERO 3E
only). Physical fit and powered cooling remain NOT RUN.
"""

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
    "poe-lid",
    "bare-lid",
    "screw",
    "insert",
    "driver",
    "heat-tool",
)
# Plate instance -> (part mesh, translation). Both bases are the same mesh.
PLACEMENTS = {
    "poe-base": ("base", (14, 10, 0)),
    "bare-base": ("base", (14, 56, 0)),
    "poe-lid": ("poe-lid", (106, 10, 0)),
    "bare-lid": ("bare-lid", (106, 56, 0)),
}
PLATE = "yoke-usb-housing-r7"
BRIDGES = {
    "base": (
        6.2,
        "Nominal design allowance: above each recessed M3 head well, a one-layer "
        "3.4 mm shank slot leaves two head-bearing shelves that bridge the "
        "6.2 mm well. Side vents span 3 mm. Actual toolpaths must pass "
        "supports-off review; physical bridging remains untested.",
    ),
    "poe-lid": (
        3.0,
        "Nominal design allowance: printed roof-down, each 3.0 mm side vent "
        "slot closes with a short bridge. Physical bridging remains untested.",
    ),
    "bare-lid": (
        3.0,
        "Nominal design allowance: printed roof-down, each 3.0 mm side vent "
        "slot closes with a short bridge. Physical bridging remains untested.",
    ),
}


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
        "expected_component_count": len(groups),
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
        span, authority = BRIDGES[PLACEMENTS[name][0]]
        result["components"]["component-" + signature[:16]] = {
            "label": "R7 " + name,
            "expected_bounds_mm": {
                "minimum": [min(p[a] for p in points) for a in range(3)],
                "maximum": [max(p[a] for p in points) for a in range(3)],
                "tolerance_mm": 0.001,
            },
            "policy": {"maximum_bridge_span_mm": span},
            "bridge_authority": authority,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=REPO / ".scratch/r7-design/export"
    )
    parser.add_argument("--openscad", default=shutil.which("openscad"))
    args = parser.parse_args()
    if not args.openscad:
        parser.error("Manifold-enabled OpenSCAD is required")
    args.output.mkdir(parents=True, exist_ok=True)
    hardware = json.loads((HERE / "hardware.json").read_text())
    insert = hardware["insert"]
    parameters = {"revision": "R7", "physical_validation": "NOT RUN"}
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
            match = re.match(r'ECHO: "R7PARAM", "([^"]+)", (.*)', line)
            if match:
                parameters[match[1]] = json.loads(match[2])
        print(f"Exported {name}")
    if "poe_envelope_mm" not in parameters:
        raise RuntimeError("Native CAD parameter echo missing")
    dump(args.output / "parameters.json", parameters)
    groups = {
        name: records(args.output / f"{mesh}.stl", delta)
        for name, (mesh, delta) in PLACEMENTS.items()
    }
    facets = [record for group in groups.values() for record in group]
    plate = args.output / f"{PLATE}.stl"
    plate.write_bytes(
        b"R7 TWO-HOUSING PLATE; PHYSICAL FIT NOT RUN".ljust(80, b"\0")
        + struct.pack("<I", len(facets))
        + b"".join(facets)
    )
    dump(args.output / f"{PLATE}.contact.json", declaration(plate, groups))
    dump(
        args.output / "identity.json",
        {
            "revision": "R7",
            "state": "COMPACT_POE_AND_BARE_FIRST_ARTICLE_PHYSICAL_NOT_RUN",
            "hardware": hardware,
            "source_sha256": {
                name: sha(HERE / name)
                for name in (
                    "housing.scad",
                    "hardware.json",
                    "build.py",
                )
            },
            "parts_sha256": {
                path.name: sha(path) for path in sorted(args.output.glob("*.stl"))
            },
            "parameters_sha256": sha(args.output / "parameters.json"),
            "plate_parts": {name: mesh for name, (mesh, _) in PLACEMENTS.items()},
            "plate_transforms": {
                name: matrix(delta) for name, (_, delta) in PLACEMENTS.items()
            },
            "assembly_transform_poe_lid": parameters["poe_lid_assembly_matrix"],
            "assembly_transform_bare_lid": parameters["bare_lid_assembly_matrix"],
            "board_matrix": parameters["board_matrix"],
            "screw_axes_mm": parameters["screw_axes_mm"],
        },
    )


if __name__ == "__main__":
    main()
