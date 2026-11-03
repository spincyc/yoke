#!/usr/bin/env python3
"""Build R3 CAD and optional documentation; never slice or dispatch."""

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
BRAND_SHA = "9cd2e3d234d98c833720469ec785b33eab2dde6797da40ca634b75f833b08390"
PARTS = (
    "base",
    "base-printed",
    "lid",
    "lid-printed",
    "coupon-base",
    "coupon-base-printed",
    "coupon-lid",
    "coupon-lid-printed",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack(output: Path, name: str, groups: list[list[bytes]]) -> None:
    records = [record for group in groups for record in group]
    path = output / f"{name}.stl"
    path.write_bytes(
        name.encode().ljust(80, b"\0")
        + struct.pack("<I", len(records))
        + b"".join(records)
    )
    components = {}
    for i, group in enumerate(groups):
        triangles = [
            [struct.unpack("<12fH", r)[j : j + 3] for j in (3, 6, 9)] for r in group
        ]
        canonical = sorted(
            tuple(sorted(tuple(round(v / 0.00001) for v in p) for p in tri))
            for tri in triangles
        )
        signature = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":")).encode()
        ).hexdigest()
        points = [p for tri in triangles for p in tri]
        components["component-" + signature[:16]] = {
            "label": ["R3 manufactured base", "R3 manufactured lid"][i],
            "expected_bounds_mm": {
                "minimum": [round(min(p[a] for p in points), 6) for a in range(3)],
                "maximum": [round(max(p[a] for p in points), 6) for a in range(3)],
                "tolerance_mm": 0.001,
            },
            "policy": {"maximum_bridge_span_mm": 6.2},
            "bridge_authority": (
                "R3 design allowance, not a release: D5.8 head wells "
                "and AF5.25 hex nut traps (6.063mm corners), each with removable "
                "0.20mm bore membrane. Side-load channels span5.25mm; blind tip "
                "relief spans3mm. Fresh exact-toolpath review required before printing."
            ),
        }
    path.with_suffix(".contact.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mesh_sha256": sha(path),
                "job_basename": name,
                "expected_component_count": 2,
                "envelope_mm": {"minimum": [0, 0, 0], "maximum": [220, 220, 220]},
                "components": components,
            },
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE)
    parser.add_argument("--documents", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    openscad = shutil.which("openscad")
    if not openscad:
        raise SystemExit("Manifold-enabled OpenSCAD required")
    if sha(HERE.parents[2] / "site/assets/yoke-mark.svg") != BRAND_SHA:
        raise SystemExit("Canonical Yoke mark changed; review before rebuilding R3")
    for part in PARTS:
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
    translated = runpy.run_path(str(HERE.parent / "build.py"))["translated_stl"]
    pack(
        args.output,
        "yoke-usb-housing-r3",
        [
            translated(args.output / "base-printed.stl", (18, 10, 0)),
            translated(args.output / "lid-printed.stl", (18, 65, 0)),
        ],
    )
    pack(
        args.output,
        "yoke-usb-housing-r3-coupon",
        [
            translated(args.output / "coupon-base-printed.stl", (20, 10, 0)),
            translated(args.output / "coupon-lid-printed.stl", (20, 10, 0)),
        ],
    )
    if args.documents:
        subprocess.run(
            [sys.executable, str(HERE / "document.py"), "--output", str(args.output)],
            check=True,
        )


if __name__ == "__main__":
    main()
