#!/usr/bin/env python3
"""Check R4 nominal assembly/coupons; no slicing, release or physical acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import manifold3d as mf
import numpy as np
import trimesh

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BOARD_SHA = "1c1268c8af4b1a6c0b07fec07b3b28c6662e5d10c0c1b794c1f1083f31af8f27"
LID = np.array([[1, 0, 0, 0], [0, -1, 0, 28.5], [0, 0, -1, 20], [0, 0, 0, 1]], float)
AXES = [(-5, 1.6), (-5, 28)]
GUIDES = [(16, 30.6, 12), (47, 30.6, 12), (1.5, -2.1, 4)]
COUPON_PLACEMENTS = [
    ("front-base", (15, 8, 0)),
    ("front-lid", (15, -2, 0)),
    ("rear-base", (-10, 8, 0)),
    ("rear-lid", (-10, 4, 0)),
    ("rear-jig", (15, 22, 0)),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def matrix(x=0, y=0, z=0):
    result = np.eye(4)
    result[:3, 3] = [x, y, z]
    return result


def solid(mesh):
    return mf.Manifold(
        mf.Mesh(
            np.asarray(mesh.vertices, dtype=np.float32),
            np.asarray(mesh.faces, dtype=np.uint32),
        )
    )


def shifted(source, names, delta):
    result = {key: np.array(value).copy() for key, value in source.items()}
    for name in names:
        result[name][:3, 3] += delta
    return {key: value.tolist() for key, value in result.items()}


def geometry_plan(out, meshes):
    """Use exact part/tool meshes in assembled coordinates and reversible paths."""
    instances = [
        {"id": "base", "mesh": "base.stl", "matrix": matrix().tolist()},
        {"id": "lid", "mesh": "lid.stl", "matrix": LID.tolist()},
        {
            "id": "board",
            "mesh": "reference/board.stl",
            "matrix": matrix(z=8.5).tolist(),
        },
    ]
    for k, (x, y) in enumerate(AXES, 1):
        screw = matrix(x, y, 16.5)
        screw[1, 1] = screw[2, 2] = -1
        for name, transform in [
            ("screw", screw),
            ("insert", matrix(x, y, 7.8)),
            ("driver", matrix(x, y, 19.5)),
            ("heat-tool", matrix(x, y, 12.5)),
        ]:
            instances.append(
                {
                    "id": f"{name}{k}",
                    "mesh": f"{name}.stl",
                    "matrix": transform.tolist(),
                }
            )
    for row in instances:
        row["sha256"] = sha(out / row["mesh"])
    closed = {row["id"]: row["matrix"] for row in instances}
    states, paths = [], []

    def path(name, active, moving, start, end):
        begin, finish = name + "-start", name + "-end"
        states.extend(
            [{"id": begin, "matrices": start}, {"id": finish, "matrices": end}]
        )
        paths.append(
            {
                "id": name,
                "from_state": begin,
                "to_state": finish,
                "active": active,
                "moving": moving,
            }
        )

    path(
        "lower-board",
        ["base", "board"],
        ["board"],
        shifted(closed, ["board"], [0, 0, 20]),
        closed,
    )
    offset = shifted(closed, ["lid"], [-1.2, 0, 0])
    path(
        "lower-offset-lid",
        ["base", "board", "lid"],
        ["lid"],
        shifted(offset, ["lid"], [0, 0, 25]),
        offset,
    )
    path("slide-lid", ["base", "board", "lid"], ["lid"], offset, closed)
    path("reverse-slide", ["base", "board", "lid"], ["lid"], closed, offset)
    path(
        "lift-offset-lid",
        ["base", "board", "lid"],
        ["lid"],
        offset,
        shifted(offset, ["lid"], [0, 0, 25]),
    )
    for k in [1, 2]:
        path(
            f"insert-screw{k}",
            ["base", "lid", f"insert{k}", f"screw{k}"],
            [f"screw{k}"],
            shifted(closed, [f"screw{k}"], [0, 0, 15]),
            closed,
        )
        path(
            f"driver-access{k}",
            ["base", "lid", f"screw{k}", f"driver{k}"],
            [f"driver{k}"],
            shifted(closed, [f"driver{k}"], [0, 0, 20]),
            closed,
        )
        path(
            f"heat-tool-access{k}",
            ["base", f"insert{k}", f"heat-tool{k}"],
            [f"heat-tool{k}"],
            shifted(closed, [f"heat-tool{k}"], [0, 0, 20]),
            closed,
        )
        path(
            f"insert-heatset{k}",
            ["base", f"insert{k}"],
            [f"insert{k}"],
            shifted(closed, [f"insert{k}"], [0, 0, 10]),
            closed,
        )
    # Tools are present only in their declared operations; store them apart at baseline.
    baseline = closed
    for k in [1, 2]:
        baseline = shifted(baseline, [f"driver{k}"], [0, 0, 50])
        baseline = shifted(baseline, [f"heat-tool{k}"], [0, 0, 100])
    for row in instances:
        row["matrix"] = baseline[row["id"]]
    contacts = [
        {
            "pair": ["base", f"insert{k}"],
            "reason": (
                "Intentional heat-set interference with catalogue RX profile; "
                "actual stock identity, pilot fit and heat setting remain "
                "untested."
            ),
        }
        for k in [1, 2]
    ]
    plan = {
        "schema": "lacuna.assembly-geometry/2",
        "instances": instances,
        "contact_pairs": contacts,
        "motion": {"states": states, "paths": paths},
    }
    dump(out / "geometry-plan.json", plan)


def board_surface_checks(out, boardpath, base, lid):
    """Actual surface-edge crossings supplement the nonmanifold vendor mesh."""
    board = trimesh.load(boardpath, force="mesh")
    vertices = board.vertices.copy()
    vertices[:, 2] += 8.5
    edges = vertices[board.edges_unique]

    def crossings(part, allow_pad=False):
        hits = []
        for p0, p1 in edges:
            direction = p1 - p0
            for hit in part.ray_cast(p0.tolist(), p1.tolist()):
                if not 1e-4 < hit.distance < 1 - 1e-4:
                    continue
                if allow_pad and abs(hit.position[2] - 6.88) < 0.0002:
                    continue
                if abs(np.dot(direction, hit.normal)) > 1e-5:
                    hits.append(hit.position)
        return {"crossings": len(hits), "first_positions_mm": hits[:5]}

    report = {
        "unique_board_edges": len(edges),
        "board_sha256": sha(boardpath),
        "board_watertight": board.is_watertight,
        "board_bounds_assembled_mm": (board.bounds + np.array([0, 0, 8.5])).tolist(),
        "base": crossings(base, True),
        "slide": [],
        "lower_offset_lid": [],
    }
    for dx in np.linspace(-1.2, 0, 13):
        report["slide"].append(
            {"x_offset_mm": round(float(dx), 3), **crossings(lid.translate([dx, 0, 0]))}
        )
    for dz in [0, 0.25, 0.5, 1, 2, 4, 8, 16, 25]:
        report["lower_offset_lid"].append(
            {"z_offset_mm": dz, **crossings(lid.translate([-1.2, 0, dz]))}
        )
    report["limits"] = (
        "Surface-edge rays detect transverse crossings, not complete "
        "containment or face-only intersections. Samples do not prove "
        "continuous motion. Lower-pad contact at z6.88 is excluded "
        "within0.0002mm. Actual-board bounds lie inside the cavity and front "
        "bosses are outside PCB/RJ45 bounds."
    )
    dump(out / "board-surface-check.json", report)
    return report


def guide_checks(out, base, lid):
    """Require every promised tongue, open slot, and both actual side walls."""
    guides = []
    for x, y, length in GUIDES:
        tongue_region = mf.Manifold.cube([length, 1, 1.2]).translate(
            [x - length / 2, y - 0.5, 13.5]
        )
        tongue = (base ^ tongue_region).volume()
        channel = mf.Manifold.cube([length + 1.7, 1.5, 1.48]).translate(
            [x - length / 2 - 0.25, y - 0.75, 13.5]
        )
        intrusion = (lid ^ channel).volume()
        walls = []
        for offset in [-1.6, 0.75]:
            region = mf.Manifold.cube([length, 0.85, 0.8]).translate(
                [x - length / 2, y + offset, 13.8]
            )
            walls.append((lid ^ region).volume())
        expected = length * 0.85 * 0.8
        guides.append(
            {
                "centre_mm": [x, y],
                "tongue_material_mm3": tongue,
                "slot_channel_plastic_mm3": intrusion,
                "side_wall_material_mm3": walls,
                "side_wall_expected_mm3": expected,
                "nominal_side_wall_mm": 0.85,
                "survives": tongue > length * 0.9
                and intrusion < 0.001
                and min(walls) > expected - 0.001,
            }
        )
    result = {
        "result": "PASS" if all(row["survives"] for row in guides) else "FAIL",
        "guides": guides,
        "path": (
            "The complete base/lid slide is checked by Lacuna at0.25mm plus "
            "actual-board surface edges at0.1mm X increments."
        ),
    }
    dump(out / "guide-survival.json", result)
    return result


def mesh_preflight(out, meshes, inspect_mesh, name, placements):
    declaration = {
        "schema_version": 1,
        "mesh_sha256": sha(out / f"{name}.stl"),
        "job_basename": name,
        "expected_component_count": len(placements),
        "envelope_mm": {"minimum": [0, 0, 0], "maximum": [220, 220, 220]},
        "components": {},
    }
    for part, delta in placements:
        mesh = meshes[part].copy()
        mesh.apply_translation(delta)
        triangles = np.asarray(mesh.triangles, dtype=np.float32).astype(float)
        canonical = sorted(
            tuple(sorted(tuple(round(v / 0.00001) for v in p) for p in tri))
            for tri in triangles
        )
        signature = hashlib.sha256(
            json.dumps(canonical, separators=(",", ":")).encode()
        ).hexdigest()
        declaration["components"]["component-" + signature[:16]] = {
            "label": "R4 " + part + " PROVISIONAL",
            "expected_bounds_mm": {
                "minimum": mesh.bounds[0].tolist(),
                "maximum": mesh.bounds[1].tolist(),
                "tolerance_mm": 0.001,
            },
            "policy": {"maximum_bridge_span_mm": 6.4},
            "bridge_authority": (
                "Nominal design allowance, not release: two flat head-bearing "
                "bridge shelves across the6.4mm well; rear roof supported at both"
                " pocket ends; chamfered lip. No removable membranes. Actual "
                "toolpaths and physical bridging NOT RUN."
            ),
        }
    sidecar = out / f"{name}.contact.json"
    dump(sidecar, declaration)
    report = inspect_mesh(
        out / f"{name}.stl", declarations_path=sidecar, supports="disabled"
    )
    dump(out / f"{name}.mesh-preflight.json", report)
    return report


def coupon_geometry(out, meshes, check_plan):
    instances = []
    transforms = {
        "front-base": matrix(),
        "front-lid": LID,
        "rear-base": matrix(z=2),
        "rear-lid": matrix(z=2) @ LID,
        "rear-jig": matrix(),
    }
    for name, transform in transforms.items():
        instances.append(
            {
                "id": name,
                "mesh": name + ".stl",
                "sha256": sha(out / (name + ".stl")),
                "matrix": transform.tolist(),
            }
        )
    closed = {row["id"]: row["matrix"] for row in instances}
    states, paths = [], []
    for name, start, end, active, moving in [
        (
            "front-lower",
            shifted(closed, ["front-lid"], [-1.2, 0, 20]),
            shifted(closed, ["front-lid"], [-1.2, 0, 0]),
            ["front-base", "front-lid"],
            ["front-lid"],
        ),
        (
            "front-slide",
            shifted(closed, ["front-lid"], [-1.2, 0, 0]),
            closed,
            ["front-base", "front-lid"],
            ["front-lid"],
        ),
        (
            "rear-lower",
            shifted(closed, ["rear-lid"], [-1.2, 0, 20]),
            shifted(closed, ["rear-lid"], [-1.2, 0, 0]),
            ["rear-base", "rear-lid"],
            ["rear-lid"],
        ),
        (
            "rear-slide",
            shifted(closed, ["rear-lid"], [-1.2, 0, 0]),
            closed,
            ["rear-base", "rear-lid"],
            ["rear-lid"],
        ),
        (
            "place-rear-in-jig",
            shifted(closed, ["rear-base", "rear-lid"], [0, 0, 25]),
            closed,
            ["rear-base", "rear-lid", "rear-jig"],
            ["rear-base", "rear-lid"],
        ),
    ]:
        states.extend(
            [
                {"id": name + "-start", "matrices": start},
                {"id": name + "-end", "matrices": end},
            ]
        )
        paths.append(
            {
                "id": name,
                "from_state": name + "-start",
                "to_state": name + "-end",
                "active": active,
                "moving": moving,
            }
        )
    # Front and rear coupon sets occupy distant actual coordinates, not overlapping.
    plan = {
        "schema": "lacuna.assembly-geometry/2",
        "instances": instances,
        "motion": {"states": states, "paths": paths},
    }
    dump(out / "coupon-geometry-plan.json", plan)
    sampled = check_plan(out / "coupon-geometry-plan.json", step_mm=0.25)
    dump(out / "coupon-sampled-geometry.json", sampled)
    jig = solid(meshes["rear-jig"])
    rear_base = solid(meshes["rear-base"]).translate([0, 0, 2])
    rear_lid_mesh = meshes["rear-lid"].copy()
    rear_lid_mesh.apply_transform(matrix(z=2) @ LID)
    rear_lid = solid(rear_lid_mesh)
    stops = {
        "base_positive_x_stop_overlap_at_0p2_mm3": (
            jig ^ rear_base.translate([0.2, 0, 0])
        ).volume(),
        "lid_negative_x_stop_overlap_at_0p2_mm3": (
            jig ^ rear_lid.translate([-0.2, 0, 0])
        ).volume(),
        "rear_separation_blocks_at_0p5_mm3": (
            rear_base ^ rear_lid.translate([0, 0, 0.5])
        ).volume(),
        "nominal_max_relative_reverse_slide_mm": 0.2,
        "physical_result": "NOT RUN",
    }
    dump(out / "coupon-fixture-check.json", stops)
    return sampled, stops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--board-mesh",
        type=Path,
        required=True,
        help="Exact official reference tessellation; SHA is pinned in source/README",
    )
    parser.add_argument("--lacuna", type=Path, default=REPO.parent / "lacuna")
    args = parser.parse_args()
    out, boardpath = args.output.resolve(), args.board_mesh.resolve()
    if sha(boardpath) != BOARD_SHA:
        parser.error(
            "Wrong board reference SHA; review a new tessellation before "
            "changing BOARD_SHA"
        )
    sys.path.insert(0, str(args.lacuna.resolve()))
    from tools.geometry.sampled import check_plan
    from tools.print.mesh_preflight import inspect_mesh

    names = [
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
    ]
    meshes = {name: trimesh.load(out / f"{name}.stl", force="mesh") for name in names}
    (out / "reference").mkdir(exist_ok=True)
    target = out / "reference/board.stl"
    if target.resolve() != boardpath:
        shutil.copyfile(boardpath, target)
    topology = {
        name: {
            "watertight": mesh.is_watertight,
            "components": len(mesh.split()),
            "minimum_triangle_area_mm2": float(mesh.area_faces.min()),
            "bounds_mm": mesh.bounds.tolist(),
        }
        for name, mesh in meshes.items()
    }
    base = solid(meshes["base"])
    assembled = meshes["lid"].copy()
    assembled.apply_transform(LID)
    lid = solid(assembled)
    geometry_plan(out, meshes)
    sampled = check_plan(out / "geometry-plan.json", step_mm=0.25)
    dump(out / "sampled-geometry.json", sampled)
    surface = board_surface_checks(out, boardpath, base, lid)
    guides = guide_checks(out, base, lid)
    full_mesh = mesh_preflight(
        out,
        meshes,
        inspect_mesh,
        "yoke-usb-housing-r4-layout",
        [("base", (15, 8, 0)), ("lid", (15, 53, 0))],
    )
    coupon_mesh = mesh_preflight(
        out,
        meshes,
        inspect_mesh,
        "yoke-usb-housing-r4-coupons-layout",
        COUPON_PLACEMENTS,
    )
    coupon_sampled, stops = coupon_geometry(out, meshes, check_plan)
    crossings = surface["base"]["crossings"] + sum(
        row["crossings"] for row in surface["slide"] + surface["lower_offset_lid"]
    )
    overlap = (base ^ lid).volume()
    retention = (base ^ lid.translate([0, 0, 0.5])).volume()
    checks = {
        "topology": all(
            row["watertight"]
            and row["components"] == 1
            and row["minimum_triangle_area_mm2"] > 1e-9
            for row in topology.values()
        ),
        "full_mesh_preflight": full_mesh["result"] == "PASS",
        "coupon_mesh_preflight": coupon_mesh["result"] == "PASS",
        "sampled_paths": sampled["collision_count"] == 0 and not sampled["errors"],
        "coupon_paths": coupon_sampled["collision_count"] == 0
        and not coupon_sampled["errors"],
        "guides": guides["result"] == "PASS",
        "board_surface_crossings": crossings == 0,
        "base_lid_clearance": abs(overlap) < 0.001,
        "rear_retention": retention > 0.001,
        "coupon_hard_stops": all(
            stops[key] > 0.001 for key in stops if key.endswith("_mm3")
        ),
    }
    report = {
        "revision": "R4",
        "state": "CATALOGUE_LAYOUT_STOCK_UNCONFIRMED",
        "result": "PASS" if all(checks.values()) else "FAIL",
        "scope": (
            "Nominal digital geometry only, not print approval or physical acceptance"
        ),
        "required_checks": checks,
        "topology": topology,
        "base_lid_overlap_mm3": overlap,
        "rear_hook_blocks_straight_lift_overlap_mm3": retention,
        "sampled_geometry": sampled["result"],
        "unreadable_instances": sampled["unreadable_instances"],
        "board_surface_crossings": crossings,
        "hardware": (
            "Catalogue RX geometry known; stock identity, screw tolerance "
            "authority and actual fits pending"
        ),
        "physical_tests": "NOT RUN",
        "slice_release_dispatch": "NOT RUN",
        "limits": [
            (
                "Samples can miss inter-sample or face-only collisions. Vendor "
                "board is nonmanifold."
            ),
            (
                "Smooth hardware envelopes establish axial overlap, not measured "
                "usable thread engagement."
            ),
            (
                "Mesh bridge allowance is nominal engineering judgement, not "
                "tested process capacity."
            ),
        ],
    }
    dump(out / "checks.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
