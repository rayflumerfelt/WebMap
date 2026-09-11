"""Cell declustering. `13-kriging.md` §5.2.

**Samples cluster in high-value areas by construction.** Nobody drills a hundred
wells into the part of the field that does not produce, so the naive sample CDF
is biased high — and that bias propagates into the trend fit, the indicator
threshold placement and the tail model, which is three of the four things the
kriging document is about.

`05` §6.3 already declusters for the experimental variogram and gives the same
reason: well control is clustered by development history, not by geology. This
generalises it to everything else that reads the sample distribution.

**Cell declustering, with the cell size swept.** The method is standard and
parameter-free: sweep the cell size over a geometric range, compute the
declustered mean at each, and take the size that minimises it when the target is
positively clustered. The offset sweep matters as much as the size sweep — a
single fixed grid origin gives an answer that depends on where the grid happened
to start, which is not a property of the data.

**Two things the method does not promise, measured rather than assumed:**

*Unclustered data gets a correct mean, not equal weights.* On 400 evenly
scattered samples of a field with no trend, the declustered mean moves 0.08
standard deviations — it does not invent a bias. The individual weights still
vary, because the size that minimises the mean can be coarse enough to put a
handful of cells over the whole field. The weights are a means to the mean here;
reading one well's weight as a statement about that well is reading more than
the method says.

*A strong trend makes the minimum spurious.* With a monotone field, some cell
size always favours the low corner, so the sweep reports clustering where there
is none. That is inherent to minimising the mean and is why §5.2 keeps the whole
sweep: the curve is the diagnostic, and a minimum that is shallow or at the edge
of the range means "no clustering found" rather than "clustering of this size".
In the RIK pipeline the trend is fitted *after* this step, so on a strongly
trending target the weights correct less than they appear to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from webmap_geo.prep.validate import Samples

#: How many cell sizes to try between the smallest and largest.
SIZE_STEPS = 24

#: Random origin offsets averaged per cell size. Four is enough to remove the
#: dependence on where the grid starts without making the sweep four times as
#: expensive as it needs to be.
OFFSETS = 4

Direction = Literal["min", "max"]


@dataclass(frozen=True)
class DeclusterResult:
    weights: NDArray[np.float64]
    cell_size: float
    #: The declustered mean at the chosen size, and the naive one, because the
    #: difference between them is the whole point and is what a diagnostic
    #: panel shows.
    declustered_mean: float
    naive_mean: float
    #: Every size tried and the mean it produced — §12's declustering panel.
    sweep: list[tuple[float, float]]

    @property
    def bias(self) -> float:
        """How much the naive mean overstates, as a fraction."""
        if self.declustered_mean == 0:
            return 0.0
        return (self.naive_mean - self.declustered_mean) / abs(self.declustered_mean)


def decluster(
    samples: Samples,
    rng: np.random.Generator,
    *,
    cell_size: float | None = None,
    direction: Direction = "min",
) -> DeclusterResult:
    """Cell-declustering weights, normalised to sum to `n`.

    `direction="min"` finds the size that minimises the declustered mean, which
    is right when samples cluster in high values — the usual case. `"max"` is
    for the rare survey that oversampled the lows.

    The `Generator` is explicit and required, per `CLAUDE.md` §3.3: the offsets
    are random, so a run that seeded itself would not be reproducible and `13`
    §4.6 requires the seed in the lineage record.
    """
    if samples.count == 0:
        raise ValueError("Declustering needs at least one sample.")

    naive = float(np.mean(samples.values))
    if cell_size is not None:
        weights = _weights_for(samples, cell_size, rng, offsets=OFFSETS)
        return DeclusterResult(
            weights=weights,
            cell_size=cell_size,
            declustered_mean=float(np.average(samples.values, weights=weights)),
            naive_mean=naive,
            sweep=[],
        )

    sizes = _candidate_sizes(samples)
    sweep: list[tuple[float, float]] = []
    best: tuple[float, NDArray[np.float64], float] | None = None

    for size in sizes:
        weights = _weights_for(samples, size, rng, offsets=OFFSETS)
        mean = float(np.average(samples.values, weights=weights))
        sweep.append((size, mean))

        if best is None:
            best = (size, weights, mean)
            continue
        better = mean < best[2] if direction == "min" else mean > best[2]
        if better:
            best = (size, weights, mean)

    assert best is not None
    size, weights, mean = best
    return DeclusterResult(
        weights=weights,
        cell_size=size,
        declustered_mean=mean,
        naive_mean=naive,
        sweep=sweep,
    )


def _candidate_sizes(samples: Samples) -> NDArray[np.float64]:
    """Cell sizes to sweep, geometric from close spacing to the whole field.

    The bottom of the range is the median nearest-neighbour distance: below it
    every point is in its own cell and every weight is 1, which is the naive
    answer wearing a hat. The top is the field's own width, where every point
    shares one cell and the weights are equal for a different reason. The
    minimum is somewhere between, and both ends are included so the sweep can
    show that.
    """
    extent = max(np.ptp(samples.x), np.ptp(samples.y))
    if extent <= 0:
        return np.array([1.0])
    smallest = max(_median_nearest(samples), extent / 1000.0)
    return np.geomspace(smallest, extent, SIZE_STEPS)


def _median_nearest(samples: Samples) -> float:
    """Median distance to the nearest other sample.

    On a subsample when there are many: this sets the bottom of a sweep and
    does not need to be exact, and all-pairs on 50,000 wells is 1.25e9
    distances for a number that moves the answer not at all.
    """
    count = samples.count
    if count < 2:
        return 0.0

    index = np.arange(count)
    if count > 2_000:
        index = np.random.default_rng(0).choice(count, size=2_000, replace=False)

    px, py = samples.x[index], samples.y[index]
    dx = px[:, None] - px[None, :]
    dy = py[:, None] - py[None, :]
    distances = np.hypot(dx, dy)
    np.fill_diagonal(distances, np.inf)
    nearest = distances.min(axis=1)
    finite = nearest[np.isfinite(nearest)]
    return float(np.median(finite)) if finite.size else 0.0


def _weights_for(
    samples: Samples, cell_size: float, rng: np.random.Generator, *, offsets: int
) -> NDArray[np.float64]:
    """Weights at one cell size, averaged over several grid origins.

    A point in a cell holding `k` points gets weight `1/k`: a cluster of ten
    wells counts once, a lone well counts once. Averaged over offsets because
    otherwise the answer depends on where the grid happens to start, which is
    not a property of the data.
    """
    if cell_size <= 0:
        return np.ones(samples.count, dtype=np.float64)

    accumulated = np.zeros(samples.count, dtype=np.float64)
    for _ in range(offsets):
        shift = rng.uniform(0.0, cell_size, size=2)
        keys = np.stack(
            [
                np.floor((samples.x + shift[0]) / cell_size).astype(np.int64),
                np.floor((samples.y + shift[1]) / cell_size).astype(np.int64),
            ],
            axis=1,
        )
        _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
        accumulated += 1.0 / counts[inverse]

    weights = accumulated / offsets
    # Normalised to sum to n, per §5.2, so a weighted mean is comparable with an
    # unweighted one and a weighted least squares keeps the same scale.
    return weights * (samples.count / weights.sum())


def weighted_quantile(
    values: NDArray[np.float64], weights: NDArray[np.float64], quantiles: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Quantiles of the declustered distribution.

    Used for indicator threshold placement (§10.1) and the global CDF behind
    tail extrapolation (§10.4) — both of which read the *declustered*
    distribution, because placing thresholds on the naive one puts them where
    the drilling was rather than where the values are.
    """
    order = np.argsort(values)
    sorted_values = values[order]
    sorted_weights = weights[order]

    cumulative = np.cumsum(sorted_weights) - 0.5 * sorted_weights
    cumulative /= sorted_weights.sum()
    return np.interp(quantiles, cumulative, sorted_values)


__all__ = [
    "OFFSETS",
    "SIZE_STEPS",
    "DeclusterResult",
    "Direction",
    "decluster",
    "weighted_quantile",
]
