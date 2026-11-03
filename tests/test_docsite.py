"""Verify the publishing/navigation seam without coupling it to site wording."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from scripts.build_docs import build_site, load_pages, validate_site
from scripts.docsite.config import load_site
from scripts.docsite.links import srcset_candidates
from scripts.docsite.navigation import (
    breadcrumbs,
    navigation,
    pagination,
    primary_navigation,
)


class Links(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.anchors: list[dict[str, str | None]] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.anchors.append(dict(attrs))


@pytest.fixture
def published_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "site/assets/diagrams").mkdir(parents=True)
    (root / "site/pages").mkdir()
    (root / "doc/design").mkdir(parents=True)
    (root / "doc/decisions").mkdir()
    sources = {
        "home": "# Product\n\n[Documentation](docs.md)\n",
        "docs": "# Documentation\n\n[Guide](guide.md)\n",
        "guide": (
            "# Guide\n\n## Install\n\n[Details](../../doc/design/NOTE.md#detail)\n"
        ),
        "engineering": "# Engineering\n\n[Notes](../../doc/design/NOTE.md)\n",
        "decisions": (
            "# Decisions\n\n[First decision](../../doc/decisions/0001-one.md)\n"
        ),
    }
    for name, text in sources.items():
        (root / f"site/pages/{name}.md").write_text(text)
    (root / "doc/design/NOTE.md").write_text("# Technical note\n\n## Detail\n\nText.\n")
    (root / "doc/decisions/0001-one.md").write_text(
        "# Decision one\n\n[Details](../design/NOTE.md#detail)\n"
    )
    (root / "site/assets/diagrams/route.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    )
    (root / "site/template.html").write_text(
        '<html><body class="$body_class" data-layout="$layout">'
        "<header>$primary_navigation</header><nav>$navigation</nav>"
        '$breadcrumbs<main id="content">$notice$body$toc$pagination</main>'
        '<a href="$source_url">Source</a></body></html>'
    )
    config = [
        'name = "Fixture"',
        'repository = "https://github.com/example/site"',
        'branch = "feature/ir"',
        'description = "A publishing fixture"',
    ]
    for name, output, parent, layout, nav in (
        ("home", "index.html", None, "product", "product"),
        ("docs", "docs/index.html", None, "docs", "docs"),
        ("guide", "guides/first.html", "docs/index.html", "docs", "docs"),
        ("engineering", "engineering/index.html", "docs/index.html", "docs", "docs"),
        (
            "decisions",
            "engineering/decisions.html",
            "engineering/index.html",
            "docs",
            "docs",
        ),
    ):
        config.extend(
            [
                "[[pages]]",
                f'source = "site/pages/{name}.md"',
                f'output = "{output}"',
                f'title = "{name.title()}"',
                'section = "Documentation"',
                f'summary = "About {name}"',
                f'layout = "{layout}"',
                f'nav = "{nav}"',
            ]
        )
        if parent:
            config.append(f'parent = "{parent}"')
    config.extend(
        [
            "[[primary_navigation]]",
            'title = "Product"\nkey = "product"\noutput = "index.html"',
            "[[primary_navigation]]",
            'title = "Docs"\nkey = "docs"\noutput = "docs/index.html"',
            "[[primary_navigation]]",
            'title = "GitHub"\nkey = "github"\nurl = "https://github.com/example/site"',
            "[[navigation]]",
            'title = "Start"\npages = ["docs/index.html", "guides/first.html"]',
            "[[navigation]]",
            'title = "Engineering"',
            'pages = ["engineering/index.html", "engineering/decisions.html"]',
        ]
    )
    (root / "site/site.toml").write_text("\n".join(config) + "\n")
    return root


def test_curated_navigation_does_not_limit_publication(published_repo: Path) -> None:
    root = published_repo
    site = load_site(root)
    assert load_pages(root) == (site.metadata, site.pages)
    current = next(
        page for page in site.pages if page.output.as_posix() == "docs/index.html"
    )
    links = Links(navigation(site.pages, current, site.navigation)).anchors
    assert [link["href"] for link in links] == [
        "index.html",
        "../guides/first.html",
        "../engineering/index.html",
        "../engineering/decisions.html",
    ]
    assert [link["href"] for link in links if "aria-current" in link] == ["index.html"]

    output = root / ".scratch/published"
    assert build_site(output, root) == 7
    search = json.loads((output / "search-index.json").read_text())
    assert {entry["url"] for entry in search} == {
        page.output.as_posix() for page in site.pages
    }
    assert (output / "decisions/0001-one.html").is_file()
    note = (output / "design/note.html").read_text()
    assert 'id="detail"' in note
    assert (
        'href="../design/note.html#detail"'
        in (output / "decisions/0001-one.html").read_text()
    )
    assert (output / "assets/diagrams/route.svg").is_file()
    prefixed = root / ".scratch/project/yoke"
    prefixed.parent.mkdir()
    output.rename(prefixed)
    validate_site(prefixed)


def test_breadcrumbs_and_primary_state_cover_unlisted_documents(
    published_repo: Path,
) -> None:
    site = load_site(published_repo)
    current = next(page for page in site.pages if page.section == "Decisions")
    chain = Links(breadcrumbs(site.pages, current)).anchors
    assert [link["href"] for link in chain] == [
        "../docs/index.html",
        "../engineering/index.html",
        "../engineering/decisions.html",
    ]
    primary = Links(primary_navigation(site.primary_navigation, current)).anchors
    assert [link["href"] for link in primary if "aria-current" in link] == [
        "../docs/index.html"
    ]
    assert primary[-1]["href"] == "https://github.com/example/site"
    sidebar = Links(navigation(site.pages, current, site.navigation)).anchors
    assert [
        (link["href"], link["aria-current"])
        for link in sidebar
        if "aria-current" in link
    ] == [("../engineering/decisions.html", "location")]
    assert pagination(site.pages, current, site.navigation) == ""


def test_previous_next_follow_curated_order(published_repo: Path) -> None:
    site = load_site(published_repo)
    guide = next(
        page for page in site.pages if page.output.as_posix() == "guides/first.html"
    )
    links = Links(pagination(site.pages, guide, site.navigation)).anchors
    assert [(link["rel"], link["href"]) for link in links] == [
        ("prev", "../docs/index.html"),
        ("next", "../engineering/index.html"),
    ]
    first = next(
        page for page in site.pages if page.output.as_posix() == "docs/index.html"
    )
    assert [
        link["rel"]
        for link in Links(pagination(site.pages, first, site.navigation)).anchors
    ] == ["next"]


def test_product_pages_have_their_own_layout_and_primary_state(
    published_repo: Path,
) -> None:
    site = load_site(published_repo)
    home = site.pages[0]
    assert breadcrumbs(site.pages, home) == ""
    assert pagination(site.pages, home, site.navigation) == ""
    links = Links(primary_navigation(site.primary_navigation, home)).anchors
    assert [link["href"] for link in links if "aria-current" in link] == ["index.html"]
    output = published_repo / ".scratch/product"
    build_site(output, published_repo)
    text = (output / "index.html").read_text()
    assert 'class="home product"' in text
    assert 'data-layout="product"' in text
    assert "On this page" not in text


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        ('output = "index.html"', 'output = "../escape.html"', "unsafe output path"),
        ('output = "index.html"', 'output = "assets/index.html"', "unsafe output path"),
        (
            'source = "site/pages/home.md"',
            'source = "../outside.md"',
            "unsafe source path",
        ),
        ('layout = "product"', 'layout = "unknown"', "unknown page layout"),
        ('nav = "product"', 'nav = "unknown"', "unknown primary navigation key"),
        ('parent = "docs/index.html"', 'parent = "missing.html"', "missing parent"),
        (
            'parent = "docs/index.html"',
            'parent = "guides/first.html"',
            "cyclic page parents",
        ),
        (
            'pages = ["docs/index.html", "guides/first.html"]',
            'pages = ["absent.html"]',
            "unknown navigation page",
        ),
        (
            'pages = ["docs/index.html", "guides/first.html"]',
            'pages = ["docs/index.html", "docs/index.html"]',
            "duplicate navigation page",
        ),
        (
            'url = "https://github.com/example/site"',
            'url = "javascript:alert(1)"',
            "unsafe primary navigation URL",
        ),
    ],
)
def test_invalid_publication_contract_fails_before_rendering(
    published_repo: Path, before: str, after: str, message: str
) -> None:
    config = published_repo / "site/site.toml"
    config.write_text(config.read_text().replace(before, after, 1))
    with pytest.raises(ValueError, match=message):
        load_site(published_repo)


@pytest.mark.parametrize("source", ["site/pages/home.md", "doc/design/NOTE.md"])
def test_page_sources_cannot_escape_through_symlinks(
    published_repo: Path, source: str
) -> None:
    outside = published_repo.parent / "private.md"
    outside.write_text("# Private\n")
    target = published_repo / source
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="source escapes repository"):
        build_site(published_repo / ".scratch/escaped", published_repo)


def test_unmapped_document_is_an_error(published_repo: Path) -> None:
    (published_repo / "doc/research").mkdir()
    (published_repo / "doc/research/NEW.md").write_text("# Research\n")
    with pytest.raises(ValueError, match="unpublished Markdown pages"):
        load_site(published_repo)


@pytest.mark.parametrize(
    ("source", "output", "prefix"),
    [("home", "index.html", ""), ("guide", "guides/first.html", "../")],
)
def test_picture_candidates_follow_repository_paths_at_each_output_depth(
    published_repo: Path, source: str, output: str, prefix: str
) -> None:
    root = published_repo
    (root / "hardware").mkdir()
    preview = root / "hardware/render.svg"
    preview.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    with (root / f"site/pages/{source}.md").open("a") as stream:
        stream.write(
            '\n<picture><source media="(max-width: 600px)" '
            'srcset="../assets/diagrams/route.svg 1x, ../../hardware/render.svg 2x">'
            '<img src="../assets/diagrams/route.svg" alt="Route"></picture>\n'
        )
    destination = root / ".scratch/project/yoke"
    build_site(destination, root)
    text = (destination / output).read_text()
    assert (
        f'srcset="{prefix}assets/diagrams/route.svg 1x, '
        f'{prefix}assets/repository/hardware/render.svg 2x"'
    ) in text
    assert (destination / "assets/repository/hardware/render.svg").read_bytes() == (
        preview.read_bytes()
    )
    validate_site(destination)


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        ("missing.svg 2x", "broken local link"),
        ("../private.svg 400w", "broken local link"),
        ("https://example.com/image.svg 1x", "external runtime resource"),
    ],
)
def test_validator_checks_every_picture_candidate(
    tmp_path: Path, candidate: str, message: str
) -> None:
    (tmp_path / "index.html").write_text(
        f'<picture><source srcset="{candidate}"></picture>'
    )
    with pytest.raises(ValueError, match=message):
        validate_site(tmp_path)


@pytest.mark.parametrize(
    "value",
    ["a.svg 0x", "a.svg -2x", "a.svg nope", "a.svg 1x extra", "", "a.svg,"],
)
def test_srcset_rejects_ambiguous_or_invalid_descriptors(value: str) -> None:
    with pytest.raises(ValueError, match="srcset"):
        srcset_candidates(value)


def test_static_picture_candidate_parsing_is_bounded() -> None:
    assert srcset_candidates("a.svg .5x, b.svg 2x") == [
        ("a.svg", ".5x"),
        ("b.svg", "2x"),
    ]
    assert srcset_candidates("a.svg 400w") == [("a.svg", "400w")]
    with pytest.raises(ValueError, match="length budget"):
        srcset_candidates("a" * 8193)
    with pytest.raises(ValueError, match="candidate budget"):
        srcset_candidates(",".join(f"{number}.svg" for number in range(17)))


def test_picture_candidates_preserve_encoded_path_delimiters(
    published_repo: Path,
) -> None:
    root = published_repo
    static = root / "site/assets/diagrams/with space,100%.svg"
    static.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    (root / "hardware").mkdir()
    preview = root / "hardware/with space,100%.svg"
    preview.write_bytes(static.read_bytes())
    with (root / "site/pages/home.md").open("a") as stream:
        stream.write(
            '\n<picture><source srcset="'
            "../assets/diagrams/with%20space%2C100%25.svg 1x, "
            '../../hardware/with%20space%2C100%25.svg 2x">'
            '<img src="../assets/diagrams/with%20space%2C100%25.svg" '
            'alt="Route"></picture>\n'
        )
    output = root / ".scratch/encoded"
    build_site(output, root)
    text = (output / "index.html").read_text()
    assert (
        'srcset="assets/diagrams/with%20space%2C100%25.svg 1x, '
        'assets/repository/hardware/with%20space%2C100%25.svg 2x"'
    ) in text
    assert 'src="assets/diagrams/with%20space%2C100%25.svg"' in text
    assert (output / "assets/repository/hardware/with space,100%.svg").is_file()
    validate_site(output)
