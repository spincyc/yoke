#!/usr/bin/env python3
"""Render trusted repository Markdown, then verify a self-contained Pages tree.

Markdown syntax belongs to Python-Markdown. This module only supplies a static
shell, source-to-output mapping, and local link validation. No network is used.
"""

from __future__ import annotations

import argparse
import html
import json
import posixpath
import shutil
import tomllib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from string import Template
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import markdown  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico"}
MAX_REPOSITORY_IMAGE_BYTES = 16 * 1024 * 1024
MAX_REPOSITORY_IMAGE_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Page:
    source: Path
    output: Path
    title: str
    section: str
    summary: str


def relative_url(target: Path, origin: Path) -> str:
    """Use relative URLs so project Pages works under any repository prefix."""
    return posixpath.relpath(target.as_posix(), origin.parent.as_posix())


def load_pages(root: Path) -> tuple[dict[str, str], list[Page]]:
    config = tomllib.loads((root / "site/site.toml").read_text(encoding="utf-8"))
    metadata = {
        key: str(config[key]) for key in ("name", "repository", "branch", "description")
    }
    pages = [
        Page(
            Path(entry["source"]),
            Path(entry["output"]),
            entry["title"],
            entry["section"],
            entry["summary"],
        )
        for entry in config["pages"]
    ]
    # Design additions and ADRs should appear without a second content copy.
    known = {page.source for page in pages}
    for directory, section in (("design", "Design"), ("decisions", "Decisions")):
        for source in sorted((root / "doc" / directory).glob("*.md")):
            path = source.relative_to(root)
            if path in known:
                continue
            title = source.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
            pages.append(
                Page(
                    path,
                    Path(directory) / f"{source.stem.lower()}.html",
                    title,
                    section,
                    f"Yoke {section.lower()}: {title}.",
                )
            )
    outputs: set[Path] = set()
    sources: set[Path] = set()
    for page in pages:
        if page.source.is_absolute() or ".." in page.source.parts:
            raise ValueError(f"unsafe source path: {page.source}")
        if page.source in sources:
            raise ValueError(f"duplicate source: {page.source}")
        sources.add(page.source)
        if page.output.is_absolute() or ".." in page.output.parts:
            raise ValueError(f"unsafe output path: {page.output}")
        if page.output in outputs:
            raise ValueError(f"duplicate output: {page.output}")
        outputs.add(page.output)
        if not (root / page.source).is_file():
            raise ValueError(f"missing source: {page.source}")
    published = {page.source for page in pages}
    missing = set(root.glob("doc/**/*.md")) - {root / path for path in published}
    if missing:
        raise ValueError(f"unpublished Markdown pages: {sorted(missing)}")
    return metadata, pages


class RepositoryLinks(HTMLParser):
    """Rewrite HTML attributes after Markdown rendering, including raw HTML."""

    def __init__(
        self, root: Path, page: Page, pages: list[Page], metadata: dict[str, str]
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.root = root
        self.page = page
        self.pages = {item.source: item.output for item in pages}
        self.metadata = metadata
        self.parts: list[str] = []
        self.images: set[Path] = set()

    def rewrite(self, value: str) -> str:
        url = urlsplit(value)
        if url.scheme or url.netloc:
            if url.scheme not in {"https", "http", "mailto"}:
                raise ValueError(f"unsupported URL in {self.page.source}: {value}")
            return value
        if not url.path:
            return value
        if url.path.startswith("/"):
            raise ValueError(f"site-root URL breaks project Pages: {value}")
        target = (self.root / self.page.source.parent / unquote(url.path)).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"link escapes repository: {value}")
        source = target.relative_to(self.root)
        if source in self.pages:
            path = relative_url(self.pages[source], self.page.output)
        elif source.is_relative_to(Path("site/assets")) and target.is_file():
            path = relative_url(
                Path("assets") / source.relative_to("site/assets"), self.page.output
            )
        elif not target.exists():
            raise ValueError(f"missing target in {self.page.source}: {value}")
        elif target.suffix == ".md":
            raise ValueError(f"unpublished Markdown link: {value}")
        elif target.is_file() and target.suffix.lower() in IMAGE_SUFFIXES:
            self.images.add(source)
            path = relative_url(Path("assets/repository") / source, self.page.output)
        else:
            kind = "tree" if target.is_dir() else "blob"
            branch = quote(self.metadata["branch"], safe="/")
            path = (
                f"{self.metadata['repository']}/{kind}/{branch}/"
                f"{quote(source.as_posix(), safe='/')}"
            )
        return urlunsplit(("", "", path, url.query, url.fragment))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rewritten = []
        for key, value in attrs:
            if value is not None and key in {"href", "src"}:
                value = self.rewrite(value)
            rewritten.append(
                key if value is None else f'{key}="{html.escape(value, quote=True)}"'
            )
        suffix = " " + " ".join(rewritten) if rewritten else ""
        self.parts.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.parts[-1] = self.parts[-1][:-1] + " />"

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")


class Inventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.resources: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if value is None:
                continue
            if key == "id":
                if value in self.ids:
                    raise ValueError(f"duplicate HTML id: {value}")
                self.ids.add(value)
            elif key in {"href", "src"}:
                self.links.append(value)
                if key == "src" or tag == "link":
                    self.resources.append(value)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def validate_site(output: Path) -> None:
    """Fail on broken internal links and anchors; never request external URLs."""
    output = output.resolve()
    inventories: dict[Path, Inventory] = {}
    for path in sorted(output.rglob("*.html")):
        inventory = Inventory()
        inventory.feed(path.read_text(encoding="utf-8"))
        inventories[path] = inventory
    for origin, inventory in inventories.items():
        for resource in inventory.resources:
            url = urlsplit(resource)
            if url.netloc or url.scheme not in {"", "data"}:
                raise ValueError(f"external runtime resource in {origin}: {resource}")
        for value in inventory.links:
            url = urlsplit(value)
            if url.scheme or url.netloc:
                continue
            if url.path.startswith("/"):
                raise ValueError(f"site-root URL in {origin}: {value}")
            target = (
                (origin.parent / unquote(url.path)).resolve() if url.path else origin
            )
            if not target.is_relative_to(output) or not target.is_file():
                raise ValueError(f"broken local link in {origin}: {value}")
            if (
                url.fragment
                and target in inventories
                and unquote(url.fragment) not in inventories[target].ids
            ):
                raise ValueError(f"broken anchor in {origin}: {value}")


def navigation(pages: list[Page], current: Page) -> str:
    groups: dict[str, list[Page]] = {}
    for page in pages:
        groups.setdefault(page.section, []).append(page)
    parts = []
    for section, entries in groups.items():
        parts.append(f'<div class="nav-group"><h2>{html.escape(section)}</h2><ul>')
        for page in entries:
            selected = ' aria-current="page"' if page == current else ""
            style = ' class="decision-link"' if section == "Decisions" else ""
            title = "Overview" if page.output == Path("index.html") else page.title
            href = html.escape(relative_url(page.output, current.output), quote=True)
            parts.append(
                f'<li><a href="{href}"{selected}{style}>{html.escape(title)}</a></li>'
            )
        parts.append("</ul></div>")
    return "".join(parts)


def status_notice(page: Page) -> str:
    if page.section == "Research":
        return (
            '<aside class="notice" aria-label="Research status">'
            "<strong>Dated research · hardware claims are not bench-verified</strong>"
            "This document preserves its stated research date and assumptions. "
            "Product availability, vendor behavior, legal interpretations, and "
            "performance estimates have not been revalidated for publication. "
            "It is not a buying recommendation or a demonstrated endpoint result."
            "</aside>"
        )
    if page.source.name == "EDGE-DEVICES.md":
        return (
            '<aside class="notice" aria-label="Endpoint status">'
            "<strong>Engineering roadmap · no endpoint hardware bench results</strong>"
            "These are proposed endpoints and milestones. DSC transport, physical "
            "latency, USB compatibility, and protected-content relay behavior "
            "remain subject to hardware validation.</aside>"
        )
    return ""


def copy_repository_images(root: Path, output: Path, images: set[Path]) -> None:
    """Copy only referenced image files, with local containment and byte budgets."""
    total = 0
    for source in sorted(images):
        target = (root / source).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"image escapes repository or is missing: {source}")
        with target.open("rb") as stream:
            data = stream.read(MAX_REPOSITORY_IMAGE_BYTES + 1)
        if len(data) > MAX_REPOSITORY_IMAGE_BYTES:
            raise ValueError(f"repository image exceeds per-file byte budget: {source}")
        total += len(data)
        if total > MAX_REPOSITORY_IMAGE_TOTAL_BYTES:
            raise ValueError("repository images exceed total byte budget")
        destination = output / "assets/repository" / source
        if destination.exists():
            raise ValueError(f"repository image collides with a site asset: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def build_site(output: Path, root: Path = ROOT) -> int:
    root = root.resolve()
    output = output.resolve()
    if not output.is_relative_to(root / ".scratch") or output == root / ".scratch":
        raise ValueError("documentation output must be a subdirectory of .scratch")
    metadata, pages = load_pages(root)
    template = Template((root / "site/template.html").read_text(encoding="utf-8"))
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(root / "site/assets", output / "assets")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    index = []
    images: set[Path] = set()
    for page in pages:
        renderer = markdown.Markdown(
            extensions=["tables", "fenced_code", "toc", "sane_lists"],
            extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}},
            output_format="html5",
        )
        rendered = str(
            renderer.convert((root / page.source).read_text(encoding="utf-8"))
        )
        links = RepositoryLinks(root, page, pages, metadata)
        links.feed(rendered)
        images.update(links.images)
        body = "".join(links.parts)
        inventory = Inventory()
        inventory.feed(body)
        base = relative_url(Path("index.html"), page.output).removesuffix("index.html")
        base = base.rstrip("/") or "."
        text = template.substitute(
            title=html.escape(page.title),
            description=html.escape(page.summary, quote=True),
            section=html.escape(page.section),
            repository=html.escape(metadata["repository"], quote=True),
            source_url=html.escape(
                f"{metadata['repository']}/blob/{metadata['branch']}/{page.source}",
                quote=True,
            ),
            base=base,
            body_class="home" if page.output == Path("index.html") else "document",
            body=body,
            notice=status_notice(page),
            navigation=navigation(pages, page),
            toc=f"<div><h2>On this page</h2>{renderer.toc}</div>",
        )
        target = output / page.output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        index.append(
            {
                "url": page.output.as_posix(),
                "title": page.title,
                "summary": page.summary,
                "text": " ".join(" ".join(inventory.text).split()),
            }
        )
    copy_repository_images(root, output, images)
    (output / "search-index.json").write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    validate_site(output)
    return len(pages)


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
