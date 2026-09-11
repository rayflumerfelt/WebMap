"""Covariate screening. `13-kriging.md` §5.4.

Three automatic checks. **All produce flags; none modify data** — §5.4 is
explicit, and the reason is that every one of them is a judgement a geologist
may legitimately overrule. Proppant and fluid intensity really are chosen
together, and a model that uses both is not wrong; it is a model whose
coefficients should not be read individually. Dropping a covariate because it
correlated with another would be the software deciding that on their behalf.

The three failures these catch all look like success:

**Collinearity** gives a fitted "law" whose coefficients change between runs on
the same data. The map looks fine. The physical story the coefficients tell is
noise.

**A spatial proxy** — vintage, operator, anything that is really a label for
*where* — lets the trend absorb the spatial signal the kriging was supposed to
model. The result fits beautifully and predicts nothing new, because the trend
has learned the map rather than the mechanism.

**Extrapolation** is the model being asked about covariate values it never saw.
It answers confidently. §12.2's covariate-spread map exists because that
confidence is the thing most likely to be believed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.flags import FlagList
from webmap_geo.prep.validate import Samples

#: Condition number above which the design matrix is unstable. 30 is the
#: conventional line; above it the smallest singular value is small enough that
#: the coefficient vector turns on noise.
CONDITION_LIMIT = 30.0

#: Variance inflation above which a covariate is substantially explained by the
#: others. 10 is conventional and coarse, which is right for a warning.
VIF_LIMIT = 10.0

#: |correlation| with a coordinate above which a covariate is behaving like a
#: spatial label rather than a physical driver.
SPATIAL_CORRELATION_LIMIT = 0.7

#: Fraction of scenario nodes outside the covariate hull that is worth a flag.
EXTRAPOLATION_LIMIT = 0.05


@dataclass(frozen=True)
class ScreenResult:
    """Numbers behind the flags, for the diagnostics panel."""

    condition_number: float
    vif: dict[str, float]
    spatial_correlation: dict[str, float]
    extrapolated_fraction: float


def screen(
    samples: Samples,
    flags: FlagList,
    *,
    covariates: list[str] | None = None,
    scenario: dict[str, NDArray[np.float64]] | None = None,
) -> ScreenResult:
    """Run the three checks and raise what they find."""
    names = covariates if covariates is not None else sorted(samples.covariates)
    columns = [samples.covariates[name] for name in names if name in samples.covariates]

    if not columns:
        return ScreenResult(
            condition_number=1.0, vif={}, spatial_correlation={}, extrapolated_fraction=0.0
        )

    design = _standardise(np.stack(columns, axis=1))
    condition = _condition_number(design)
    vif = _vif(design, names)
    spatial = _spatial_correlation(samples, names)
    extrapolated = _extrapolation(samples, names, scenario)

    _flag_collinearity(flags, names, condition, vif)
    _flag_spatial(flags, spatial)
    _flag_extrapolation(flags, extrapolated)

    return ScreenResult(
        condition_number=condition,
        vif=vif,
        spatial_correlation=spatial,
        extrapolated_fraction=extrapolated,
    )


def _standardise(design: NDArray[np.float64]) -> NDArray[np.float64]:
    """Centre and scale each column.

    The condition number of an unstandardised design says more about the units
    than about the data — a column in feet beside one in fractions is ill
    conditioned by construction, and that is not the collinearity this is
    looking for.
    """
    centred = design - design.mean(axis=0)
    scale = centred.std(axis=0)
    scale[scale == 0] = 1.0
    return centred / scale


def _condition_number(design: NDArray[np.float64]) -> float:
    singular = np.linalg.svd(design, compute_uv=False)
    if singular.size == 0 or singular[-1] == 0:
        return float("inf")
    return float(singular[0] / singular[-1])


def _vif(design: NDArray[np.float64], names: list[str]) -> dict[str, float]:
    """Variance inflation per covariate: how well the others predict it."""
    vif: dict[str, float] = {}
    for index, name in enumerate(names):
        others = np.delete(design, index, axis=1)
        if others.shape[1] == 0:
            vif[name] = 1.0
            continue
        target = design[:, index]
        coefficients, *_ = np.linalg.lstsq(others, target, rcond=None)
        residual = target - others @ coefficients
        total = float(np.sum((target - target.mean()) ** 2))
        if total == 0:
            vif[name] = float("inf")
            continue
        r_squared = 1.0 - float(np.sum(residual**2)) / total
        vif[name] = float("inf") if r_squared >= 1.0 else 1.0 / (1.0 - r_squared)
    return vif


def _spatial_correlation(samples: Samples, names: list[str]) -> dict[str, float]:
    """The strongest correlation each covariate has with a coordinate.

    A cheap proxy for §5.4's fuller test — which also compares each covariate's
    own variogram range against the target's. The correlation catches the
    common case, which is a covariate that trends across the field: vintage
    increasing eastward as development moved, or an operator holding one block.
    """
    correlation: dict[str, float] = {}
    for name in names:
        column = samples.covariates[name]
        if np.std(column) == 0:
            correlation[name] = 0.0
            continue
        with np.errstate(invalid="ignore"):
            against_x = abs(float(np.corrcoef(column, samples.x)[0, 1]))
            against_y = abs(float(np.corrcoef(column, samples.y)[0, 1]))
        correlation[name] = float(np.nanmax([against_x, against_y]))
    return correlation


def _extrapolation(
    samples: Samples,
    names: list[str],
    scenario: dict[str, NDArray[np.float64]] | None,
) -> float:
    """Fraction of scenario nodes outside the covariate range.

    A bounding box rather than a convex hull in more than two dimensions: the
    hull is what §5.4 asks for and is exact, but a hull in five covariates on
    fifty thousand nodes is expensive for a number used as a warning threshold.
    The box is conservative in the direction that matters — it under-reports,
    so a flag it raises is always real.
    """
    if not scenario:
        return 0.0

    outside = None
    for name in names:
        values = scenario.get(name)
        if values is None:
            continue
        column = samples.covariates[name]
        beyond = (values < column.min()) | (values > column.max())
        outside = beyond if outside is None else (outside | beyond)

    if outside is None:
        return 0.0
    return float(np.mean(outside))


def _flag_collinearity(
    flags: FlagList, names: list[str], condition: float, vif: dict[str, float]
) -> None:
    if condition <= CONDITION_LIMIT and all(value <= VIF_LIMIT for value in vif.values()):
        return

    implicated = (
        sorted(
            (name for name, value in vif.items() if value > VIF_LIMIT),
            key=lambda name: -vif[name],
        )
        or names
    )
    flags.add(
        "COVAR_COLLINEAR",
        f"The covariates are collinear (condition number {condition:,.0f}, limit "
        f"{CONDITION_LIMIT:.0f}); {', '.join(implicated)} "
        f"{'carry' if len(implicated) > 1 else 'carries'} much the same "
        f"information. The map will be fine and the individual coefficients will "
        f"not — they change between runs on the same data, so do not read the "
        f"fitted law as a physical story. Drop one, or combine them into a single "
        f"index.",
        condition_number=condition,
        vif={name: round(value, 2) for name, value in vif.items()},
        implicated=implicated,
    )


def _flag_spatial(flags: FlagList, spatial: dict[str, float]) -> None:
    proxies = {
        name: value for name, value in spatial.items() if value > SPATIAL_CORRELATION_LIMIT
    }
    if not proxies:
        return

    flags.add(
        "COVAR_SPATIAL_PROXY",
        f"{', '.join(sorted(proxies))} "
        f"{'are' if len(proxies) > 1 else 'is'} strongly correlated with position "
        f"(|r| up to {max(proxies.values()):.2f}), which makes "
        f"{'them' if len(proxies) > 1 else 'it'} effectively a label for *where* "
        f"rather than a driver of *what*. The trend will absorb the spatial "
        f"signal the kriging was meant to model: the fit improves and the "
        f"prediction does not. Check the variance budget (§12.4) before trusting "
        f"the coefficients.",
        correlation={name: round(value, 3) for name, value in proxies.items()},
    )


def _flag_extrapolation(flags: FlagList, fraction: float) -> None:
    if fraction <= EXTRAPOLATION_LIMIT:
        return

    flags.add(
        "COVAR_EXTRAPOLATION",
        f"{fraction:.0%} of the mapped nodes have covariate values outside the "
        f"range the model was fitted on. The model will answer for them, and it "
        f"will answer confidently — the covariate-spread map (§12.2) shows where. "
        f"Treat those areas as untested rather than as predicted.",
        fraction=fraction,
    )


__all__ = [
    "CONDITION_LIMIT",
    "EXTRAPOLATION_LIMIT",
    "SPATIAL_CORRELATION_LIMIT",
    "VIF_LIMIT",
    "ScreenResult",
    "screen",
]
