"""Tile endpoint authorization. `03-auth-security.md` §6.

`12-roadmap.md` Phase 2: *"Tile requests without a valid scoped token return
403."* Tile endpoints are hit thousands of times during a single pan, and the
temptation to leave them open for performance is exactly how data leaks.

Driven over HTTP against the running API rather than through the service
layer, because the property under test is what an unauthenticated *request*
gets — the dependency wiring is as much a part of that as the check itself.
"""

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8000"
MIDLAND_TILE = "10/221/416"


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


def bearer(api: str, user: str) -> dict[str, str]:
    response = httpx.post(f"{api}/auth/dev/token", params={"user": user}, timeout=30)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def dataset_of(api: str, headers: dict[str, str], kind: str) -> dict[str, Any]:
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"kind": kind}, headers=headers, timeout=30
    )
    response.raise_for_status()
    items = response.json()["items"]
    if not items:
        pytest.skip(f"No seeded {kind} dataset. Run: uv run python scripts/seed.py")
    return dict(items[0])


# --- no credential ----------------------------------------------------------


def test_a_tile_request_with_no_credential_is_refused(api: str) -> None:
    """The criterion. Nothing about a tile endpoint is public."""
    dataset = dataset_of(api, bearer(api, "ada"), "pointset")

    response = httpx.get(f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt", timeout=30)

    assert response.status_code == 401
    assert response.content[:4] != b"\x1a\x00\x00\x00", "no tile body on refusal"


def test_a_geojson_request_with_no_credential_is_refused(api: str) -> None:
    """The GeoJSON path is the same data by another route, and needs the same
    guard — an endpoint that returns whole layers is the more attractive one."""
    dataset = dataset_of(api, bearer(api, "ada"), "pointset")

    response = httpx.get(f"{api}/api/v1/features/{dataset['id']}.geojson", timeout=30)

    assert response.status_code == 401


def test_a_raster_tile_with_no_credential_is_refused(api: str) -> None:
    """TiTiler has no concept of a WebMap user.

    Anything that could reach it directly could read every grid in the bucket,
    so the proxy is the only thing standing in front of it.
    """
    grid = dataset_of(api, bearer(api, "ada"), "grid")

    response = httpx.get(f"{api}/api/v1/cog/{grid['id']}/{MIDLAND_TILE}.png", timeout=30)

    assert response.status_code == 401


# --- wrong user -------------------------------------------------------------


def test_a_user_without_access_cannot_fetch_tiles(api: str) -> None:
    """Alan is on another team. He gets the same answer as for a dataset that
    does not exist — confirming the id would itself be a disclosure."""
    dataset = dataset_of(api, bearer(api, "ada"), "pointset")

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt",
        headers=bearer(api, "alan"),
        timeout=30,
    )

    assert response.status_code == 404


def test_a_user_without_access_cannot_mint_a_tile_token(api: str) -> None:
    """The mint is where the permission check happens, so it is the door."""
    dataset = dataset_of(api, bearer(api, "ada"), "pointset")

    response = httpx.post(
        f"{api}/api/v1/datasets/{dataset['id']}/tile-token",
        headers=bearer(api, "alan"),
        timeout=30,
    )

    assert response.status_code == 404


# --- scoped tokens ----------------------------------------------------------


def test_a_scoped_token_fetches_tiles_without_a_bearer(api: str) -> None:
    """What the browser actually uses.

    A style's tile URL cannot carry an Authorization header, so the credential
    has to be in the URL — which is why it is scoped rather than general.
    """
    headers = bearer(api, "ada")
    dataset = dataset_of(api, headers, "pointset")
    token = httpx.post(
        f"{api}/api/v1/datasets/{dataset['id']}/tile-token", headers=headers, timeout=30
    ).json()["token"]

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt",
        params={"token": token},
        timeout=30,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert len(response.content) > 0


def test_a_token_for_one_dataset_cannot_fetch_another(api: str) -> None:
    """**The scoping property, and the reason this design is acceptable.**

    A tile URL ends up in devtools, in bug reports, in screenshots. Because
    the token names one dataset, a leak exposes exactly one layer the person
    who leaked it could already see — rather than everything they can access.
    """
    headers = bearer(api, "ada")
    points = dataset_of(api, headers, "pointset")
    faults = dataset_of(api, headers, "fault_network")
    token = httpx.post(
        f"{api}/api/v1/datasets/{points['id']}/tile-token", headers=headers, timeout=30
    ).json()["token"]

    response = httpx.get(
        f"{api}/api/v1/tiles/{faults['id']}/{MIDLAND_TILE}.mvt",
        params={"token": token},
        timeout=30,
    )

    assert response.status_code == 401
    assert "not valid for this layer" in response.json()["detail"]


def test_a_tampered_token_is_refused(api: str) -> None:
    """The payload is readable; only the signature stops it being editable."""
    headers = bearer(api, "ada")
    dataset = dataset_of(api, headers, "pointset")
    token = httpx.post(
        f"{api}/api/v1/datasets/{dataset['id']}/tile-token", headers=headers, timeout=30
    ).json()["token"]

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt",
        params={"token": token[:-4] + "AAAA"},
        timeout=30,
    )

    assert response.status_code == 401


