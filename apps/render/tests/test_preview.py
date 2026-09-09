"""Preview encoding. `adr/0006-render-image-delivery.md`.

Separate from `test_render.py` because these need no browser: one screenshot,
two encodes, and the encode is the part under test. Keeping them out of the
async module also keeps that module honestly all-async.
"""

from __future__ import annotations

import io

from PIL import Image

from webmap_render.service import downscale_png


def test_a_large_render_is_downscaled_for_the_mcp_response() -> None:
    """`adr/0006`. One screenshot, two encodes: the preview is what reaches
    Claude, the master is what goes on a slide."""
    original = Image.new("RGB", (2560, 1440), "#2b93b3")
    buffer = io.BytesIO()
    original.save(buffer, format="PNG")

    preview = downscale_png(buffer.getvalue(), 1600)

    assert max(Image.open(io.BytesIO(preview)).size) == 1600


def test_a_small_render_is_returned_unchanged_rather_than_re_encoded() -> None:
    """A round trip through PIL at the same size is lossless in content and
    still changes the bytes, which would make two identical renders differ."""
    original = Image.new("RGB", (640, 360), "#2b93b3")
    buffer = io.BytesIO()
    original.save(buffer, format="PNG")
    png = buffer.getvalue()

    assert downscale_png(png, 1600) is png
