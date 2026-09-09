"""Coordinate transformation. The only module permitted to import pyproj.

`webmap_core.crs.CrsContext` is a thin wrapper over this module, not a second
implementation — see `adr/0003-geoprocessing-owns-crs.md` for why the
dependency points this way round.

Owning the capability is not licence to use it freely. `CLAUDE.md` §3.1
rule 3 is unchanged: reprojection happens at defined boundaries only, never
mid-algorithm. Inside this package there are exactly two legitimate callers
(`05-geoprocessing.md` §2.2), and both run before any solver.
"""

from functools import lru_cache

import numpy as np
from numpy.typing import NDArray
from pyproj import CRS, Transformer

from webmap_geo.exceptions import NotProjected
from webmap_geo.frame import AnalysisFrame, LengthUnit

# Re-exported so `webmap_core.crs` can name the type it returns without
# importing pyproj itself (adr/0003-geoprocessing-owns-crs.md).
__all__ = [
    "WEB_MERCATOR",
    "WGS84",
    "Transformer",
    "axis_units",
    "crs_definition",
    "crs_of",
    "epsg_from_user_input",
    "frame_for",
    "is_geographic",
    "transform_bbox",
    "transform_points",
    "transformer",
]

WGS84 = 4326
WEB_MERCATOR = 3857

# pyproj reports axis units by name. Map the spellings that actually occur to
# the three units the data model admits (`length_unit_t` in 02 §3.1). US
# survey feet and international feet differ by 2 ppm — about 0.6 m across the
# width of Texas — so collapsing them into one "ft" would put a State Plane
# grid metres away from its wells.
_UNIT_NAMES: dict[str, LengthUnit] = {
    "metre": "m",
    "meter": "m",
    "m": "m",
    "foot": "ft",
    "international foot": "ft",
    "ft": "ft",
    "us survey foot": "usft",
    "usfoot": "usft",
    "us_survey_foot": "usft",
}


@lru_cache(maxsize=256)
def crs_of(srid: int) -> CRS:
    return CRS.from_epsg(srid)


@lru_cache(maxsize=256)
def transformer(src: int, dst: int) -> Transformer:
    """A cached, always-xy transformer.

    `always_xy=True` is not optional. EPSG defines EPSG:4326 as
    (latitude, longitude); every file format and every map API in this system
    uses (longitude, latitude). Omitting the flag swaps them silently, and the
    result is data 300 km from where it belongs with no error anywhere.
    """
    return Transformer.from_crs(crs_of(src), crs_of(dst), always_xy=True)


def is_geographic(srid: int) -> bool:
    return bool(crs_of(srid).is_geographic)


def crs_definition(srid: int) -> str:
    """The CRS as WKT, for a client that must reproject.

    Exists for one caller: the browser's status-bar cursor readout, which shows
    the pointer position in the project's analysis CRS. Serving the definition
    rather than letting the client look the code up in its own EPSG table keeps
    one source of truth for what a CRS means — two tables eventually disagree
    about a datum shift, and the disagreement would stay invisible until
    someone compared a readout against a well file.

    **WKT rather than a PROJ string.** `to_proj4()` warns that it loses
    projection information, and it is right to: the PROJ4 form cannot carry a
    datum's full definition, which for NAD83 against WGS84 is a shift of about
    a metre. A metre does not matter for a cursor readout, but shipping a
    lossy definition when a lossless one parses just as well would be choosing
    to be wrong for no reason. proj4js reads WKT.

    This is not a licence to reproject geometry client-side. Stored geometry is
    reprojected at defined boundaries, server-side
    (`adr/0003-geoprocessing-owns-crs.md`); a cursor position has no lineage
    and no stored consequence.
    """
    return str(crs_of(srid).to_wkt())


