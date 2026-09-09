"""Session endpoints. `02-data-model.md` §3.8, `04-mcp-server.md` §7.

Driven over HTTP against the running API, because the property under test is
what a *request* gets: the dependency wiring, RLS, and the service checks are
one mechanism from the caller's side, and testing the service alone would miss
a route that forgot to depend on a principal.

The central case is `a session crossing teams drops the layers its reader
cannot see`. A session is a request to show some datasets, not a grant to see
them — and the three wrong answers (return them, refuse the whole session,
drop them silently) are each worse than the one implemented.
"""

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8000"
MIDLAND_VIEW = {"center": [-102.08, 31.99], "zoom": 9.5}


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


def datasets_of(api: str, headers: dict[str, str], kind: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"kind": kind}, headers=headers, timeout=30
    )
    response.raise_for_status()
    items = response.json()["items"]
    if not items:
        pytest.skip(f"No seeded {kind} dataset. Run: uv run python scripts/seed.py")
    return list(items)


def make_session(
    api: str, headers: dict[str, str], dataset_ids: list[str], **body: Any
) -> dict[str, Any]:
    response = httpx.post(
        f"{api}/api/v1/sessions",
        headers=headers,
        json={
            "layers": [{"dataset_id": d} for d in dataset_ids],
            "view": MIDLAND_VIEW,
            **body,
        },
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json())


# --- create and load --------------------------------------------------------


def test_a_session_returns_a_short_link_the_user_can_open(api: str) -> None:
    """`04-mcp-server.md` §7.1: the tool's whole output is a link."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]

    created = make_session(api, headers, [dataset["id"]], name="Wolfcamp review")

    assert len(created["short_code"]) == 6
    assert created["url"].endswith(f"/s/{created['short_code']}")
    assert created["layer_count"] == 1


def test_a_session_loads_by_short_code_and_by_id_alike(api: str) -> None:
    """Callers hold one or the other and should not have to know which: Claude
    keeps the id it was returned, the browser keeps the code from the URL."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]
    created = make_session(api, headers, [dataset["id"]])

    by_code = httpx.get(
        f"{api}/api/v1/sessions/{created['short_code']}", headers=headers, timeout=30
    )
    by_id = httpx.get(f"{api}/api/v1/sessions/{created['id']}", headers=headers, timeout=30)

    assert by_code.status_code == by_id.status_code == 200
    assert by_code.json()["id"] == by_id.json()["id"] == created["id"]


def test_a_loaded_layer_carries_its_dataset_rather_than_just_an_id(api: str) -> None:
    """The browser needs the name, bbox and kind to draw a layer tree. Making
    it fetch each dataset separately would be one request per layer on open."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]
    created = make_session(api, headers, [dataset["id"]])

    loaded = httpx.get(
        f"{api}/api/v1/sessions/{created['id']}", headers=headers, timeout=30
    ).json()

    (layer,) = loaded["layers"]
    assert layer["dataset"]["name"] == dataset["name"]
    assert layer["dataset"]["kind"] == "pointset"


def test_a_session_stores_references_not_copies(api: str) -> None:
    """`02` §3.8. The stored layer names a dataset and how to draw it; nothing
    about the data itself, which would balloon the row and go stale."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]
    created = make_session(api, headers, [dataset["id"]])

    loaded = httpx.get(
        f"{api}/api/v1/sessions/{created['id']}", headers=headers, timeout=30
    ).json()

    (layer,) = loaded["layers"]
    assert set(layer) >= {"dataset_id", "opacity", "visible", "z"}
    assert "features" not in layer
    assert "geometry" not in layer


# --- permissions ------------------------------------------------------------


def test_a_session_on_another_team_is_not_visible(api: str) -> None:
    """Alan is on exploration; the session is team-scoped to permian. He gets
    the same answer as for a session that does not exist."""
    ada = bearer(api, "ada")
    dataset = datasets_of(api, ada, "pointset")[0]
    created = make_session(api, ada, [dataset["id"]])

    response = httpx.get(
        f"{api}/api/v1/sessions/{created['short_code']}",
        headers=bearer(api, "alan"),
        timeout=30,
    )

    assert response.status_code == 404


def test_a_teammate_sees_the_whole_session(api: str) -> None:
    """Grace is on permian with Ada. Nothing is dropped for her."""
    ada = bearer(api, "ada")
    dataset = datasets_of(api, ada, "pointset")[0]
    created = make_session(api, ada, [dataset["id"]])

    loaded = httpx.get(
        f"{api}/api/v1/sessions/{created['id']}", headers=bearer(api, "grace"), timeout=30
    )

    assert loaded.status_code == 200
    assert loaded.json()["hidden_layer_count"] == 0
    assert len(loaded.json()["layers"]) == 1


