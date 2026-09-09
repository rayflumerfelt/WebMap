"""Glyph serving. `08-styling-palettes.md`, `adr/0010` styling work.

**A missing glyph is silent.** MapLibre has no system-font fallback: a stack
it cannot fetch draws nothing, with no console error and no failed-tile
warning. So the failure this file guards against is a map that looks finished
and has no labels on it.

The path-traversal cases matter more than they usually would, because the font
stack name arrives in the URL and becomes a directory name — one of the few
places in this system where user input reaches the filesystem.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8000"

#: Built by `scripts/fetch_fonts.py`. Ten families, and Oswald has no italic
#: because none exists — the styling UI disables the control rather than
#: offering one that does nothing.
EXPECTED_FAMILIES = (
    "Noto Sans",
    "Open Sans",
    "Roboto",
    "Source Sans 3",
    "Lato",
    "PT Sans",
    "Oswald",
    "Noto Serif",
    "Playfair Display",
    "Roboto Mono",
)


@pytest.fixture(scope="module")
def api() -> str:
    try:
        httpx.get(f"{BASE_URL}/health", timeout=3).raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"No WebMap API at {BASE_URL} ({type(exc).__name__}). Start it with: "
            f"docker compose -f infra/compose.yaml up -d"
        )
    return BASE_URL


def fetch(api: str, stack: str, codepoints: str = "0-255") -> httpx.Response:
    return httpx.get(
        f"{api}/static/glyphs/{urllib.parse.quote(stack)}/{codepoints}.pbf", timeout=30
    )


# --- the roster ---------------------------------------------------------------


def test_every_family_has_a_regular_and_a_bold(api: str) -> None:
    listed = set(httpx.get(f"{api}/static/glyphs", timeout=30).json()["stacks"])

    missing = [
        f"{family} {style}"
        for family in EXPECTED_FAMILIES
        for style in ("Regular", "Bold")
        if f"{family} {style}" not in listed
    ]
    assert not missing, f"not built: {missing}"


def test_every_family_but_oswald_has_an_italic(api: str) -> None:
    """Oswald ships no italic. Synthesising one by slanting the glyphs would
    look wrong and would be worse than not offering it."""
    listed = set(httpx.get(f"{api}/static/glyphs", timeout=30).json()["stacks"])

    for family in EXPECTED_FAMILIES:
        has_italic = f"{family} Italic" in listed
        if family == "Oswald":
            assert not has_italic, "Oswald italic does not exist and must not be faked"
        else:
            assert has_italic, f"{family} Italic was not built"


def test_the_listing_is_what_the_ui_offers(api: str) -> None:
    """The styling UI reads this rather than hard-coding a font list, so a
    font that failed to build is absent rather than offered and then blank."""
    stacks = httpx.get(f"{api}/static/glyphs", timeout=30).json()["stacks"]

    assert len(stacks) == 29, f"expected 29 stacks, got {len(stacks)}"
    assert stacks == sorted(stacks), "the listing must be stable for the UI"


# --- the glyphs themselves ------------------------------------------------------


def test_a_range_returns_a_protobuf(api: str) -> None:
    response = fetch(api, "Noto Sans Regular")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    assert len(response.content) > 1000, "a Latin range should carry ~200 glyphs"


def test_bold_is_not_the_same_bytes_as_regular(api: str) -> None:
    """**The bug this caught.** A variable font carries every weight in one
    file and fontnik renders the default instance, so Bold built straight from
    `Roboto[wght].ttf` was byte-identical to Regular. Eight of the ten
    families are variable, so this would have shipped as "bold does nothing"
    across most of the roster."""
    for family in EXPECTED_FAMILIES:
        regular = fetch(api, f"{family} Regular").content
        bold = fetch(api, f"{family} Bold").content
        assert regular != bold, f"{family} Bold is identical to Regular"


def test_the_glyphs_are_cacheable_forever(api: str) -> None:
    """A stack's glyphs never change — a new font is a new stack name — so the
    browser should never re-fetch them."""
    response = fetch(api, "Roboto Regular")

    assert "immutable" in response.headers.get("cache-control", "")


def test_the_ranges_a_label_needs_are_all_present(api: str) -> None:
    """Latin, Latin Extended, Greek/Cyrillic and the punctuation block that
    carries degree signs, primes and en-dashes — everything a well name, a
    formation name or a contour label uses."""
    for codepoints in ("0-255", "256-511", "512-767", "768-1023", "8192-8447"):
        response = fetch(api, "Noto Sans Regular", codepoints)
        assert response.status_code == 200, f"{codepoints} missing"


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    "stack",
    ["../../etc/passwd", "..%2f..%2fetc", "Noto/Sans", "Noto\\Sans", ""],
)
def test_a_traversal_attempt_is_refused(api: str, stack: str) -> None:
    """The stack name becomes a directory name, which makes this one of the
    few places user input reaches the filesystem."""
    response = fetch(api, stack)

    assert response.status_code in (404, 405), f"{stack!r} was not refused"
    assert b"passwd" not in response.content


@pytest.mark.parametrize("codepoints", ["5-99", "0-100", "1-256", "abc-def"])
def test_an_unaligned_range_is_refused(api: str, codepoints: str) -> None:
    """Ranges are 256 wide and aligned. Anything else is a client that has
    invented a URL, and answering it would mean reading arbitrary filenames."""
    assert fetch(api, "Roboto Regular", codepoints).status_code == 404


def test_an_unbuilt_stack_says_how_to_build_it(api: str) -> None:
    """`CLAUDE.md` §8. A deployment with no glyphs draws no labels and gives
    no other clue, so the 404 has to name the fix."""
    response = fetch(api, "Comic Sans MS")

    assert response.status_code == 404
    assert "fetch_fonts.py" in response.text


def test_glyphs_need_no_token(api: str) -> None:
    """Deliberate: they are open-licensed font outlines with no user data in
    them, and requiring a token would mean the isolated render worker needed
    one to draw a label (`03` §7.3)."""
    response = httpx.get(f"{api}/static/glyphs/Roboto%20Regular/0-255.pbf", timeout=30)

    assert response.status_code == 200
