"""Render the publication inventory through the local site shell."""

from __future__ import annotations

import html
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from string import Template
from urllib.parse import quote

import markdown  # type: ignore[import-untyped]

from .config import ROOT, load_site
from .links import (
    Inventory,
    RepositoryLinks,
    copy_repository_images,
    relative_url,
    validate_site,
)
from .navigation import (
    breadcrumbs,
    navigation,
    pagination,
    primary_navigation,
    status_notice,
)


def build_site(
    output: Path,
    root: Path = ROOT,
    *,
    copy_images: Callable[[Path, Path, set[Path]], None] = copy_repository_images,
) -> int:
    root = root.resolve()
    output = output.resolve()
    if not output.is_relative_to(root / ".scratch") or output == root / ".scratch":
        raise ValueError("documentation output must be a subdirectory of .scratch")
    site = load_site(root)
    metadata, pages = site.metadata, site.pages
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
        body_class = "product" if page.layout == "product" else "document docs"
        if page.output == Path("index.html"):
            body_class = "home " + body_class
        source_path = quote(page.source.as_posix(), safe="/")
        branch = quote(metadata["branch"], safe="/")
        text = template.substitute(
            title=html.escape(page.title),
            description=html.escape(page.summary, quote=True),
            section=html.escape(page.section),
            repository=html.escape(metadata["repository"], quote=True),
            source_url=html.escape(
                f"{metadata['repository']}/blob/{branch}/{source_path}", quote=True
            ),
            base=base,
            layout=page.layout,
            body_class=body_class,
            body=body,
            notice=status_notice(page),
            navigation=navigation(pages, page, site.navigation),
            primary_navigation=primary_navigation(site.primary_navigation, page),
            breadcrumbs=breadcrumbs(pages, page),
            pagination=pagination(pages, page, site.navigation),
            toc=(
                f"<div><h2>On this page</h2>{renderer.toc}</div>"
                if page.layout == "docs" and renderer.toc_tokens
                else ""
            ),
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
    copy_images(root, output, images)
    (output / "search-index.json").write_text(
        json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    validate_site(output)
    return len(pages)
