"""The browser end-to-end harness. `01-architecture.md` §5, `07-frontend.md` §5.

The visual harness renders a map headlessly and compares pixels. This one
drives the *application*: a real browser at a real resolution, clicking the
things a geologist clicks.

Between them they gate the acceptance criteria across Phases 2, 3 and 6 that
need a browser, and the roadmap calls the absence of both "the gap worth
naming" — the code behind those criteria is written and unit-tested, and none
of it was demonstrated.

**Desktop is the base case** (`07` §5.1), so the viewport is 1440×900: the
layout the application is designed for, not a default 1280×720 that would
exercise the one width the CSS treats as a degraded case. `07` §5.1 also puts a
minimum-width notice below 1280 px, and a harness running under it would test
the notice.

Every fixture here **skips with instructions** when the stack is not up. A
developer running the unit suite should not have to start Docker, and a skip
that says how to fix it is the difference between "not run" and "broken".
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

#: The web application. Overridable so the harness runs against a container in
#: CI and a Vite dev server on a workstation.
WEB_URL = os.environ.get("WEBMAP_WEB_URL", "http://localhost:5173")
API_URL = os.environ.get("WEBMAP_API_URL", "http://localhost:8000")

#: `07` §5.1's design width. Not a phone, not a laptop's smallest useful size —
#: the workstation the application targets.
VIEWPORT = {"width": 1440, "height": 900}

#: A wider one, for the `min-width: 1920px` breakpoint that widens the panels.
WIDE_VIEWPORT = {"width": 1920, "height": 1080}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: drives a real browser against the web app")


def _reachable(url: str) -> str | None:
    """None if the service answers, else why not."""
    import httpx

    try:
        httpx.get(url, timeout=5)
    # Broad on purpose, and not a suppression: every way a service can be
    # unreachable — refused, DNS, TLS, timeout — becomes the same skip, and
    # the exception's type name is what makes the message specific.
    except Exception as error:
        return f"{url} ({type(error).__name__})"
    return None


@pytest.fixture(scope="session")
def stack() -> tuple[str, str]:
    """The web app and the API, or a skip naming whichever is down."""
    unreachable = [
        problem
        for problem in (_reachable(f"{API_URL}/health"), _reachable(WEB_URL))
        if problem is not None
    ]
    if unreachable:
        pytest.skip(
            f"The stack is not up: {', '.join(unreachable)}. Start it with: "
            f"docker compose -f infra/compose.yaml up -d"
        )
    return WEB_URL, API_URL


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    """One Chromium for the session.

    Chromium specifically: it is what the render service runs
    (`06-rendering.md` §2), so a difference between what the browser shows and
    what a slide shows is a difference in the application rather than in the
    engine.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover — an optional dependency
        pytest.skip(
            "Playwright is not installed. Install it with: "
            "uv sync --group e2e && uv run playwright install chromium"
        )

    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser: Any, stack: tuple[str, str]) -> Iterator[Any]:
    """A fresh page at the design viewport, with console errors collected.

    **A console error fails the test that caused it.** A React error boundary
    catching an exception leaves the page looking almost right, and an
    assertion on what is visible passes while the application is broken — which
    is exactly the failure an end-to-end harness exists to catch and the one it
    is easiest to miss.
    """
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    active = context.new_page()

    errors: list[str] = []
    active.on(
        "console",
        lambda message: errors.append(message.text) if message.type == "error" else None,
    )
    active.on("pageerror", lambda error: errors.append(str(error)))

    try:
        yield active
        assert not errors, (
            "The page logged errors while this test ran, so anything it asserted "
            f"passed over a broken application: {errors}"
        )
    finally:
        context.close()


@pytest.fixture
def dev_token(stack: tuple[str, str]) -> str:
    """A bearer token from the development verifier.

    Only reachable when `auth_mode` is `dev`, which four separate guards refuse
    to combine with a production environment (`adr/0009`). A harness that
    needed a real OIDC round trip would be a harness nobody runs.
    """
    import httpx

    _, api = stack
    response = httpx.post(f"{api}/auth/dev/token", params={"user": "e2e"}, timeout=30)
    if response.status_code != 200:
        pytest.skip(
            f"The API would not issue a development token ({response.status_code}). "
            f"This deployment is not in dev auth mode, and the end-to-end harness "
            f"has no other way in."
        )
    return str(response.json()["access_token"])
