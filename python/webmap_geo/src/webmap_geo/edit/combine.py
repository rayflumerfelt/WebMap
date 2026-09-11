"""Combine, explode and dissolve. `09-editing.md` §11.4.

Three operations that look similar and are not, which is why §9.1 forbids
calling any two of them "Merge":

- **Combine** wraps features into one multi-part feature. The geometry does not
  change at all — two leases that did not touch still do not touch, they are
  simply one record now.
- **Explode** is its inverse: one multi-part feature becomes several.
- **Dissolve** is a true union. Interior boundaries *go away*, and two leases
  that shared an edge become one polygon with no edge between them.

**Dissolve must state what happens to the attributes**, and §11.4 says leaving
it unspecified is silent data loss. It is not a hypothetical: dissolving five
leases with different operators and summing their acreage gives one polygon
whose `operator` is whatever the first row happened to be, and nothing on the
map says so. So the policy is an argument, it has no default that guesses, and
the caller is expected to have asked.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

import shapely
from shapely.geometry.base import BaseGeometry

from webmap_geo.exceptions import DegenerateInput


def _wrap(kind: str, parts: list[Any]) -> BaseGeometry:
    """The multi-part geometry holding these single parts.

    A function rather than a lookup table because each constructor has its own
    signature to mypy, and a dict of them types as the intersection of three
    unrelated ones.
    """
    if kind == "Point":
        return shapely.MultiPoint(parts)
    if kind == "LineString":
        return shapely.MultiLineString(parts)
    return shapely.MultiPolygon(parts)


class AttributePolicy(StrEnum):
    """What happens to the attributes of the features being dissolved.

    `PROMPT` is the default in the UI and is not a value this module accepts:
    by the time geometry is being unioned somebody has answered the question.
    """

    #: Numerics summed, text from the largest-area feature.
    SUM_NUMERIC = "sum_numeric"
    #: Everything from the largest-area feature.
    LARGEST = "largest"
    #: Only the fields that agree across every input; the rest are dropped.
    COMMON = "common"


def combine(geometries: list[BaseGeometry]) -> BaseGeometry:
    """Wrap several features into one multi-part feature.

    The geometry is untouched — this changes how many records there are, not
    where anything is. Mixed types are refused: a multipolygon holding a line
    is not a thing, and the alternative is a GeometryCollection, which most of
    the pipeline downstream cannot draw or export.
    """
    if len(geometries) < 2:
        raise DegenerateInput(
            "Combine needs at least two features. Select the ones you want as a "
            "single record and try again."
        )

    kinds = {_single_kind(geometry) for geometry in geometries}
    if len(kinds) > 1:
        raise DegenerateInput(
            f"These features are of different kinds ({', '.join(sorted(kinds))}), "
            f"and a multi-part feature holds one kind. Combine the polygons and "
            f"the lines separately."
        )

    parts: list[BaseGeometry] = []
    for geometry in geometries:
        parts.extend(_parts(geometry))
    return _wrap(next(iter(kinds)), parts)


def explode(geometry: BaseGeometry) -> list[BaseGeometry]:
    """One multi-part feature into its parts.

    A single-part geometry comes back as a list of one rather than raising:
    exploding a mixed selection is ordinary, and refusing the whole operation
    because one feature was already single-part would be the software arguing
    with a reasonable request.
    """
    return _parts(geometry)


def dissolve(
    geometries: list[BaseGeometry],
    props: list[dict[str, Any]],
    policy: AttributePolicy,
) -> tuple[BaseGeometry, dict[str, Any]]:
    """Union several polygons, and resolve their attributes explicitly.

    The union is the easy half. The attributes are where the data is lost, and
    the policy argument is what makes the loss a decision somebody made.
    """
    if len(geometries) < 2:
        raise DegenerateInput(
            "Dissolve needs at least two features. With one there is nothing to "
            "remove a boundary between."
        )
    if any(geometry.geom_type not in {"Polygon", "MultiPolygon"} for geometry in geometries):
        raise DegenerateInput(
            "Dissolve unions polygons: it removes the boundary between areas. "
            "For lines, Combine makes one multi-part feature without changing "
            "the geometry."
        )

    united = shapely.union_all(geometries)
    return united, resolve_attributes(geometries, props, policy)


def resolve_attributes(
    geometries: list[BaseGeometry],
    props: list[dict[str, Any]],
    policy: AttributePolicy,
) -> dict[str, Any]:
    """Attributes for a dissolved feature, by the stated policy.

    Separate from `dissolve` because the UI shows this as a preview before the
    union runs — §11.4 asks for one, and a preview computed by different code
    than the result is a preview that can lie.
    """
    if len(props) != len(geometries):
        raise DegenerateInput(
            f"{len(geometries)} geometries and {len(props)} attribute rows — the "
            f"two must line up, or the values land on the wrong feature."
        )

    if policy is AttributePolicy.LARGEST:
        return dict(props[_largest(geometries)])

    if policy is AttributePolicy.COMMON:
        shared = {}
        for key in props[0]:
            values = [row.get(key) for row in props]
            if all(value == values[0] for value in values):
                shared[key] = values[0]
        return shared

    # SUM_NUMERIC: sums what can be summed, and takes the rest from the largest
    # feature — because "the biggest one's operator" is at least a defensible
    # answer, where "the first one's" is an accident of selection order.
    resolved = dict(props[_largest(geometries)])
    for key in props[0]:
        values = [row.get(key) for row in props]
        numbers = [
            value
            for value in values
            if isinstance(value, int | float) and not isinstance(value, bool)
        ]
        if len(numbers) == len(values):
            resolved[key] = sum(numbers)
    return resolved


def _largest(geometries: list[BaseGeometry]) -> int:
    areas = [geometry.area for geometry in geometries]
    return max(range(len(areas)), key=lambda index: areas[index])


def _single_kind(geometry: BaseGeometry) -> str:
    kind = geometry.geom_type
    return kind.removeprefix("Multi") if kind.startswith("Multi") else kind


def _parts(geometry: BaseGeometry) -> list[BaseGeometry]:
    parts = getattr(geometry, "geoms", None)
    if parts is None:
        return [geometry]
    return [part for part in parts if not part.is_empty]


def attribute_preview(
    props: list[dict[str, Any]], geometries: list[BaseGeometry]
) -> dict[str, dict[str, Any]]:
    """Every policy's answer, for the dialog that asks which one.

    All three at once, because the question "what happens to my attributes" is
    answered by seeing the three results side by side and not by reading three
    descriptions of them.
    """
    return {
        policy.value: resolve_attributes(geometries, props, policy)
        for policy in AttributePolicy
    }


#: For the callers that want to name the operation rather than the function.
OPERATIONS: dict[str, Callable[..., Any]] = {
    "combine": combine,
    "explode": explode,
    "dissolve": dissolve,
}


__all__ = [
    "OPERATIONS",
    "AttributePolicy",
    "attribute_preview",
    "combine",
    "dissolve",
    "explode",
    "resolve_attributes",
]
