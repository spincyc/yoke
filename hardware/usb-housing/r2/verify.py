#!/usr/bin/env python3
"""Check exact R2 meshes against R1 and rebuilds (numpy, trimesh, manifold3d)."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import manifold3d as mf
import numpy as np
import trimesh

HERE = Path(__file__).resolve().parent
PARTS = ("base.stl", "lid.stl", "lid-printed.stl")
OUTPUTS = (*PARTS, "yoke-usb-housing-r2.stl", "yoke-usb-housing-r2.contact.json")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def solid(path: Path) -> tuple[trimesh.Trimesh, mf.Manifold]:
    mesh = trimesh.load(path, force="mesh", process=True)
    assert mesh.is_watertight and mesh.is_winding_consistent, path
    assert mesh.volume > 0, path
    shape = mf.Manifold(
        mf.Mesh(
            np.asarray(mesh.vertices, dtype=np.float32),
            np.asarray(mesh.faces, dtype=np.uint32),
        )
    )
    assert shape.status() == mf.Error.NoError, path
    return mesh, shape


def box(lo: list[float], hi: list[float]) -> mf.Manifold:
    return mf.Manifold.cube(tuple(np.array(hi) - lo)).translate(tuple(lo))


def row(shape: mf.Manifold) -> dict:
    return {
        "volume_mm3": round(shape.volume(), 9),
        "bounds_mm": None if shape.is_empty() else list(shape.bounding_box()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild",
        type=Path,
        required=True,
        help="transient directory for independent R1 and R2 rebuilds",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    r1_rebuild, r2_rebuild = args.rebuild / "r1", args.rebuild / "r2"
    for source, output in ((HERE.parent, r1_rebuild), (HERE, r2_rebuild)):
        subprocess.run(
            [sys.executable, str(source / "build.py"), "--output", str(output)],
            check=True,
        )
    reproduced = {}
    for filename in OUTPUTS:
        reproduced[filename] = sha(HERE / filename) == sha(r2_rebuild / filename)
        assert reproduced[filename], f"R2 rebuild differs: {filename}"
    for filename in PARTS:
        assert sha(HERE.parent / filename) == sha(r1_rebuild / filename), filename

    # Delta windows use the delivered PRINT coordinates, including inverted lid.
    # Their volume lies strictly inside the existing 2 mm floor/roof.
    windows = {
        "base.stl": [([10, 26.5, -0.001], [40, 31, 0.461])],
        "lid.stl": [
            ([10, -1, -0.001], [40, 3.5, 0.461]),
            ([5, 10.6666, -0.001], [13, 19.3334, 0.451]),
        ],
    }
    windows["lid-printed.stl"] = windows["lid.stl"]
    result = {
        "status": "PASS",
        "basis": "Nominal exact-mesh comparison; no physical test",
        "brand_sha256": sha(HERE.parents[2] / "site/assets/yoke-mark.svg"),
        "r1_mechanical_source_sha256": sha(HERE.parent / "housing.scad"),
        "r2_source_sha256": sha(HERE / "housing.scad"),
        "rebuild_byte_identical": reproduced,
        "meshes": {},
        "delta": {},
    }
    shapes = {}
    for filename in PARTS:
        old_mesh, old = solid(HERE.parent / filename)
        mesh, new = solid(HERE / filename)
        shapes[filename] = (old, new)
        assert len(mesh.split()) == 1, filename
        assert np.array_equal(mesh.bounds, old_mesh.bounds), filename
        result["meshes"][filename] = {
            "sha256": sha(HERE / filename),
            "facets": len(mesh.faces),
            "watertight": True,
            "consistent_winding": True,
            "components": 1,
            "volume_mm3": round(mesh.volume, 6),
            "bounds_mm": mesh.bounds.tolist(),
            "r1_bounds_identical": True,
        }
        allowed = mf.Manifold.batch_boolean(
            [box(*bounds) for bounds in windows[filename]], mf.OpType.Add
        )
        added, removed = new - old, old - new
        outside = (added + removed) - allowed
        assert outside.volume() < 0.00001, (filename, row(outside))
        result["delta"][filename] = {
            "added": row(added),
            "removed": row(removed),
            "allowed_windows_print_coordinates_mm": windows[filename],
            "outside_allowed_windows_mm3": round(outside.volume(), 9),
        }

    # The four removable screw-bore membranes must be the original R1 volume.
    old_membranes = shapes["lid-printed.stl"][0] - shapes["lid.stl"][0]
    new_membranes = shapes["lid-printed.stl"][1] - shapes["lid.stl"][1]
    membrane_delta = (old_membranes - new_membranes) + (new_membranes - old_membranes)
    assert membrane_delta.volume() < 0.00001, row(membrane_delta)
    result["membranes"] = {
        "unchanged": True,
        **row(new_membranes),
        "symmetric_difference_mm3": round(membrane_delta.volume(), 9),
    }
    plate, _ = solid(HERE / "yoke-usb-housing-r2.stl")
    assert len(plate.split()) == 2
    result["meshes"]["yoke-usb-housing-r2.stl"] = {
        "sha256": sha(HERE / "yoke-usb-housing-r2.stl"),
        "watertight": True,
        "consistent_winding": True,
        "components": 2,
        "bounds_mm": plate.bounds.tolist(),
    }

    # These are nominal design dimensions, distinct from physical observations.
    result["brand_placement_assembly_mm"] = {
        "center_xy": [9, 15],
        "visible_width": 8,
        "visible_height": 26 / 3,
        "engrave_depth": 0.45,
        "minimum_roof_remaining": 1.55,
        "minimum_mark_stroke_width": 2,
        "first_vent_clearance": 3,
        "bounds": [[5, 15 - 13 / 3, 19.55], [13, 15 + 13 / 3, 20]],
    }
    result["unchanged_interfaces"] = [
        "inner floor and roof",
        "side walls and 0.15mm closure gap",
        "four mounting-pad support posts",
        "screw bores and hex nut bearing seats",
        "Ethernet/USB-C/micro-HDMI/microSD openings",
        "all 3x13mm ventilation slots",
        "four 0.20mm sacrificial lid-bore membranes",
    ]
    result["limits"] = [
        "No R2 slice, print release, printer dispatch, or physical validation.",
        "R1 board-fit, cable, hand-snug strength and thermal limits still apply.",
        "The vendor board mesh remains nonmanifold; "
        "no new board Boolean collision claim.",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        "PASS: exact R2 deltas stay in the shallow exterior windows; "
        "R1 interfaces unchanged"
    )


if __name__ == "__main__":
    main()
