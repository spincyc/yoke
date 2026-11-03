"""Required browser gate for the exact Pages artifact, using pinned Playwright.

Run ``make browser-install verify-browser`` once developer tools are installed.
No executable is discovered on PATH and no browser is downloaded by this check.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Request, Response, Route

PREFIX = "/yoke/"
TIMEOUT_MS = 10_000


@contextmanager
def serve_site(site: Path) -> Iterator[str]:
    """Serve only the artifact, below the production project Pages prefix."""
    site = site.resolve(strict=True)
    if not site.is_dir() or not (site / "index.html").is_file():
        raise ValueError(f"not a built documentation site: {site}")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, message: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self._project_path():
                super().do_GET()

        def do_HEAD(self) -> None:
            if self._project_path():
                super().do_HEAD()

        def _project_path(self) -> bool:
            if not urlsplit(self.path).path.startswith(PREFIX):
                self.send_error(404)
                return False
            self.path = self.path[len(PREFIX) - 1 :]
            return True

    handler = partial(Handler, directory=str(site))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="docs-browser-server",
            daemon=True,
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}{PREFIX}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("documentation HTTP server did not stop")


def check_navigation(page: Page, base: str) -> None:
    """Protect disclosure state and real keyboard navigation across breakpoints."""
    from playwright.sync_api import expect

    navigation = page.locator(".navigation")
    summary = navigation.locator("summary")

    def resize(width: int) -> None:
        page.set_viewport_size({"width": width, "height": 1000})
        # Let media-query and native details toggle events finish in the browser.
        page.evaluate("() => new Promise(requestAnimationFrame)")

    def desktop_keyboard() -> None:
        expect(navigation).to_have_attribute("open", "")
        links = navigation.locator("nav a")
        assert links.count() >= 2, "documentation navigation needs keyboard stops"
        expect(links.first).to_be_visible()
        links.first.focus()
        page.keyboard.press("Tab")
        expect(links.nth(1)).to_be_focused()
        expect(links.nth(1)).to_be_visible()

    def toggle(opened: bool) -> None:
        summary.focus()
        page.keyboard.press("Enter")
        if opened:
            expect(navigation).to_have_attribute("open", "")
        else:
            expect(navigation).not_to_have_attribute("open", "")
        page.evaluate("() => new Promise(requestAnimationFrame)")

    resize(390)
    page.goto(base + "design/architecture.html", wait_until="load")
    expect(navigation).not_to_have_attribute("open", "")
    resize(1440)
    desktop_keyboard()
    resize(390)
    expect(navigation).not_to_have_attribute("open", "")
    toggle(True)
    resize(1440)
    desktop_keyboard()
    resize(390)
    expect(navigation).to_have_attribute("open", "")
    toggle(False)
    resize(1440)
    desktop_keyboard()


def check_without_javascript(browser: Browser, base: str) -> None:
    """The native disclosure must remain usable when JavaScript is disabled."""
    from playwright.sync_api import expect

    with browser.new_context(
        java_script_enabled=False, viewport={"width": 390, "height": 1000}
    ) as context:
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        page.set_default_navigation_timeout(TIMEOUT_MS)
        page.goto(base + "design/architecture.html", wait_until="load")
        navigation = page.locator(".navigation")
        summary = navigation.locator("summary")
        expect(navigation).to_have_attribute("open", "")
        summary.focus()
        page.keyboard.press("Enter")
        expect(navigation).not_to_have_attribute("open", "")
        page.set_viewport_size({"width": 1440, "height": 1000})
        expect(summary).to_be_visible()
        summary.focus()
        expect(summary).to_be_focused()
        page.keyboard.press("Enter")
        expect(navigation.locator("nav a").first).to_be_visible()
        page.keyboard.press("Tab")
        expect(navigation.locator("a").first).to_be_focused()


def check_search(page: Page, site: Path, base: str) -> None:
    """Load the real search index and follow a result at the nested Pages URL."""
    from playwright.sync_api import expect

    search_pages = sorted(
        path for path in site.rglob("*.html") if "data-doc-search" in path.read_text()
    )
    assert search_pages, "built site has no documentation search"
    search_url = base + search_pages[0].relative_to(site).as_posix()
    page.goto(search_url, wait_until="load")
    search = page.locator("[data-doc-search]")
    field = search.locator("input")
    expect(field).to_be_enabled()
    field.fill("architecture")
    result = search.locator(".search-results a").first
    expect(result).to_be_visible()
    result.click()
    expect(page.locator("h1")).to_be_visible()
    assert page.url.startswith(base), f"search escaped project Pages prefix: {page.url}"

    page.goto(search_url, wait_until="load")
    expect(field).to_be_enabled()

    def results_for(query: str) -> list[str]:
        field.fill(query)
        links = search.locator(".search-results a")
        expect(links.first).to_be_visible()
        routes = []
        for link in links.all():
            href = link.get_attribute("href")
            assert href and href.startswith(base), (
                f"search result escaped project Pages prefix: {query!r}: {href}"
            )
            routes.append(href.removeprefix(base))
        return routes

    # Search the actual published corpus: short acronyms must not match letters
    # inside "pair" or "wire", and current product scope should be easy to find.
    infrared = results_for("IR")
    assert "product.html" in infrared[:3], f"IR connections ranking: {infrared}"
    assert "docs/project-status.html" in infrared[:5], f"IR status ranking: {infrared}"
    assert not {"guides/pairing.html", "design/protocol.html"}.intersection(infrared), (
        f"IR matched pair/wire substrings: {infrared}"
    )

    pairing = results_for("pairing")
    assert "guides/pairing.html" in pairing, f"pairing task guide missing: {pairing}"
    guide_position = pairing.index("guides/pairing.html")
    assert all(
        guide_position < position
        for position, route in enumerate(pairing)
        if route.startswith("decisions/")
    ), f"historical ADRs outrank pairing task guide: {pairing}"

    # Slash-separated protocol names still match after short-term tokenization.
    usbip = results_for("USB/IP")
    assert "product.html" in usbip[:5], f"USB/IP connection search: {usbip}"


def verify_site(site: Path, artifacts: Path) -> dict[str, int | str]:
    """Fail on launch, frontend, responsive, keyboard, search or resource errors."""
    from playwright.sync_api import expect, sync_playwright

    site = site.resolve(strict=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "report.json").unlink(missing_ok=True)
    (artifacts / "failure-trace.zip").unlink(missing_ok=True)
    pages = sorted(path.relative_to(site).as_posix() for path in site.rglob("*.html"))
    failures: list[str] = []
    checks = 0
    with serve_site(site) as base, sync_playwright() as playwright:
        # The exact browser revision comes from the pinned Python dependency.
        # A missing browser is a gate failure, never an optional test skip.
        browser = playwright.chromium.launch(headless=True, timeout=30_000)
        try:
            with browser.new_context() as context:
                context.set_default_timeout(TIMEOUT_MS)
                context.set_default_navigation_timeout(TIMEOUT_MS)
                context.tracing.start(screenshots=True, snapshots=True)

                def allow_local(route: Route) -> None:
                    if route.request.url.startswith(base):
                        route.continue_()
                    else:
                        failures.append(
                            f"unexpected runtime request: {route.request.url}"
                        )
                        route.abort()

                def response_error(response: Response) -> None:
                    if response.status >= 400:
                        failures.append(f"HTTP {response.status}: {response.url}")

                def request_failed(request: Request) -> None:
                    failures.append(f"request failed: {request.url}: {request.failure}")

                context.route("**/*", allow_local)
                context.on("response", response_error)
                context.on("requestfailed", request_failed)
                page = context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        failures.append(message.text)
                        if message.type == "error"
                        else None
                    ),
                )
                try:
                    for scheme in ("light", "dark"):
                        page.emulate_media(
                            color_scheme=scheme,
                            reduced_motion="reduce"
                            if scheme == "dark"
                            else "no-preference",
                        )
                        for width in (320, 768, 1440):
                            page.set_viewport_size({"width": width, "height": 1000})
                            for route in pages:
                                page.goto(base + route, wait_until="load")
                                page.evaluate(
                                    "() => document.fonts.ready.then(() => true)"
                                )
                                expect(page.locator("h1")).to_be_visible()
                                assert page.evaluate(
                                    "document.documentElement.scrollWidth"
                                    " <= innerWidth + 1"
                                ), f"horizontal overflow: {route} ({width}px, {scheme})"
                                if route == "index.html":
                                    page.screenshot(
                                        path=str(
                                            artifacts / f"home-{width}-{scheme}.png"
                                        ),
                                        full_page=True,
                                    )
                                checks += 1
                    check_navigation(page, base)
                    check_search(page, site, base)
                    check_without_javascript(browser, base)
                    assert not failures, "\n".join(failures)
                except BaseException as error:
                    try:
                        context.tracing.stop(path=str(artifacts / "failure-trace.zip"))
                    except Exception as trace_error:
                        error.add_note(f"could not save browser trace: {trace_error}")
                    raise
                else:
                    context.tracing.stop()
                report: dict[str, int | str] = {
                    "browser": browser.version,
                    "pages": len(pages),
                    "responsive_checks": checks,
                    "navigation": "passed",
                    "search": "passed",
                    "without_javascript": "passed",
                    "reduced_motion": "passed",
                }
        finally:
            browser.close()
    (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=Path(".scratch/site"))
    parser.add_argument(
        "--artifacts", type=Path, default=Path(".scratch/browser-report")
    )
    arguments = parser.parse_args()
    print(json.dumps(verify_site(arguments.site, arguments.artifacts), indent=2))


if __name__ == "__main__":
    main()
