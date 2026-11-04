#!/usr/bin/env python3
"""Check the R7 case-screw joint: fused shelf layers and hard-stop seat area.

For each screw axis, the base is sectioned at every printed layer from the
head-bearing plane to the seat. Each layer records its role (two sacrificial
bridge layers, then fused full layers), its area within 4.5 mm of the axis, and
whether the full ring r 1.9-3.8 mm is solid. The seat contact is the base top
face intersected with each lid's bottom face at the seat height, in assembled
position. Requires trimesh and manifold3d. Physical strength remains NOT RUN.
"""

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
    seat, bearing = p["post_contact_z_mm"], p["head_bearing_z_mm"]
    count = round((seat - bearing) / 0.2)
    layers = [round(bearing + 0.1 + 0.2 * k, 2) for k in range(count)]
    roles = {0: "sacrificial slot bridge", 1: "sacrificial square bridge"}
    axes = []
    for index, (x, y) in enumerate(p["screw_axes_mm"], start=1):
        near = disk(x, y, 4.5)
        ring = disk(x, y, 3.8) - disk(x, y, 1.9)
        rows = []
        for k, z in enumerate(layers):
            cut = base.slice(z)
            rows.append(
                {
                    "z_mid_mm": z,
                    "role": roles.get(k, "fused full layer"),
                    "area_mm2": round((cut ^ near).area(), 2),
                    "ring_r1.9_to_3.8_missing_mm2": round((ring - cut).area(), 3),
                }
            )
        wall = (base.slice(bearing + 0.1) ^ near) - disk(
            x, y, p["head_well_diameter_mm"] / 2
        )
        top = base.slice(seat - 0.02) ^ near
        axis = {
            "axis": index,
            "xy_mm": [x, y],
            "layers": rows,
            "fused_full_layers": sum(
                1
                for r in rows
                if r["role"] == "fused full layer"
                and r["ring_r1.9_to_3.8_missing_mm2"] < 0.01
            ),
            "unbridged_post_wall_at_shelf_mm2": round(wall.area(), 2),
        }
        for name, lid in lids.items():
            axis[f"seat_contact_{name}_mm2"] = round(
                (top ^ lid.slice(seat + 0.02)).area(), 2
            )
            axis[f"{name}_material_below_seat_mm2"] = round(
                (top ^ lid.slice(seat - 0.05)).area(), 3
            )
        axes.append(axis)
    fused = min(a["fused_full_layers"] for a in axes)
    contact = min(
        min(a["seat_contact_poe-lid_mm2"], a["seat_contact_bare-lid_mm2"]) for a in axes
    )
    overlap = max(
        max(a["poe-lid_material_below_seat_mm2"], a["bare-lid_material_below_seat_mm2"])
        for a in axes
    )
    report = {
        "schema": "yoke.r7-joint-check/1",
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
        "limits": f"PASS needs >= {MIN_FUSED_LAYERS} fused full layers and >= "
        f"{MIN_SEAT_MM2:g} mm2 seat per post, no lid material below the seat",
        "result": "PASS"
        if fused >= MIN_FUSED_LAYERS and contact >= MIN_SEAT_MM2 and overlap < 0.01
        else "FAIL",
        "axes": axes,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        report["result"],
        f"fused full layers >= {fused}, seat contact >= {contact} mm2 per post",
    )
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
