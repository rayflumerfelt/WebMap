"""Session document validation. `02-data-model.md` §3.8.

Unit tests for the pure half — layer and view normalisation, short codes, and
the document schema seam. The permission behaviour that matters most (a
session crossing teams drops the layers its reader cannot see) needs a
database and lives in `tests/test_session_endpoints.py`.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import pytest

from webmap_core.exceptions import LimitExceeded, SessionError
from webmap_core.services.sessions import (
    MAX_LAYERS,
    SCHEMA_VERSION,
    _migrate_document,
    generate_short_code,
    normalise_layers,
    normalise_view,
)

MIDLAND = {"center": [-102.08, 31.99], "zoom": 9.5}


def layer(**overrides: Any) -> dict[str, Any]:
    return {"dataset_id": str(uuid4()), **overrides}


# --- layers -----------------------------------------------------------------


def test_draw_order_comes_from_list_position_when_z_is_absent() -> None:
    """Claude passes a bare list and means its order; the SPA passes explicit
    z after a drag. Both have to work."""
    first, second, third = layer(), layer(), layer()

    normalised = normalise_layers([first, second, third])

    assert [entry["z"] for entry in normalised] == [0, 1, 2]
    assert [entry["dataset_id"] for entry in normalised] == [
        first["dataset_id"],
        second["dataset_id"],
        third["dataset_id"],
    ]


def test_explicit_z_wins_over_list_position() -> None:
    bottom, top = layer(z=10), layer(z=1)

    normalised = normalise_layers([bottom, top])

    assert [entry["dataset_id"] for entry in normalised] == [
        top["dataset_id"],
        bottom["dataset_id"],
    ]


def test_an_explicit_null_z_means_list_order_not_an_error() -> None:
    """The shape every request from the API actually arrives in.

    A Pydantic model with an unset optional field serialises it as an explicit
    null, and `dict.get(key, default)` does not fall back for a key that is
    present and null. Found by the endpoint tests, so it is asserted here where
    it is cheap to run.
    """
    first, second = layer(z=None), layer(z=None)

    normalised = normalise_layers([first, second])

    assert [entry["z"] for entry in normalised] == [0, 1]
    assert [entry["dataset_id"] for entry in normalised] == [
        first["dataset_id"],
        second["dataset_id"],
    ]


def test_an_explicit_null_visible_keeps_the_layer_shown() -> None:
    """Same trap, opposite consequence: a null read as false would open every
    session with all its layers switched off."""
    (entry,) = normalise_layers([layer(visible=None)])

    assert entry["visible"] is True


def test_ordering_is_total_so_two_saves_of_one_map_match() -> None:
    """Ties on z break by dataset id rather than by input order.

    Without that, saving the same map twice could store two different layer
    orders, and a diff of two session documents would show a change that is
    not one.
    """
    a, b, c = layer(z=1), layer(z=1), layer(z=1)

    assert normalise_layers([a, b, c]) == normalise_layers([c, a, b])


def test_a_layer_with_no_dataset_id_says_a_session_stores_references() -> None:
    """The likeliest mistake is passing the data, so the message names the
    rule rather than just the missing field."""
    with pytest.raises(SessionError, match="never copies of their data"):
        normalise_layers([{"opacity": 0.5}])


def test_a_dataset_name_in_place_of_an_id_names_the_tool_that_returns_ids() -> None:
    with pytest.raises(SessionError, match="webmap_search_datasets"):
        normalise_layers([{"dataset_id": "Wolfcamp A Structure"}])


def test_the_same_dataset_twice_is_refused_rather_than_drawn_twice() -> None:
    """Legitimate occasionally — a fault set as lines and again as labels —
    but far more often a duplicated paste, which renders as a layer that looks
    subtly wrong rather than as an error."""
    duplicate = str(uuid4())

    with pytest.raises(SessionError, match="appears twice"):
        normalise_layers([{"dataset_id": duplicate}, {"dataset_id": duplicate}])


def test_opacity_as_a_percentage_is_refused_with_the_conversion() -> None:
    """60 rather than 0.6 is the mistake; the message does the arithmetic."""
    with pytest.raises(SessionError, match=re.escape("60% is 0.6")):
        normalise_layers([layer(opacity=60)])


def test_a_non_numeric_opacity_is_refused() -> None:
    with pytest.raises(SessionError, match="not a number"):
        normalise_layers([layer(opacity="mostly")])


def test_too_many_layers_is_refused_before_anything_is_written() -> None:
    with pytest.raises(LimitExceeded, match=f"at most {MAX_LAYERS}"):
        normalise_layers([layer() for _ in range(MAX_LAYERS + 1)])


def test_defaults_are_filled_so_the_stored_document_is_complete() -> None:
    """The browser reads this document directly. A missing `visible` would be
    a falsy undefined in JavaScript, which is the opposite of the default."""
    (entry,) = normalise_layers([layer()])

    assert entry["visible"] is True
    assert entry["opacity"] == 1.0
    assert entry["style_template_id"] is None
    assert entry["symbology_override"] is None


# --- view -------------------------------------------------------------------


def test_a_camera_view_round_trips() -> None:
    assert normalise_view(MIDLAND) == {"center": [-102.08, 31.99], "zoom": 9.5}


def test_a_bbox_view_is_kept_as_a_bbox() -> None:
    """Not converted to a centre and zoom: that needs the viewport aspect
    ratio, which the browser has and the server does not."""
    assert normalise_view({"bbox": [-103.0, 31.0, -101.0, 33.0]}) == {
        "bbox": [-103.0, 31.0, -101.0, 33.0]
    }


def test_swapped_centre_coordinates_are_refused() -> None:
    """[31.99, -102.08] puts a Midland Basin session at 31°N 102°E, in the
    Gobi — which renders happily and is wrong."""
    with pytest.raises(SessionError, match="longitude, latitude"):
        normalise_view({"center": [31.99, -102.08], "zoom": 9})


def test_an_inverted_bbox_is_refused_rather_than_silently_empty() -> None:
    with pytest.raises(SessionError, match="empty or inverted"):
        normalise_view({"bbox": [-101.0, 31.0, -103.0, 33.0]})


def test_a_session_with_no_view_says_the_map_has_nowhere_to_open() -> None:
    with pytest.raises(SessionError, match="nowhere"):
        normalise_view(None)


def test_a_view_missing_zoom_lists_what_it_needs() -> None:
    with pytest.raises(SessionError, match="either 'bbox' or both"):
        normalise_view({"center": [-102.0, 32.0]})


def test_zoom_and_pitch_are_clamped_to_what_maplibre_accepts() -> None:
    """A stored zoom of 40 is a bug elsewhere, but refusing to open the
    session over it would strand the user with a link that never works."""
    view = normalise_view({"center": [-102.0, 32.0], "zoom": 40, "pitch": 120, "bearing": 450})

    assert view["zoom"] == 24.0
    assert view["pitch"] == 85.0
    assert view["bearing"] == 90.0


# --- short codes ------------------------------------------------------------


def test_short_codes_avoid_ambiguous_characters() -> None:
    """Read aloud in a meeting and typed by hand. 0/O and 1/l/I are the pairs
    that cost someone five minutes."""
    codes = "".join(generate_short_code() for _ in range(500))

    assert not set(codes) & set("01loi")
    assert codes.islower()


def test_short_codes_are_not_sequential() -> None:
    """A locator rather than a credential, but a guessable one would let
    anyone enumerate every session and learn which they happen to have access
    to — which is a map of the organisation."""
    codes = {generate_short_code() for _ in range(2_000)}

    assert len(codes) > 1_990, "short codes collide far more than chance allows"


# --- document schema --------------------------------------------------------


def test_a_document_from_a_newer_server_is_refused_with_a_next_step() -> None:
    """A session link outlives a deploy. Reading a newer document with older
    code would drop the fields it does not know about and then save that
    truncation back."""
    with pytest.raises(SessionError, match="Reload the page"):
        _migrate_document(
            {
                "short_code": "k3n8fq",
                "schema_version": SCHEMA_VERSION + 1,
                "layers": [],
                "view": {},
            }
        )


def test_a_document_stored_as_json_text_is_parsed() -> None:
    """asyncpg returns jsonb as a string on some paths and as parsed objects
    on others. Both reach this function."""
    document = _migrate_document(
        {
            "short_code": "k3n8fq",
            "schema_version": 1,
            "layers": '[{"dataset_id": "x", "z": 0}]',
            "view": '{"center": [-102.0, 32.0], "zoom": 9}',
        }
    )

    assert document["layers"] == [{"dataset_id": "x", "z": 0}]
    assert document["view"]["zoom"] == 9
