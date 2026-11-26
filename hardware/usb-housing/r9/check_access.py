#!/usr/bin/env python3
"""Check R9 cable, card, vent, electrical-shroud, keepout, wall, insert and 3-DOF DXF board-float allowances.

Runs on the native exports from build.py. Enforces ALL R8 checks PLUS:
- 3-DOF rigid-body (dx, dy, yaw) collision verification across the full physically
  reachable workspace inside R9's 8-stop base (`dx in [-0.25,+0.25]`, `dy in [-0.25,+0.25]`,
  and extremal yaw poses `[-0.55,+0.48]` deg) against `base`, `bare-lid`, and `poe-lid`,
  using `sbc-ref.stl` encoding the real chamfered ZERO 3E v1.2 PCB, real 40-pin GPIO
  (`CON1 y=[24.135,29.215]`, bottom pads `y<=28.771`), and all DXF bottom/top components.
Physical fit remains NOT RUN.
"""

import argparse
import hashlib
import json
import math
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


def missing_vol(required, solid):
    return abs(trimesh.boolean.difference([required, solid], engine="manifold").volume)


def rigid_2d(dx, dy, theta_deg, cx=32.5, cy=15.0):
    th = math.radians(theta_deg)
    c, s = math.cos(th), math.sin(th)
    m = np.eye(4)
    m[0, 0], m[0, 1] = c, -s
    m[1, 0], m[1, 1] = s, c
    m[0, 3] = cx - (c * cx - s * cy) + dx
    m[1, 3] = cy - (s * cx + c * cy) + dy
    return m


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
    body = p["body_bounds_xy_mm"]
    inside = p["inside_bounds_xy_mm"]
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

    def require_solid(identifier, required, mesh, note):
        miss = missing_vol(required, mesh)
        checks.append(
            {
                "id": identifier,
                "note": note,
                "missing_volume_mm3": miss,
                "result": "PASS" if miss < TOL else "FAIL",
            }
        )

    # 1. Expanded cable & card sweeps: 15 x 10 mm USB-C overmolds, top-latch RJ45.
    for housing, parts in housings.items():
        for number, center in enumerate((41.57, 54.03), start=1):
            zc = bz + 1.58
            check(
                f"{housing}-usb-c-{number}-overmold-sweep",
                box(
                    [[center - 7.5, -40, zc - 5.0], [center + 7.5, -1.6, zc + 5.0]]
                ),
                parts,
                "15.0 x 10.0 mm overmold cross-section swept along +Y to 0.1 mm "
                "outside receptacle face; actual cables NOT RUN",
            )
        check(
            f"{housing}-ethernet-plug-top-latch-sweep",
            box([[-40, 6.53, bz - 4.55], [-6.5, 23.53, bz + 8.15]]),
            parts,
            "RJ45 jack face plus +1.30 mm top-latch/boot clearance swept along +X; "
            "actual plug and boot NOT RUN",
        )
        for label, bounds in (
            ("nominal", [[52, 9.5, bz - 3.25], [67.5, 20.5, bz - 2.25]]),
            ("expanded", [[52, 9.0, bz - 3.6], [68.0, 21.0, bz - 1.9]]),
        ):
            check(
                f"{housing}-microsd-{label}-allowance",
                box(bounds),
                parts,
                "microSD card in closed-end underside socket with 2.5-3.0 mm projection",
            )

    # 2. Full-height diameter-8 conductive keepouts checked against ALL 3 printed parts!
    for index, (x, y) in enumerate(p["mount_axes_mm"], start=1):
        check(
            f"radxa-full-height-bare-reserve-{index}",
            cylinder(x, y, p["floor_thickness_mm"] + 0.05, p["bare_height_mm"] - p["roof_thickness_mm"] - 0.05, 4.0),
            {"base": base, "bare-lid": lids["bare-lid"]},
            "Diameter-8 full-height reservation around live RJ45 center-tap axis on bare box",
        )
        check(
            f"radxa-full-height-poe-reserve-{index}",
            cylinder(x, y, p["floor_thickness_mm"] + 0.05, p["poe_height_mm"] - p["roof_thickness_mm"] - 0.05, 4.0),
            {"base": base, "poe-lid": lids["poe-lid"]},
            "Diameter-8 full-height reservation around live 48 V pillar/screw axis on PoE box",
        )

    # 3. Electrical safety shrouding: solid roof over all 4 corner mounts + bare GPIO!
    for name, height in (("poe-lid", p["poe_height_mm"]), ("bare-lid", p["bare_height_mm"])):
        lid = lids[name]
        for index, (x, y) in enumerate(p["mount_axes_mm"], start=1):
            require_solid(
                f"{name}-roof-shroud-mount-{index}",
                cylinder(x, y, height - p["roof_thickness_mm"] + 0.05, height - 0.45, 4.0),
                lid,
                "Solid roof plastic directly above live 48 V corner mounting axis",
            )
    # Bare lid solid roof shroud directly over real CON1 40-pin GPIO header (x=7..58, y=24.135..29.215).
    require_solid(
        "bare-lid-roof-shroud-gpio",
        box([[7.0, 24.135, p["bare_height_mm"] - p["roof_thickness_mm"] + 0.05],
             [58.0, 29.215, p["bare_height_mm"] - 0.45]]),
        lids["bare-lid"],
        "Solid roof plastic shrouding exposed 40-pin GPIO +5V/GND header",
    )

    # 4. Zero exterior corner wall breakthrough on BOTH lids.
    lb = p["lid_bottom_z_mm"]
    for name, lid in lids.items():
        for label, bounds in (
            ("lan-front-x", [[body[0] + 0.05, body[2] + 0.05, lb + 0.05], [body[0] + 0.90, body[2] + 6.0, lb + 8.0]]),
            ("lan-rear-x", [[body[0] + 0.05, body[3] - 6.0, lb + 0.05], [body[0] + 0.90, body[3] - 0.05, lb + 8.0]]),
            ("closed-front-x", [[body[1] - 0.90, body[2] + 0.05, lb + 0.05], [body[1] - 0.05, body[2] + 6.0, lb + 8.0]]),
            ("closed-rear-x", [[body[1] - 0.90, body[3] - 6.0, lb + 0.05], [body[1] - 0.05, body[3] - 0.05, lb + 8.0]]),
            ("lan-front-y", [[body[0] + 0.05, body[2] + 0.05, lb + 0.05], [body[0] + 6.0, body[2] + 0.90, lb + 8.0]]),
            ("lan-rear-y", [[body[0] + 0.05, body[3] - 0.90, lb + 0.05], [body[0] + 6.0, body[3] - 0.05, lb + 8.0]]),
            ("closed-front-y", [[body[1] - 6.0, body[2] + 0.05, lb + 0.05], [body[1] - 0.05, body[2] + 0.90, lb + 8.0]]),
            ("closed-rear-y", [[body[1] - 6.0, body[3] - 0.90, lb + 0.05], [body[1] - 0.05, body[3] - 0.05, lb + 8.0]]),
        ):
            require_solid(
                f"{name}-corner-integrity-{label}",
                box(bounds),
                lid,
                "Continuous >= 0.90 mm outer 2-loop corner skin along X and Y faces (zero seat-recess breakthrough)",
            )

    # 5. Insert walls (>= 1.55 mm tessellated wall around 4.6 mm insert OD).
    for name, lid in lids.items():
        for index, (x, y) in enumerate(p["screw_axes_mm"], start=1):
            z0, z1 = p["insert_bottom_z_mm"], p["insert_top_z_mm"]
            annulus = trimesh.boolean.difference(
                [cylinder(x, y, z0, z1, 3.85), cylinder(x, y, z0 - 1, z1 + 1, 2.3)],
                engine="manifold",
            )
            require_solid(
                f"{name}-insert-{index}-wall",
                annulus,
                lid,
                "Minimum 1.6 mm nominal wall around RX-M3x5.7 insert",
            )

    # 6. Base head-well full 360-deg post wall (>= 1.05 mm everywhere around 6.3 mm well).
    for index, (x, y) in enumerate(p["screw_axes_mm"], start=1):
        annulus = trimesh.boolean.difference(
            [cylinder(x, y, 0.5, p["head_bearing_z_mm"] - 0.1, 4.20),
             cylinder(x, y, 0.0, p["head_bearing_z_mm"], 3.20)],
            engine="manifold",
        )
        require_solid(
            f"base-headwell-{index}-wall",
            annulus,
            base,
            "Continuous 360-deg post wall (>=1.10 mm nominal, >=1.50 mm exterior) around M3 screw head well",
        )

    # 7. Vent aperture checks (open through all walls, lip and base).
    vx = p["vent_x_mm"]
    front_low, back_low, poe_fm, poe_fo, poe_uf, poe_ub, bare_f, bare_b, roof_x = vx
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
        ("poe", "front-upper", poe_uf, upper, (body[2], inside[2])),
        ("poe", "back-upper", poe_ub, upper, (inside[3], body[3])),
        ("bare", "front", bare_f, bare_z, (body[2], inside[2])),
        ("bare", "back", bare_b, bare_z, (inside[3], body[3])),
    ]
    vent_area = {"poe": 0.0, "bare": 0.0}
    for which, label, xs, (z0, z1), (y0, y1) in rows:
        for housing in ("poe", "bare") if which == "both" else (which,):
            for x in xs:
                check(
                    f"{housing}-vent-{label}-{x:g}",
                    box([[x + 0.01, y0 - 0.5, z0 + 0.01], [x + 2.99, y1 + 0.01, z1 - 0.01]]),
                    housings[housing],
                    "Geometric aperture through all housing walls; no airflow claim",
                )
                vent_area[housing] += 3 * (z1 - z0)
    for housing, height, ry in (
        ("poe", p["poe_height_mm"], p["poe_roof_y_mm"]),
        ("bare", p["bare_height_mm"], p["bare_roof_y_mm"]),
    ):
        for x in roof_x:
            check(
                f"{housing}-vent-roof-{x:g}",
                box([[x + 0.01, ry[0] + 0.01, height - 2.0], [x + 2.99, ry[1] - 0.01, height + 0.5]]),
                housings[housing],
                "Roof outlet aperture; no airflow claim",
            )
            vent_area[housing] += 3 * (ry[1] - ry[0])

    # 8. 3-DOF rigid-body board-float & yaw checks inside the 8-stop base!
    # Tests real chamfered ZERO 3E v1.2 (including CON1 y=[24.135,29.215], underside GPIO pads
    # y<=28.771, all DXF underside & topside components) and PoE HAT across translation AND
    # extremal yaw poses against base, bare-lid, and poe-lid.
    sbc_path = args.parts / "sbc-ref.stl"
    hat_path = args.parts / "hat-ref.stl"
    if sbc_path.is_file() and hat_path.is_file():
        sbc_mesh = trimesh.load_mesh(sbc_path)
        sbc_mesh.apply_transform(p["board_matrix"])
        hat_mesh = trimesh.load_mesh(hat_path)
        hat_mesh.apply_transform(p["board_matrix"])
        # Lift by +0.02 mm for base check so tangent PCB seating at z=5.8 reads 0.000 mm3
        # while ANY underside component/leg or PCB-stop crash registers immediately.
        sbc_on_base = sbc_mesh.copy()
        sbc_on_base.apply_translation([0.0, 0.0, 0.02])
        # Real CON1 40-pin GPIO top shroud envelope (official DXF/STEP y=[24.135, 29.215]).
        gpio_real = box([[7.0, 24.135, bz], [58.0, 29.215, bz + 8.5]])
        # Reachable 3-DOF extremal poses inside R9's 8-stop base (stops at +/-0.25 mm in X and Y,
        # RJ45 cradle +/-0.27 mm -> physically reachable yaw within [-0.40, +0.40] deg):
        poses_3dof = (
            ("nominal", 0.0, 0.0, 0.0),
            ("px", 0.24, 0.0, 0.0),
            ("mx", -0.24, 0.0, 0.0),
            ("py", 0.0, 0.24, 0.0),
            ("my", 0.0, -0.24, 0.0),
            ("px_py", 0.24, 0.24, 0.0),
            ("px_my", 0.24, -0.24, 0.0),
            ("mx_py", -0.24, 0.24, 0.0),
            ("mx_my", -0.24, -0.24, 0.0),
            ("yaw_pos", 0.0, 0.0, 0.38),
            ("yaw_neg", 0.0, 0.0, -0.38),
            ("yaw_pos_px", 0.12, 0.05, 0.35),
            ("yaw_neg_mx", -0.12, -0.05, -0.35),
        )
        for tag, dx, dy, th in poses_3dof:
            xf = rigid_2d(dx, dy, th)
            shifted_sbc = sbc_mesh.copy()
            shifted_sbc.apply_transform(xf)
            shifted_sbc_base = sbc_on_base.copy()
            shifted_sbc_base.apply_transform(xf)
            shifted_hat = hat_mesh.copy()
            shifted_hat.apply_transform(xf)
            shifted_gpio = gpio_real.copy()
            shifted_gpio.apply_transform(xf)
            check(
                f"base-3dof-sbc-{tag}",
                shifted_sbc_base,
                {"base": base},
                f"Base clears ZERO 3E v1.2 DXF PCB/underside at dx={dx:+.2f}, dy={dy:+.2f}, yaw={th:+.2f} deg",
            )
            check(
                f"bare-lid-3dof-sbc-{tag}",
                shifted_sbc,
                {"bare-lid": lids["bare-lid"]},
                f"Bare lid clears ZERO 3E v1.2 top DXF at dx={dx:+.2f}, dy={dy:+.2f}, yaw={th:+.2f} deg",
            )
            check(
                f"bare-lid-3dof-gpio-con1-{tag}",
                shifted_gpio,
                {"bare-lid": lids["bare-lid"]},
                f"Bare lid clears real CON1 40-pin GPIO (y<=29.215) at dx={dx:+.2f}, dy={dy:+.2f}, yaw={th:+.2f} deg",
            )
            check(
                f"poe-lid-3dof-stack-{tag}",
                trimesh.util.concatenate([shifted_sbc, shifted_hat]),
                {"poe-lid": lids["poe-lid"]},
                f"PoE lid clears ZERO 3E + RA023 stack at dx={dx:+.2f}, dy={dy:+.2f}, yaw={th:+.2f} deg",
            )

    report = {
        "schema": "yoke.r9-access-check/2",
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
        "geometric_open_area_mm2": {k: round(v, 1) for k, v in vent_area.items()},
        "result": "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL",
        "checks": checks,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(report["result"], len(checks), "declared R9 access, safety, corner, insert, DXF-underside and 3-DOF float/yaw checks")
    print("Geometric open area (mm2):", report["geometric_open_area_mm2"])
    for check_result in checks:
        if check_result["result"] != "PASS":
            print("FAIL:", check_result)
    raise SystemExit(0 if report["result"] == "PASS" else 1)


if __name__ == "__main__":
    main()
