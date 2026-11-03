#!/usr/bin/env python3
"""Check declared cable and passive vent allowances against the native exports.

These are design allowances, not measurements of an owner's cables or airflow.
Requires trimesh, numpy and manifold3d. Hardware fit remains a physical test.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import trimesh


def box(bounds):
    bounds = np.asarray(bounds, dtype=float)
    mesh = trimesh.creation.box(bounds[1] - bounds[0])
    mesh.apply_translation(bounds.mean(axis=0))
    return mesh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    parameters = json.loads((args.parts / "parameters.json").read_text())
    meshes = {}
    for name in ("base", "lid", "front-clip", "right-clip"):
        mesh = trimesh.load_mesh(args.parts / f"{name}.stl")
        if name == "lid":
            mesh.apply_transform(parameters["lid_assembly_matrix"])
        elif "clip" in name:
            mesh.apply_transform(parameters["clip_assembly_matrix"])
        meshes[name] = mesh
    checks = []

    def check(name, bounds, note):
        candidate = box(bounds)
        overlaps = {
            key: abs(
                trimesh.boolean.intersection(
                    [candidate, mesh], engine="manifold"
                ).volume
            )
            for key, mesh in meshes.items()
        }
        checks.append(
            {
                "id": name,
                "bounds_mm": bounds,
                "note": note,
                "overlap_mm3": overlaps,
                "result": "PASS" if max(overlaps.values()) < 0.001 else "FAIL",
            }
        )

    # The full swept collar region ends 0.5mm outside the source socket mouth.
    # Plug metal enters its mating receptacle, which is not a solid obstacle.
    for number, center in enumerate((41.29975, 54.29975), start=1):
        check(
            f"usb-c-{number}-collar-sweep",
            [[center - 6, -35, 6.88], [center + 6, -2, 13.88]],
            "12x7mm collar cross section; straight insertion along +Y; "
            "actual cable NOT RUN",
        )
    check(
        "ethernet-boot-sweep",
        [[-40, 7, 4.8], [-6.8, 23, 16.8]],
        "16x12mm boot cross section; straight insertion along +X; "
        "actual latch/cable NOT RUN",
    )
    check(
        "installed-microsd-allowance",
        [[52, 9.53553, 5.55], [67, 20.53553, 6.55]],
        "Nominal 15x11x1mm card allowance with 2mm right-edge projection. "
        "Install/service outside case; actual card/socket fit NOT RUN.",
    )
    # Test narrow interior samples to avoid counting coincident boundary faces.
    for index, x in enumerate(np.arange(8, 62.01, 4.5)):
        for row, (z, h) in enumerate(((15.5, 12), (32.5, 8))):
            for side, ys in (("front", (-11.1, -7.2)), ("rear", (30.7, 34.6))):
                check(
                    f"vent-{side}-{row}-{index}",
                    [
                        [float(x + 0.01), ys[0], z + 0.01],
                        [float(x + 2.99), ys[1], z + h - 0.01],
                    ],
                    "Geometric aperture through wall and skirt; "
                    "no airflow/temperature claim",
                )
    report = {
        "schema": "yoke.r6-access-check/1",
        "physical_validation": "NOT RUN",
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), *(args.parts / f"{n}.stl" for n in meshes)]
        },
        "result": "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL",
        "checks": checks,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(report["result"], len(checks), "declared access/vent checks")
    for check_result in checks:
        if check_result["result"] != "PASS":
            print(check_result)
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
