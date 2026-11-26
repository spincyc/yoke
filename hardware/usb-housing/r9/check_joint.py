#!/usr/bin/env python3
"""Check the R9 case-screw joint: fused shelf layers and hard-stop seat area."""

import argparse
import hashlib
import json
from pathlib import Path

import manifold3d
import numpy as np
import trimesh

MIN_FUSED_LAYERS = 2
MIN_SEAT_MM2 = 10.0


def solid(mesh):
    mesh = mesh.copy()
    mesh.merge_vertices()
    return manifold3d.Manifold(
        manifold3d.Mesh(
            vert_properties=np.asarray(mesh.vertices, np.float32),
            tri_verts=np.asarray(mesh.faces, np.uint32),
        )
    )


def disk(x, y, radius):
    return manifold3d.CrossSection.circle(radius, 256).translate([x, y])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    p = json.loads((args.parts / "parameters.json").read_text())
    base = solid(trimesh.load_mesh(args.parts / "base.stl"))
    lids = {}
    for name, key in (("poe-lid", "poe"), ("bare-lid", "bare")):
        mesh = trimesh.load_mesh(args.parts / f"{name}.stl")
        mesh.apply_transform(p[f"{key}_lid_assembly_matrix"])
        lids[name] = solid(mesh)
    bearing = p["head_bearing_z_mm"]
    seat = p["post_contact_z_mm"]
    axes = []
    for index, (x, y) in enumerate(p["screw_axes_mm"], start=1):
        ring = disk(x, y, 3.8) - disk(x, y, 1.9)
        window = disk(x, y, 4.5)
        layers = []
        for step in range(round((seat - bearing) / 0.2)):
            z = bearing + (step + 0.5) * 0.2
            section = base.slice(z)
            missing = (ring - section).area()
            role = (
                "slot-bridge"
                if step == 0
                else ("square-bridge" if step == 1 else "fused-full")
            )
            layers.append(
                {
                    "z_mm": round(z, 2),
                    "role": role,
                    "area_within_4p5mm_mm2": round((section ^ window).area(), 2),
                    "ring_r1p9_3p8_missing_mm2": round(missing, 4),
                    "full_ring_solid": missing < 0.01,
                }
            )
        top = base.slice(seat - 0.05) ^ window
        axis = {
            "axis": index,
            "xy_mm": [x, y],
            "layers": layers,
            "fused_full_layers": sum(
                1 for row in layers if row["role"] == "fused-full" and row["full_ring_solid"]
            ),
        }
        for name, lid in lids.items():
            axis[f"seat_contact_{name}_mm2"] = round((top ^ lid.slice(seat + 0.05)).area(), 2)
            axis[f"{name}_material_below_seat_mm2"] = round((top ^ lid.slice(seat - 0.05)).area(), 3)
        axes.append(axis)
    fused = min(a["fused_full_layers"] for a in axes)
    contact = min(min(a["seat_contact_poe-lid_mm2"], a["seat_contact_bare-lid_mm2"]) for a in axes)
    overlap = max(max(a["poe-lid_material_below_seat_mm2"], a["bare-lid_material_below_seat_mm2"]) for a in axes)
    report = {
        "schema": "yoke.r9-joint-check/1",
        "physical_validation": "NOT RUN",
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__),
                args.parts / "parameters.json",
                args.parts / "base.stl",
                args.parts / "poe-lid.stl",
                args.parts / "bare-lid.stl",
            ]
        },
        "head_bearing_z_mm": bearing,
        "seat_z_mm": seat,
        "minimum_fused_full_layers": fused,
        "minimum_seat_contact_mm2": contact,
        "maximum_lid_material_below_seat_mm2": overlap,
        "limits": f"PASS needs >= {MIN_FUSED_LAYERS} fused full layers and >= {MIN_SEAT_MM2:g} mm2 seat per post, no lid material below the seat",
        "result": "PASS" if fused >= MIN_FUSED_LAYERS and contact >= MIN_SEAT_MM2 and overlap < 0.01 else "FAIL",
        "axes": axes,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(report["result"], f"fused full layers >= {fused}, seat contact >= {contact} mm2 per post")
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
