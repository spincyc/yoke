"""Accessible page context derived from the curated reading order."""

from __future__ import annotations

import html
from pathlib import Path

from .config import NavigationGroup, Page, PrimaryLink
from .links import relative_url


def page_link(page: Page, current: Page, *, location: Path | None = None) -> str:
    href = html.escape(relative_url(page.output, current.output), quote=True)
    selected = ' aria-current="page"' if page.output == current.output else ""
    if not selected and page.output == location:
        selected = ' aria-current="location"'
    return f'<a href="{href}"{selected}>{html.escape(page.title)}</a>'


def navigation(
    pages: list[Page],
    current: Page,
    groups: tuple[NavigationGroup, ...] | None = None,
) -> str:
    """Keep the publication inventory independent of the sidebar's link count."""
    if groups is None:
        sections: dict[str, list[Path]] = {}
        for page in pages:
            if page.listed and page.layout == "docs":
                sections.setdefault(page.section, []).append(page.output)
        groups = tuple(
            NavigationGroup(title, tuple(routes)) for title, routes in sections.items()
        )
    routes = {page.output: page for page in pages}
    listed = {output for group in groups for output in group.pages}
    location = current.output
    parent = current.parent
    while location not in listed and parent is not None:
        location = parent
        parent = routes[parent].parent
    parts = []
    for group in groups:
        parts.append(f'<div class="nav-group"><h2>{html.escape(group.title)}</h2><ul>')
        for output in group.pages:
            parts.append(
                f"<li>{page_link(routes[output], current, location=location)}</li>"
            )
        parts.append("</ul></div>")
    return "".join(parts)


def primary_navigation(links: tuple[PrimaryLink, ...], current: Page) -> str:
    parts = []
    for link in links:
        href = (
            relative_url(link.output, current.output)
            if link.output is not None
            else link.url
        )
        selected = ' aria-current="page"' if current.nav == link.key else ""
        parts.append(
            f'<a class="primary-link" href="{html.escape(href, quote=True)}"'
            f"{selected}>{html.escape(link.title)}</a>"
        )
    return "".join(parts)


def breadcrumbs(pages: list[Page], current: Page) -> str:
    if current.layout == "product":
        return ""
    routes = {page.output: page for page in pages}
    chain = [current]
    parent = current.parent
    while parent is not None:
        page = routes[parent]
        chain.append(page)
        parent = page.parent
    parts = ['<ol class="breadcrumbs">']
    for page in reversed(chain):
        if page == current:
            parts.append(f'<li aria-current="page">{html.escape(page.title)}</li>')
        else:
            parts.append(f"<li>{page_link(page, current)}</li>")
    parts.append("</ol>")
    return "".join(parts)


def pagination(
    pages: list[Page], current: Page, groups: tuple[NavigationGroup, ...]
) -> str:
    """Previous/next follow the same order the reader sees in the sidebar."""
    if current.layout == "product":
        return ""
    ordered = [output for group in groups for output in group.pages]
    if current.output not in ordered:
        return ""
    position = ordered.index(current.output)
    routes = {page.output: page for page in pages}
    parts = []
    for offset, relation, label in ((-1, "prev", "Previous"), (1, "next", "Next")):
        neighbor = position + offset
        if 0 <= neighbor < len(ordered):
            page = routes[ordered[neighbor]]
            href = html.escape(relative_url(page.output, current.output), quote=True)
            css = "previous" if offset == -1 else "next"
            parts.append(
                f'<a class="pagination-{css}" rel="{relation}" href="{href}">'
                f"<span>{label}</span><strong>{html.escape(page.title)}</strong></a>"
            )
    if not parts:
        return ""
    return (
        '<nav class="page-pagination" aria-label="Page navigation">'
        + "".join(parts)
        + "</nav>"
    )


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
