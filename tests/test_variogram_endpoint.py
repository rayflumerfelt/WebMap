"""Fitting a variogram through the API. `05-geoprocessing.md` §6.3.

Synchronous and read-only, which is the whole design decision: `10` §6 puts
work under five seconds on the request path, and "what is the range on this
layer?" is asked while deciding whether to grid at all. An answer behind a poll
cycle is an answer nobody waits for.

What is worth testing here is not that a curve gets fitted — the variogram code
has its own unit tests — but that the *reporting* is honest. A fitted variogram
always produces numbers, and the numbers look equally authoritative whether
there is spatial structure or none at all.

Needs a running API and the seeded layer. Skips with instructions otherwise.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8000"


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
def pointset(api: str, headers: dict[str, str]) -> dict[str, Any]:
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"limit": 100}, headers=headers, timeout=30
    )
    response.raise_for_status()
    items = [
        item
        for item in response.json()["items"]
        if item.get("geometry_kind") == "point" and (item.get("feature_count") or 0) >= 100
    ]
    if not items:
        pytest.skip("No seeded point layer. Run: uv run python scripts/seed.py")
    return dict(items[0])


def fit(api: str, headers: dict[str, str], dataset_id: str, **body: Any) -> httpx.Response:
    return httpx.post(
        f"{api}/api/v1/datasets/{dataset_id}/variogram",
        json=body,
        headers=headers,
        timeout=120,
    )


def value_column(api: str, headers: dict[str, str], dataset_id: str) -> str:
    detail = httpx.get(
        f"{api}/api/v1/datasets/{dataset_id}", headers=headers, timeout=30
    ).json()
    schema = detail.get("attribute_schema") or []
    # The registry writes JSON-schema-ish type names ("number"), the
    # aggregation writer writes SQL-ish ones ("double"). Accept both rather
    # than skipping every test on a layer that has a perfectly good column.
    numeric = [
        f["name"]
        for f in schema
        if f.get("type") in ("number", "double", "integer", "bigint", "float")
    ]
    if not numeric:
        pytest.skip(f"{detail.get('name')} has no numeric column to fit against")
    return str(numeric[0])


# --- it answers, and the answer is judgeable ---------------------------------


def test_a_fit_comes_back_inline_with_the_numbers_that_qualify_it(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """Not a job handle. And not just the model's parameters: the pair count
    and the nugget fraction are what say whether to believe them."""
    column = value_column(api, headers, pointset["id"])

    response = fit(api, headers, pointset["id"], value_column=column)

    assert response.status_code == 200, response.text
    result = response.json()
    assert "job_id" not in result, "this is synchronous by design (`10` §6)"
    assert result["range"] > 0
    assert result["model"]
    assert result["n_points"] >= 100
    assert result["n_pairs_used"] > result["n_points"], "pairs, not points"
    assert 0.0 <= result["nugget_ratio"] <= 1.0
    assert result["units"], "a range without units is not a distance"


def test_the_empirical_points_come_back_with_the_model(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """A variogram plot without them is a curve with nothing to judge it
    against, and `07` §9.5 sizes its points by `n_pairs` so a tail built from
    nine pairs does not look like the rest of the curve."""
    column = value_column(api, headers, pointset["id"])

    result = fit(api, headers, pointset["id"], value_column=column, n_lags=12).json()

    lags = result["lags"]
    assert len(lags) > 0
    assert all(lag["n_pairs"] > 0 for lag in lags), "an empty lag is not a point"
    assert [lag["distance"] for lag in lags] == sorted(lag["distance"] for lag in lags)


def test_the_same_request_gives_the_same_fit(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """**The reason the seed is fixed rather than random.** The fit runs on a
    subsample, so an unseeded run returns a different range every time it is
    asked — and a geologist comparing two answers cannot tell whether the data
    changed or the dice did."""
    column = value_column(api, headers, pointset["id"])

    first = fit(api, headers, pointset["id"], value_column=column).json()
    second = fit(api, headers, pointset["id"], value_column=column).json()

    assert first["range"] == second["range"]
    assert first["sill"] == second["sill"]
    assert first["nugget"] == second["nugget"]
    assert first["seed"] == second["seed"]


def test_a_different_seed_is_allowed_to_give_a_different_answer(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """Determinism is per-seed, not absolute. If this ever stopped varying it
    would mean the subsample is not actually random, which would make the
    declustering in `05` §6.3 a no-op."""
    column = value_column(api, headers, pointset["id"])

    other = fit(api, headers, pointset["id"], value_column=column, seed=7).json()

    assert other["seed"] == 7
    assert other["range"] > 0


# --- honesty about a fit that should not be trusted ----------------------------


def test_a_trending_surface_is_flagged_rather_than_quietly_kriged(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """A power model has no sill: semivariance rises without bound, which is
    the signature of a regional trend rather than stationary structure.
    Ordinary kriging assumes stationarity, and the grid still looks fine — so
    the warning is the only place anyone learns."""
    column = value_column(api, headers, pointset["id"])

    result = fit(api, headers, pointset["id"], value_column=column).json()

    if result["model"] == "power":
        assert any("trend" in warning for warning in result["warnings"]), (
            "a power model must say why it is one"
        )


def test_an_explicit_model_is_honoured_even_when_another_fits_better(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """Matching a partner's map is a legitimate reason to ask for a model the
    data does not prefer."""
    column = value_column(api, headers, pointset["id"])

    result = fit(api, headers, pointset["id"], value_column=column, model="spherical").json()

    assert result["model"] == "spherical"


# --- refusals -------------------------------------------------------------------


def test_a_column_that_is_not_there_lists_the_ones_that_are(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """`CLAUDE.md` §8. "No such column" with nothing else is the least useful
    thing to hand back to a conversation trying to analyse something."""
    response = fit(api, headers, pointset["id"], value_column="not_a_column")

    assert response.status_code == 400, response.text
    assert "Available:" in response.text


def test_a_dataset_the_caller_cannot_see_is_not_confirmed_to_exist(
    api: str, headers: dict[str, str]
) -> None:
    """Same rule as everywhere else: the response must not distinguish "does
    not exist" from "not yours"."""
    response = fit(api, headers, "00000000-0000-4000-8000-000000000000", value_column="x")

    assert response.status_code == 404
    assert "permission" not in response.text.lower()