def axis_units(srid: int) -> LengthUnit:
    """The horizontal linear unit of a projected CRS.

    Raises NotProjected for a geographic CRS — degrees are not a length, and a
    caller asking this question is about to do arithmetic that requires one.
    """
    crs = crs_of(srid)
    if crs.is_geographic:
        raise NotProjected(
            f"EPSG:{srid} is geographic, so it has no linear axis unit. "
            f"Distances and areas in degrees are not meaningful. Choose a UTM "
            f"zone or State Plane zone appropriate to the data extent."
        )
    name = crs.axis_info[0].unit_name.strip().lower()
    unit = _UNIT_NAMES.get(name)
    if unit is None:
        raise NotProjected(
            f"EPSG:{srid} uses axis unit '{crs.axis_info[0].unit_name}', which "
            f"WebMap does not model. Supported horizontal units are metres, "
            f"international feet, and US survey feet."
        )
    return unit


def frame_for(srid: int) -> AnalysisFrame:
    """Build an AnalysisFrame, validating that the CRS is projected."""
    return AnalysisFrame(srid=srid, units=axis_units(srid))


def transform_points(
    x: NDArray[np.float64], y: NDArray[np.float64], src: int, dst: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Transform coordinate arrays between CRSs.

    A no-op when src == dst, rather than a round-trip through PROJ that would
    introduce sub-millimetre drift into data that never needed to move.
    """
    if src == dst:
        return x, y
    tx, ty = transformer(src, dst).transform(x, y)
    return np.asarray(tx, dtype=np.float64), np.asarray(ty, dtype=np.float64)


def transform_bbox(
    bbox: tuple[float, float, float, float], src: int, dst: int, *, densify: int = 21
) -> tuple[float, float, float, float]:
    """Transform [west, south, east, north] to a bound in the target CRS.

    Samples `densify` points along each edge rather than transforming the four
    corners. `06-rendering.md` §7 argues corners are exact because the
    projections in scope are separable and monotonic — true for Web Mercator
    against State Plane, but this function is reached with whatever
    `storage_srid` a user registered, and the tile-pruning predicate built
    from its result is only correct if the bound actually contains the region.
    An oblique or conic projection curves the edges outward; four corners
    under-cover it and features vanish from tiles with nothing logged.

    Densifying costs about 80 extra point transforms per tile request against
    a cached transformer. That is not the bottleneck, and the failure it
    prevents is invisible.
    """
    west, south, east, north = bbox
    if src == dst:
        return bbox

    t = np.linspace(0.0, 1.0, densify)
    horizontal = west + (east - west) * t
    vertical = south + (north - south) * t
    xs = np.concatenate(
        [horizontal, horizontal, np.full(densify, west), np.full(densify, east)]
    )
    ys = np.concatenate([np.full(densify, south), np.full(densify, north), vertical, vertical])
    px, py = transform_points(xs, ys, src, dst)
    finite = np.isfinite(px) & np.isfinite(py)
    if not finite.any():
        raise NotProjected(
            f"No point of bbox {bbox} in EPSG:{src} is representable in "
            f"EPSG:{dst}. The two CRSs likely cover disjoint regions — check "
            f"the dataset's storage CRS against its coordinates."
        )
    return (
        float(px[finite].min()),
        float(py[finite].min()),
        float(px[finite].max()),
        float(py[finite].max()),
    )


def epsg_from_user_input(description: str, *, min_confidence: int = 70) -> int | None:
    """Identify a CRS description as an EPSG code, or return None.

    Takes WKT, PROJ strings, or an "EPSG:xxxx" token — whatever a file's
    `.prj` or embedded metadata happens to hold. Lives here rather than in
    `webmap_io` because it is a pyproj call, and adr/0003 puts all of those in
    this module.

    `min_confidence` is the important argument. `to_epsg()` defaults to a
    permissive match and will happily return a code for a projection that is
    merely *similar* — a near-miss datum that shifts everything a few hundred
    metres and raises nothing. Refusing to identify is the safe answer,
    because the caller's response is to ask the user rather than to guess.
    """
    try:
        crs = CRS.from_user_input(description.strip())
    except Exception:
        return None
    epsg = crs.to_epsg(min_confidence=min_confidence)
    return int(epsg) if epsg is not None else None
