"""The GLS step and the trend loop. `13-kriging.md` §8.1, §8.2, §8.4.

**The loop does not introduce spatial variation in theta.** It produces one
global coefficient vector, and what iterating corrects is the *weighting*:
without it, samples in dense clusters are effectively counted many times over,
so the fitted law bends toward whatever completion practice prevails where
people drilled most. It fixes a clustering-bias problem, not a spatial-variation
problem — §8.1 says the name suggests otherwise, and it does.

The four failure modes in §8.4 are the substance of this module. Each produces a
plausible answer if unhandled:

- **Oscillation.** Returning the last iterate would hand back whichever of two
  competing fits the loop happened to stop on. Falls back to declustered OLS
  with the trajectory attached, so the oscillation is visible.
- **An unidentified coefficient.** The map is fine and the coefficient is noise;
  §8.4 wants it named, which means naming the *smallest singular vector's*
  largest components rather than saying "the fit is ill conditioned".
- **A non-finite trend.** An error, not a warning: there is nothing to krige.
- **A non-monotone trend.** Legal and usually wrong — a completion response that
  turns over inside the data range is a fit chasing noise, and symbolic
  regression produces them constantly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy import optimize
from scipy.linalg import cho_factor, cho_solve, solve_triangular

from webmap_geo.flags import FlagList, raise_flag
from webmap_geo.trend.forms import CHECK_POINTS, TrendForm, validate_form

#: What the loop calls to get a covariance from the current residual: it takes
#: the coordinates, the residual and the Jacobian, and returns `C`. Injected
#: rather than imported so this module does not depend on the REML fitter — and
#: so a test can hand it an exact covariance and check the loop in isolation.
CovarianceFitter = Callable[
    [NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]],
    NDArray[np.float64],
]

#: §8.1's cap. Two to four iterations typically suffice; five without
#: convergence is an identifiability problem, not a numerical one.
MAX_ITERATIONS = 5

#: Relative change in theta below which the loop has converged.
TOLERANCE = 1e-4

#: Condition number of `J' C^-1 J` above which coefficients are unidentified.
#: Higher than the covariate-screening limit because this is the *fitted*
#: information matrix, where some correlation is expected and only an extreme
#: value means a coefficient is not determined by the data.
IDENTIFIABILITY_LIMIT = 1e8

#: Relative size a partial derivative must reach before a sign reversal counts
#: as non-monotone. Numerical noise around a flat response changes sign
#: constantly and means nothing.
MONOTONE_NOISE = 0.01


@dataclass
class TrendFit:
    """A fitted global trend, and everything a reader needs to judge it."""

    theta: NDArray[np.float64]
    #: `cov(theta)` from the final iterate — §8.2. Standard errors come from
    #: its diagonal, and §11.2's optional trend-uncertainty term needs all of it.
    covariance: NDArray[np.float64]
    residuals: NDArray[np.float64]
    iterations: int
    converged: bool
    method: str
    #: Every iterate, so an oscillation is visible rather than inferred.
    trajectory: list[NDArray[np.float64]] = field(default_factory=list)

    @property
    def standard_errors(self) -> NDArray[np.float64]:
        return np.sqrt(np.clip(np.diag(self.covariance), 0.0, None))


def gls_fit(
    values: NDArray[np.float64],
    design: NDArray[np.float64],
    form: TrendForm,
    covariance: NDArray[np.float64] | None,
    theta0: NDArray[np.float64],
    *,
    weights: NDArray[np.float64] | None = None,
) -> NDArray[np.float64]:
    """One whitened nonlinear least-squares step. §8.2.

    Whitening by the Cholesky factor is what makes this generalised least
    squares rather than ordinary: `L^-1 e` has identity covariance, so the
    ordinary sum of squares of *that* is the right objective. One factorisation
    per outer iteration.
    """
    factor = None
    if covariance is not None:
        try:
            factor = np.linalg.cholesky(covariance)
        except np.linalg.LinAlgError:
            # Not fatal: an un-whitened step is ordinary least squares, which is
            # §8.1's documented fallback. The loop reports the method it used.
            factor = None

    root_weights = np.sqrt(weights) if weights is not None else None

    def residual(theta: NDArray[np.float64]) -> NDArray[np.float64]:
        error = values - form.function(design, *theta)
        if root_weights is not None:
            error = error * root_weights
        if factor is None:
            return np.asarray(error, dtype=np.float64)
        return np.asarray(
            solve_triangular(factor, error, lower=True, check_finite=False),
            dtype=np.float64,
        )

    lower = np.array([bound[0] for bound in form.bounds], dtype=float)
    upper = np.array([bound[1] for bound in form.bounds], dtype=float)
    outcome = optimize.least_squares(residual, theta0, bounds=(lower, upper), method="trf")
    return np.asarray(outcome.x, dtype=np.float64)


def coefficient_covariance(
    design: NDArray[np.float64],
    form: TrendForm,
    theta: NDArray[np.float64],
    covariance: NDArray[np.float64] | None,
) -> NDArray[np.float64]:
    """`cov(theta) ~ (J' C^-1 J)^-1` from the final iterate. §8.2.

    Reported rather than optional: a coefficient without a standard error is a
    number people quote, and the whole argument of §12 is that the fitted law
    should be readable as a physical claim or not at all.
    """
    jacobian = numeric_jacobian(design, form, theta)
    if covariance is None:
        information = jacobian.T @ jacobian
    else:
        factor = cho_factor(covariance, lower=True, check_finite=False)
        information = jacobian.T @ cho_solve(factor, jacobian, check_finite=False)

    try:
        return np.asarray(np.linalg.inv(information), dtype=np.float64)
    except np.linalg.LinAlgError:
        # Singular information: at least one coefficient is not determined at
        # all. Pseudo-inverse rather than a raise, because the fit itself may
        # still be usable and §8.4 wants this reported as a warning.
        return np.asarray(np.linalg.pinv(information), dtype=np.float64)


def numeric_jacobian(
    design: NDArray[np.float64], form: TrendForm, theta: NDArray[np.float64]
) -> NDArray[np.float64]:
    """`J = df/dtheta` by central differences.

    Numerical rather than analytic because a trend can be a typed expression,
    and §8.3 requires an analytic Jacobian to be finite-difference checked
    anyway — so this is the check as well as the fallback.
    """
    base = np.asarray(theta, dtype=float)
    jacobian = np.zeros((len(design), len(base)), dtype=np.float64)

    for index in range(len(base)):
        step = 1e-6 * max(abs(base[index]), 1.0)
        forward = base.copy()
        backward = base.copy()
        forward[index] += step
        backward[index] -= step
        jacobian[:, index] = (
            form.function(design, *forward) - form.function(design, *backward)
        ) / (2.0 * step)
    return jacobian


def fit_trend(
    values: NDArray[np.float64],
    design: NDArray[np.float64],
    form: TrendForm,
    *,
    coords: NDArray[np.float64] | None = None,
    weights: NDArray[np.float64] | None = None,
    flags: FlagList | None = None,
    method: str = "gls",
    covariance_of: CovarianceFitter | None = None,
    max_iterations: int = MAX_ITERATIONS,
    tolerance: float = TOLERANCE,
) -> TrendFit:
    """§8.1's loop: residual, REML covariance, GLS, repeat.

    `method="ols"` runs the documented fallback — declustered ordinary least
    squares — which §8.1 says gets close on the point estimates and is what to
    use when the loop fails or when speed matters more than the standard errors.
    """
    theta = np.asarray(form.theta0, dtype=float)
    _require_finite(values, design, form, theta, flags)

    if method == "ols" or covariance_of is None or coords is None:
        theta = gls_fit(values, design, form, None, theta, weights=weights)
        residual = values - form.function(design, *theta)
        fit = TrendFit(
            theta=theta,
            covariance=coefficient_covariance(design, form, theta, None),
            residuals=residual,
            iterations=1,
            converged=True,
            method="ols",
            trajectory=[theta],
        )
        _check_identifiability(fit, design, form, flags)
        _check_monotonicity(fit, design, form, flags)
        return fit

    trajectory: list[NDArray[np.float64]] = []
    covariance: NDArray[np.float64] | None = None
    converged = False
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        residual = values - form.function(design, *theta)
        covariance = covariance_of(coords, residual, numeric_jacobian(design, form, theta))

        previous = theta
        theta = gls_fit(values, design, form, covariance, theta, weights=weights)
        trajectory.append(theta)

        if _relative_change(theta, previous) < tolerance:
            converged = True
            break

    if not converged:
        return _fallback(values, design, form, weights, trajectory, iteration, flags)

    fit = TrendFit(
        theta=theta,
        covariance=coefficient_covariance(design, form, theta, covariance),
        residuals=values - form.function(design, *theta),
        iterations=iteration,
        converged=True,
        method="gls",
        trajectory=trajectory,
    )
    _check_identifiability(fit, design, form, flags)
    _check_monotonicity(fit, design, form, flags)
    return fit


def _relative_change(theta: NDArray[np.float64], previous: NDArray[np.float64]) -> float:
    scale = np.maximum(np.abs(previous), 1e-12)
    return float(np.max(np.abs(theta - previous) / scale))


def _fallback(
    values: NDArray[np.float64],
    design: NDArray[np.float64],
    form: TrendForm,
    weights: NDArray[np.float64] | None,
    trajectory: list[NDArray[np.float64]],
    iterations: int,
    flags: FlagList | None,
) -> TrendFit:
    """§8.4: do **not** return the last iterate silently.

    The last iterate of an oscillating loop is whichever of two competing fits
    the cap happened to stop on. Declustered OLS is a defensible answer that
    does not depend on where the loop was interrupted, and the trajectory is
    attached so the oscillation can be seen rather than inferred.
    """
    theta = gls_fit(values, design, form, None, np.asarray(form.theta0, float), weights=weights)
    fit = TrendFit(
        theta=theta,
        covariance=coefficient_covariance(design, form, theta, None),
        residuals=values - form.function(design, *theta),
        iterations=iterations,
        converged=False,
        method="ols_fallback",
        trajectory=trajectory,
    )

    if flags is not None:
        flags.add(
            "TREND_NOT_CONVERGED",
            f"The trend loop did not converge in {iterations} iterations, so the "
            f"coefficients came from declustered ordinary least squares instead. "
            f"Failing to converge by {MAX_ITERATIONS} usually means an "
            f"identifiability problem — collinear covariates — rather than a "
            f"numerical one. The coefficient trajectory is attached.",
            iterations=iterations,
            trajectory=[list(map(float, step)) for step in trajectory],
        )
    _check_identifiability(fit, design, form, flags)
    _check_monotonicity(fit, design, form, flags)
    return fit


def _require_finite(
    values: NDArray[np.float64],
    design: NDArray[np.float64],
    form: TrendForm,
    theta: NDArray[np.float64],
    flags: FlagList | None,
) -> None:
    """§8.4: `f` returning NaN or Inf over the observed range is an ERROR.

    There is nothing to krige from a trend that cannot be evaluated, so this
    raises rather than warning.
    """
    del values
    output = validate_form(form, design, [float(value) for value in theta])
    bad = ~np.isfinite(output)
    if np.any(bad):
        offending = design[bad][:3]
        raise_flag(
            "TREND_NONFINITE",
            f"The trend '{form.name}' returns a non-finite value for "
            f"{int(bad.sum()):,} of {len(design):,} samples — the first few are at "
            f"covariates {offending.tolist()}. A log or a negative power of a "
            f"covariate that reaches zero is the usual cause.",
            count=int(bad.sum()),
        )
    del flags


def _check_identifiability(
    fit: TrendFit, design: NDArray[np.float64], form: TrendForm, flags: FlagList | None
) -> None:
    """§8.4: name *which* coefficients are unidentified.

    "The fit is ill conditioned" tells a geologist nothing they can act on. The
    smallest singular vector's largest components are the coefficients the data
    does not separate, and those are what to say.
    """
    if flags is None:
        return

    jacobian = numeric_jacobian(design, form, fit.theta)
    _, singular, right = np.linalg.svd(jacobian, full_matrices=False)
    if singular[-1] <= 0 or singular[0] / singular[-1] < IDENTIFIABILITY_LIMIT:
        return

    loading = np.abs(right[-1])
    labels = form.labels or tuple(f"theta_{index}" for index in range(len(fit.theta)))
    implicated = [
        labels[index] for index in np.argsort(loading)[::-1][:2] if index < len(labels)
    ]
    flags.add(
        "TREND_UNIDENTIFIED",
        f"The data does not separate {' and '.join(implicated)}: they enter the "
        f"fit in nearly the same way, so their individual values are noise even "
        f"though their combination is determined. The map is unaffected; the "
        f"coefficients should not be read individually. A form with fewer "
        f"covariates would be honest about the same information.",
        implicated=implicated,
        condition_number=float(singular[0] / max(singular[-1], 1e-300)),
    )


def _check_monotonicity(
    fit: TrendFit, design: NDArray[np.float64], form: TrendForm, flags: FlagList | None
) -> None:
    """§8.4: flag a trend that turns over inside the data.

    Evaluated per covariate on a grid with the others held at their median, as
    §8.4 specifies. A completion response that rises and then falls within the
    observed range is a fit chasing noise — and symbolic-regression expressions
    turn over inside the data range constantly.
    """
    if flags is None or design.shape[1] == 0:
        return

    medians = np.median(design, axis=0)
    reversed_in: list[int] = []

    for column in range(design.shape[1]):
        low, high = float(design[:, column].min()), float(design[:, column].max())
        if high <= low:
            continue
        probe = np.tile(medians, (CHECK_POINTS, 1))
        probe[:, column] = np.linspace(low, high, CHECK_POINTS)

        response = form.function(probe, *fit.theta)
        slope = np.diff(response)
        scale = float(np.max(np.abs(slope))) if slope.size else 0.0
        if scale <= 0:
            continue
        meaningful = slope[np.abs(slope) > MONOTONE_NOISE * scale]
        if meaningful.size and not (np.all(meaningful > 0) or np.all(meaningful < 0)):
            reversed_in.append(column)

    if not reversed_in:
        return

    labels = [f"covariate {index}" for index in reversed_in]
    flags.add(
        "TREND_NONMONOTONE",
        f"The fitted trend reverses direction in {', '.join(labels)} within the "
        f"observed range: more of it helps up to a point and then hurts. That can "
        f"be real, and far more often it is the form chasing noise at the edge of "
        f"the data. Check the response curve before quoting the coefficients.",
        covariates=reversed_in,
    )


__all__ = [
    "IDENTIFIABILITY_LIMIT",
    "MAX_ITERATIONS",
    "TOLERANCE",
    "TrendFit",
    "coefficient_covariance",
    "fit_trend",
    "gls_fit",
    "numeric_jacobian",
]
