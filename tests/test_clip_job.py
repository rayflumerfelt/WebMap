"""The grid-clipping job's contract. `08` §5.2.

`python/webmap_geo/tests/test_clip.py` covers the arithmetic. This covers the
parts only the orchestration can get wrong: the request that cannot express an
ambiguous clip, the round trip through a job payload, and the wiring that makes
the job reachable at all.

No database and no object store — these are the checks that should fail fast,
in every suite, rather than at the end of a two-minute job.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from webmap_core.permissions import Visibility
from webmap_core.services.clipping import ClipRequest


def a_request(**overrides: object) -> ClipRequest:
    fields: dict[str, object] = {
        "dataset_id": uuid4(),
        "boundary_dataset_id": uuid4(),
    }
    fields.update(overrides)
    return ClipRequest(**fields)  # type: ignore[arg-type]


def test_a_clip_needs_exactly_one_boundary() -> None:
    """**Refused rather than resolved by precedence.** Which one the map was
    cut to is not recoverable from the result, so it cannot be guessed at
    submission time either."""
    grid = uuid4()

    with pytest.raises(ValueError, match="exactly one boundary"):
        ClipRequest(dataset_id=grid)

    with pytest.raises(ValueError, match="exactly one boundary"):
        ClipRequest(
            dataset_id=grid,
            boundary_dataset_id=uuid4(),
            to_control="convex_hull",
            control_dataset_id=uuid4(),
        )


def test_clipping_to_the_control_needs_the_control_layer() -> None:
    """Asked for rather than read from the grid's lineage: the control that
    made a grid belongs to the source dataset, and re-reading it through
    lineage would read a dataset the caller may no longer be able to see."""
    with pytest.raises(ValueError, match="control_dataset_id"):
        ClipRequest(dataset_id=uuid4(), to_control="convex_hull")


def test_a_control_clip_with_its_layer_is_accepted() -> None:
    request = ClipRequest(
        dataset_id=uuid4(), to_control="concave_hull", control_dataset_id=uuid4()
    )
    assert request.boundary_dataset_id is None


def test_the_request_survives_a_job_payload() -> None:
    """A job payload is JSON, and every field has to come back as the type the
    task expects — a `feature_ids` that returns as a list would still work and
    a `dataset_id` that returns as a string would not."""
    original = a_request(
        feature_ids=("a", "b"),
        invert=True,
        output_name="Clipped to Section 14",
        project_id=uuid4(),
        visibility=Visibility.TEAM,
        owner_team_id=uuid4(),
    )
    restored = ClipRequest.from_parameters(original.to_parameters())
    assert restored == original


def test_a_control_clip_survives_a_job_payload() -> None:
    original = ClipRequest(
        dataset_id=uuid4(), to_control="radius", control_dataset_id=uuid4()
    )
    assert ClipRequest.from_parameters(original.to_parameters()) == original


def test_the_job_has_progress_phases() -> None:
    """A kind with no phases reports nothing for its whole run, which reads as
    a hang — `ProgressReporter` refuses to construct for one."""
    from webmap_worker.progress import PHASES, total_weight

    assert "clip" in PHASES
    assert total_weight("clip") == pytest.approx(1.0)


def test_the_endpoint_is_registered_and_needs_a_principal() -> None:
    from webmap_api.dependencies import get_principal
    from webmap_api.main import create_app

    from tests.test_route_wiring import _api_routes, _dependency_calls

    routes = [
        route for route in _api_routes(create_app()) if route.path == "/api/v1/jobs/clip"
    ]
    assert routes, "POST /api/v1/jobs/clip is not registered"
    assert get_principal in _dependency_calls(routes[0].dependant)
