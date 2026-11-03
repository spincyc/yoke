#!/usr/bin/env python3
"""Rebuild the reviewed board mesh from Radxa's exact STEP, using CadQuery."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
STEP_SHA = "97b0a1100cb7cb1e0fbd5aed50f8d6b0422b8885580f937163bdd119ceaab4ca"
MESH_SHA = "1c1268c8af4b1a6c0b07fec07b3b28c6662e5d10c0c1b794c1f1083f31af8f27"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO / ".scratch/usb-housing-r4-reference/board.stl",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((REPO / ".scratch").resolve()):
        parser.error("Reference output must stay in this repository's .scratch")
    if sha(args.step) != STEP_SHA:
        parser.error("Wrong STEP SHA; use the reviewed rs109_pcba.stp reference")

    import cadquery as cq

    output.parent.mkdir(parents=True, exist_ok=True)
    board = cq.importers.importStep(str(args.step))
    # Original exporter defaults: binary STL, relative=True, parallel=True.
    # The 0.05 tolerance is relative edge-scaled deflection, not 0.05 mm.
    cq.exporters.export(board, str(output), tolerance=0.05, angularTolerance=0.1)
    if sha(output) != MESH_SHA:
        parser.error(
            "Reference mesh differs; use the documented CadQuery/OCCT versions "
            "or deliberately review a new tessellation"
        )
    print(f"PASS: exact reviewed board mesh {MESH_SHA}")


if __name__ == "__main__":
    main()
