"""Job endpoints over HTTP. `10-jobs-async.md` §5, §9, §10.

Driven against the running stack, because what is under test is what a
*request* gets: the dependency wiring, the arq submission, and the worker
picking the task up are one mechanism from the caller's side, and testing the
service alone would miss a route that forgot to depend on a principal or an
enqueue that never reached Redis.

The seeded well-pick layer is the input, so this is also the closest thing in
the suite to what a geologist actually does: submit a grid, watch it, get a
layer.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:8000"

#: Long enough for a real solve on the seeded layer, short enough that a stuck
#: worker fails the suite rather than hanging it.
#: Generous on purpose. These submit real gridding jobs to a real worker, and a
#: seeded grid takes 20-90 s unloaded. At 120 s this failed once on a machine
#: that was also running a 181 s measurement — a false failure that says nothing
#: about the code. The number that matters for latency is the measured budget in
#: `05` §10, asserted there; this one only needs to outlast a busy laptop.
JOB_TIMEOUT_SECONDS = 300

#: A token unique to this run, mixed into every job's output name.
#:
#: Idempotency is keyed on (kind, user, parameters) for an hour (`10` §10), so
#: two runs of this file within that window would have the second one's
#: submissions deduplicated against the first's — and every assertion about a
#: *new* job would fail on correct behaviour. The test for deduplication
#: submits the same name twice on purpose.
RUN = uuid4().hex[:8]


@pytest.fixture(scope="module")
def api() -> Iterator[str]:
    """The live API, with this module's datasets removed afterwards.

    **These tests write to the shared development stack**, unlike the ones on
    the `engine` fixture, which get a throwaway database. Without the teardown
    every run leaves its grids and contour layers behind: after a dozen runs
    the seeded Wolfcamp layer had been pushed off the first page of
    `webmap_list_datasets`, and tests that look for it began failing on
    accumulated litter rather than on anything real.

    Soft-delete, through the same endpoint a user would call — `CLAUDE.md`
    §3.4 keeps deletes recoverable for 30 days, and a test is not a reason to
    reach past that into the table.
    """
    try:
        httpx.get(f"{BASE_URL}/health", timeout=3).raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"No WebMap API at {BASE_URL} ({type(exc).__name__}). Start it with: "
            f"docker compose -f infra/compose.yaml up -d"
        )
    yield BASE_URL
    purge_run_artefacts(BASE_URL, RUN)


def purge_run_artefacts(api: str, token: str) -> None:
    """Soft-delete every dataset this run named after itself.

    Best-effort: a teardown that raises would turn a passing run red for
    housekeeping, and the litter is visible in the next run's listing anyway.
    """
    try:
        headers = bearer(api, "grace")
        response = httpx.get(
            f"{api}/api/v1/datasets", params={"limit": 100}, headers=headers, timeout=30
        )
        response.raise_for_status()
        for item in response.json().get("items", []):
            if token in str(item.get("name", "")):
                httpx.delete(f"{api}/api/v1/datasets/{item['id']}", headers=headers, timeout=30)
    except Exception as exc:
        warnings.warn(
            f"Could not clean up datasets tagged {token}: {type(exc).__name__}: {exc}",
            stacklevel=2,
        )


def bearer(api: str, user: str) -> dict[str, str]:
    response = httpx.post(f"{api}/auth/dev/token", params={"user": user}, timeout=30)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="module")
def headers(api: str) -> dict[str, str]:
    return bearer(api, "grace")


@pytest.fixture(scope="module")
def pointset(api: str, headers: dict[str, str]) -> dict[str, Any]:
    response = httpx.get(
        f"{api}/api/v1/datasets", params={"kind": "pointset"}, headers=headers, timeout=30
    )
    response.raise_for_status()
    items = response.json()["items"]
    if not items:
        pytest.skip("No seeded pointset. Run: uv run python scripts/seed.py")
    return dict(items[0])


def value_column(api: str, headers: dict[str, str], dataset_id: str) -> str:
    """A numeric attribute of the seeded layer, read from the layer itself."""
    response = httpx.get(
        f"{api}/api/v1/features/{dataset_id}/attributes",
        params={"limit": 1},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    items = response.json()["items"]
    if not items:
        pytest.skip("The seeded pointset has no attributes to grid.")

    numeric = [
        name
        for name, value in items[0].items()
        if name != "id" and isinstance(value, int | float) and not isinstance(value, bool)
    ]
    if not numeric:
        pytest.skip("The seeded pointset has no numeric attribute to grid.")
    return str(numeric[0])


def await_job(api: str, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    """Poll to a terminal state, following the server's own interval hint.

    Using `poll_after_seconds` rather than a constant is the point: if the
    hint were wrong or missing, this would either hammer the API or hang, and
    the test would say so.
    """
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = httpx.get(f"{api}/api/v1/jobs/{job_id}", headers=headers, timeout=30)
        response.raise_for_status()
        job = response.json()
        if job["state"] in ("succeeded", "failed", "cancelled"):
            return dict(job)
        time.sleep(max(1, int(job.get("poll_after_seconds") or 3)))

    pytest.fail(
        f"Job {job_id} did not finish within {JOB_TIMEOUT_SECONDS}s. The worker "
        f"may not be running: docker compose -f infra/compose.yaml up -d worker"
    )


def submit_grid(
    api: str, headers: dict[str, str], dataset_id: str, column: str, **extra: Any
) -> httpx.Response:
    return httpx.post(
        f"{api}/api/v1/jobs/interpolate",
        json={"dataset_id": dataset_id, "value_column": column, **extra},
        headers=headers,
        timeout=30,
    )


# --- the whole path -----------------------------------------------------------


def test_a_submitted_grid_runs_in_the_worker_and_registers_a_layer(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """**The path a geologist takes.** Submit, poll, open the layer.

    Nothing here is stubbed: the request goes over HTTP, the task crosses
    Redis into the worker container, and the dataset it registers is fetched
    back through the same API.
    """
    column = value_column(api, headers, pointset["id"])

    accepted = submit_grid(
        api,
        headers,
        pointset["id"],
        column,
        method="minimum_curvature",
        cell_size=1000,
        output_name=f"End to end {RUN}",
    )
    assert accepted.status_code == 202, accepted.text
    submitted = accepted.json()
    assert submitted["already_running"] is False

    job = await_job(api, headers, submitted["job_id"])
    assert job["state"] == "succeeded", job.get("error")
    assert job["progress"] == 1.0

    dataset_id = job["result"]["dataset_id"]
    layer = httpx.get(f"{api}/api/v1/datasets/{dataset_id}", headers=headers, timeout=30)
    layer.raise_for_status()
    assert layer.json()["kind"] == "grid"


def test_the_job_reports_progress_with_a_sentence(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """`10` §4. A frozen bar is indistinguishable from a hung worker, and
    "Solving" is what tells someone the setup finished."""
    column = value_column(api, headers, pointset["id"])

    submitted = submit_grid(
        api, headers, pointset["id"], column, cell_size=500, output_name=f"Progress probe {RUN}"
    ).json()

    seen: list[str] = []
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = httpx.get(
            f"{api}/api/v1/jobs/{submitted['job_id']}", headers=headers, timeout=30
        ).json()
        if job.get("progress_message"):
            seen.append(job["progress_message"])
        if job["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.5)

    assert seen, "the job never reported a progress message"
    assert any(message[0].isupper() for message in seen), (
        "progress messages are sentences a person reads, not codes"
    )


def test_contouring_the_grid_a_job_just_made(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """The two jobs compose over HTTP as well as in process."""
    column = value_column(api, headers, pointset["id"])

    grid_job = await_job(
        api,
        headers,
        submit_grid(
            api,
            headers,
            pointset["id"],
            column,
            cell_size=1000,
            output_name=f"Contour source {RUN}",
        ).json()["job_id"],
    )
    assert grid_job["state"] == "succeeded", grid_job.get("error")

    accepted = httpx.post(
        f"{api}/api/v1/jobs/contour",
        json={"dataset_id": grid_job["result"]["dataset_id"]},
        headers=headers,
        timeout=30,
    )
    assert accepted.status_code == 202, accepted.text

    contour_job = await_job(api, headers, accepted.json()["job_id"])
    assert contour_job["state"] == "succeeded", contour_job.get("error")
    assert contour_job["result"]["feature_count"] > 0
    assert contour_job["result"]["interval"] > 0


# --- what a poll tells the caller ---------------------------------------------


def test_a_poll_carries_its_own_backoff_hint(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """So Claude's polling and the SPA's agree rather than drifting apart, and
    neither has to invent an interval."""
    column = value_column(api, headers, pointset["id"])
    submitted = submit_grid(
        api, headers, pointset["id"], column, cell_size=1000, output_name=f"Backoff probe {RUN}"
    ).json()

    assert submitted["poll_after_seconds"] > 0

    finished = await_job(api, headers, submitted["job_id"])
    assert finished["poll_after_seconds"] == 0, "a finished job must not ask to be polled again"


def test_an_estimate_is_withheld_until_progress_is_meaningful(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """Dividing elapsed time by 2% progress produces a confident-looking hour.
    Silence is more useful than that."""
    column = value_column(api, headers, pointset["id"])
    submitted = submit_grid(
        api,
        headers,
        pointset["id"],
        column,
        cell_size=1000,
        output_name=f"Estimate probe {RUN}",
    ).json()

    queued = httpx.get(
        f"{api}/api/v1/jobs/{submitted['job_id']}", headers=headers, timeout=30
    ).json()
    if queued["state"] == "queued":
        assert queued["estimated_remaining_seconds"] is None

    await_job(api, headers, submitted["job_id"])


# --- idempotency and cancellation ----------------------------------------------


def test_an_identical_resubmission_says_it_is_already_running(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """**The retry case, over HTTP.** Without this a timed-out tool call that
    is retried produces two grids and a conversation that cannot say which is
    which."""
    column = value_column(api, headers, pointset["id"])
    body = {
        "dataset_id": pointset["id"],
        "value_column": column,
        "cell_size": 1500,
        "output_name": f"Idempotency probe {RUN}",
    }

    first = httpx.post(
        f"{api}/api/v1/jobs/interpolate", json=body, headers=headers, timeout=30
    ).json()
    second = httpx.post(
        f"{api}/api/v1/jobs/interpolate", json=body, headers=headers, timeout=30
    ).json()

    assert second["job_id"] == first["job_id"]
    assert second["already_running"] is True

    await_job(api, headers, first["job_id"])


def test_cancelling_says_whether_it_stopped_or_was_asked_to(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """`10` §9. A 200 with no message would leave a caller watching a job run
    for another ten seconds and concluding cancellation did not work."""
    column = value_column(api, headers, pointset["id"])
    submitted = submit_grid(
        api, headers, pointset["id"], column, cell_size=250, output_name=f"Cancel probe {RUN}"
    ).json()

    response = httpx.post(
        f"{api}/api/v1/jobs/{submitted['job_id']}/cancel", headers=headers, timeout=30
    )
    response.raise_for_status()
    message = response.json()["message"]

    assert "cancel" in message.lower()
    job = await_job(api, headers, submitted["job_id"])
    assert job["state"] in ("cancelled", "succeeded"), (
        "a cancel that landed after the job finished is a race, not a failure"
    )


# --- identity ------------------------------------------------------------------


def test_a_job_is_not_visible_to_anyone_else(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """A job's parameters name the dataset and the method, which is what
    somebody is working on."""
    column = value_column(api, headers, pointset["id"])
    submitted = submit_grid(
        api, headers, pointset["id"], column, cell_size=1000, output_name=f"Privacy probe {RUN}"
    ).json()

    other = bearer(api, "alan")
    response = httpx.get(f"{api}/api/v1/jobs/{submitted['job_id']}", headers=other, timeout=30)

    assert response.status_code == 403, response.text

    await_job(api, headers, submitted["job_id"])


def test_submitting_without_a_token_is_refused(api: str, pointset: dict[str, Any]) -> None:
    response = httpx.post(
        f"{api}/api/v1/jobs/interpolate",
        json={"dataset_id": pointset["id"], "value_column": "x"},
        timeout=30,
    )

    assert response.status_code == 401


def test_a_listing_shows_only_your_own_jobs(api: str, headers: dict[str, str]) -> None:
    response = httpx.get(f"{api}/api/v1/jobs", headers=headers, timeout=30)
    response.raise_for_status()

    other = httpx.get(f"{api}/api/v1/jobs", headers=bearer(api, "alan"), timeout=30).json()
    mine = {job["id"] for job in response.json()}

    assert not mine & {job["id"] for job in other}


# --- refusals ------------------------------------------------------------------


def test_an_unknown_value_column_is_refused_by_the_job_not_the_route(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """The route cannot know a layer's columns without reading the object, so
    this fails in the worker — and the failure has to come back through the
    poll with a message naming what would have worked."""
    submitted = submit_grid(
        api, headers, pointset["id"], "no_such_column", output_name=f"Bad column {RUN}"
    ).json()

    job = await_job(api, headers, submitted["job_id"])

    assert job["state"] == "failed"
    assert job["error_kind"] == "input", "a bad column must never be retried"
    assert "not an attribute" in job["error"]


def test_a_malformed_request_is_refused_at_the_route(
    api: str, headers: dict[str, str], pointset: dict[str, Any]
) -> None:
    """`extra="forbid"` catches a typo in a Claude-supplied parameter loudly
    rather than ignoring it — a silently dropped `cell_size` would grid at the
    default and look like it worked."""
    response = httpx.post(
        f"{api}/api/v1/jobs/interpolate",
        json={
            "dataset_id": pointset["id"],
            "value_column": "x",
            "cellsize": 500,  # the typo
        },
        headers=headers,
        timeout=30,
    )

    assert response.status_code == 422
    assert "cellsize" in response.text
