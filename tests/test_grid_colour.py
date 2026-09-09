"""How a grid gets its colours. `08-styling-palettes.md`.

**MapLibre does not colour a grid.** Raster layers have no data-driven paint,
so the colour is baked into the PNG by TiTiler before the browser sees it.
Everything a palette editor produces therefore has to travel as a tile
parameter, and the two failure modes are both silent: a grid that renders as a
patchwork because each tile scaled itself, and a grid that renders as nothing
because the bands were compared against the wrong numbers.

Both happened. Both are tested here.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8000"


def tile_of(lon: float, lat: float, zoom: int) -> tuple[int, int, int]:
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return zoom, x, y


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


@pytest.fixture(scope="module")
def headers(api: str) -> dict[str, str]:
    token = httpx.post(f"{api}/auth/dev/token", params={"user": "grace"}, timeout=30).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def grid(api: str, headers: dict[str, str]) -> dict[str, Any]:
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"kind": "grid"}, headers=headers, timeout=30
    )
    response.raise_for_status()
    items = response.json()["items"]
    if not items:
        pytest.skip("No seeded grid. Run: uv run python scripts/seed.py")
    return dict(items[0])


def fetch_tile(
    api: str, headers: dict[str, str], dataset_id: str, **params: str
) -> httpx.Response:
    z, x, y = tile_of(-102.0, 31.75, 10)
    return httpx.get(
        f"{api}/api/v1/cog/{dataset_id}/{z}/{x}/{y}.png",
        params=params,
        headers=headers,
        timeout=60,
    )


def colours(payload: bytes) -> dict[tuple[int, ...], int]:
    """Distinct RGBA values in a PNG, with their pixel counts."""
    import numpy as np
    import rasterio

    with rasterio.io.MemoryFile(payload) as memory, memory.open() as src:
        pixels = src.read().reshape(src.count, -1).T
    unique, counts = np.unique(pixels, axis=0, return_counts=True)
    return {tuple(int(v) for v in u): int(c) for u, c in zip(unique, counts, strict=True)}


# --- every grid has a range to scale against ----------------------------------


def test_a_grid_records_the_range_it_is_coloured_across(
    api: str, headers: dict[str, str], grid: dict[str, Any]
) -> None:
    """**The patchwork bug.** Without `value_min`/`value_max` the tile endpoint
    sends no `rescale`, TiTiler stretches each tile to *that tile's* local
    range, and every tile gets its own scale. Every grid the gridding job
    produced rendered that way, because the job never recorded a range."""
    detail = httpx.get(
        f"{api}/api/v1/datasets/{grid['id']}", headers=headers, timeout=30
    ).json()

    assert detail["value_min"] is not None, "no range: this grid renders as a patchwork"
    assert detail["value_max"] > detail["value_min"]


# --- the two colormap forms ----------------------------------------------------


def test_a_named_ramp_renders(api: str, headers: dict[str, str], grid: dict[str, Any]) -> None:
    response = fetch_tile(api, headers, grid["id"], colormap_name="viridis")

    assert response.status_code == 200
    opaque = {c: n for c, n in colours(response.content).items() if c[3] > 0}
    assert len(opaque) > 10, "a continuous ramp should produce many colours"


def test_discrete_bands_render_exactly_the_colours_they_name(
    api: str, headers: dict[str, str], grid: dict[str, Any]
) -> None:
    """Interval symbology. The bands are in **data units** and TiTiler does a
    direct lookup — no interpolation, no normalisation — so the rendered
    pixels are byte-for-byte what the palette editor chose."""
    detail = httpx.get(
        f"{api}/api/v1/datasets/{grid['id']}", headers=headers, timeout=30
    ).json()
    low, high = float(detail["value_min"]), float(detail["value_max"])
    mid = (low + high) / 2
    sent = [
        [[low - 1e6, mid], [44, 123, 182, 255]],
        [[mid, high + 1e6], [215, 25, 28, 255]],
    ]

    response = fetch_tile(api, headers, grid["id"], colormap=json.dumps(sent))

    assert response.status_code == 200
    opaque = {c for c, _ in colours(response.content).items() if c[3] > 0}
    assert opaque, "the whole tile rendered transparent"
    assert opaque <= {(44, 123, 182, 255), (215, 25, 28, 255)}, (
        f"rendered colours the palette did not name: {opaque}"
    )


def test_discrete_bands_are_not_ruined_by_the_default_rescale(
    api: str, headers: dict[str, str], grid: dict[str, Any]
) -> None:
    """**The bug that rendered a whole tile transparent.**

    `rescale` normalises the data to 0-255 *before* the colormap applies. A
    band list is in raw data units, so with a default rescale in play its
    bounds get compared against 0-255 and nothing ever matches — 65,536 pixels
    of alpha zero, behind a 200 response. The endpoint must drop its default
    rescale when the colormap is a band list.
    """
    detail = httpx.get(
        f"{api}/api/v1/datasets/{grid['id']}", headers=headers, timeout=30
    ).json()
    # Deliberately spans everything, so any pixel that comes back transparent
    # is the bug rather than an uncovered value.
    sent = [
        [
            [float(detail["value_min"]) - 1e6, float(detail["value_max"]) + 1e6],
            [10, 200, 30, 255],
        ]
    ]

    response = fetch_tile(api, headers, grid["id"], colormap=json.dumps(sent))

    rendered = colours(response.content)
    transparent = sum(n for c, n in rendered.items() if c[3] == 0)
    assert transparent == 0, f"{transparent:,} px transparent under a band covering everything"
    assert set(rendered) == {(10, 200, 30, 255)}


def test_an_explicit_rescale_is_still_honoured_with_bands(
    api: str, headers: dict[str, str], grid: dict[str, Any]
) -> None:
    """Only the *default* rescale is dropped. A caller who sends both has said
    what they mean, and the endpoint does not second-guess them."""
    sent = [[[0, 255], [10, 200, 30, 255]]]

    response = fetch_tile(api, headers, grid["id"], colormap=json.dumps(sent), rescale="0,255")

    assert response.status_code == 200


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-json", "[", "{oops}"])
def test_a_malformed_colormap_says_what_shape_is_expected(
    api: str, headers: dict[str, str], grid: dict[str, Any], bad: str
) -> None:
    """`CLAUDE.md` §8. This string is forwarded to an internal service, so an
    unparseable one must fail here rather than come back as an opaque 500."""
    response = fetch_tile(api, headers, grid["id"], colormap=bad)

    assert response.status_code == 400
    assert "discrete bands" in response.text or "valid JSON" in response.text


def test_an_empty_colormap_is_refused(
    api: str, headers: dict[str, str], grid: dict[str, Any]
) -> None:
    """It would render the whole grid transparent, which looks like no-data."""
    response = fetch_tile(api, headers, grid["id"], colormap="[]")

    assert response.status_code == 400
    assert "transparent" in response.text


def test_an_oversized_colormap_is_refused(
    api: str, headers: dict[str, str], grid: dict[str, Any]
) -> None:
    """A 256-entry lookup is about 6 KB. Anything far larger is not a palette,
    and this endpoint should not proxy arbitrary payloads inward.

    The limit sits well under the ~64 KB URL length clients enforce: set any
    higher and it would be unreachable, because httpx (and every proxy) refuses
    to build the request at all.
    """
    response = fetch_tile(api, headers, grid["id"], colormap=json.dumps(["x" * 40_000]))

    assert response.status_code == 400
    assert "not a palette" in response.text
