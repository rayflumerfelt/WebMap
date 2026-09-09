"""Glyph ranges for map labels. `08-styling-palettes.md`.

MapLibre fetches signed-distance-field glyphs from the style's `glyphs` URL as
`{fontstack}/{start}-{end}.pbf`, one request per range of codepoints it needs
to draw. There is no system-font fallback: a stack it cannot fetch draws
nothing, silently.

**Unauthenticated, deliberately.** These are open-licensed font outlines
(`scripts/fetch_fonts.py` records the licence for each). They carry no user
data, they are identical for every principal, and requiring a token would mean
the render service needed one to draw a label — `03-auth-security.md` §7.3
keeps that worker as isolated as possible. Everything that *is* user data
stays behind `require()` as before.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Response

from webmap_api.dependencies import AppSettings
from webmap_core.exceptions import NotFound
from webmap_core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/static/glyphs", tags=["glyphs"])

#: A font stack name, as `text-font` spells it: "Noto Sans Bold". Letters,
#: digits, spaces and hyphens only. This is the path-traversal guard — the
#: name arrives in the URL and becomes a directory, so `..` and separators
#: must be impossible rather than stripped.
STACK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,63}$")

#: Ranges are 256 codepoints wide and always aligned, so the start is a
#: multiple of 256 and the end is start + 255.
RANGE = re.compile(r"^(\d{1,6})-(\d{1,6})$")

#: Glyphs never change for a given stack and range — a new font is a new
#: stack name. A year is the conventional "forever" for immutable assets.
CACHE_CONTROL = "public, max-age=31536000, immutable"


@lru_cache(maxsize=1)
def _root(configured: str) -> Path:
    """The glyph directory, resolved once.

    Defaults to `infra/glyphs` relative to the repository root so a developer
    needs no configuration; a container sets `WEBMAP_GLYPH_DIR` to wherever
    the image put them.
    """
    if configured:
        return Path(configured).resolve()
    # apps/api/src/webmap_api/routes/glyphs.py → repository root
    return (Path(__file__).resolve().parents[5] / "infra" / "glyphs").resolve()


@router.get("/{fontstack}/{codepoints}.pbf")
async def glyph_range(fontstack: str, codepoints: str, settings: AppSettings) -> Response:
    """One range of one font stack.

    A missing range is a 404 rather than an empty body: MapLibre treats an
    empty PBF as "no glyphs here" and a 404 as a load failure, and the second
    is the honest answer when the stack was never built.
    """
    if not STACK.match(fontstack):
        raise NotFound(
            f"'{fontstack}' is not a font stack name. Names look like "
            f"'Noto Sans Bold' — letters, digits, spaces and hyphens."
        )
    matched = RANGE.match(codepoints)
    if not matched:
        raise NotFound(
            f"'{codepoints}' is not a codepoint range. Ranges look like "
            f"'0-255' and are aligned to 256."
        )

    start, end = int(matched.group(1)), int(matched.group(2))
    if start % 256 or end != start + 255:
        raise NotFound(
            f"Range {start}-{end} is not aligned. Ranges are 256 codepoints "
            f"wide and start on a multiple of 256."
        )

    root = _root(settings.glyph_dir)
    path = root / fontstack / f"{start}-{end}.pbf"

    # Belt and braces over the regex: resolve and confirm the result is still
    # inside the glyph directory. The regex already forbids separators, so
    # this can only fire if that pattern is ever loosened.
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise NotFound(
            f"No glyphs for '{fontstack}' at {start}-{end}. Build them with "
            f"`uv run python scripts/fetch_fonts.py`; a deployment with no "
            f"glyphs draws no labels at all."
        )

    return Response(
        content=resolved.read_bytes(),
        media_type="application/x-protobuf",
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.get("")
async def list_stacks(settings: AppSettings) -> dict[str, list[str]]:
    """The stacks this deployment can draw.

    The styling UI reads this rather than hard-coding a font list, so a
    deployment that built a different roster offers what it actually has —
    and a font that failed to build is absent here rather than offered and
    then silently blank.
    """
    root = _root(settings.glyph_dir)
    if not root.is_dir():
        return {"stacks": []}
    return {"stacks": sorted(d.name for d in root.iterdir() if d.is_dir())}
