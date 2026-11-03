#!/usr/bin/env python3
"""Build and validate the repository's self-contained documentation site.

The CLI and public imports stay here; publication inventory, reader navigation,
rendering, and link validation live in the focused ``scripts.docsite`` modules.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Direct execution starts with scripts/ on sys.path. Module execution and tests
# already have the repository root; both entry points use the same package.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.docsite.builder import build_site as _build_site
from scripts.docsite.config import ROOT as ROOT
from scripts.docsite.config import Page as Page
from scripts.docsite.config import load_pages as load_pages
from scripts.docsite.links import IMAGE_SUFFIXES as IMAGE_SUFFIXES
from scripts.docsite.links import (
    MAX_REPOSITORY_IMAGE_BYTES as MAX_REPOSITORY_IMAGE_BYTES,
)
from scripts.docsite.links import (
    MAX_REPOSITORY_IMAGE_TOTAL_BYTES as MAX_REPOSITORY_IMAGE_TOTAL_BYTES,
)
from scripts.docsite.links import Inventory as Inventory
from scripts.docsite.links import RepositoryLinks as RepositoryLinks
from scripts.docsite.links import copy_repository_images as _copy_repository_images
from scripts.docsite.links import relative_url as relative_url
from scripts.docsite.links import validate_site as validate_site
from scripts.docsite.navigation import navigation as navigation
from scripts.docsite.navigation import status_notice as status_notice


def copy_repository_images(root: Path, output: Path, images: set[Path]) -> None:
    """Retain the CLI module's configurable image budgets for existing callers."""
    _copy_repository_images(
        root,
        output,
        images,
        per_file_limit=MAX_REPOSITORY_IMAGE_BYTES,
        total_limit=MAX_REPOSITORY_IMAGE_TOTAL_BYTES,
    )


def build_site(output: Path, root: Path = ROOT) -> int:
    return _build_site(output, root, copy_images=copy_repository_images)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".scratch/site")
    args = parser.parse_args()
    try:
        count = build_site(args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"docs: {exc}\n")
    print(f"docs: built and validated {count} pages in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
