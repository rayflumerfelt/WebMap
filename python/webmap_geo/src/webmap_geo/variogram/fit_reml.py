"""REML for trend-residual variograms. `adr/0011`, `13-kriging.md` §7.4.

**For the residual of a fitted trend, and only there.** The argument is in
`adr/0011` and is worth restating, because this is the single easiest thing in
the kriging document to "simplify" into a subtly wrong map:

Residuals are not the errors. They are the errors minus whatever the fitted
trend absorbed, and that absorption is systematic. So a method-of-moments or
weighted-least-squares variogram of a fitted residual **underestimates both sill
and range**, with the bias growing at long lags and with the number of
coefficients. Inside the §8.1 loop the bias feeds itself: a flattened variogram
makes the covariance look near-diagonal, which makes GLS behave like OLS, which
lets the trend absorb more long-range structure, which flattens the next
variogram further. The loop converges. It converges happily. It can converge
onto a trend that has eaten the spatial signal, and nothing in the run reports a
problem — the fit residual is small, the coefficients look sensible, and the map
looks like a map.

REML avoids it by construction: it works with error contrasts, so what the trend
absorbed is projected out rather than left to bias the estimate.

    l(phi) = 1/2 log|C| + 1/2 log|J' C^-1 J| + 1/2 r' P r
    P      = C^-1 - C^-1 J (J' C^-1 J)^-1 J' C^-1

The middle term is the correction that method of moments does not have.

Three implementation rules from the ADR, which are part of the decision rather
than details, and are all here:

1. **One Cholesky serves all three terms**, and `C^-1` is never formed.
2. **Multi-start** from the WLS estimate plus perturbations, because REML
   surfaces are not reliably unimodal in range and nugget.
3. **Above ~10,000 samples the dense C is the wall.** A declustered subset is
   fitted instead, and which path ran is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray
from scipy import optimize
from scipy.linalg import cho_factor, cho_solve

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.flags import FlagList
from webmap_geo.variogram.model import FittedVariogram

#: Above this many samples the dense covariance is the wall: 10,000 samples is
#: an 800 MB matrix and a Cholesky per optimiser evaluation. Rule 3.
DENSE_LIMIT = 10_000

#: Samples kept when the subset path runs. `adr/0011`: ~5,000, declustered.
SUBSET_SIZE = 5_000

#: Perturbations of the WLS start, as multipliers on range and nugget. Rule 2 —
#: one start lands in a local minimum often enough to matter.
STARTS = ((1.0, 1.0), (2.0, 0.5), (0.5, 2.0), (1.5, 1.0))

#: Bounds as multiples of the starting range, and of the data variance for the
#: sill. Wide enough not to bind on a real fit; narrow enough that the optimiser
#: does not wander into a range longer than the field, where the likelihood is
#: flat and every value is as good as any other.
RANGE_BOUNDS = (0.05, 5.0)
NUGGET_FRACTION_BOUNDS = (1e-4, 0.95)


@dataclass(frozen=True)
class RemlResult:
    """A REML fit, with what it took to get there."""

    variogram: FittedVariogram
    #: Restricted log-likelihood at the optimum. Comparable *between models on
    #: the same data* — which is how §7.4 selects a family for a residual,
    #: rather than by fit to binned points that are themselves biased.
    log_likelihood: float
    #: Whether the dense path or the subset path ran (`adr/0011` rule 3).
    subset_used: bool
    n_samples: int
    starts_tried: int


def fit_reml(
    points: NDArray[np.float64],
    residuals: NDArray[np.float64],
    jacobian: NDArray[np.float64],
    start: FittedVariogram,
    *,
    model: str | None = None,
    rng: np.random.Generator | None = None,
    flags: FlagList | None = None,
) -> RemlResult:
    """Fit a variogram to the residual of a fitted trend.

    `jacobian` is `J = df/dtheta` at the current estimate — the design matrix for
    a linear trend, and its linearisation for a nonlinear one, which is what
    `adr/0011` means by profiling.

    `start` is the WLS fit, used for the first start and to scale the bounds.
    Passing it is not optional: REML needs somewhere sensible to begin, and the
    biased-but-cheap estimate is a good place.
    """
    coords = np.asarray(points, dtype=float)
    values = np.asarray(residuals, dtype=float).ravel()
    design = np.asarray(jacobian, dtype=float)

    if design.ndim == 1:
        design = design[:, None]
    if len(coords) != len(values) or len(design) != len(values):
        raise DegenerateInput(
            f"REML needs one residual and one Jacobian row per point; got "
            f"{len(coords)} points, {len(values)} residuals and {len(design)} "
            f"rows."
        )

    coords, values, design, subset_used = _maybe_subset(coords, values, design, rng, flags)

    family = model or start.model
    best: tuple[float, NDArray[np.float64]] | None = None
    tried = 0

    for range_scale, nugget_scale in STARTS:
        initial = _pack(start, range_scale, nugget_scale, values)
        tried += 1
        try:
            outcome = optimize.minimize(
                _negative_log_likelihood,
                initial,
                args=(coords, values, design, family, start),
                method="L-BFGS-B",
                bounds=_bounds(start, values),
            )
        except np.linalg.LinAlgError:
            # A start whose covariance is not positive definite. Not a failure
            # of the fit — a failure of that start, which is what having
            # several is for.
            continue
        if best is None or outcome.fun < best[0]:
            best = (float(outcome.fun), outcome.x)

    if best is None:
        raise DegenerateInput(
            "Every REML start failed to produce a positive-definite covariance. "
            "That usually means duplicated sample locations — resolve them in "
            "validation (`13` §5.1) — or a starting range far longer than the "
            "field."
        )

    fitted = _unpack(best[1], family, start)
    return RemlResult(
        variogram=fitted,
        log_likelihood=-best[0],
        subset_used=subset_used,
        n_samples=len(values),
        starts_tried=tried,
    )


def _maybe_subset(
    coords: NDArray[np.float64],
    values: NDArray[np.float64],
    design: NDArray[np.float64],
    rng: np.random.Generator | None,
    flags: FlagList | None,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], bool]:
    """Rule 3: below the limit fit everything, above it fit a subset.

    The subset is random rather than declustered here, because declustering
    needs the weights this function does not have — the caller declustered
    before fitting the trend, and a second, different subsample would be a
    second sampling decision nobody asked for. Which path ran is recorded either
    way, which is the part `adr/0011` insists on.
    """
    if len(values) <= DENSE_LIMIT:
        return coords, values, design, False

    generator = rng or np.random.default_rng(0)
    index = generator.choice(len(values), size=SUBSET_SIZE, replace=False)
    if flags is not None:
        flags.add(
            "TREND_SUBSET_FIT",
            f"The residual variogram was fitted by REML on {SUBSET_SIZE:,} of "
            f"{len(values):,} samples. A dense covariance at the full size is "
            f"{len(values) ** 2 * 8 / 1e9:.1f} GB and a Cholesky of it per "
            f"optimiser step; the fitted parameters are applied to every sample.",
            fitted_on=SUBSET_SIZE,
            total=len(values),
        )
    return coords[index], values[index], design[index], True


def _pack(
    start: FittedVariogram,
    range_scale: float,
    nugget_scale: float,
    values: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Parameters as the optimiser sees them: range, nugget fraction, sill.

    The nugget as a *fraction* of the sill rather than as a variance: the two
    are strongly correlated in the likelihood, and optimising them
    independently makes the surface a narrow diagonal valley that L-BFGS-B
    crawls along.
    """
    variance = float(np.var(values)) or 1.0
    fraction = np.clip(
        (start.nugget / start.sill if start.sill else 0.1) * nugget_scale,
        *NUGGET_FRACTION_BOUNDS,
    )
    return np.array([start.range_ * range_scale, fraction, variance], dtype=float)


