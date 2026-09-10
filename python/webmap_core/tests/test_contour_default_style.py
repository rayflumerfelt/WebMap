"""What a contour layer draws with no stored styling. `adr/0015`.

A contour dataset holds lines and label points together, so it is the first
dataset in the system whose default styling is **two** symbologies. The rule
worth pinning down is the one a `geometry_kind` check gets wrong: `mixed` says
only that a dataset holds more than one geometry type, and defaulting a mixed
dataset to points would draw a structure map as a field of dots.
"""

from __future__ import annotations

from typing import Any

from webmap_core.services.style_builder import _default_symbologies

CONTOUR_SCHEMA = [
    {"name": "value", "type": "double"},
    {"name": "is_index", "type": "boolean"},
    {"name": "closed", "type": "boolean"},
    {"name": "kind", "type": "text"},
    {"name": "bearing", "type": "double"},
    {"name": "label", "type": "text"},
]


def contours() -> dict[str, Any]:
    return {
        "name": "Wolfcamp A contours",
        "geometry_kind": "mixed",
        "attribute_schema": CONTOUR_SCHEMA,
    }


def geometries(symbologies: list[dict[str, Any]]) -> list[str]:
    return [str(entry["symbol"]["geometry"]) for entry in symbologies]


class TestContourDefaults:
    def test_a_contour_set_draws_lines_and_labels(self) -> None:
        """One symbology describes one geometry, and a contour set that drew
        its lines and not its labels would be a map with gaps in it for no
        visible reason."""
        assert geometries(_default_symbologies(contours())) == ["line", "label"]

    def test_the_label_is_point_placed_and_rotated(self) -> None:
        """The anchor is ours, at the middle of a gap cut into the line, so
        MapLibre is not choosing a vertex — which is what `08`'s line-center
        rule exists to prevent."""
        label = _default_symbologies(contours())[1]["symbol"]

        assert label["placement"] == "point"
        assert label["rotateField"] == "bearing"
        # Null on every line piece and on every unlabelled intermediate
        # contour, and MapLibre places no symbol for an empty text-field —
        # which is what keeps this layer to the label points without a filter.
        assert label["field"] == "label"

    def test_a_mixed_dataset_that_is_not_contours_is_unaffected(self) -> None:
        """`mixed` says only that a dataset holds more than one geometry type.
        The three columns together are what a labelled contour set has."""
        other = {"geometry_kind": "mixed", "attribute_schema": [{"name": "value"}]}

        assert geometries(_default_symbologies(other)) == ["point"]

    def test_an_ordinary_line_layer_is_still_one_symbology(self) -> None:
        faults = {"geometry_kind": "linestring", "attribute_schema": []}

        assert geometries(_default_symbologies(faults)) == ["line"]
