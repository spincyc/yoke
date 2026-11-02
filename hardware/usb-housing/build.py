#!/usr/bin/env python3
"""Render the two native OpenSCAD parts and preserve their facets on one plate."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess

HERE = Path(__file__).resolve().parent


def translated_stl(path: Path, offset: tuple[float, float, float]) -> list[bytes]:
    data = path.read_bytes()
    count = struct.unpack_from('<I', data, 80)[0]
    if len(data) != 84 + 50 * count:
        raise ValueError('Expected binary STL')
    records = []
    for i in range(count):
        values = list(struct.unpack_from('<12fH', data, 84 + 50 * i))
        for j in (3, 6, 9):
            for axis in range(3):
                values[j + axis] += offset[axis]
        records.append(struct.pack('<12fH', *values))
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    openscad = shutil.which('openscad')
    if not openscad:
        raise SystemExit('OpenSCAD with the Manifold backend is required')
    for part in ('base', 'lid', 'lid-printed'):
        subprocess.run([openscad, '--backend', 'Manifold', '--export-format',
                        'binstl', '-D', f'part="{part}"', '-o',
                        str(args.output / f'{part}.stl'), str(HERE / 'housing.scad')],
                       check=True)
    groups = [translated_stl(args.output / 'base.stl', (12, 3, 0)),
              translated_stl(args.output / 'lid-printed.stl', (12, 45, 0))]
    records = groups[0] + groups[1]
    (args.output / 'yoke-usb-housing-r1.stl').write_bytes(
        b'Yoke USB housing R1; base and inverted lid'.ljust(80, b'\0')
        + struct.pack('<I', len(records)) + b''.join(records))

    plate = args.output / 'yoke-usb-housing-r1.stl'
    declarations = {'schema_version': 1,
                    'mesh_sha256': hashlib.sha256(plate.read_bytes()).hexdigest(),
                    'job_basename': 'yoke-usb-housing-r1',
                    'expected_component_count': 2,
                    'envelope_mm': {'minimum': [0, 0, 0], 'maximum': [220, 220, 220]},
                    'components': {}}
    for index, group in enumerate(groups):
        triangles = [[struct.unpack('<12fH', r)[j:j+3] for j in (3, 6, 9)]
                     for r in group]
        canonical = sorted(tuple(sorted(tuple(round(v / 0.00001) for v in point)
                                        for point in tri)) for tri in triangles)
        signature = hashlib.sha256(json.dumps(canonical, separators=(',', ':')).encode('ascii')).hexdigest()
        points = [point for tri in triangles for point in tri]
        row = {'label': ['R1 base', 'R1 manufactured lid'][index],
               'expected_bounds_mm': {
                   'minimum': [round(min(p[a] for p in points), 6) for a in range(3)],
                   'maximum': [round(max(p[a] for p in points), 6) for a in range(3)],
                   'tolerance_mm': 0.001}}
        if index == 1:
            row['policy'] = {'maximum_bridge_span_mm': 6.2}
            row['bridge_authority'] = ('R1 first-article scope: four5.25mm AF nut-pocket roofs, '
                'with0.20mm sacrificial3mm screw-bore diaphragms forming complete bridges. '
                'Circumscribed span6.063mm. Inspect flat seats and hand-clear/ream lid bores before assembly.')
        declarations['components']['component-' + signature[:16]] = row
    plate.with_suffix('.contact.json').write_text(json.dumps(declarations, indent=2) + '\n')


if __name__ == '__main__':
    main()
