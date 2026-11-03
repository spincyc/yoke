#!/usr/bin/env python3
"""Create the R3 monochrome build/test guide from exact delivered meshes."""

from __future__ import annotations

import argparse
import re
import runpy
import subprocess
import tempfile
from pathlib import Path

import trimesh
from reportlab.lib.colors import black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    output = args.output
    scratch = HERE.parents[2] / ".scratch/housing-r3-author/documents"
    scratch.mkdir(parents=True, exist_ok=True)
    render = runpy.run_path(str(HERE / "render.py"))["render"]
    render(output, "preview.png", False)
    render(output, "lid-detail.png", True)
    render(output, "exploded.png", False, 22)
    base = trimesh.load(output / "base.stl", force="mesh")
    lid = trimesh.load(output / "lid.stl", force="mesh")
    lid.apply_transform([[1, 0, 0, 0], [0, -1, 0, 30], [0, 0, -1, 20], [0, 0, 0, 1]])
    pdf = canvas.Canvas(str(output / "assembly.pdf"), pagesize=(612, 792), invariant=1)
    pdf.setTitle("Yoke USB housing R3 - build, inspect and test")

    def text(x: float, y: float, s: str, size: float = 10) -> None:
        s = re.sub(r"(?<=[a-z])(?=\d)", " ", s)
        s = re.sub(r"(?<=\d)(?=mm|wide|high|N\b|s\b|minutes)", " ", s)
        s = re.sub(r"([:;,])(?=\S)", r"\1 ", s)
        s = re.sub(r"\b(AF|D)(?=\d)", r"\1 ", s)
        s = re.sub(r"\s*(<=|>=)\s*", r" \1 ", s)
        pdf.setFillColor(black)
        pdf.setFont("Helvetica", size)
        pdf.drawString(x, y, s)

    def lines(
        items: list[str], y: float, x: float = 34, size: float = 10, gap: float = 15
    ) -> float:
        for item in items:
            text(x, y, item, size)
            y -= gap
        return y

    def title(page: int, heading: str) -> None:
        text(34, 755, f"YOKE / USB HOUSING R3 / {page} OF 4", 11)
        text(34, 729, heading, 19)

    def foot() -> None:
        text(
            34,
            24,
            "DESIGN REVIEW ONLY / NOT PRINTED / all physical observations NOT RUN",
            8,
        )

    def section(
        mesh: trimesh.Trimesh,
        plane: str,
        at: float,
        bounds: tuple,
        origin: tuple,
        scale: float,
        hatch: bool = True,
        reverse: bool = False,
    ) -> None:
        normal = [0, 1, 0] if plane == "y" else [1, 0, 0]
        point = [0, at, 0] if plane == "y" else [at, 0, 0]
        cut = mesh.section(plane_origin=point, plane_normal=normal)
        if cut is None:
            return
        axes = [0, 2] if plane == "y" else [1, 2]
        lo, hi, zlo, zhi = bounds
        pdf.saveState()
        clip = pdf.beginPath()
        clip.rect(origin[0], origin[1], (hi - lo) * scale, (zhi - zlo) * scale)
        pdf.clipPath(clip, stroke=0, fill=0)
        path = pdf.beginPath()
        for contour in cut.discrete:
            q = (contour[:, axes] - [lo, zlo]) * scale + origin
            path.moveTo(*q[0])
            for v in q[1:]:
                path.lineTo(*v)
            path.close()
        pdf.setFillColor(white)
        pdf.setStrokeColor(black)
        pdf.setLineWidth(0.65)
        pdf.drawPath(path, fill=1, stroke=1, fillMode=0)
        if hatch:
            pdf.saveState()
            pdf.clipPath(path, stroke=0, fill=0, fillMode=0)
            pdf.setLineWidth(0.25)
            x, y = origin
            w = (hi - lo) * scale
            h = (zhi - zlo) * scale
            for k in range(-int(h) - 20, int(w) + int(h) + 20, 7):
                if reverse:
                    pdf.line(x + k, y, x + k - h, y + h)
                else:
                    pdf.line(x + k, y, x + k + h, y + h)
            pdf.restoreState()
        pdf.restoreState()

    def leader(label: str, x: float, y: float, px: float, py: float) -> None:
        text(x, y, label, 9)
        pdf.setLineWidth(0.5)
        pdf.line(x, y - 3, px, py)

    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        ink = Path(temporary) / "exploded.png"
        render(output, "", False, 22, True, ink)
        title(1, "Parts, hardware and orientation")
        lines(
            [
                (
                    "Two printed case parts: R3 B base and R3 L lid. Screws "
                    "use real METAL nuts."
                ),
                (
                    "There are no printed threads. This R3 design has not "
                    "been sliced or printed."
                ),
                "Central body:79.6 x38 x20mm; end-ear envelope:90.4 x39.6 x20mm.",
                "Use four M2.5 x16mm Phillips pan-head screws: head <=5 x2.12mm, PH1,",
                (
                    "under-head length15.65-16.35mm, pitch0.45. Do not "
                    "reuse R1 20mm screws."
                ),
                "Use four M2.5 hex nuts: AF4.82-5.00mm, thickness1.75-2.00mm.",
            ],
            697,
        )
        pdf.drawImage(
            ImageReader(str(ink)), 38, 270, width=536, height=330, mask="auto"
        )
        text(40, 268, "BASE: four tall keeper fingers, two board-locating pegs", 9)
        text(40, 609, "LID: four side-loading nut traps; three matching key slots", 9)
        lines(
            [
                (
                    "Supply PH1 driver (shaft <=4.5mm), caliper/depth "
                    "gauge,3mm hand twist bit with"
                ),
                (
                    "118-degree tip (point length <=1.0mm), depth stop, "
                    "wood blocks and nut tray."
                ),
                (
                    "Caliper tips <=1mm for nut-floor access; depth probe "
                    "<=2.5mm for blind bore."
                ),
                "For service: blunt1.2mm pin. For tests: padded mat,0-10N gauge/scale,",
                (
                    "actual Ethernet/two USB-C cables, powered host with "
                    "temperature/time readout."
                ),
                "Optional coupon: CB +CL, using ONE of the same16mm screws and nuts.",
                "Base and lid print outside-face down. Each has FOUR removable bore",
                "membranes: EIGHT total. They are temporary0.20mm bridge skins.",
            ],
            250,
        )
        foot()
        pdf.showPage()

    title(2, "Exact-mesh sections and load paths")
    text(
        34,
        698,
        "A-A: front-left ear, plane Y=0; X horizontal, Z upward. Dimensions mm.",
        10,
    )
    origin = (55, 430)
    bounds = (-16.5, -6, 0, 20.5)
    scale = 11
    section(base, "y", 0, bounds, origin, scale)
    section(lid, "y", 0, bounds, origin, scale, reverse=True)

    def project_a(x, z):
        return (origin[0] + (x - bounds[0]) * scale, origin[1] + z * scale)

    pdf.setFillGray(0.82)
    pdf.setStrokeColor(black)
    xx, zz = project_a(-13.7, 0.28)
    pdf.rect(xx, zz, 5 * scale, 2.12 * scale, fill=1, stroke=1)
    xx, zz = project_a(-12.45, 2.4)
    pdf.rect(xx, zz, 2.5 * scale, 16.35 * scale, fill=1, stroke=1)
    xx, zz = project_a(-14.0868, 15.4)
    pdf.rect(xx, zz, 5.7736 * scale, 2 * scale, fill=1, stroke=1)
    leader("Blind tip ceiling19.2; max tip18.75", 225, 652, *project_a(-11.2, 19.2))
    leader("Nut roof17.65 / keeper top17.85", 225, 620, *project_a(-15.1, 17.65))
    leader(
        "Metal nut: floor15.4, full2mm engagement", 225, 588, *project_a(-11.2, 15.4)
    )
    leader("Hard stop faces meet at10.15", 225, 555, *project_a(-8.6, 10.15))
    leader("Head bearing plane2.4", 225, 489, *project_a(-11.2, 2.4))
    leader("Head max2.12; nominal recess0.28", 225, 458, *project_a(-13.7, 0.28))
    text(
        34,
        406,
        "Hatching: base / and lid \\. Grey is nominal metal; threads are schematic.",
        9,
    )
    text(34, 380, "B-B: rear tongue, plane X=15", 11)
    section(base, "x", 15, (29.8, 34.5, 9.2, 12.2), (50, 272), 34)
    section(lid, "x", 15, (29.8, 34.5, 9.2, 12.2), (50, 272), 34, reverse=True)
    lines(
        [
            "Tongue1.4wide x1.35high;",
            "0.30 top/entry chamfers.",
            "Slot1.90wide:0.25 each side.",
            "Tongue ceiling clearance0.35.",
            "Body-wall gap0.15 remains.",
            "Keys locate; ear stops take preload.",
        ],
        367,
        x=250,
        size=10,
    )
    text(34, 242, "C-C: round board peg, plane Y=3.599942; nominal board context", 11)
    section(base, "y", 3.599942, (0.5, 6.5, 6.3, 9.6), (50, 130), 30)
    section(lid, "y", 3.599942, (0.5, 6.5, 6.3, 9.6), (50, 130), 30, reverse=True)
    pdf.setFillGray(0.9)
    for lo, hi in [(0.5, 3.549904 - 1.4), (3.549904 + 1.4, 6.5)]:
        pdf.rect(
            50 + (lo - 0.5) * 30,
            130 + (6.9 - 6.3) * 30,
            (hi - lo) * 30,
            1.6 * 30,
            fill=1,
            stroke=1,
        )
    lines(
        [
            "Pad faces6.88 /8.52:1.64 capture.",
            "Round pegD2.35 in STEP D2.75 hole.",
            "Diagonal peg relieved to1.6 along",
            "hole baseline;2.35 transverse.",
            "Lid peg recessD3, ceiling9.15.",
            "Actual pad thickness/fit must be tested.",
        ],
        228,
        x=250,
        size=10,
    )
    lines(
        [
            "Load: metal head -> base ear -> ear stop -> lid ear -> metal nut.",
            "Screw preload bypasses the PCB. Supports capture its bare mounting pads.",
            "No qualified tightening torque or load rating is claimed.",
        ],
        95,
        size=9,
        gap=13,
    )
    foot()
    pdf.showPage()

    title(3, "Clean, load nuts, then close")
    lines(
        [
            (
                "1. Cool parts. Support the broad case body on "
                "scrap-wood blocks; leave the"
            ),
            (
                "   tall keeper fingers and head wells unsupported in "
                "free space. Never bear"
            ),
            (
                "   drilling force on a keeper finger. Keep the PCB "
                "away from all drilling."
            ),
            "2. Hand-clear FOUR base membranes from the mating-ear face, bit directed",
            "   toward the head recess. Set TIP depth to9.0mm from that mating face.",
            "3. Hand-clear FOUR lid membranes from its mating-ear face, bit directed",
            "   toward the nut pocket. Set TIP depth to6.5mm from that mating face.",
            (
                "   Include the <=1mm drill point in these depths. No "
                "power drill or top drilling."
            ),
            "4. Remove swarf. Require eight3.0-3.2mm clear bores, intact flat head/nut",
            "   seats, unbroken keepers and blind caps. Do not enlarge the nut flats.",
        ],
        698,
    )
    text(
        34,
        524,
        "5. LID ALONE: slide each metal nut inward through its OUTBOARD slot.",
        11,
    )
    section(lid, "y", 0, (-20, -6, 13.8, 18.4), (50, 418), 15, reverse=True)
    pdf.setFillGray(0.85)
    pdf.rect(50, 418 + (15.4 - 13.8) * 15, 5.77 * 15, 2 * 15, fill=1, stroke=1)
    pdf.setLineWidth(1)
    pdf.line(50, 413, 155, 413)
    pdf.line(155, 413, 146, 417)
    pdf.line(155, 413, 146, 409)
    text(280, 476, "Align nut flats with slot walls.", 10)
    text(280, 459, "Push gently with a blunt pin.", 10)
    text(280, 442, "Lid remains level above a tray.", 10)
    text(280, 425, "Nuts are loose until halves seat.", 10)
    lines(
        [
            (
                "6. Keep PCB parallel: Ethernet faces the larger "
                "full-height LEFT end opening."
            ),
            (
                "   Align round hole, then relieved hole; lower "
                "onto both pegs without force."
            ),
            (
                "7. Keep everything level over the nut tray. Lower lid "
                "straight down: three"
            ),
            (
                "   tongues enter their slots; four base fingers close "
                "the nut loading exits."
            ),
            "   Ear faces must meet freely while the body seam remains0.15mm nominal.",
            "8. Insert four16mm screws upward through the recessed head wells. Start",
            "   all nuts, then lightly snug alternating corners with the PH1 driver.",
            (
                "   Heads stay below the base. Nuts are captive with "
                "the halves fully seated."
            ),
            (
                "   Stop for bow, cracks, whitening, spinning nuts or "
                "rising torque before stops."
            ),
        ],
        378,
    )
    text(34, 224, "Coupon first, after a future print release", 12)
    lines(
        [
            "CB/CL reproduce this actual corner: head well, nut trap and keeper, front",
            "tongue and round PCB peg. Use one of the specified screws/nuts. Clear",
            "TWO membranes with the same depth limits. Test nut loading and capture",
            "over a tray, free key engagement, full stop seating and head recess.",
            (
                "Do not infer full-board fit or strength from the "
                "coupon. A coupon also needs"
            ),
            "its own exact-artifact review/release and clear-bed authorization.",
            (
                "Before screws, hold the coupon/empty case stops "
                "fully seated over a tray."
            ),
            (
                "Gently turn through six orientations; require all "
                "nuts retained and restrained"
            ),
            (
                "against free rotation. Reopen level into the tray "
                "to check controlled release."
            ),
        ],
        201,
    )
    foot()
    pdf.showPage()

    title(4, "Measure, test and service")
    lines(
        [
            "Physical measurements and tests remain NOT RUN. Record actual dimensions.",
            "Before board assembly, use the coupon/parts and measured hardware:",
            "A = base mating face to head bearing land (nominal7.75).",
            "B = lid mating face to blind tip ceiling (nominal9.05).",
            "C = lid mating face to nut floor (nominal5.25).",
            (
                "Measure C with thin jaws: one on the mating land, one "
                "through the nut slot."
            ),
            "For A, seat a screw; measure protrusion P above the mating face: A=L-P.",
            (
                "Probe B directly through the blind bore. Use light "
                "pressure; do not pierce cap."
            ),
            (
                "L = actual under-head screw length; H = actual head "
                "height; T = nut thickness."
            ),
            "Require A+B-L >=0.30mm tip clearance, and L-(A+C+T) >=0.45mm",
            "beyond the nut. Require actual head recess depth-H >=0.15mm.",
            "Nominal worst hardware:0.45mm tip clearance,0.65mm beyond a2mm nut,",
            (
                "0.28mm head recess. These are hardware-only numbers, "
                "not print tolerances."
            ),
            (
                "Reject or revise the stack if measured acceptance "
                "fails; never force a long screw."
            ),
        ],
        698,
        gap=14,
    )
    lines(
        [
            "With actual board unpowered:",
            "- Measure pad-to-pad thickness against the1.64mm nominal support gap.",
            (
                "  Require all four pads seated, no visible bow and no "
                "perceptible rocking."
            ),
            (
                "- Require pegs enter freely, ear stops seat by hand "
                "and all three keys slide"
            ),
            (
                "  without force. No screw may pull an interfering case "
                "or board into position."
            ),
            (
                "- Test full insertion/release of Ethernet and both "
                "actual USB-C cables five"
            ),
            "  times. USB-C overmould assumption is <=12wide x7high.",
            "- Over a padded mat, apply a measured5N cable pull for10s in service",
            (
                "  directions. Require no case/board movement, cracking "
                "or interrupted seating."
            ),
            "- Only after fit passes, run30minutes of intended powered load with vents",
            (
                "  clear. Log ambient/CPU temperature. Stop at75C or a "
                "lower applicable limit."
            ),
        ],
        472,
        gap=14,
    )
    text(34, 302, "Reverse service sequence", 12)
    lines(
        [
            "Disconnect power and cables. Work over a tray. Remove four screws while",
            (
                "holding both halves together; nuts remain trapped "
                "while the case stays shut."
            ),
            (
                "Lift the lid straight up and LEVEL over the tray. As "
                "keepers withdraw, nut"
            ),
            "exits reopen; nuts can fall out before the lid is fully detached.",
            "To replace a nut with lid removed, push it outward through the inner1.5mm",
            (
                "service hole using the blunt1.2mm pin. Catch it in the "
                "tray. Never pry a roof."
            ),
            (
                "Lift the board off both pegs without levering "
                "components. Reinspect all seats,"
            ),
            (
                "keepers, keys and pegs before reassembly. Stop after "
                "any crack or deformation."
            ),
        ],
        278,
        gap=14,
    )
    text(34, 148, "Scope and release", 12)
    lines(
        [
            "Bare ZERO3E V1.2 only: no PoE HAT, heatsink, bracket or ingress rating.",
            (
                "Digital checks are nominal and sampled; the vendor "
                "board mesh is not manifold."
            ),
            "R3 changes mechanics and hardware length; no R1/R2 approval carries over.",
            (
                "No slice, printer dispatch or physical validation is "
                "supplied. Any future job"
            ),
            "needs fresh exact-file review/release and an explicitly clear bed.",
        ],
        124,
        size=9,
        gap=14,
    )
    foot()
    pdf.save()
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            "2",
            "-l",
            "2",
            "-singlefile",
            "-png",
            "-r",
            "150",
            str(output / "assembly.pdf"),
            str(output / "section"),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
