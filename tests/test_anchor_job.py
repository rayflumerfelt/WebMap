"""The label-anchor job's contract. `08` §2.4.

`python/webmap_geo/tests/test_label.py` covers where an anchor goes. This
covers what only the job can get wrong: the request round trip, the schema the
anchor layer registers, the caption a geologist reads, and the wiring.

No database and no object store, so these run in every suite.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from webmap_core.permissions import Visibility
from webmap_core.services.aggregation import LayerSource
from webmap_core.services.anchors import (
    AnchorRequest,
    AnchorResult,
    _schema,
    caption,
)


def a_source(name: str = "Leases") -> LayerSource:
    return LayerSource(
        dataset_id=uuid4(),
        parquet_key="features/x/v1.parquet",
        storage_srid=2277,
        name=name,
        feature_count=1000,
        bbox_4326=None,
        project_id=None,
    )


def a_result(**overrides: object) -> AnchorResult:
    fields: dict[str, object] = {
        "geometry": None,
        "props": [
            {"source_feature_id": "1", "anchor_method": "centroid", "clearance": 400.0},
            {"source_feature_id": "2", "anchor_method": "pole", "clearance": 120.0},
        ],
        "n_features": 3,
        "n_anchored": 2,
        "n_centroid": 1,
        "n_pole": 1,
    }
    fields.update(overrides)
    return AnchorResult(**fields)  # type: ignore[arg-type]


def test_the_request_survives_a_job_payload() -> None:
    original = AnchorRequest(
        dataset_id=uuid4(),
        label_columns=("lease_name", "operator"),
        output_name="Lease labels",
        project_id=uuid4(),
        visibility=Visibility.TEAM,
        owner_team_id=uuid4(),
    )
    assert AnchorRequest.from_parameters(original.to_parameters()) == original


def test_label_columns_default_to_none_copied() -> None:
    """The honest default. An anchor layer carrying forty attributes is a
    duplicate of the source that then drifts out of date with it."""
    assert AnchorRequest(dataset_id=uuid4()).label_columns == ()


def test_the_schema_infers_a_copied_column_from_its_values() -> None:
    """Guessing 'text' for a numeric label column makes the styling UI offer
    the wrong controls for it — a colour ramp needs a number."""
    request = AnchorRequest(dataset_id=uuid4(), label_columns=("acres",))
    result = a_result(
        props=[
            {
                "source_feature_id": "1",
                "anchor_method": "centroid",
                "clearance": 1.0,
                "acres": 640.0,
            },
        ]
    )
    schema = {entry["name"]: entry["type"] for entry in _schema(request, result)}
    assert schema["acres"] == "double"
    assert schema["clearance"] == "double"
    assert schema["anchor_method"] == "text"


def test_a_column_null_in_every_feature_is_still_in_the_schema() -> None:
    """Absent, it looks like the request asked for a column that does not
    exist — which is a different problem with a different fix."""
    request = AnchorRequest(dataset_id=uuid4(), label_columns=("operator",))
    result = a_result(
        props=[
            {
                "source_feature_id": "1",
                "anchor_method": "centroid",
                "clearance": 1.0,
                "operator": None,
            }
        ]
    )
    schema = {entry["name"]: entry["type"] for entry in _schema(request, result)}
    assert schema["operator"] == "text"


def test_the_caption_names_the_pole_count_and_the_skips() -> None:
    """The pole count is the interesting number: a layer where most anchors
    fell back to it is a layer of crescents and doughnuts, which is exactly
    where a hand correction gets wanted."""
    line = caption(a_source(), a_result())

    assert "2 label anchors from Leases" in line
    assert "pole of inaccessibility" in line
    assert "1 features had no polygonal area" in line


def test_the_caption_stays_quiet_when_there_is_nothing_to_report() -> None:
    """A caption that always ends in '0 skipped' trains people not to read it."""
    line = caption(a_source(), a_result(n_features=2, n_pole=0, n_centroid=2))
    assert "pole" not in line
    assert "no polygonal area" not in line


def test_the_job_has_progress_phases() -> None:
    from webmap_worker.progress import PHASES, total_weight

    assert "label_anchors" in PHASES
    assert total_weight("label_anchors") == pytest.approx(1.0)


def test_the_endpoint_is_registered_and_needs_a_principal() -> None:
    from tests.test_route_wiring import _api_routes, _dependency_calls
    from webmap_api.dependencies import get_principal
    from webmap_api.main import create_app

    routes = [
        route
        for route in _api_routes(create_app())
        if route.path == "/api/v1/jobs/label-anchors"
    ]
    assert routes, "POST /api/v1/jobs/label-anchors is not registered"
    assert get_principal in _dependency_calls(routes[0].dependant)
