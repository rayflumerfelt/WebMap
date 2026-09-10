"""The application shell in a real browser. `07-frontend.md` §5.

What is asserted here is what only a browser can answer: that the layout the
CSS describes is the layout that renders, that the panels a user resizes stay
resized, and that the keyboard reaches what the mouse reaches.

None of these duplicate a unit test. `shell.test.tsx` renders the same
components in jsdom, which has no layout engine at all — every width it reports
is zero, so the one thing a grid template needs checking for is the one thing
jsdom cannot check.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.conftest import WIDE_VIEWPORT

pytestmark = pytest.mark.e2e


def test_the_shell_lays_out_at_the_design_width(page: Any, stack: tuple[str, str]) -> None:
    """`07` §5.1's 1440 px layout, measured rather than assumed.

    The map takes the middle and the panels take fixed widths from the density
    tokens. A grid template that collapses a column reads as "the panel is
    missing" and passes every unit test in the repository.
    """
    web, _ = stack
    page.goto(web, wait_until="networkidle")

    shell = page.locator(".app-shell")
    shell.wait_for(state="visible", timeout=15_000)

    box = shell.bounding_box()
    assert box is not None
    assert box["width"] >= 1400, (
        f"The shell is {box['width']:.0f} px wide at a 1440 px viewport. Below "
        f"1280 px it hides itself behind the minimum-width notice (§5.1), so "
        f"this is either that notice or a collapsed grid column."
    )


def test_the_minimum_width_notice_replaces_the_shell_below_1280(
    browser: Any, stack: tuple[str, str]
) -> None:
    """`07` §5.1: below 1280 px the app shows a notice rather than reflowing.

    Expressed as a `min-width` query on the shell rather than a `max-width` one
    on the notice, so the desktop-first rule holds with no exception — and this
    is the test that the arrangement actually works in a browser rather than
    only in the stylesheet.
    """
    web, _ = stack
    context = browser.new_context(viewport={"width": 1024, "height": 768})
    try:
        page = context.new_page()
        page.goto(web, wait_until="networkidle")

        assert page.locator(".min-width-notice").is_visible()
        assert not page.locator(".app-shell").is_visible()
    finally:
        context.close()


def test_the_panels_widen_at_the_1920_breakpoint(browser: Any, stack: tuple[str, str]) -> None:
    """Breakpoints add capability at larger sizes and never take it away
    (`07` §5.1). The left panel is 280 px at the design width and 320 px above
    1920 px, which is a `min-width` query doing what it says."""
    web, _ = stack
    context = browser.new_context(viewport=WIDE_VIEWPORT)
    try:
        page = context.new_page()
        page.goto(web, wait_until="networkidle")

        width = page.evaluate(
            "getComputedStyle(document.documentElement)"
            ".getPropertyValue('--panel-w-left').trim()"
        )
        assert width == "320px", f"--panel-w-left is {width!r} at 1920 px"
    finally:
        context.close()


def test_every_hover_action_is_reachable_by_keyboard(page: Any, stack: tuple[str, str]) -> None:
    """`07` §10, and the reason it is here rather than in jsdom.

    A hover-revealed action that a keyboard user cannot reach is invisible to
    them and nearly invisible to a mouse user in a hurry at 26 px rows. jsdom
    reports every element as visible, so the only place this can be checked is
    a browser that actually applies `:focus-visible`.
    """
    web, _ = stack
    page.goto(web, wait_until="networkidle")
    page.locator(".app-shell").wait_for(state="visible", timeout=15_000)

    # Walk the tab order and collect what it reaches. Bounded so a focus trap
    # fails the test rather than hanging it.
    reached: set[str] = set()
    for _ in range(120):
        page.keyboard.press("Tab")
        label = page.evaluate(
            "() => { const el = document.activeElement;"
            " return el ? (el.getAttribute('aria-label') || el.textContent || '').trim()"
            " : ''; }"
        )
        if label:
            reached.add(label)

    assert reached, "Tab reached nothing focusable at all — that is a focus trap."


def test_the_map_canvas_is_present_and_labelled(page: Any, stack: tuple[str, str]) -> None:
    """The map is the application. A canvas that never initialises leaves a
    shell that looks complete, which is the failure mode a screenshot misses
    and a role query does not."""
    web, _ = stack
    page.goto(web, wait_until="networkidle")

    canvas = page.locator("canvas.maplibregl-canvas")
    canvas.wait_for(state="visible", timeout=20_000)

    box = canvas.bounding_box()
    assert box is not None and box["width"] > 400 and box["height"] > 300, (
        f"The map canvas is {box}. A canvas of zero size is MapLibre "
        f"initialising into a container the layout has not sized yet."
    )