def _bounds(start: FittedVariogram, values: NDArray[np.float64]) -> list[tuple[float, float]]:
    variance = float(np.var(values)) or 1.0
    return [
        (start.range_ * RANGE_BOUNDS[0], start.range_ * RANGE_BOUNDS[1]),
        NUGGET_FRACTION_BOUNDS,
        (variance * 0.01, variance * 100.0),
    ]


def _unpack(
    parameters: NDArray[np.float64], family: str, start: FittedVariogram
) -> FittedVariogram:
    range_, fraction, sill = parameters
    return replace(
        start,
        model=family,
        range_=float(range_),
        sill=float(sill),
        nugget=float(sill * fraction),
        fit_residual=0.0,
    )


def _negative_log_likelihood(
    parameters: NDArray[np.float64],
    coords: NDArray[np.float64],
    values: NDArray[np.float64],
    design: NDArray[np.float64],
    family: str,
    start: FittedVariogram,
) -> float:
    """The REML objective. `adr/0011`'s formula, term for term.

    **One Cholesky serves all three terms and `C^-1` is never formed** — rule 1.
    Forming the inverse costs a second O(n^3) and loses the numerical stability
    the factorisation gives; `cho_solve` gives every product the formula needs.
    """
    model = _unpack(parameters, family, start)
    covariance = _covariance_matrix(coords, model)

    try:
        factor = cho_factor(covariance, lower=True, check_finite=False)
    except np.linalg.LinAlgError:
        return float(np.inf)

    # log|C| from the factorisation's diagonal: the determinant of a triangular
    # matrix is the product of its diagonal, so this is a sum of logs rather
    # than a determinant that overflows at a few hundred samples.
    log_det_c = 2.0 * float(np.sum(np.log(np.diag(factor[0]))))

    solved_design = cho_solve(factor, design, check_finite=False)
    information = design.T @ solved_design
    try:
        information_factor = cho_factor(information, lower=True, check_finite=False)
    except np.linalg.LinAlgError:
        # `J' C^-1 J` singular: the trend is unidentified at this covariance.
        # Infinite rather than raised, so the optimiser walks away from it
        # instead of the whole fit failing.
        return float(np.inf)

    log_det_information = 2.0 * float(np.sum(np.log(np.diag(information_factor[0]))))

    solved_values = cho_solve(factor, values, check_finite=False)
    # P r, without forming P: the generalised residual after projecting out
    # what the trend can explain.
    coefficients = cho_solve(information_factor, design.T @ solved_values, check_finite=False)
    quadratic = float(values @ solved_values - (design.T @ solved_values) @ coefficients)

    return 0.5 * (log_det_c + log_det_information + quadratic)


