#!/usr/bin/env python3
"""Build source-matched R4 layout parts; never slice, approve or contact a printer."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PARTS = (
    "base",
    "lid",
    "screw",
    "insert",
    "driver",
    "heat-tool",
    "front-base",
    "front-lid",
    "rear-base",
    "rear-lid",
    "rear-jig",
)


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def translated(path, delta):
    data = path.read_bytes()
    records = []
    for i in range(struct.unpack_from("<I", data, 80)[0]):
        row = list(struct.unpack_from("<12fH", data, 84 + 50 * i))
        for j in (3, 6, 9):
            for a in range(3):
                row[j + a] += delta[a]
        records.append(struct.pack("<12fH", *row))
    return records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=REPO / ".scratch/usb-housing-r4")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--provisional", action="store_true")
    mode.add_argument(
        "--coupon-trial",
        action="store_true",
        help="Prepare nominal joint samples to test actual fit; no full-case release",
    )
    p.add_argument("--documents", action="store_true")
    p.add_argument("--openscad", default=shutil.which("openscad"))
    a = p.parse_args()
    if not a.openscad:
        p.error(
            "Manifold-enabled OpenSCAD is required; use --openscad /path/to/openscad"
        )
    h = json.loads((HERE / "hardware.json").read_text())
    i = h["insert"]
    confirmed = h["status"] == "CONFIRMED" and isinstance(
        i["pilot_diameter_mm"], (int, float)
    )
    if not confirmed and not (a.provisional or a.coupon_trial):
        p.error(
            "Catalogue profile is known; actual stock identity/fit remains "
            "unconfirmed. Use --provisional for layout or --coupon-trial "
            "for joint samples."
        )
    if not confirmed and not a.output.resolve().is_relative_to(
        (REPO / ".scratch").resolve()
    ):
        p.error(
            "Provisional meshes must remain in repository .scratch; no "
            "print-ready assets may be emitted."
        )
    a.output.mkdir(parents=True, exist_ok=True)
    definitions = [
        "-D",
        f"provisional={str(not confirmed).lower()}",
        "-D",
        f"insert_od={i['outside_diameter_mm']}",
        "-D",
        f"insert_l={i['length_mm']}",
        "-D",
        f"insert_pilot={i['pilot_diameter_mm'] or -1}",
    ]
    for n in PARTS:
        subprocess.run(
            [
                a.openscad,
                "--backend",
                "Manifold",
                "--export-format",
                "binstl",
                *definitions,
                "-D",
                f'part="{n}"',
                "-o",
                str(a.output / f"{n}.stl"),
                str(HERE / "housing.scad"),
            ],
            check=True,
        )
    groups = [
        translated(a.output / "base.stl", (15, 8, 0)),
        translated(a.output / "lid.stl", (15, 53, 0)),
    ]
    records = [record for group in groups for record in group]
    plate = a.output / "yoke-usb-housing-r4-layout.stl"
    plate.write_bytes(
        b"R4 PROVISIONAL LAYOUT - NOT FOR PRINT".ljust(80, b"\0")
        + struct.pack("<I", len(records))
        + b"".join(records)
    )
    coupon_groups = [
        translated(a.output / f"{n}.stl", d)
        for n, d in [
            ("front-base", (15, 8, 0)),
            ("front-lid", (15, -2, 0)),
            ("rear-base", (-10, 8, 0)),
            ("rear-lid", (-10, 4, 0)),
            ("rear-jig", (15, 22, 0)),
        ]
    ]
    coupon_records = [record for group in coupon_groups for record in group]
    (a.output / "yoke-usb-housing-r4-coupons-layout.stl").write_bytes(
        (
            b"R4 JOINT FIT TRIAL - NOMINAL RX PROFILE"
            if a.coupon_trial
            else b"R4 PROVISIONAL COUPONS - NOT FOR PRINT"
        ).ljust(80, b"\0")
        + struct.pack("<I", len(coupon_records))
        + b"".join(coupon_records)
    )
    (a.output / "identity.json").write_text(
        json.dumps(
            {
                "revision": "R4",
                "state": (
                    "COUPON_FIT_TRIAL_STOCK_UNCONFIRMED"
                    if a.coupon_trial
                    else "PROVISIONAL_NOT_FOR_PRINT"
                    if not confirmed
                    else "DESIGN_REVIEW_ONLY"
                ),
                "print_scope": (
                    "Joint samples only; exact-job review and authorization required. "
                    "Match actual inserts before heat setting. "
                    "Full case remains blocked."
                    if a.coupon_trial
                    else "No print release"
                ),
                "hardware": h,
                "source_sha256": {
                    n: sha(HERE / n)
                    for n in [
                        "housing.scad",
                        "hardware.json",
                        "build.py",
                        "check.py",
                        "render.py",
                        "document.py",
                        "README.md",
                    ]
                },
                "parts_sha256": {
                    p.name: sha(p) for p in sorted(a.output.glob("*.stl"))
                },
                "plate_transforms": {
                    "base": [[1, 0, 0, 15], [0, 1, 0, 8], [0, 0, 1, 0], [0, 0, 0, 1]],
                    "lid": [[1, 0, 0, 15], [0, 1, 0, 53], [0, 0, 1, 0], [0, 0, 0, 1]],
                },
                "assembly_transform_lid": [
                    [1, 0, 0, 0],
                    [0, -1, 0, 28.5],
                    [0, 0, -1, 20],
                    [0, 0, 0, 1],
                ],
            },
            indent=2,
        )
        + "\n"
    )
    if a.documents:
        subprocess.run(
            [
                sys.executable,
                str(HERE / "document.py"),
                "--output",
                str(a.output),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
