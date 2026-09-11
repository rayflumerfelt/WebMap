"""Sample validation. `13-kriging.md` §5.1.

The first step of the pipeline, and the one that decides whether anything after
it is meaningful. Four checks, in the order §5 requires — each assumes the
previous has run.

**The frame is not checked here.** `CrsContext` refuses a geographic SRID at the
boundary that prepares the arrays, which is where the SRID is actually known.
Sniffing coordinate magnitudes to guess whether these are degrees is the
alternative, and it is wrong for a local grid in metres near the origin.

**Duplicates are resolved by a stated policy, never averaged silently.** Two
wells at one location with different targets is either a real pair of
measurements or a data error, and which one it is changes the answer. The
default averages and *says so* with an INFO flag carrying the count, because
that is the behaviour that surprises fewest people — but `"error"` exists for
the caller who would rather be stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from webmap_geo.flags import FlagList, raise_flag

#: Below this, a variogram is a line through noise and a kriging weight is an
#: opinion. §5.1's default, and low enough to be permissive rather than safe.
MIN_SAMPLES = 30

#: Fraction of the 1st-percentile inter-point distance within which two points
#: are the same point. Half, per §5.1: close enough that no survey distinguishes
#: them, far enough that genuinely adjacent wells survive.
DEDUP_FRACTION = 0.5

DuplicatePolicy = Literal["mean", "first", "error"]


@dataclass(frozen=True)
class Samples:
    """Control points, after validation. `13` §4.1.

    Coordinates in the analysis frame, values in the target's own units, and a
    covariate table that may be empty. Frozen because every step downstream
    takes one and returns a new one — a pipeline that mutates its input cannot
    be re-run from the middle when a diagnostic asks why.
    """

    x: NDArray[np.float64]
    y: NDArray[np.float64]
    values: NDArray[np.float64]
    #: Named covariate columns, each the same length as `values`.
    covariates: dict[str, NDArray[np.float64]]
    #: Declustering weights, normalised to sum to `n`. Ones until §5.2 runs.
    weights: NDArray[np.float64]

    @property
    def count(self) -> int:
        return int(self.values.size)

    def take(self, index: NDArray[np.intp] | NDArray[np.bool_]) -> Samples:
        """The same samples, subset. Keeps every array in step."""
        return Samples(
            x=self.x[index],
            y=self.y[index],
            values=self.values[index],
            covariates={name: column[index] for name, column in self.covariates.items()},
            weights=self.weights[index],
        )

    def with_weights(self, weights: NDArray[np.float64]) -> Samples:
        return replace(self, weights=weights)


def samples_from(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    values: NDArray[np.float64],
    covariates: dict[str, NDArray[np.float64]] | None = None,
) -> Samples:
    """Assemble samples with unit weights, before validation."""
    covariates = covariates or {}
    lengths = {len(x), len(y), len(values)} | {len(column) for column in covariates.values()}
    if len(lengths) > 1:
        raise ValueError(
            f"Coordinates, values and covariates must be the same length; got "
            f"{sorted(lengths)}. A mismatch here puts a covariate on the wrong "
            f"well, which no diagnostic downstream can detect."
        )
    return Samples(
        x=np.asarray(x, dtype=np.float64),
        y=np.asarray(y, dtype=np.float64),
        values=np.asarray(values, dtype=np.float64),
        covariates={
            name: np.asarray(column, dtype=np.float64) for name, column in covariates.items()
        },
        weights=np.ones(len(values), dtype=np.float64),
    )


def validate(
    samples: Samples,
    flags: FlagList,
    *,
    used_covariates: list[str] | None = None,
    dedup_tol: float | None = None,
    duplicates: DuplicatePolicy | Callable[[NDArray[np.float64]], float] = "mean",
    min_samples: int = MIN_SAMPLES,
) -> Samples:
    """Drop what cannot be used, resolve what is duplicated, refuse what is degenerate."""
    samples = _drop_missing(samples, flags, used_covariates)
    samples = _resolve_duplicates(samples, flags, dedup_tol, duplicates)
    _require_usable(samples, min_samples)
    return samples


def _drop_missing(
    samples: Samples, flags: FlagList, used_covariates: list[str] | None
) -> Samples:
    """Drop rows with a missing target, or a missing covariate the trend uses.

    *The trend uses* — not every covariate present. A table carrying five
    columns of which the trend reads two should not lose a well because a third
    column is blank; that is the difference between cleaning the data and
    throwing it away.
    """
    keep = np.isfinite(samples.values) & np.isfinite(samples.x) & np.isfinite(samples.y)
    for name in used_covariates or []:
        column = samples.covariates.get(name)
        if column is not None:
            keep &= np.isfinite(column)

    dropped = int((~keep).sum())
    if dropped:
        flags.add(
            "ROWS_DROPPED",
            f"Dropped {dropped:,} of {samples.count:,} samples with a missing "
            f"target or coordinate"
            + (
                f", or a missing value in {', '.join(used_covariates)}"
                if used_covariates
                else ""
            )
            + ". They cannot contribute to a variogram or a trend fit.",
            dropped=dropped,
            kept=int(keep.sum()),
        )
    return samples.take(keep)


def default_dedup_tol(x: NDArray[np.float64], y: NDArray[np.float64]) -> float:
    """Half the 1st-percentile inter-point distance. §5.1's default.

    The 1st percentile rather than the minimum: the minimum is itself often a
    duplicate pair, and a tolerance derived from it is zero. Computed on a
    sample of pairs when there are many points, because all-pairs on 50,000
    wells is 1.25e9 distances for a number that only has to be roughly right.
    """
    count = len(x)
    if count < 2:
        return 0.0

    rng = np.random.default_rng(0)
    if count > 2_000:
        index = rng.choice(count, size=2_000, replace=False)
        px, py = x[index], y[index]
    else:
        px, py = x, y

    dx = px[:, None] - px[None, :]
    dy = py[:, None] - py[None, :]
    distances = np.hypot(dx, dy)
    upper = distances[np.triu_indices_from(distances, k=1)]
    positive = upper[upper > 0]
    if positive.size == 0:
        return 0.0
    return float(np.percentile(positive, 1.0) * DEDUP_FRACTION)


def _resolve_duplicates(
    samples: Samples,
    flags: FlagList,
    dedup_tol: float | None,
    policy: DuplicatePolicy | Callable[[NDArray[np.float64]], float],
) -> Samples:
    """Merge points closer than the tolerance, by the stated policy."""
    if samples.count < 2:
        return samples

    tolerance = dedup_tol if dedup_tol is not None else default_dedup_tol(samples.x, samples.y)
    if tolerance <= 0:
        return samples

    # Grid-snap rather than all-pairs: two points within the tolerance land in
    # the same cell, which is O(n) instead of O(n²) and is what makes this
    # runnable on the 50,000-well tables this is aimed at. A pair straddling a
    # cell boundary survives as two points, which is the acceptable direction
    # for the error to go — it keeps data rather than merging it.
    keys = np.stack(
        [
            np.round(samples.x / tolerance).astype(np.int64),
            np.round(samples.y / tolerance).astype(np.int64),
        ],
        axis=1,
    )
    _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    if counts.max() == 1:
        return samples

    merged = int((counts - 1).sum())
    if policy == "error":
        raise_flag(
            "INSUFFICIENT_DATA",
            f"{merged:,} samples are collocated within {tolerance:,.2f} of "
            f"another, and the duplicate policy is 'error'. Resolve them "
            f"upstream, or pass duplicates='mean' to average them.",
            duplicates=merged,
            tolerance=tolerance,
        )

    groups = int(inverse.max()) + 1
    order = np.argsort(inverse, kind="stable")
    first_of = np.zeros(groups, dtype=np.intp)
    first_of[inverse[order][::-1]] = order[::-1]

    if policy == "first":
        resolved = samples.take(np.sort(first_of))
    else:
        resolved = _average_groups(samples, inverse, groups, policy)

    flags.add(
        "DUPLICATES_RESOLVED",
        f"{merged:,} collocated sample(s) within {tolerance:,.2f} were resolved "
        f"by '{policy if isinstance(policy, str) else 'a supplied function'}'. "
        f"{resolved.count:,} distinct locations remain.",
        merged=merged,
        tolerance=tolerance,
        remaining=resolved.count,
    )
    return resolved


def _average_groups(
    samples: Samples,
    inverse: NDArray[np.intp],
    groups: int,
    policy: DuplicatePolicy | Callable[[NDArray[np.float64]], float],
) -> Samples:
    """One sample per group, values combined by the policy."""
    reducer: Callable[[NDArray[np.float64]], float]
    reducer = (lambda block: float(np.mean(block))) if policy == "mean" else policy  # type: ignore[assignment]

    x = np.zeros(groups)
    y = np.zeros(groups)
    values = np.zeros(groups)
    covariates = {name: np.zeros(groups) for name in samples.covariates}
    weights = np.zeros(groups)

    for group in range(groups):
        members = inverse == group
        # Coordinates are averaged whatever the value policy is: the merged
        # sample has to sit somewhere, and the centroid of two wells 3 ft apart
        # is the only defensible place.
        x[group] = samples.x[members].mean()
        y[group] = samples.y[members].mean()
        values[group] = reducer(samples.values[members])
        for name, column in samples.covariates.items():
            covariates[name][group] = column[members].mean()
        weights[group] = samples.weights[members].sum()

    return Samples(x=x, y=y, values=values, covariates=covariates, weights=weights)


def _require_usable(samples: Samples, min_samples: int) -> None:
    """Refuse a sample set that cannot support a variogram."""
    if samples.count < min_samples:
        raise_flag(
            "INSUFFICIENT_DATA",
            f"{samples.count:,} samples is below the minimum of {min_samples:,}. "
            f"A variogram fitted to fewer is a line through noise, and the "
            f"kriging weights that follow are opinions rather than estimates. "
            f"Add control, or interpolate with a method that does not model "
            f"spatial structure — minimum curvature or IDW.",
            count=samples.count,
            minimum=min_samples,
        )

    if samples.count >= 3 and _is_collinear(samples.x, samples.y):
        raise_flag(
            "INSUFFICIENT_DATA",
            "Every sample lies on a single line, so there is no two-dimensional "
            "structure to estimate: the variogram is defined along one direction "
            "and undefined across it. This usually means one coordinate column "
            "is constant, or the X and Y columns were mapped to the same field.",
            count=samples.count,
        )


def _is_collinear(x: NDArray[np.float64], y: NDArray[np.float64]) -> bool:
    """Whether every point lies on one line.

    By the smaller singular value of the centred coordinates, relative to the
    larger: a rank-one cloud is a line. Scale-free, so it is the same test for
    a field in feet and one in metres.
    """
    centred = np.stack([x - x.mean(), y - y.mean()], axis=1)
    singular = np.linalg.svd(centred, compute_uv=False)
    if singular[0] == 0:
        return True
    return bool(singular[1] / singular[0] < 1e-10)


def as_array(samples: Samples) -> NDArray[np.float64]:
    """Coordinates as an (n, 2) array, for the neighbourhood search."""
    return np.stack([samples.x, samples.y], axis=1)


__all__ = [
    "DEDUP_FRACTION",
    "MIN_SAMPLES",
    "DuplicatePolicy",
    "Samples",
    "as_array",
    "default_dedup_tol",
    "samples_from",
    "validate",
]
