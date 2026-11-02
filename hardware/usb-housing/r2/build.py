#!/usr/bin/env python3
"""Build the R2 branded variant; does not slice, release, or submit a print."""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MECHANICAL = HERE.parent / "housing.scad"
BRAND = HERE.parents[2] / "site/assets/yoke-mark.svg"
MECHANICAL_SHA = "58a4b039121b7737c01a4c54c03ca8a42df1e5e116984c651a3614551209ff02"
BRAND_SHA = "9cd2e3d234d98c833720469ec785b33eab2dde6797da40ca634b75f833b08390"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE)
    parser.add_argument(
        "--renders",
        action="store_true",
        help="also render exact STL facets (numpy, trimesh, Pillow required)",
    )
    args = parser.parse_args()
    openscad = shutil.which("openscad")
    if not openscad:
        raise SystemExit("OpenSCAD with the Manifold backend is required")
    for path, expected in ((MECHANICAL, MECHANICAL_SHA), (BRAND, BRAND_SHA)):
        if sha(path) != expected:
            raise SystemExit(
                f"Source changed: {path}; review geometry before updating its pin"
            )
    args.output.mkdir(parents=True, exist_ok=True)
    for part in ("base", "lid", "lid-printed"):
        subprocess.run(
            [
                openscad,
                "--backend",
                "Manifold",
                "--export-format",
                "binstl",
                "-D",
                f'part="{part}"',
                "-o",
                str(args.output / f"{part}.stl"),
                str(HERE / "housing.scad"),
            ],
            check=True,
        )
    # Reuse the R1 facet-preserving plate packer, without invoking its main.
    translated_stl = runpy.run_path(str(HERE.parent / "build.py"))["translated_stl"]
    groups = [
        translated_stl(args.output / "base.stl", (12, 3, 0)),
        translated_stl(args.output / "lid-printed.stl", (12, 45, 0)),
    ]
    records = groups[0] + groups[1]
    plate = args.output / "yoke-usb-housing-r2.stl"
    plate.write_bytes(
        b"Yoke USB housing R2; base and inverted branded lid".ljust(80, b"\0")
        + struct.pack("<I", len(records))
        + b"".join(records)
    )
    contact = {
        "schema_version": 1,
        "mesh_sha256": sha(plate),
        "job_basename": "yoke-usb-housing-r2",
        "expected_component_count": 2,
        "envelope_mm": {"minimum": [0, 0, 0], "maximum": [220, 220, 220]},
        "components": {},
    }
    for index, group in enumerate(groups):
        triangles = [
            [struct.unpack("<12fH", record)[j : j + 3] for j in (3, 6, 9)]
            for record in group
        ]
        canonical = sorted(
            tuple(sorted(tuple(round(v / 0.00001) for v in point) for point in tri))
            for tri in triangles
        )
        signature = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        points = [point for tri in triangles for point in tri]
        row = {
            "label": ["R2 base", "R2 manufactured lid"][index],
            "expected_bounds_mm": {
                "minimum": [round(min(p[a] for p in points), 6) for a in range(3)],
                "maximum": [round(max(p[a] for p in points), 6) for a in range(3)],
                "tolerance_mm": 0.001,
            },
        }
        if index == 1:
            row["policy"] = {"maximum_bridge_span_mm": 6.2}
            row["bridge_authority"] = (
                "R2 design-review allowance only; not a print release. Unchanged R1 "
                "four AF5.25 nut-pocket roofs and 0.20mm sacrificial bore membranes; "
                "circumscribed span 6.063mm. The 0.45mm-deep exterior mark has 2mm "
                "broad strokes. Re-slice and review the exact R2 artifacts "
                "before printing."
            )
        contact["components"]["component-" + signature[:16]] = row
    plate.with_suffix(".contact.json").write_text(json.dumps(contact, indent=2) + "\n")
    if args.renders:
        subprocess.run(
            [sys.executable, str(HERE / "render.py"), "--output", str(args.output)],
            check=True,
        )


if __name__ == "__main__":
    main()
