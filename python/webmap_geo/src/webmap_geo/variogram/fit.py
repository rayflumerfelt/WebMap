"""Fitting a model to an experimental variogram. `05-geoprocessing.md` §6.3.

Two paths, both required: an automatic one so Claude can grid without asking a
geologist to fit a curve, and the parameters exposed so a geologist can
override every one of them.

**Short lags are weighted more heavily.** Kriging weights are decided almost
entirely by the variogram's behaviour near the origin — the first few bins
determine whether a nearby point dominates or is averaged with distant ones.
A plain least-squares fit treats a bin at the survey's far edge as equally
important, and produces a model that fits the tail and is wrong where it
matters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.variogram.experimental import ExperimentalVariogram, estimate_experimental
from webmap_geo.variogram.model import MODELS, FittedVariogram

#: `05` §6.3: eight azimuths, an ellipse fitted to the resulting ranges.
ANISOTROPY_AZIMUTHS = (0.0, 22.5, 45.0, 67.5, 90.0, 112.5, 135.0, 157.5)

#: Below this, the ellipse is within the noise of the directional fits and
#: reporting it would dress up scatter as structure.
MIN_REPORTABLE_ANISOTROPY = 1.2

#: How much of the variation between directional ranges the ellipse must
#: explain before it is believed. Eight ranges fitted from a few hundred pairs
#: each scatter considerably; a real grain shows up as a clean cosine, and
#: noise does not.
MIN_ELLIPSE_FIT = 0.55


@dataclass(frozen=True)
class _Candidate:
    model: str
    nugget: float
    sill: float
    range_: float
    residual: float


def fit(
    experimental: ExperimentalVariogram,
    model: str | None = None,
    *,
    detect_anisotropy: bool = False,
) -> FittedVariogram:
    """Fit a model to a binned experimental variogram.

    `model=None` fits every candidate and returns the best by weighted least
    squares. That is the automatic path: a geologist who knows the property is
    exponential says so, and Claude, which does not, gets a defensible answer.

    Anisotropy is not detected from a single experimental variogram — it needs
    directional ones, which need the original points. Use `fit_auto` for that;
    this function only carries through a ratio the caller already knows.
    """
    if detect_anisotropy:
        raise DegenerateInput(
            "Anisotropy cannot be detected from an already-binned variogram — it "
            "needs directional variograms, which need the original points. Call "
            "fit_auto(points, values) instead."
        )

    candidates = [model] if model else list(MODELS)
    for candidate in candidates:
        if candidate not in MODELS:
            raise DegenerateInput(
                f"Unknown variogram model '{candidate}'. Available models: {', '.join(MODELS)}."
            )

    best = min(
        (_fit_one(experimental, name) for name in candidates),
        key=lambda c: c.residual,
    )
    return FittedVariogram(
        model=best.model,
        nugget=best.nugget,
        sill=best.sill,
        range_=best.range_,
        fit_residual=best.residual,
        n_pairs_used=experimental.n_pairs,
    )


def fit_auto(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    *,
    model: str | None = None,
    detect_anisotropy: bool = True,
    n_lags: int = 20,
    rng: np.random.Generator | None = None,
) -> FittedVariogram:
    """Estimate and fit in one call — the path Claude takes.

    Anisotropy detection computes directional variograms in eight azimuths and
    fits an ellipse to their ranges. It matters for structure maps especially:
    a basin with a structural grain has a range twice as long along strike as
    across it, and an isotropic model smears the structure into a dome.
    """
    experimental = estimate_experimental(points, values, n_lags=n_lags, rng=rng)
    isotropic = fit(experimental, model)

    if not detect_anisotropy:
        return isotropic

    ratio, angle = detect_anisotropy_from(points, values, model=isotropic.model, rng=rng)
    if ratio < MIN_REPORTABLE_ANISOTROPY:
        # Reported as isotropic rather than as 1.08:1. A ratio inside the noise
        # of the directional fits is scatter, and dressing it up as structure
        # invites someone to read a grain that is not there.
        return isotropic

    # Refit with the anisotropy applied, so the range is the *major*-axis range
    # rather than a direction-averaged one.
    corrected = estimate_experimental(
        points,
        values,
        n_lags=n_lags,
        anisotropy_ratio=ratio,
        anisotropy_angle=angle,
        rng=rng,
    )
    refitted = fit(corrected, isotropic.model)

    return FittedVariogram(
        model=refitted.model,
        nugget=refitted.nugget,
        sill=refitted.sill,
        range_=refitted.range_,
        anisotropy_ratio=ratio,
        anisotropy_angle=angle,
        fit_residual=refitted.fit_residual,
        n_pairs_used=corrected.n_pairs,
    )


def detect_anisotropy_from(
    points: NDArray[np.floating],
    values: NDArray[np.floating],
    *,
    model: str = "spherical",
    rng: np.random.Generator | None = None,
) -> tuple[float, float]:
    """Ratio and azimuth of the major axis, from directional variograms.

    Returns `(1.0, 0.0)` unless the directions agree on an ellipse. Two guards,
    and the first is the one that matters:

    **The ratio comes from the fitted ellipse, not from max/min of the eight
    ranges.** Taking the extremes of eight noisy estimates is biased upward by
    construction — order statistics alone give a ratio near 1.7 on a field with
    no anisotropy at all, which is what this originally did and what the
    isotropic test caught. The cosine fit uses every direction and is not
    dragged by whichever one happened to fit badly.

    **The ellipse must actually explain the scatter.** An R-squared threshold
    is what separates a structural grain from eight numbers that vary because
    each was fitted from a few hundred pairs. Reporting a grain that is not
    there is worse than missing a weak one: a map drawn with a false azimuth
    smears structure along a direction nobody should be reading.

    **The reported ratio is conservative.** Directional variograms admit pairs
    within 22.5 degrees of their azimuth, which mixes directions and dilutes
    the contrast — a true 4:1 grain reads as about 3:1. Under-reporting is the
    right direction to be wrong in: it makes the kriged surface slightly less
    elongated than the truth rather than inventing elongation.
    """
    # **One max_lag for every direction, computed once.**
    #
    # Letting each direction pick its own is a geometric trap. In a square
    # survey the diagonal reaches 1.41x further than the axes, so a diagonal
    # variogram is binned over a longer lag range and fits a longer range —
    # and the ellipse comes out 1.8:1 with its major axis at 045 on a field
    # with no anisotropy whatsoever. That is what this originally did, and the
    # detected azimuth landing exactly on the diagonal is what gave it away.
    #
    # A third of the shorter side of the bounding box. Every direction has
    # pairs out to that distance, so every direction is measured over the same
    # window — and it is short enough that the domain's *shape* stops biasing
    # the result. Measured on a synthetic isotropic field at 700 points:
    #
    #   window      isotropic reads     true 4:1 reads
    #   span/2          1.67:1              3.49:1
    #   span/3          1.18:1              3.12:1
    #   span/4          1.14:1              3.26:1
    #
    # span/2 reports a grain on a field that has none, which is the failure
    # that matters: a map drawn along a false azimuth smears structure in a
    # direction nobody should be reading. span/3 keeps the false reading below
    # the reporting threshold while still finding a real grain.
    coords = np.asarray(points, dtype=float)
    span = coords.max(axis=0) - coords.min(axis=0)
    shared_max_lag = float(min(span)) / 3.0
    if not shared_max_lag > 0:
        return (1.0, 0.0)

    ranges: list[tuple[float, float]] = []
    for azimuth in ANISOTROPY_AZIMUTHS:
        try:
            directional = estimate_experimental(
                points,
                values,
                azimuth=azimuth,
                n_lags=12,
                max_lag=shared_max_lag,
                rng=rng,
            )
            fitted = fit(directional, model)
        except DegenerateInput:
            # A direction with too few pairs. Skipped rather than defaulted:
            # a made-up range in one azimuth would tilt the whole ellipse.
            continue
        ranges.append((azimuth, fitted.range_))

    if len(ranges) < 6:
        return (1.0, 0.0)

    azimuths = np.array([a for a, _ in ranges])
    lengths = np.array([r for _, r in ranges])

    # r(theta) ~ mean + amplitude*cos(2*(theta - theta_major)). The doubling is
    # because a variogram has no direction, only an orientation.
    radians = np.radians(2.0 * azimuths)
    design = np.column_stack([np.ones_like(radians), np.cos(radians), np.sin(radians)])
    coefficients, *_ = np.linalg.lstsq(design, lengths, rcond=None)
    mean_range, cos_term, sin_term = coefficients

    predicted = design @ coefficients
    total = float(np.sum((lengths - lengths.mean()) ** 2))
    residual = float(np.sum((lengths - predicted) ** 2))
    explained = 1.0 - residual / total if total > 0 else 0.0
    if explained < MIN_ELLIPSE_FIT:
        return (1.0, 0.0)

    amplitude = float(np.hypot(cos_term, sin_term))
    major_length = float(mean_range) + amplitude
    minor_length = float(mean_range) - amplitude
    if minor_length <= 0:
        # The ellipse is degenerate — the fit implies a zero or negative range
        # across the minor axis, which is not a physical anisotropy but a sign
        # the directional fits disagreed wildly.
        return (1.0, 0.0)

    ratio = major_length / minor_length
    if ratio < MIN_REPORTABLE_ANISOTROPY:
        return (1.0, 0.0)

    major_azimuth = math.degrees(math.atan2(sin_term, cos_term)) / 2.0 % 180.0
    return (ratio, major_azimuth)


def _fit_one(experimental: ExperimentalVariogram, model: str) -> _Candidate:
    """Least squares over (nugget, partial sill, range) for one model.

    A grid search followed by refinement rather than a gradient method: the
    residual surface has local minima — a long range with a high nugget fits
    almost as well as a short range with a low one — and a gradient solver
    lands in whichever the initial guess is nearest. The starting values
    matter more than the algorithm here, so the search covers them explicitly.
    """
    lags = experimental.lags
    gamma = experimental.gamma

    # Weight by pair count and by 1/lag: short lags decide kriging weights, and
    # a bin holding four pairs should not pull as hard as one holding four
    # thousand (`05` §6.3).
    weights = experimental.counts.astype(float) / np.maximum(lags, lags[0] * 0.5)

    max_lag = float(lags.max())
    sill_guess = float(max(gamma.max(), experimental.sample_variance))

    best: _Candidate | None = None
    for range_fraction in np.linspace(0.15, 1.5, 16):
        for nugget_fraction in np.linspace(0.0, 0.6, 13):
            trial_range = max_lag * range_fraction
            trial_nugget = sill_guess * nugget_fraction

            candidate = FittedVariogram(
                model=model,
                nugget=trial_nugget,
                sill=sill_guess,
                range_=trial_range,
            )
            # The sill that minimises the residual for this shape, in closed
            # form: with nugget and range fixed, gamma is linear in the partial
            # sill, so there is no reason to search over it too.
            shape = (candidate.gamma(lags) - trial_nugget) / max(candidate.partial_sill, 1e-12)
            denominator = float(np.sum(weights * shape**2))
            if denominator <= 0:
                continue
            partial = float(np.sum(weights * shape * (gamma - trial_nugget)) / denominator)
            if partial <= 0:
                continue

            fitted = FittedVariogram(
                model=model,
                nugget=trial_nugget,
                sill=trial_nugget + partial,
                range_=trial_range,
            )
            residual = float(np.sum(weights * (fitted.gamma(lags) - gamma) ** 2))
            residual /= max(float(np.sum(weights)), 1e-12)

            if best is None or residual < best.residual:
                best = _Candidate(
                    model=model,
                    nugget=trial_nugget,
                    sill=trial_nugget + partial,
                    range_=trial_range,
                    residual=residual,
                )

    if best is None:
        raise DegenerateInput(
            f"Could not fit a {model} variogram: every candidate produced a "
            f"non-positive sill. The experimental variogram may be flat, which "
            f"means the values have no spatial structure at this scale."
        )
    return best


__all__ = [
    "ANISOTROPY_AZIMUTHS",
    "MIN_ELLIPSE_FIT",
    "MIN_REPORTABLE_ANISOTROPY",
    "detect_anisotropy_from",
    "fit",
    "fit_auto",
]
