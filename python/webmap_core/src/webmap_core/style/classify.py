"""Class-break computation. `08-styling-palettes.md` §4.

One implementation, not two. Classification needs the values themselves —
hundreds of thousands of them — and NumPy to work on them, so it runs where
the data is. The frontend asks for breaks and styles with what it gets back,
which is also why `Graduated.breaks` is stored explicitly rather than being
recomputed from the method on every render.

**Every function here returns exactly `n_classes - 1` interior breaks.** That
is the invariant `compile_symbology` enforces and the legend depends on; the
off-by-one it prevents renders a 5-class map with a 4-entry legend (§8).
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

from webmap_core.exceptions import WebMapError

#: Nice-number mantissas. 2.5 is included because a 0.25 %-porosity step reads
#: naturally to a geologist even though it is not a power of two or ten.
_NICE = (1.0, 2.0, 2.5, 5.0, 10.0)

METHODS = (
    "equal_interval",
    "quantile",
    "natural_breaks",
    "standard_deviation",
    "pretty",
    "manual",
)


class ClassificationError(WebMapError):
    """Values cannot be classified as asked."""


def classify(values: NDArray[np.floating], method: str, n_classes: int) -> list[float]:
    """Compute class breaks. Returns `n_classes - 1` interior breaks.

    Non-finite values are dropped first. A grid read from Surfer arrives full
    of NaN outside its blanking polygon, and including those would collapse
    every break to the same number.
    """
    if n_classes < 2:
        raise ClassificationError(
            f"Classification needs at least 2 classes; got {n_classes}. A single "
            f"class has no breaks — use a single-symbol symbology instead."
        )

    finite = np.asarray(values, dtype=float).ravel()
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise ClassificationError(
            "No finite values to classify. Every value in this attribute is null, "
            "NaN, or infinite — check the field name, and check whether a blanking "
            "value (Surfer's 1.70141e38) was converted to NaN on read."
        )

    vmin = float(finite.min())
    vmax = float(finite.max())
    if vmin == vmax:
        raise ClassificationError(
            f"Every value is {vmin:g}, so there is nothing to classify into "
            f"{n_classes} classes. Style this layer with a single symbol, or pick "
            f"an attribute that varies."
        )

    if method == "equal_interval":
        breaks = list(np.linspace(vmin, vmax, n_classes + 1)[1:-1])
    elif method == "quantile":
        qs = np.linspace(0.0, 100.0, n_classes + 1)[1:-1]
        breaks = list(np.percentile(finite, qs))
    elif method == "natural_breaks":
        breaks = jenks_breaks(finite, n_classes)
    elif method == "standard_deviation":
        breaks = std_dev_breaks(finite, n_classes)
    elif method == "pretty":
        breaks = pretty_breaks(vmin, vmax, n_classes)
    elif method == "manual":
        raise ClassificationError(
            "Manual classification has no breaks to compute — supply them directly "
            "in Graduated.breaks."
        )
    else:
        raise ClassificationError(
            f"Unknown classification method '{method}'. Expected one of: {', '.join(METHODS)}."
        )

    return _validated(breaks, method, n_classes)


def _validated(breaks: list[float], method: str, n_classes: int) -> list[float]:
    """Guard the two properties everything downstream assumes.

    Breaks must be strictly ascending: they compile to a MapLibre `step`
    expression, whose stops must ascend or the style is rejected outright. Ties
    are not hypothetical — a quantile classification of an attribute where half
    the wells read 0.0 produces repeated breaks, and the failure would surface
    as a blank map rather than as a message about the data.
    """
    result = [float(b) for b in breaks]
    if len(result) != n_classes - 1:
        raise ClassificationError(
            f"'{method}' produced {len(result)} breaks for {n_classes} classes; "
            f"{n_classes - 1} were required. This is a bug in the classifier, not "
            f"in the data."
        )
    if any(b >= a for a, b in zip(result[1:], result[:-1], strict=True)):
        raise ClassificationError(
            f"'{method}' produced repeated breaks at {n_classes} classes "
            f"({', '.join(f'{b:g}' for b in result)}). The values are too "
            f"concentrated to split this many ways — use fewer classes, or "
            f"'natural_breaks', which places boundaries where the data has gaps."
        )
    return result


def pretty_breaks(vmin: float, vmax: float, n_classes: int) -> list[float]:
    """Round breaks at 1, 2, 2.5, or 5 × 10^n.

    Usually the right default for geological maps. A porosity range of
    4.1–21.8 % into 5 classes gives breaks at 5, 10, 15, 20 — not 7.64, 11.18,
    14.72, 18.26. Geologists read breaks off the legend and expect numbers they
    can hold in their head.

    **Pretty numbers and an exact class count are in tension**, and the class
    count wins: it is the single source of truth the compiler and the legend
    share (§8), so returning "about five" classes would break the thing this
    module exists to keep consistent. The search therefore prefers a step whose
    round multiples happen to fall `n_classes - 1` times inside the range, and
    falls back to an evenly spaced run of round numbers anchored above `vmin`
    when no step in the family does. The fallback's top class is wider than the
    rest; that is the visible cost, and it is a smaller cost than breaks nobody
    can read.
    """
    span = vmax - vmin
    ideal = span / n_classes
    exponent = math.floor(math.log10(ideal)) if ideal > 0 else 0

    # Descending, so the first match is the largest step — largest means
    # roundest, and roundest is the point. Four decades below the ideal is far
    # more headroom than the fallback below ever needs, and costs one pass over
    # a twenty-element list.
    steps = sorted(
        {m * 10.0**e for e in range(exponent - 4, exponent + 2) for m in _NICE},
        reverse=True,
    )

    # First preference: a step whose round multiples happen to fall exactly
    # n_classes - 1 times inside the range. Evenly spaced and evenly placed.
    for step in steps:
        interior = _multiples_within(vmin, vmax, step)
        if len(interior) == n_classes - 1:
            return interior

    # Fallback: an evenly spaced run of round numbers anchored above vmin,
    # taking the largest step whose whole run still lands strictly inside. The
    # top class comes out wider than the rest — the visible cost of holding the
    # class count fixed, and a smaller cost than breaks nobody can read.
    for step in (s for s in steps if s <= ideal * 10):
        first = math.floor(vmin / step + 1e-9) * step + step
        run = [_snap(first + i * step, step) for i in range(n_classes - 1)]
        if all(vmin < b < vmax for b in run) and len(set(run)) == len(run):
            return run

    # Unreachable for any finite range — the steps above descend four decades
    # below span/n_classes. Equal interval rather than an exception, because a
    # legend with unlovely numbers beats a layer that will not style at all.
    return [vmin + span * i / n_classes for i in range(1, n_classes)]


def _multiples_within(vmin: float, vmax: float, step: float) -> list[float]:
    """Multiples of `step` strictly inside `(vmin, vmax)`.

    Strictly: a break equal to the minimum would open an empty first class, and
    one equal to the maximum an empty last class. Both render as a legend entry
    that matches nothing on the map.
    """
    lowest = math.floor(vmin / step) + 1
    highest = math.ceil(vmax / step) - 1
    if highest < lowest:
        return []
    return [_snap(i * step, step) for i in range(lowest, highest + 1) if vmin < i * step < vmax]


def _snap(value: float, step: float) -> float:
    """Round away binary-representation dust.

    `3 * 0.1` is `0.30000000000000004`, and a legend that reads
    "0.30000000000000004 – 0.4" defeats the entire purpose of this function.
    """
    decimals = max(0, -math.floor(math.log10(step)) + 2)
    return round(value, decimals)


def std_dev_breaks(values: NDArray[np.floating], n_classes: int) -> list[float]:
    """Breaks at whole standard deviations, centred on the mean.

    The classes are symmetric about the mean with open tails, which is what
    makes this the method for anomaly and difference maps: the reader is being
    shown how unusual a value is, not where it sits in the range.
    """
    mean = float(np.mean(values))
    # Population, not sample: these values *are* the population being mapped,
    # not a sample drawn from a larger one.
    sigma = float(np.std(values))
    if sigma == 0.0:
        raise ClassificationError(
            "Standard deviation is zero, so every value equals the mean and there "
            "are no deviations to classify by."
        )
    centre = (n_classes - 2) / 2.0
    return [mean + (j - centre) * sigma for j in range(n_classes - 1)]


def jenks_breaks(
    values: NDArray[np.floating], n_classes: int, max_sample: int = 5_000
) -> list[float]:
    """Fisher-Jenks natural breaks.

    Minimises within-class variance, which puts boundaries where the data
    already has gaps — the reason it is the right method for a multimodal
    attribute like net-to-gross across two facies.

    O(n² k) if written directly. Two things make it tractable: the values are
    subsampled above `max_sample`, and the dynamic program uses the
    divide-and-conquer optimisation available because the cost is concave-Monge
    (the optimal split point is non-decreasing in the segment end). That takes
    each class layer from O(n²) to O(n log n).

    **The subsample is systematic, not random** — every k-th value of the
    sorted array. `08-styling-palettes.md` §4 sketches a seeded
    `default_rng(0)` instead; systematic sampling is used here because it is
    deterministic *without* a seed and independent of row order, so re-reading
    the same layer from a differently sorted Parquet file cannot move the
    breaks. It also tracks the distribution's shape more closely than a random
    draw of the same size, which is precisely what Jenks is measuring.
    """
    ordered = np.sort(np.asarray(values, dtype=float).ravel())
    if ordered.size > max_sample:
        indices = np.linspace(0, ordered.size - 1, max_sample).round().astype(int)
        ordered = ordered[np.unique(indices)]

    n = ordered.size
    if n < n_classes:
        raise ClassificationError(
            f"Cannot form {n_classes} natural-break classes from {n} distinct "
            f"values. Use at most {n} classes, or a different method."
        )

    # Prefix sums make the within-segment sum of squared deviations an O(1)
    # lookup, which is what the vectorised candidate search below depends on.
    p1 = np.concatenate(([0.0], np.cumsum(ordered)))
    p2 = np.concatenate(([0.0], np.cumsum(ordered * ordered)))

    def ssd(starts: NDArray[np.int64], end: int) -> NDArray[np.float64]:
        counts = (end - starts).astype(float)
        s1 = p1[end] - p1[starts]
        s2 = p2[end] - p2[starts]
        return np.asarray(s2 - (s1 * s1) / counts, dtype=np.float64)

    # One class covering the first m values, for every m at once.
    sizes = np.arange(n + 1, dtype=float)
    previous = p2 - (p1 * p1) / np.where(sizes == 0.0, 1.0, sizes)
    previous[0] = 0.0

    splits: list[NDArray[np.int64]] = []
    for j in range(2, n_classes + 1):
        current = np.full(n + 1, np.inf)
        argmin = np.zeros(n + 1, dtype=np.int64)
        _dp_layer(previous, current, argmin, ssd, j, j, n, j - 1, n - 1)
        splits.append(argmin)
        previous = current

    # Backtrack. A break is the *first* value of the upper class, not the last
    # of the lower one: the compiled `step` expression sends a feature equal to
    # the break upward, so taking the lower value would misfile every feature
    # sitting exactly on a boundary.
    breaks: list[float] = []
    end = n
    for argmin in reversed(splits):
        start = int(argmin[end])
        breaks.append(float(ordered[start]))
        end = start
    breaks.reverse()
    return breaks


def _dp_layer(
    previous: NDArray[np.float64],
    current: NDArray[np.float64],
    argmin: NDArray[np.int64],
    ssd: Callable[[NDArray[np.int64], int], NDArray[np.float64]],
    classes: int,
    mlo: int,
    mhi: int,
    ilo: int,
    ihi: int,
) -> None:
    """One divide-and-conquer step of the Fisher-Jenks dynamic program.

    Solves the midpoint of `[mlo, mhi]`, then recurses either side with the
    candidate window narrowed by the split it found. That narrowing is the
    whole optimisation, and it is sound because the within-class sum of squares
    is concave-Monge: the optimal split point never moves left as the segment
    end moves right. O(n log n) per class layer instead of O(n²).

    At module level rather than nested in the layer loop so the arrays it
    writes are parameters rather than captured loop variables — a closure over
    a rebound name is a bug waiting for someone to make this lazy.
    """
    if mlo > mhi:
        return
    mid = (mlo + mhi) // 2
    lo, hi = max(ilo, classes - 1), min(ihi, mid - 1)
    if hi < lo:
        # No admissible split for this end. Leave the cost at infinity and
        # carry on rather than returning: abandoning the recursion here would
        # leave the halves either side of `mid` unsolved.
        _dp_layer(previous, current, argmin, ssd, classes, mlo, mid - 1, ilo, ihi)
        _dp_layer(previous, current, argmin, ssd, classes, mid + 1, mhi, ilo, ihi)
        return

    starts = np.arange(lo, hi + 1, dtype=np.int64)
    costs = previous[starts] + ssd(starts, mid)
    best = int(np.argmin(costs))
    current[mid] = costs[best]
    argmin[mid] = starts[best]

    split = int(argmin[mid])
    _dp_layer(previous, current, argmin, ssd, classes, mlo, mid - 1, ilo, split)
    _dp_layer(previous, current, argmin, ssd, classes, mid + 1, mhi, split, ihi)


__all__ = [
    "METHODS",
    "ClassificationError",
    "classify",
    "jenks_breaks",
    "pretty_breaks",
    "std_dev_breaks",
]
