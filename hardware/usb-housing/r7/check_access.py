#!/usr/bin/env python3
"""Check R7 cable, card, vent, hardware-reserve and insert-wall allowances.

Runs on the native exports from build.py. These are design allowances, not
measurements of an owner's cables, cards, hardware or airflow. Requires
trimesh, numpy and manifold3d. Physical fit remains NOT RUN.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import trimesh

TOL = 0.001


def box(bounds):
    bounds = np.asarray(bounds, dtype=float)
    mesh = trimesh.creation.box(bounds[1] - bounds[0])
    mesh.apply_translation(bounds.mean(axis=0))
    return mesh


def cylinder(x, y, z0, z1, radius):
    mesh = trimesh.creation.cylinder(radius=radius, height=z1 - z0, sections=96)
    mesh.apply_translation([x, y, (z0 + z1) / 2])
    return mesh


def overlap(a, b):
    return abs(trimesh.boolean.intersection([a, b], engine="manifold").volume)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    p = json.loads((args.parts / "parameters.json").read_text())
    base = trimesh.load_mesh(args.parts / "base.stl")
    lids = {}
    for name, key in (("poe-lid", "poe"), ("bare-lid", "bare")):
        mesh = trimesh.load_mesh(args.parts / f"{name}.stl")
        mesh.apply_transform(p[f"{key}_lid_assembly_matrix"])
        lids[name] = mesh
    housings = {
        "poe": {"base": base, "poe-lid": lids["poe-lid"]},
        "bare": {"base": base, "bare-lid": lids["bare-lid"]},
    }
    bz = p["board_top_z_mm"]
    checks = []

    def check(identifier, candidate, parts, note):
        overlaps = {key: overlap(candidate, mesh) for key, mesh in parts.items()}
        checks.append(
            {
                "id": identifier,
                "note": note,
                "overlap_mm3": overlaps,
                "result": "PASS" if max(overlaps.values()) < TOL else "FAIL",
            }
        )

    # Cable sweeps. Receptacle centres and faces come from the official SBC
    # mesh: USB-C x 37.10..46.04 and 49.56..58.50, face y -1.5, z 0..3.16 above
    # the SBC top; RJ45 face x -6.4, y 6.93..23.13, z -4.55..6.85.
    for housing, parts in housings.items():
        for number, center in enumerate((41.57, 54.03), start=1):
            zc = bz + 1.58
            check(
                f"{housing}-usb-c-{number}-overmold-sweep",
                box(
                    [[center - 6.25, -40, zc - 3.75], [center + 6.25, -1.6, zc + 3.75]]
                ),
                parts,
                "12.5 x 7.5 mm overmold cross-section swept along +Y to 0.1 mm "
                "outside the receptacle face; actual cables NOT RUN",
            )
        check(
            f"{housing}-ethernet-plug-sweep",
            box([[-40, 6.93, bz - 4.55], [-6.5, 23.13, bz + 6.85]]),
            parts,
            "Full RJ45 jack-face outline swept along +X to 0.1 mm outside the "
            "face; actual plug, latch and boot NOT RUN",
        )
        for label, bounds in (
            ("nominal", [[52, 9.5, bz - 3.25], [67.5, 20.5, bz - 2.25]]),
            ("expanded", [[52, 9.0, bz - 3.6], [68.0, 21.0, bz - 1.9]]),
        ):
            check(
                f"{housing}-microsd-{label}-allowance",
                box(bounds),
                parts,
                "microSD card in the closed-end underside socket with a 2.5 mm "
                "(nominal) or 3.0 mm (expanded) projection past the PCB edge; "
                "actual card seating NOT RUN",
            )
    # Diameter-8 reservations for the Radxa pillars, screws and heads.
    for index, (x, y) in enumerate(p["mount_axes_mm"], start=1):
        for label, z0, z1, parts in (
            ("lower-head", bz - 5.6, bz - 1.6, {"base": base, **lids}),
            (
                "pillar",
                bz,
                bz + p["hat_gap_mm"],
                {"base": base, "poe-lid": lids["poe-lid"]},
            ),
            (
                "upper-head",
                bz + p["hat_gap_mm"] + 1.6,
                bz + p["hat_gap_mm"] + 5.6,
                {"poe-lid": lids["poe-lid"]},
            ),
        ):
            check(
                f"radxa-{label}-{index}-reserve",
                cylinder(x, y, z0, z1, 4.0),
                parts,
                "Diameter-8 design reservation around a conductive Radxa "
                "mounting axis; not measured hardware",
            )
    # Insert walls: an annulus 2.3..3.85 mm around each axis over the insert
    # length must lie entirely inside the lid (>= 1.55 mm wall, 0.05 mm
    # tessellation allowance against the 1.6 mm manufacturer minimum).
    walls = []
    for name, lid in lids.items():
        for index, (x, y) in enumerate(p["screw_axes_mm"], start=1):
            z0, z1 = p["insert_bottom_z_mm"], p["insert_top_z_mm"]
            annulus = trimesh.boolean.difference(
                [cylinder(x, y, z0, z1, 3.85), cylinder(x, y, z0 - 1, z1 + 1, 2.3)],
                engine="manifold",
            )
            missing = abs(
                trimesh.boolean.difference([annulus, lid], engine="manifold").volume
            )
            walls.append(
                {
                    "id": f"{name}-insert-{index}-wall",
                    "axis_mm": [x, y],
                    "missing_volume_mm3": missing,
                    "result": "PASS" if missing < TOL else "FAIL",
                }
            )
    checks.extend(walls)
    # Vent apertures: thin samples inside every slot must be clear of all
    # parts of that housing (wall, lip and base together).
    vx = p["vent_x_mm"]
    front_low, back_low, poe_fm, poe_fo, poe_bm, poe_up, bare_f, bare_b, roof_x = vx
    body, inside = p["body_bounds_xy_mm"], p["inside_bounds_xy_mm"]
    low = p["low_inlet_z_mm"]
    mid, over, upper = p["poe_vent_z_mm"]
    bare_z = p["bare_vent_z_mm"]
    lip_in_front = inside[2] + p["lip_per_side_clearance_mm"] + p["lip_thickness_mm"]
    lip_in_back = inside[3] - p["lip_per_side_clearance_mm"] - p["lip_thickness_mm"]
    rows = [
        ("both", "front-low", front_low, low, (body[2], lip_in_front)),
        ("both", "back-low", back_low, low, (lip_in_back, body[3])),
        ("poe", "front-mid", poe_fm, mid, (body[2], inside[2])),
        ("poe", "front-over-usb", poe_fo, over, (body[2], inside[2])),
        ("poe", "back-mid", poe_bm, mid, (inside[3], body[3])),
        ("poe", "front-upper", poe_up, upper, (body[2], inside[2])),
        ("poe", "back-upper", poe_up, upper, (inside[3], body[3])),
        ("bare", "front", bare_f, bare_z, (body[2], inside[2])),
        ("bare", "back", bare_b, bare_z, (inside[3], body[3])),
    ]
    vent_area = {"poe": 0.0, "bare": 0.0}
    for which, label, xs, (z0, z1), (y0, y1) in rows:
        for housing in ("poe", "bare") if which == "both" else (which,):
            for x in xs:
                check(
                    f"{housing}-vent-{label}-{x:g}",
                    box(
                        [
                            [x + 0.01, y0 - 0.5, z0 + 0.01],
                            [x + 2.99, y1 + 0.01, z1 - 0.01],
                        ]
                    ),
                    housings[housing],
                    "Geometric aperture through all housing walls; no airflow claim",
                )
                vent_area[housing] += 3 * (z1 - z0)
    for housing, height in (("poe", p["poe_height_mm"]), ("bare", p["bare_height_mm"])):
        for x in roof_x:
            check(
                f"{housing}-vent-roof-{x:g}",
                box([[x + 0.01, 2.01, height - 2.0], [x + 2.99, 26.99, height + 0.5]]),
                housings[housing],
                "Roof outlet aperture; no airflow claim",
            )
            vent_area[housing] += 3 * 25
    report = {
        "schema": "yoke.r7-access-check/1",
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
        "geometric_open_area_mm2": vent_area,
        "result": "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL",
        "checks": checks,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        report["result"],
        len(checks),
        "declared access, reserve, insert-wall and vent checks",
    )
    print("Geometric open area (mm2):", vent_area)
    for check_result in checks:
        if check_result["result"] != "PASS":
            print(check_result)
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
