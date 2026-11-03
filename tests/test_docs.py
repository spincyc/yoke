"""Exercise the published artifact, including links at a project Pages prefix."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from scripts.build_docs import ROOT, Inventory, build_site, load_pages, validate_site


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_site_is_deterministic_complete_and_self_contained(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    count = build_site(first)
    assert build_site(second) == count
    assert tree_digest(first) == tree_digest(second)
    _, pages = load_pages(ROOT)
    published = {page.source for page in pages}
    assert all(path.relative_to(ROOT) in published for path in ROOT.glob("doc/**/*.md"))
    assert len(list(first.rglob("*.html"))) == count
    index = json.loads((first / "search-index.json").read_text())
    assert len(index) == count
    for path in first.rglob("*.html"):
        text = path.read_text()
        assert 'href="#content"' in text
        assert 'aria-label="Documentation"' in text
        assert "<h1" in text
        assert "https://fonts." not in text
        assert "cdn." not in text
    # Every project-relative URL must still work when hosted below /yoke/.
    nested = tmp_path / "project-prefix" / "yoke"
    nested.parent.mkdir()
    first.rename(nested)
    validate_site(nested)
    research = (nested / "research/feasibility.html").read_text()
    assert "Dated research" in research
    assert "not been revalidated for publication" in research
    landing = (nested / "index.html").read_text()
    assert "docs/project-status.html" in landing
    status = (nested / "docs/project-status.html").read_text()
    assert "ratio models, not codec implementations" in status
    assert "has not been built and bench-tested" in " ".join(status.split())


def test_brand_derivatives_preserve_the_canonical_cad_geometry() -> None:
    """A web identity update must not silently redraw the manufacturing mark."""
    assets = ROOT / "site/assets"
    namespace = "{http://www.w3.org/2000/svg}"
    canonical = ElementTree.parse(assets / "yoke-mark.svg").getroot()
    assert canonical.attrib == {"viewBox": "0 0 32 32"}
    assert [element.tag for element in canonical] == [
        namespace + "title",
        namespace + "path",
    ]
    path = canonical.find(namespace + "path")
    assert path is not None
    assert set(path.attrib) == {"fill", "d"}
    assert path.attrib["fill"] == "#000000"
    for name in ("yoke-lockup.svg", "favicon.svg"):
        derivative = ElementTree.parse(assets / name).getroot()
        symbol = derivative.find(namespace + "path")
        assert symbol is not None
        assert symbol.attrib["d"] == path.attrib["d"], name
        assert not list(derivative.iter(namespace + "text")), name


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("missing.html", "broken local link"),
        ("index.html#missing", "broken anchor"),
        ("/index.html", "site-root URL"),
        ("../outside.html", "broken local link"),
    ],
)
def test_validator_rejects_broken_navigation(
    tmp_path: Path, link: str, message: str
) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    (root / "index.html").write_text(
        f'<h1 id="present">Page</h1><a href="{link}">Go</a>'
    )
    with pytest.raises(ValueError, match=message):
        validate_site(root)


def test_validator_rejects_duplicate_anchors() -> None:
    inventory = Inventory()
    with pytest.raises(ValueError, match="duplicate HTML id"):
        inventory.feed('<h1 id="duplicate">A</h1><h2 id="duplicate">B</h2>')


def test_builder_refuses_to_delete_outside_scratch() -> None:
    with pytest.raises(ValueError, match=r"subdirectory of \.scratch"):
        build_site(ROOT / "site")


def test_validator_rejects_external_runtime_assets(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        '<h1>Page</h1><script src="https://example.com/tracker.js"></script>'
    )
    with pytest.raises(ValueError, match="external runtime resource"):
        validate_site(tmp_path)


@pytest.fixture
def image_docs_repo(tmp_path: Path) -> Path:
    """A small published CAD guide with one referenced and one private preview."""
    root = tmp_path / "repository"
    (root / "site/assets").mkdir(parents=True)
    (root / "doc/design").mkdir(parents=True)
    (root / "hardware/case").mkdir(parents=True)
    (root / "site/template.html").write_text("<html><body>$body</body></html>")
    (root / "site/site.toml").write_text(
        'name = "Fixture"\n'
        'repository = "https://github.com/example/case"\n'
        'branch = "feature/ir"\n'
        'description = "A fixture"\n'
        "[[pages]]\n"
        'source = "doc/design/CASE.md"\n'
        'output = "design/case.html"\n'
        'title = "Case"\nsection = "Design"\nsummary = "A CAD guide"\n'
    )
    (root / "hardware/case/preview.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>'
    )
    (root / "hardware/case/unreferenced.svg").write_bytes(b"not published")
    (root / "hardware/case/model.stl").write_bytes(b"CAD source")
    (root / "doc/design/CASE.md").write_text(
        "# Case\n\n![CAD preview](../../hardware/case/preview.svg)\n\n"
        "[CAD source](../../hardware/case/model.stl)\n"
    )
    return root


def test_referenced_repository_images_are_local_and_deterministic(
    image_docs_repo: Path,
) -> None:
    root = image_docs_repo
    first, second = root / ".scratch/first", root / ".scratch/second"
    build_site(first, root)
    build_site(second, root)
    assert tree_digest(first) == tree_digest(second)
    copied = first / "assets/repository/hardware/case/preview.svg"
    assert copied.read_bytes() == (root / "hardware/case/preview.svg").read_bytes()
    assert not (first / "assets/repository/hardware/case/unreferenced.svg").exists()
    assert not (first / "assets/repository/hardware/case/model.stl").exists()
    page = (first / "design/case.html").read_text()
    assert 'src="../assets/repository/hardware/case/preview.svg"' in page
    assert (
        "https://github.com/example/case/blob/feature/ir/hardware/case/model.stl"
        in page
    )
    validate_site(first)


def test_repository_image_symlinks_cannot_escape_source_root(
    image_docs_repo: Path, tmp_path: Path
) -> None:
    root = image_docs_repo
    outside = tmp_path / "outside.svg"
    outside.write_bytes(b"not published")
    preview = root / "hardware/case/preview.svg"
    preview.unlink()
    preview.symlink_to(outside)
    with pytest.raises(ValueError, match="link escapes repository"):
        build_site(root / ".scratch/site", root)


@pytest.mark.parametrize("budget", ["single", "total"])
def test_repository_image_byte_budgets(
    image_docs_repo: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    from scripts import build_docs

    root = image_docs_repo
    preview = root / "hardware/case/preview.svg"
    size = preview.stat().st_size
    if budget == "single":
        monkeypatch.setattr(build_docs, "MAX_REPOSITORY_IMAGE_BYTES", size - 1)
        message = "per-file byte budget"
    else:
        second = root / "hardware/case/second.svg"
        second.write_bytes(preview.read_bytes())
        with (root / "doc/design/CASE.md").open("a") as document:
            document.write("\n![Second](../../hardware/case/second.svg)\n")
        monkeypatch.setattr(build_docs, "MAX_REPOSITORY_IMAGE_TOTAL_BYTES", size + 1)
        message = "total byte budget"
    with pytest.raises(ValueError, match=message):
        build_site(root / ".scratch/site", root)