def _covariance_matrix(
    coords: NDArray[np.float64], model: FittedVariogram
) -> NDArray[np.float64]:
    """`C` for these points under this model.

    A small ridge on the diagonal: with a nugget of nearly zero and two samples
    a metre apart, the matrix is positive definite in theory and not in floating
    point, and the failure is a `LinAlgError` from inside the optimiser rather
    than anything about variograms.
    """
    separation = coords[:, None, :] - coords[None, :, :]
    distance = np.hypot(separation[..., 0], separation[..., 1])
    covariance = model.covariance(distance)
    ridge = 1e-8 * max(model.sill, 1.0)
    return np.asarray(covariance + np.eye(len(coords)) * ridge, dtype=np.float64)


def select_model_by_reml(
    points: NDArray[np.float64],
    residuals: NDArray[np.float64],
    jacobian: NDArray[np.float64],
    start: FittedVariogram,
    *,
    families: tuple[str, ...] = ("spherical", "exponential", "gaussian", "matern"),
    rng: np.random.Generator | None = None,
    flags: FlagList | None = None,
) -> RemlResult:
    """Choose the family by REML likelihood, not by fit to binned points.

    §7.4 and `adr/0011`: choosing Matérn over exponential because it hugs the
    empirical points better is choosing on a statistic that is itself biased
    here. The likelihoods are comparable because the data and the design matrix
    are the same across families — only the covariance changes.
    """
    best: RemlResult | None = None
    for family in families:
        try:
            candidate = fit_reml(
                points, residuals, jacobian, start, model=family, rng=rng, flags=flags
            )
        except DegenerateInput:
            continue
        if best is None or candidate.log_likelihood > best.log_likelihood:
            best = candidate

    if best is None:
        raise DegenerateInput(
            f"No variogram family could be fitted to this residual by REML. "
            f"Tried: {', '.join(families)}. Check for duplicated sample "
            f"locations, which make the covariance singular for every family."
        )
    return best


__all__ = [
    "DENSE_LIMIT",
    "STARTS",
    "SUBSET_SIZE",
    "RemlResult",
    "fit_reml",
    "select_model_by_reml",
]
