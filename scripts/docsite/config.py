"""Load the publication inventory and its deliberately curated navigation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Page:
    source: Path
    output: Path
    title: str
    section: str
    summary: str
    layout: str = "docs"
    nav: str = "docs"
    parent: Path | None = None
    listed: bool = True


@dataclass(frozen=True)
class NavigationGroup:
    title: str
    pages: tuple[Path, ...]


@dataclass(frozen=True)
class PrimaryLink:
    title: str
    key: str
    output: Path | None = None
    url: str = ""


@dataclass(frozen=True)
class Site:
    metadata: dict[str, str]
    pages: list[Page]
    navigation: tuple[NavigationGroup, ...]
    primary_navigation: tuple[PrimaryLink, ...]


def local_path(value: str, kind: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError(f"unsafe {kind} path: {value}")
    return path


def load_site(root: Path) -> Site:
    """Publish every document, while listing only intentional reader journeys."""
    root = root.resolve()
    config = tomllib.loads((root / "site/site.toml").read_text(encoding="utf-8"))
    metadata = {
        key: str(config[key]) for key in ("name", "repository", "branch", "description")
    }
    pages = [
        Page(
            source=local_path(entry["source"], "source"),
            output=local_path(entry["output"], "output"),
            title=entry["title"],
            section=entry["section"],
            summary=entry["summary"],
            layout=entry.get("layout", "docs"),
            nav=entry.get("nav", "docs"),
            parent=local_path(entry["parent"], "parent") if "parent" in entry else None,
            listed=entry.get("listed", True),
        )
        for entry in config["pages"]
    ]
    known = {page.source for page in pages}
    configured_outputs = {page.output for page in pages}
    # Publishing is independent of the sidebar. New design notes and ADRs keep
    # their stable URLs and search entries without enlarging the main menu.
    for directory, section in (("design", "Design"), ("decisions", "Decisions")):
        for source in sorted((root / "doc" / directory).glob("*.md")):
            path = source.relative_to(root)
            if path in known:
                continue
            if not source.resolve().is_relative_to(root):
                raise ValueError(f"source escapes repository: {path}")
            title = source.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
            auto_parent = Path(
                "engineering/decisions.html"
                if directory == "decisions"
                else "engineering/index.html"
            )
            pages.append(
                Page(
                    path,
                    Path(directory) / f"{source.stem.lower()}.html",
                    title,
                    section,
                    f"Yoke {section.lower()}: {title}.",
                    parent=auto_parent if auto_parent in configured_outputs else None,
                    listed=False,
                )
            )

    outputs: dict[Path, Page] = {}
    sources: set[Path] = set()
    for page in pages:
        if page.source in sources:
            raise ValueError(f"duplicate source: {page.source}")
        sources.add(page.source)
        if page.output.suffix != ".html" or page.output.parts[0] == "assets":
            raise ValueError(f"unsafe output path: {page.output}")
        if page.output in outputs:
            raise ValueError(f"duplicate output: {page.output}")
        outputs[page.output] = page
        target = (root / page.source).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"source escapes repository: {page.source}")
        if not target.is_file():
            raise ValueError(f"missing source: {page.source}")
        if page.layout not in {"product", "docs"}:
            raise ValueError(f"unknown page layout: {page.layout}")
    for page in pages:
        parents = {page.output}
        parent = page.parent
        while parent is not None:
            if parent not in outputs:
                raise ValueError(f"missing parent for {page.output}: {parent}")
            if parent in parents:
                raise ValueError(f"cyclic page parents: {page.output}")
            parents.add(parent)
            parent = outputs[parent].parent
    missing = set(root.glob("doc/**/*.md")) - {root / path for path in sources}
    if missing:
        raise ValueError(f"unpublished Markdown pages: {sorted(missing)}")

    groups = []
    listed: set[Path] = set()
    for entry in config.get("navigation", []):
        routes = tuple(local_path(value, "navigation") for value in entry["pages"])
        for route in routes:
            if route not in outputs:
                raise ValueError(f"unknown navigation page: {route}")
            if route in listed:
                raise ValueError(f"duplicate navigation page: {route}")
            listed.add(route)
        groups.append(NavigationGroup(entry["title"], routes))
    if "navigation" not in config:
        sections: dict[str, list[Path]] = {}
        for page in pages:
            if page.listed and page.layout == "docs":
                sections.setdefault(page.section, []).append(page.output)
        groups = [
            NavigationGroup(title, tuple(routes)) for title, routes in sections.items()
        ]

    primary = []
    keys: set[str] = set()
    for entry in config.get("primary_navigation", []):
        if entry["key"] in keys:
            raise ValueError(f"duplicate primary navigation key: {entry['key']}")
        keys.add(entry["key"])
        if ("output" in entry) == ("url" in entry):
            raise ValueError("primary navigation needs exactly one output or url")
        output = (
            local_path(entry["output"], "navigation") if "output" in entry else None
        )
        url = entry.get("url", "")
        if output is not None and output not in outputs:
            raise ValueError(f"unknown primary navigation page: {output}")
        if output is None:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError(f"unsafe primary navigation URL: {url}")
        primary.append(PrimaryLink(entry["title"], entry["key"], output, url))
    if primary:
        for page in pages:
            if page.nav not in keys:
                raise ValueError(
                    f"unknown primary navigation key in {page.source}: {page.nav}"
                )
    return Site(metadata, pages, tuple(groups), tuple(primary))


def load_pages(root: Path) -> tuple[dict[str, str], list[Page]]:
    """Compatibility API for consumers that only need the publication inventory."""
    site = load_site(root)
    return site.metadata, site.pages
