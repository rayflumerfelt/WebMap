"""Experimental variogram estimation. `05-geoprocessing.md` §6.3.

Two decisions here are not optional at this scale, and both are stated in the
spec as requirements rather than optimisations.

**Subsampling is mandatory.** All-pairs on 500,000 points is 1.25e11 distances
— not slow, impossible. Subsampling to 20,000 gives 2e8 pairs, which is a few
seconds and statistically indistinguishable for binned estimation.

**Declustering matters more than people expect.** Well control is clustered by
development history, not by geology: a pad with twelve wells in a quarter
section contributes hundreds of near-zero-lag pairs that say nothing about the
reservoir and everything about drilling economics. Without declustering the
variogram is dominated by them and reports a nugget that is really clustering
— which then flattens the kriged surface everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.variogram.model import anisotropy_transform

#: `05` §6.3. 20,000 points is 2e8 pairs — a few seconds, and enough that the
#: binned estimate is stable.
DEFAULT_SUBSAMPLE = 20_000

#: Below this a lag bin's mean is noise. Bins under it are dropped rather than
#: fitted, because a single stray pair in a far bin can drag a fitted range by
#: a factor of two.
MIN_PAIRS_PER_LAG = 30


@dataclass(frozen=True)
class ExperimentalVariogram:
    """Binned semivariance against lag distance.

    `counts` is carried alongside because the fit weights by it: a bin holding
    four pairs should not pull the model as hard as one holding four thousand.
    """

    lags: NDArray[np.float64]
    gamma: NDArray[np.float64]
    counts: NDArray[np.int64]
    #: Variance of the (declustered) sample. The sill has to land near this,
    #: and a fit that puts it far above is fitting noise.
    sample_variance: float
    n_points_used: int
    #: Azimuth this was computed along, or None for an omnidirectional
    #: variogram. Directional ones are how anisotropy is detected.
    azimuth: float | None = None

    @property
    def n_pairs(self) -> int:
        return int(self.counts.sum())


def declustering_weights(
    points: NDArray[np.floating], cell_size: float | None = None
) -> NDArray[np.float64]:
    """Cell-declustering weights. `05-geoprocessing.md` §6.3.

    Each point's weight is the reciprocal of how many points share its cell, so
    a twelve-well pad contributes about as much as a single isolated well.
    Normalised to sum to the point count, which keeps the weighted variance
    comparable to the ordinary one.

    The default cell size is the mean nearest-neighbour spacing: small enough
    that isolated wells sit alone, large enough that a pad falls in one cell.
    Choosing it from the data rather than fixing it matters because "clustered"
    is relative to the survey, not an absolute distance.
    """
    coords = np.asarray(points, dtype=float)
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise DegenerateInput(
            f"Points must be an (n, 2) array of planar coordinates; got shape {coords.shape}."
        )
    if len(coords) < 2:
        return np.ones(len(coords), dtype=np.float64)

    if cell_size is None:
        from scipy.spatial import cKDTree

        # Distance to the nearest *other* point, hence k=2 and column 1.
        tree = cKDTree(coords)
        spacing, _ = tree.query(coords, k=2)
        nearest = spacing[:, 1]
        finite = nearest[np.isfinite(nearest) & (nearest > 0)]
        if finite.size == 0:
            return np.ones(len(coords), dtype=np.float64)
        # Two spacings wide: one cell then holds a pad but not its neighbours.
        cell_size = float(np.median(finite) * 2.0)

    if cell_size <= 0:
        return np.ones(len(coords), dtype=np.float64)

    cells = np.floor(coords / cell_size).astype(np.int64)
    _, inverse, counts = np.unique(cells, axis=0, return_inverse=True, return_counts=True)
    weights = 1.0 / counts[inverse].astype(np.float64)
    return np.asarray(weights * (len(coords) / weights.sum()), dtype=np.float64)


def estimate_experimental(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    *,
    n_lags: int = 20,
    max_lag: float | None = None,
    subsample: int = DEFAULT_SUBSAMPLE,
    declustering: bool = True,
    azimuth: float | None = None,
    azimuth_tolerance: float = 22.5,
    anisotropy_ratio: float = 1.0,
    anisotropy_angle: float = 0.0,
    rng: np.random.Generator | None = None,
) -> ExperimentalVariogram:
    """Binned experimental variogram.

    `rng` is threaded through explicitly rather than defaulted to global state
    (`CLAUDE.md` §3.3): two runs of the same job must produce the same
    variogram, or the lineage record cannot reproduce the grid.

    `azimuth` restricts pairs to a direction, which is how anisotropy is
    detected — eight directional variograms, an ellipse fitted to their ranges.
    """
    rng = rng if rng is not None else np.random.default_rng(0)

    coords = np.asarray(points, dtype=float)
    z = np.asarray(values, dtype=float).ravel()
    if coords.ndim != 2 or coords.shape[1] != 2:
        raise DegenerateInput(
            f"Points must be an (n, 2) array of planar coordinates; got shape {coords.shape}."
        )
    if len(coords) != len(z):
        raise DegenerateInput(
            f"Got {len(coords)} points and {len(z)} values. Every control point "
            f"needs exactly one value."
        )

    finite = np.isfinite(z) & np.isfinite(coords).all(axis=1)
    coords, z = coords[finite], z[finite]
    if len(coords) < 10:
        raise DegenerateInput(
            f"Only {len(coords)} control points have finite coordinates and "
            f"values. A variogram from fewer than about 30 points is not "
            f"meaningful; from fewer than 10 it cannot be binned at all."
        )

    weights = declustering_weights(coords) if declustering else np.ones(len(coords))

    if len(coords) > subsample:
        # Declustered weights as selection probabilities: the subsample is then
        # spatially even rather than a scale copy of the clustering.
        probability = weights / weights.sum()
        chosen = rng.choice(len(coords), size=subsample, replace=False, p=probability)
        coords, z, weights = coords[chosen], z[chosen], weights[chosen]

    if anisotropy_ratio > 1.0:
        coords = coords @ anisotropy_transform(anisotropy_ratio, anisotropy_angle).T

    # Every pair, once. `triu(k=1)` is what keeps a point from being paired
    # with itself and every pair from being counted twice.
    upper_i, upper_j = np.triu_indices(len(coords), k=1)
    separation = coords[upper_j] - coords[upper_i]
    distance = np.hypot(separation[:, 0], separation[:, 1])
    squared_difference = (z[upper_j] - z[upper_i]) ** 2
    pair_weight = weights[upper_i] * weights[upper_j]

    if azimuth is not None:
        keep = _within_azimuth(separation, azimuth, azimuth_tolerance)
        distance, squared_difference, pair_weight = (
            distance[keep],
            squared_difference[keep],
            pair_weight[keep],
        )

    if max_lag is None:
        # Half the maximum separation. Beyond that, each bin holds only pairs
        # spanning the whole survey, and the estimate is dominated by trend
        # rather than by spatial correlation.
        max_lag = float(distance.max() / 2.0) if distance.size else 0.0
    if not max_lag > 0:
        raise DegenerateInput(
            "Every control point is at the same location, so there are no lag "
            "distances to bin. Check for duplicated coordinates."
        )

    edges = np.linspace(0.0, max_lag, n_lags + 1)
    within = distance <= max_lag
    bin_index = np.digitize(distance[within], edges) - 1
    bin_index = np.clip(bin_index, 0, n_lags - 1)

    kept_sq = squared_difference[within]
    kept_w = pair_weight[within]
    kept_d = distance[within]

    # Matheron's estimator, weighted: gamma(h) = sum(w * dz^2) / (2 * sum(w)).
    weight_sum = np.bincount(bin_index, weights=kept_w, minlength=n_lags)
    value_sum = np.bincount(bin_index, weights=kept_w * kept_sq, minlength=n_lags)
    distance_sum = np.bincount(bin_index, weights=kept_w * kept_d, minlength=n_lags)
    counts = np.bincount(bin_index, minlength=n_lags).astype(np.int64)

    usable = (counts >= MIN_PAIRS_PER_LAG) & (weight_sum > 0)
    if not np.any(usable):
        raise DegenerateInput(
            f"No lag bin holds at least {MIN_PAIRS_PER_LAG} pairs, so there is "
            f"nothing to fit. The points may be too few, or clustered so tightly "
            f"that every pair falls in one bin — try a larger max_lag or fewer "
            f"lag bins."
        )

    mean_weight = np.average(z, weights=weights)
    variance = float(np.average((z - mean_weight) ** 2, weights=weights))

    return ExperimentalVariogram(
        lags=np.asarray(distance_sum[usable] / weight_sum[usable], dtype=np.float64),
        gamma=np.asarray(value_sum[usable] / (2.0 * weight_sum[usable]), dtype=np.float64),
        counts=counts[usable],
        sample_variance=variance,
        n_points_used=len(coords),
        azimuth=azimuth,
    )


def _within_azimuth(
    separation: NDArray[np.floating], azimuth: float, tolerance: float
) -> NDArray[np.bool_]:
    """Pairs whose separation lies within `tolerance` of `azimuth`.

    Azimuth is clockwise from north — a geologist's convention, not the
    mathematical one. The comparison is modulo 180 because a pair has no
    direction: A-to-B at 035 and B-to-A at 215 are the same pair.
    """
    # atan2(dx, dy) rather than (dy, dx): that is what makes the result an
    # azimuth from north rather than an angle from east.
    pair_azimuth = np.degrees(np.arctan2(separation[:, 0], separation[:, 1])) % 180.0
    difference = np.abs(pair_azimuth - (azimuth % 180.0))
    return np.asarray(np.minimum(difference, 180.0 - difference) <= tolerance)


__all__ = [
    "DEFAULT_SUBSAMPLE",
    "MIN_PAIRS_PER_LAG",
    "ExperimentalVariogram",
    "declustering_weights",
    "estimate_experimental",
]
