"""Regression kriging. `13-kriging.md` §10.0, §6.

The first estimator that puts §8's trend and §11's kriging together, and the
first genuinely useful deliverable in `13` §19's build order.

    Z(x) = f(X(x); theta) + R(x)

The trend is global and the residual is kriged. Four steps: fit the trend by
GLS, fit the residual's variogram **by REML** (`adr/0011` — not least squares,
because a residual's variogram is biased), krige the residual, and add the trend
back at each node.

**Distinct from universal kriging, and callers conflate them** (§11.1). UK
solves trend and residual jointly per neighbourhood; RK fits one global trend
first and kriges what is left. The difference shows where the covariates are
strong: UK lets the drift vary locally, RK does not, and RK's global law is the
thing a geologist can read and argue with.

**Quantiles, not the mean, through a transform** (§6). A shift is monotone, so
the q-quantile of the residual's distribution plus the trend *is* the q-quantile
of the target's. A mean is not: `exp(E[log Z])` is not `E[Z]`, and the smearing
corrections rest on assumptions nobody checks. Where a mean is genuinely wanted,
integrate the recovered distribution.

**The known limitation is not hidden.** With a global trend, genuine regional
differences in covariate *response* are absorbed into the residual and kriged as
though they were spatial structure. §6 calls that an accepted approximation
rather than an oversight; §12.1's standardised-residual map is how a user sees
it, and splitting the field is a decision they make, never one this makes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from webmap_geo.flags import FlagList
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.kriging import ordinary_kriging
from webmap_geo.prep.transform import Identity, Transform
from webmap_geo.prep.validate import Samples
from webmap_geo.trend.forms import TrendForm
from webmap_geo.trend.gls import TrendFit, fit_trend, numeric_jacobian
from webmap_geo.variogram.experimental import estimate_experimental
from webmap_geo.variogram.fit import fit as fit_wls
from webmap_geo.variogram.fit_reml import fit_reml
from webmap_geo.variogram.model import FittedVariogram

#: nugget/sill above which §7.5 says there is little spatial structure to
#: exploit and the map will be close to the trend alone.
HIGH_NUGGET = 0.6


@dataclass
class RegressionKrigingResult:
    """A kriged surface, and everything needed to judge or reproduce it."""

    estimate: NDArray[np.float64]
    #: Residual kriging variance, per §11.2. The trend's contribution is *not*
    #: included by default — it is an approximation for a nonlinear `f`, and
    #: `include_trend_uncertainty` says so wherever it is on.
    variance: NDArray[np.float64]
    trend_surface: NDArray[np.float64]
    residual_surface: NDArray[np.float64]
    trend: TrendFit
    variogram: FittedVariogram
    flags: FlagList
    n_unestimated: int = 0
    trend_uncertainty_included: bool = False

    @property
    def unestimated_fraction(self) -> float:
        return self.n_unestimated / self.estimate.size if self.estimate.size else 0.0

    def lineage(self) -> dict[str, Any]:
        """§4.6: there is no run manifest — the lineage record is it."""
        return {
            "estimator": "rk",
            "trend": {
                "form": self.trend.method,
                "theta": [float(value) for value in self.trend.theta],
                "standard_errors": [float(value) for value in self.trend.standard_errors],
                "iterations": self.trend.iterations,
                "converged": self.trend.converged,
            },
            "variogram": {
                "model": self.variogram.model,
                "range": self.variogram.range_,
                "sill": self.variogram.sill,
                "nugget": self.variogram.nugget,
            },
            "trend_uncertainty_included": self.trend_uncertainty_included,
            "unestimated_fraction": self.unestimated_fraction,
            "flags": self.flags.as_list(),
        }


def regression_kriging(
    samples: Samples,
    design: NDArray[np.float64],
    node_covariates: NDArray[np.float64],
    grid: GridDefinition,
    form: TrendForm,
    *,
    transform: Transform | None = None,
    rng: np.random.Generator | None = None,
    flags: FlagList | None = None,
    include_trend_uncertainty: bool = False,
    max_neighbours: int = 48,
) -> RegressionKrigingResult:
    """§10.0's four steps.

    `design` is the covariate matrix at the samples and `node_covariates` the
    same columns at every grid node — the scenario being mapped. They are
    separate arguments because they are separate decisions: the first is what
    happened, the second is what is being asked about, and §5.4 flags the case
    where the second reaches outside the first.
    """
    flags = flags if flags is not None else FlagList()
    transform = transform or Identity()
    rng = rng or np.random.default_rng(0)

    # Step 0: into the space the model is fitted in. A transform's forward is
    # applied to the samples and its inverse to the *quantiles* at the end,
    # never to a mean (§6).
    values = transform.forward(samples.values)

    # Step 1: the global trend, by GLS with the declustering weights (§8.2).
    trend = fit_trend(
        values,
        design,
        form,
        coords=_coords(samples),
        weights=samples.weights,
        flags=flags,
        covariance_of=_reml_covariance(rng, flags),
    )

    # Step 2: the residual's variogram, by REML. `adr/0011`: a least-squares
    # variogram of a fitted residual underestimates sill and range, and inside
    # the loop that bias feeds itself.
    residual = trend.residuals
    variogram = _residual_variogram(samples, residual, design, form, trend, rng, flags)
    _check_nugget(variogram, flags)

    # Step 3: krige the residual. Ordinary rather than simple: the residual's
    # mean is zero by construction *globally*, and ordinary kriging re-estimates
    # it locally, which absorbs the small departures a global fit leaves behind.
    kriged = ordinary_kriging(
        _coords(samples), residual, grid, variogram, n_neighbors=max_neighbours
    )

    # Step 4: add the trend back at every node.
    trend_surface = form.function(node_covariates, *trend.theta).reshape(grid.ny, grid.nx)
    estimate = trend_surface + kriged.estimate

    variance = kriged.variance
    if include_trend_uncertainty:
        variance = variance + _trend_variance(node_covariates, form, trend, grid)

    if not isinstance(transform, Identity):
        # A quantile back-transforms exactly and a mean does not — so what comes
        # back is the *median* surface, which is the 0.5 quantile and is exact.
        estimate = transform.inverse(estimate)
        trend_surface = transform.inverse(trend_surface)

    return RegressionKrigingResult(
        estimate=estimate,
        variance=variance,
        trend_surface=trend_surface,
        residual_surface=kriged.estimate,
        trend=trend,
        variogram=variogram,
        flags=flags,
        n_unestimated=kriged.n_extrapolated,
        trend_uncertainty_included=include_trend_uncertainty,
    )


def _coords(samples: Samples) -> NDArray[np.float64]:
    return np.stack([samples.x, samples.y], axis=1)


def _reml_covariance(rng: np.random.Generator, flags: FlagList) -> Any:
    """The covariance fitter §8.1's loop calls each iteration.

    Closes over the generator and the flag list so the loop itself does not have
    to know about either — and so a test can substitute an exact covariance and
    check the loop in isolation.
    """

    def fitter(
        coords: NDArray[np.float64],
        residual: NDArray[np.float64],
        jacobian: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        start = fit_wls(estimate_experimental(coords, residual))
        model = fit_reml(coords, residual, jacobian, start, rng=rng, flags=flags).variogram
        separation = coords[:, None, :] - coords[None, :, :]
        distance = np.hypot(separation[..., 0], separation[..., 1])
        matrix = model.covariance(distance)
        # A ridge for the same reason as in the REML objective: positive
        # definite in theory, not always in floating point.
        return np.asarray(
            matrix + np.eye(len(coords)) * 1e-8 * max(model.sill, 1.0), dtype=np.float64
        )

    return fitter


def _residual_variogram(
    samples: Samples,
    residual: NDArray[np.float64],
    design: NDArray[np.float64],
    form: TrendForm,
    trend: TrendFit,
    rng: np.random.Generator,
    flags: FlagList,
) -> FittedVariogram:
    """The final residual variogram, by REML at the converged coefficients."""
    coords = _coords(samples)
    start = fit_wls(estimate_experimental(coords, residual))
    jacobian = numeric_jacobian(design, form, trend.theta)
    return fit_reml(coords, residual, jacobian, start, rng=rng, flags=flags).variogram


def _check_nugget(variogram: FittedVariogram, flags: FlagList) -> None:
    """§7.5's guard. Fit the nugget freely, then say what it means."""
    total = variogram.total_sill
    if total <= 0:
        return
    ratio = variogram.nugget / total
    if ratio <= HIGH_NUGGET:
        return

    flags.add(
        "HIGH_NUGGET",
        f"The residual's nugget is {ratio:.0%} of its sill, above the "
        f"{HIGH_NUGGET:.0%} at which there is little spatial structure left to "
        f"exploit. The kriged surface will be close to the trend alone, and the "
        f"map's detail comes from the covariates rather than from the geology "
        f"between wells. That is a legitimate answer; it is not the answer most "
        f"people assume they are getting from a kriged map.",
        nugget_fraction=float(ratio),
    )


def _trend_variance(
    node_covariates: NDArray[np.float64],
    form: TrendForm,
    trend: TrendFit,
    grid: GridDefinition,
) -> NDArray[np.float64]:
    """§11.2's optional delta-method term: `g' cov(theta) g`.

    Off by default and labelled approximate wherever reported, because for a
    nonlinear `f` the delta method is a first-order approximation and the whole
    point of reporting a variance is that somebody believes it.
    """
    gradient = numeric_jacobian(node_covariates, form, trend.theta)
    contribution = np.einsum("ij,jk,ik->i", gradient, trend.covariance, gradient, optimize=True)
    return np.asarray(
        np.clip(contribution, 0.0, None).reshape(grid.ny, grid.nx), dtype=np.float64
    )


__all__ = ["HIGH_NUGGET", "RegressionKrigingResult", "regression_kriging"]