def test_a_session_shared_across_teams_drops_the_layers_its_reader_cannot_see(
    api: str,
) -> None:
    """**The case this design exists for.**

    Ada builds an org-visible session over two layers: a pointset only her team
    can read, and the fault network the seed grants to Alan's team. Alan gets
    the layer he may see, a count of what was withheld, and no trace of the
    rest — not the name, not the styling, not the id. Returning the withheld
    layer would make a session document a way around the registry.
    """
    ada = bearer(api, "ada")
    alan = bearer(api, "alan")
    permian_only = datasets_of(api, ada, "pointset")[0]
    shared = datasets_of(api, alan, "fault_network")[0]

    created = make_session(
        api, ada, [permian_only["id"], shared["id"]], visibility="org", name="Cross-team"
    )
    loaded = httpx.get(f"{api}/api/v1/sessions/{created['id']}", headers=alan, timeout=30)

    assert loaded.status_code == 200
    body = loaded.json()
    assert body["hidden_layer_count"] == 1, "the permian-only layer should be withheld"
    assert [layer["dataset_id"] for layer in body["layers"]] == [shared["id"]]
    assert permian_only["name"] not in loaded.text, "a withheld layer must leave no trace"
    assert permian_only["id"] not in loaded.text


def test_a_session_cannot_reference_a_dataset_its_creator_cannot_see(api: str) -> None:
    """Caught at creation, when the caller still knows which dataset they
    meant — rather than at load, when the layer would simply be missing."""
    ada_only = datasets_of(api, bearer(api, "ada"), "pointset")[0]

    response = httpx.post(
        f"{api}/api/v1/sessions",
        headers=bearer(api, "alan"),
        json={"layers": [{"dataset_id": ada_only["id"]}], "view": MIDLAND_VIEW},
        timeout=30,
    )

    assert response.status_code in (403, 404)


def test_a_session_with_no_credential_is_refused(api: str) -> None:
    response = httpx.post(
        f"{api}/api/v1/sessions",
        json={
            "layers": [{"dataset_id": "00000000-0000-0000-0000-000000000000"}],
            "view": MIDLAND_VIEW,
        },
        timeout=30,
    )

    assert response.status_code == 401


# --- autosave and concurrency -----------------------------------------------


def test_autosave_moves_the_view_without_writing_an_audit_record(api: str) -> None:
    """The browser saves every few seconds while a user pans. Recording each
    one would bury the events `03` §10 exists to preserve."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]
    created = make_session(api, headers, [dataset["id"]])

    response = httpx.patch(
        f"{api}/api/v1/sessions/{created['id']}",
        params={"autosave": "true"},
        headers=headers,
        json={"view": {"center": [-101.5, 32.5], "zoom": 11}},
        timeout=30,
    )

    assert response.status_code == 200
    loaded = httpx.get(
        f"{api}/api/v1/sessions/{created['id']}", headers=headers, timeout=30
    ).json()
    assert loaded["view"]["zoom"] == 11


def test_a_stale_save_is_refused_rather_than_overwriting(api: str) -> None:
    """A session open in two tabs is the normal case, not the exception. The
    loser rebases; nothing is silently overwritten."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]
    created = make_session(api, headers, [dataset["id"]])
    loaded = httpx.get(
        f"{api}/api/v1/sessions/{created['id']}", headers=headers, timeout=30
    ).json()

    first = httpx.patch(
        f"{api}/api/v1/sessions/{created['id']}",
        headers=headers,
        json={"name": "Renamed by tab one", "expected_updated_at": loaded["updated_at"]},
        timeout=30,
    )
    second = httpx.patch(
        f"{api}/api/v1/sessions/{created['id']}",
        headers=headers,
        json={"name": "Renamed by tab two", "expected_updated_at": loaded["updated_at"]},
        timeout=30,
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "reapply" in second.json()["detail"]


def test_a_viewer_cannot_save_over_a_session_they_can_read(api: str) -> None:
    """Grace can open Ada's team session. Reading is not editing."""
    ada = bearer(api, "ada")
    dataset = datasets_of(api, ada, "pointset")[0]
    created = make_session(api, ada, [dataset["id"]], visibility="team")

    response = httpx.patch(
        f"{api}/api/v1/sessions/{created['id']}",
        headers=bearer(api, "grace"),
        json={"name": "Not mine to rename"},
        timeout=30,
    )

    assert response.status_code in (403, 404)


# --- validation over the wire -----------------------------------------------


def test_a_malformed_view_returns_422_with_the_fix(api: str) -> None:
    """The message is the interface for an MCP caller, so it has to survive
    the trip through the exception mapping intact."""
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]

    response = httpx.post(
        f"{api}/api/v1/sessions",
        headers=headers,
        json={
            "layers": [{"dataset_id": dataset["id"]}],
            "view": {"center": [31.99, -102.08], "zoom": 9},
        },
        timeout=30,
    )

    assert response.status_code == 422
    assert "longitude, latitude" in response.json()["detail"]


def test_a_deleted_session_stops_resolving(api: str) -> None:
    headers = bearer(api, "ada")
    dataset = datasets_of(api, headers, "pointset")[0]
    created = make_session(api, headers, [dataset["id"]])

    deleted = httpx.delete(
        f"{api}/api/v1/sessions/{created['id']}", headers=headers, timeout=30
    )
    reloaded = httpx.get(
        f"{api}/api/v1/sessions/{created['short_code']}", headers=headers, timeout=30
    )

    assert deleted.status_code == 204
    assert reloaded.status_code == 404
