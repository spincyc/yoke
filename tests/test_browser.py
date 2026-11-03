"""Browser-gate infrastructure stays testable without a browser installation."""

from __future__ import annotations

from pathlib import Path
from threading import enumerate as threads
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest
from scripts.verify_browser import serve_site, verify_site


def test_browser_server_requires_the_production_pages_prefix(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("homepage")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/page.html").write_text("nested guide")
    with serve_site(tmp_path) as base:
        with urlopen(base + "index.html", timeout=2) as response:
            assert response.read() == b"homepage"
        with urlopen(base + "docs/page.html", timeout=2) as response:
            assert response.read() == b"nested guide"
        for path in ("index.html", "docs/page.html", "yoke-other/index.html"):
            with pytest.raises(HTTPError) as error:
                urlopen(base.removesuffix("yoke/") + path, timeout=2)
            assert error.value.code == 404


def test_browser_server_stops_even_when_a_check_fails(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("homepage")
    with (
        pytest.raises(AssertionError, match="browser check failed"),
        serve_site(tmp_path) as base,
    ):
        with urlopen(base, timeout=2) as response:
            assert response.read() == b"homepage"
        raise AssertionError("browser check failed")
    with pytest.raises(URLError):
        urlopen(base, timeout=2)


def test_browser_server_rejects_an_unbuilt_site(tmp_path: Path) -> None:
    with (
        pytest.raises(ValueError, match="not a built documentation site"),
        serve_site(tmp_path),
    ):
        pytest.fail("an empty directory must not be served")


def test_required_gate_fails_when_its_pinned_browser_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATH Chromium must never turn missing provisioned tools into a skip."""
    from playwright.sync_api import Error

    (tmp_path / "index.html").write_text("<h1>Fixture</h1>")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "missing-browser"))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "report.json").write_text("stale success")
    with pytest.raises(Error, match="Executable doesn't exist"):
        verify_site(tmp_path, artifacts)
    assert not (artifacts / "report.json").exists()
    assert not any(thread.name == "docs-browser-server" for thread in threads())