@pytest.mark.parametrize("token", ["", "garbage", "not.a.token", "YWJj"])
def test_malformed_tokens_are_refused_without_a_server_error(api: str, token: str) -> None:
    """A malformed token is a 401, never a 500.

    Otherwise a scanner can tell forged tokens from malformed ones by the
    status code alone.
    """
    dataset = dataset_of(api, bearer(api, "ada"), "pointset")

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt",
        params={"token": token} if token else None,
        timeout=30,
    )

    assert response.status_code == 401, f"got {response.status_code} for {token!r}"


# --- behaviour --------------------------------------------------------------


def test_tiles_are_cached_by_dataset_version_and_coordinates(api: str) -> None:
    """`06-rendering.md` §7. The version in the key means an edit invalidates
    exactly the layer that changed, with no explicit purge."""
    headers = bearer(api, "ada")
    dataset = dataset_of(api, headers, "pointset")
    url = f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt"

    first = httpx.get(url, headers=headers, timeout=30)
    second = httpx.get(url, headers=headers, timeout=30)

    assert first.status_code == second.status_code == 200
    assert second.headers["X-Tile-Cache"] == "hit"
    assert first.content == second.content


def test_an_empty_tile_is_204_not_an_empty_body(api: str) -> None:
    """MapLibre stops asking about an area it gets a 204 for.

    A 200 with a header-only tile costs a request and a decode to say the same
    thing, on every pan, forever.
    """
    headers = bearer(api, "ada")
    dataset = dataset_of(api, headers, "pointset")

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/10/1/1.mvt", headers=headers, timeout=30
    )

    assert response.status_code == 204
    assert response.content == b""


def test_tiles_are_marked_private_in_the_cache_header(api: str) -> None:
    """A tile is one user's authorized view.

    A shared cache serving it to the next requester would undo the scoping
    entirely — the most expensive possible way to leak data.
    """
    headers = bearer(api, "ada")
    dataset = dataset_of(api, headers, "pointset")

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/{MIDLAND_TILE}.mvt", headers=headers, timeout=30
    )

    assert "private" in response.headers["cache-control"]


def test_a_raster_tile_renders_through_the_proxy(api: str) -> None:
    headers = bearer(api, "ada")
    grid = dataset_of(api, headers, "grid")

    response = httpx.get(
        f"{api}/api/v1/cog/{grid['id']}/{MIDLAND_TILE}.png", headers=headers, timeout=60
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_colormap_change_is_a_url_parameter_not_a_regrid(api: str) -> None:
    """`01-architecture.md` §2.5: the COG + TiTiler payoff.

    Two palettes over the same stored grid must produce different images with
    no server-side work beyond rendering, which is what makes palette editing
    feel instant.
    """
    headers = bearer(api, "ada")
    grid = dataset_of(api, headers, "grid")
    url = f"{api}/api/v1/cog/{grid['id']}/{MIDLAND_TILE}.png"

    viridis = httpx.get(url, params={"colormap_name": "viridis"}, headers=headers, timeout=60)
    spectral = httpx.get(url, params={"colormap_name": "spectral"}, headers=headers, timeout=60)

    assert viridis.status_code == spectral.status_code == 200
    assert viridis.content != spectral.content


def test_geojson_refuses_a_layer_above_the_threshold(api: str) -> None:
    """Refuses rather than truncating.

    A silently partial layer is worse than an error, because it looks like the
    data (`06-rendering.md` §7.1).
    """
    headers = bearer(api, "ada")
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"kind": "pointset"}, headers=headers, timeout=30
    )
    large = [d for d in response.json()["items"] if (d.get("feature_count") or 0) >= 5000]
    if not large:
        pytest.skip("No seeded layer above the GeoJSON threshold")

    result = httpx.get(
        f"{api}/api/v1/features/{large[0]['id']}.geojson", headers=headers, timeout=60
    )

    assert result.status_code == 404
    assert "vector tile endpoint" in result.json()["detail"]


def test_tile_coordinates_outside_the_grid_are_rejected(api: str) -> None:
    """Refused before reaching DuckDB, so a scan cannot probe with them."""
    headers = bearer(api, "ada")
    dataset = dataset_of(api, headers, "pointset")

    response = httpx.get(
        f"{api}/api/v1/tiles/{dataset['id']}/2/99/99.mvt", headers=headers, timeout=30
    )

    assert response.status_code >= 400
    assert response.status_code != 500


def test_the_geojson_path_returns_longitude_latitude(api: str) -> None:
    """RFC 7946 order, over the wire.

    Asserted at the endpoint as well as in the library because this is the
    representation MapLibre consumes, and an axis swap here puts the whole
    layer in the Indian Ocean while every unit test still passes.
    """
    headers = bearer(api, "ada")
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"kind": "fault_network"}, headers=headers, timeout=30
    )
    items = response.json()["items"]
    if not items:
        pytest.skip("No seeded fault network")

    result = httpx.get(
        f"{api}/api/v1/features/{items[0]['id']}.geojson", headers=headers, timeout=60
    )
    result.raise_for_status()
    coords = result.json()["features"][0]["geometry"]["coordinates"][0]

    assert -104 < coords[0] < -100, f"longitude {coords[0]} — axes swapped?"
    assert 30.5 < coords[1] < 33.5, f"latitude {coords[1]} — axes swapped?"
