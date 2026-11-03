"""Rewrite repository links and verify the self-contained publication tree."""

from __future__ import annotations

import html
import math
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .config import Page

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico"}
MAX_REPOSITORY_IMAGE_BYTES = 16 * 1024 * 1024
MAX_REPOSITORY_IMAGE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SRCSET_LENGTH = 8192
MAX_SRCSET_CANDIDATES = 16


def srcset_candidates(value: str) -> list[tuple[str, str]]:
    """Parse bounded static image candidates; URL commas/spaces must be encoded."""
    if len(value) > MAX_SRCSET_LENGTH:
        raise ValueError("srcset exceeds length budget")
    candidates = value.split(",")
    if len(candidates) > MAX_SRCSET_CANDIDATES:
        raise ValueError("srcset exceeds candidate budget")
    result = []
    for candidate in candidates:
        parts = candidate.split()
        if not 1 <= len(parts) <= 2:
            raise ValueError("srcset needs a URL and optional width/density descriptor")
        descriptor = parts[1] if len(parts) == 2 else ""
        if descriptor:
            if not re.fullmatch(
                r"(?:[1-9][0-9]*w|(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)x)", descriptor
            ):
                raise ValueError(f"unsupported srcset descriptor: {descriptor}")
            number = float(descriptor[:-1])
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"unsupported srcset descriptor: {descriptor}")
        result.append((parts[0], descriptor))
    return result


def relative_url(target: Path, origin: Path) -> str:
    """Use relative URLs so project Pages works under any repository prefix."""
    path = posixpath.relpath(target.as_posix(), origin.parent.as_posix())
    return quote(path, safe="/")


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
            if value is not None:
                if key in {"href", "src"}:
                    value = self.rewrite(value)
                elif key == "srcset":
                    value = ", ".join(
                        self.rewrite(url) + (f" {descriptor}" if descriptor else "")
                        for url, descriptor in srcset_candidates(value)
                    )
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
            elif key == "srcset":
                for url, _ in srcset_candidates(value):
                    self.links.append(url)
                    self.resources.append(url)

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


def copy_repository_images(
    root: Path,
    output: Path,
    images: set[Path],
    *,
    per_file_limit: int = MAX_REPOSITORY_IMAGE_BYTES,
    total_limit: int = MAX_REPOSITORY_IMAGE_TOTAL_BYTES,
) -> None:
    """Copy only referenced image files, with local containment and byte budgets."""
    total = 0
    for source in sorted(images):
        target = (root / source).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"image escapes repository or is missing: {source}")
        with target.open("rb") as stream:
            data = stream.read(per_file_limit + 1)
        if len(data) > per_file_limit:
            raise ValueError(f"repository image exceeds per-file byte budget: {source}")
        total += len(data)
        if total > total_limit:
            raise ValueError("repository images exceed total byte budget")
        destination = output / "assets/repository" / source
        if destination.exists():
            raise ValueError(f"repository image collides with a site asset: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
