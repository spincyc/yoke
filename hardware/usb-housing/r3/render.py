#!/usr/bin/env python3
"""Render exact STL facets with feature edges (numpy, trimesh, Pillow)."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def render(
    directory: Path,
    filename: str,
    lid_only: bool,
    explode: float = 0,
    ink: bool = False,
    target: Path | None = None,
) -> None:
    # Orthographic camera. Screen X is right, screen Y is up; depth toward eye.
    azimuth, elevation = (
        math.radians(-28 if not lid_only else -10),
        math.radians(42 if not lid_only else 65),
    )
    c, s, ce, se = (
        math.cos(azimuth),
        math.sin(azimuth),
        math.cos(elevation),
        math.sin(elevation),
    )
    rotation = np.array([[c, s, 0], [-s * se, c * se, ce], [s * ce, -c * ce, se]])
    width, height, sample = 1400, 900, 3
    rows, points, visible_edges = [], [], []
    for name in ("lid",) if lid_only else ("base", "lid"):
        mesh = trimesh.load(directory / f"{name}.stl", force="mesh", process=True)
        vertices, normals = mesh.vertices.copy(), mesh.face_normals.copy()
        if name == "lid":
            vertices[:, 1], vertices[:, 2] = 30 - vertices[:, 1], 20 - vertices[:, 2]
            normals[:, 1:] *= -1
            vertices[:, 2] += explode
        vertices, normals = vertices @ rotation.T, normals @ rotation.T
        facing = normals[:, 2] > 1e-7
        adjacency = {}
        for fi, face in enumerate(mesh.faces):
            for a, b in zip(face, np.roll(face, -1), strict=True):
                adjacency.setdefault(tuple(sorted((int(a), int(b)))), []).append(fi)
        features = set()
        for edge, faces in adjacency.items():
            if (
                len(faces) != 2
                or facing[faces[0]] != facing[faces[1]]
                or np.dot(normals[faces[0]], normals[faces[1]])
                < math.cos(math.radians(24))
            ):
                features.add(edge)
        for fi, face in enumerate(mesh.faces):
            if not facing[fi]:
                continue
            polygon = vertices[face]
            edges = [
                vertices[[a, b]]
                for a, b in zip(face, np.roll(face, -1), strict=True)
                if tuple(sorted((int(a), int(b)))) in features
            ]
            brightness = np.clip(
                0.52 + 0.38 * np.dot(normals[fi], [-0.25, 0.35, 0.9]), 0.35, 0.9
            )
            shade = (
                (255, 255, 255)
                if ink
                else tuple(int(value * brightness) for value in (200, 208, 201))
            )
            rows.append((polygon, shade))
            visible_edges.extend(edges)
            points.append(polygon[:, :2])
    points = np.vstack(points)
    low, high = points.min(axis=0), points.max(axis=0)
    center = (low + high) / 2
    scale = min((width - 180) / (high[0] - low[0]), (height - 170) / (high[1] - low[1]))

    def project(p: np.ndarray) -> np.ndarray:
        q = p.copy()
        q[:, :2] = (p[:, :2] - center) * scale
        q[:, 0] += width / 2
        q[:, 1] = height / 2 - q[:, 1]
        q[:, :2] *= sample
        return q

    pixels = np.empty((height * sample, width * sample, 3), dtype=np.uint8)
    pixels[:] = [255, 255, 255] if ink else [246, 245, 240]
    depth = np.full((height * sample, width * sample), -np.inf, dtype=np.float32)
    for polygon, shade in rows:
        triangle = project(polygon)
        x0, y0 = np.floor(triangle[:, :2].min(axis=0)).astype(int)
        x1, y1 = np.ceil(triangle[:, :2].max(axis=0)).astype(int)
        x0, x1 = max(x0, 0), min(x1, width * sample - 1)
        y0, y1 = max(y0, 0), min(y1, height * sample - 1)
        a, b, c = triangle
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-8:
            continue
        xx, yy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        u = ((b[1] - c[1]) * (xx - c[0]) + (c[0] - b[0]) * (yy - c[1])) / denominator
        v = ((c[1] - a[1]) * (xx - c[0]) + (a[0] - c[0]) * (yy - c[1])) / denominator
        z = u * a[2] + v * b[2] + (1 - u - v) * c[2]
        old_z = depth[y0 : y1 + 1, x0 : x1 + 1]
        mask = (u >= -1e-8) & (v >= -1e-8) & (u + v <= 1 + 1e-8) & (z >= old_z)
        old_z[mask] = z[mask]
        pixels[y0 : y1 + 1, x0 : x1 + 1][mask] = shade
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    for edge in visible_edges:
        a, b = project(edge)
        count = int(np.linalg.norm(b[:2] - a[:2]) * 1.5) + 2
        samples = a + np.linspace(0, 1, count)[:, None] * (b - a)
        screen = np.rint(samples[:, :2]).astype(int)
        screen[:, 0] = np.clip(screen[:, 0], 0, width * sample - 1)
        screen[:, 1] = np.clip(screen[:, 1], 0, height * sample - 1)
        visible = samples[:, 2] >= depth[screen[:, 1], screen[:, 0]] - 0.035
        for i in range(count - 1):
            if visible[i] and visible[i + 1]:
                draw.line(
                    [tuple(screen[i]), tuple(screen[i + 1])],
                    fill="#000000" if ink else "#3B443E",
                    width=sample * 2 if ink else sample,
                )
    image.resize((width, height), Image.Resampling.LANCZOS).save(
        target if target is not None else directory / filename
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=HERE,
        help="directory containing meshes and receiving previews",
    )
    args = parser.parse_args()
    render(args.output, "preview.png", False)
    render(args.output, "lid-detail.png", True)
    render(args.output, "exploded.png", False, 22)


if __name__ == "__main__":
    main()
