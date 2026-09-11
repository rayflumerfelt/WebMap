"""Target transforms. `13-kriging.md` §5.3.

Three transforms and a null one, all exposing `forward`, `inverse` and
`is_monotone` — which is always `True`, and that is the property that matters.
Monotone means the transform preserves order, which means **a back-transformed
quantile is exact**: the 90th percentile of the transformed distribution maps to
the 90th percentile of the original, whatever the transform did in between.

**A back-transformed mean is not exact**, and §6 is emphatic about it. The mean
of a log-normal is not the exponential of the mean of its logs — it is that
times `exp(σ²/2)` — so a kriged mean in log space back-transformed naively is
biased low, systematically, everywhere. That is why the estimators that matter
here work in quantiles and why this module offers no `inverse_mean`: the
correction depends on the kriging variance, which is the estimator's to know,
not the transform's.

**Normal-score is required for any Gaussian comparison run** (SGS especially),
because the whole method assumes a Gaussian field and the field is not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import stats

from webmap_geo.exceptions import DegenerateInput

#: Offset for a log transform when the data touches zero. A fraction of the
#: smallest positive value rather than a constant: on a porosity column in
#: fractions and a rate column in barrels, one constant cannot be small for both.
LOG_OFFSET_FRACTION = 0.5


class Transform(ABC):
    """A monotone map from the target's units to the space kriging happens in."""

    name: str

    @abstractmethod
    def forward(self, values: NDArray[np.float64]) -> NDArray[np.float64]: ...

    @abstractmethod
    def inverse(self, values: NDArray[np.float64]) -> NDArray[np.float64]: ...

    @property
    def is_monotone(self) -> bool:
        """Always true, and the reason quantiles back-transform exactly."""
        return True

    def parameters(self) -> dict[str, float]:
        """What the lineage record needs to reproduce this transform."""
        return {}


class Identity(Transform):
    name = "none"

    def forward(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        return values

    def inverse(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        return values


@dataclass
class Log(Transform):
    """Natural log, with an offset when the data reaches zero."""

    offset: float = 0.0
    name: str = "log"

    def forward(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        shifted = values + self.offset
        if np.any(shifted <= 0):
            raise DegenerateInput(
                f"A log transform needs positive values; {int((shifted <= 0).sum())} "
                f"sample(s) are at or below zero even after the offset of "
                f"{self.offset:g}. Use 'boxcox', which handles zeros, or 'nscore', "
                f"which handles anything."
            )
        return np.log(shifted)

    def inverse(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.exp(values) - self.offset

    def parameters(self) -> dict[str, float]:
        return {"offset": self.offset}


@dataclass
class BoxCox(Transform):
    """Box-Cox, with λ by profile likelihood on the declustered data.

    Fitted on the *declustered* values, per §5.2: a λ chosen against the naive
    distribution is a λ chosen against the drilling pattern.
    """

    lam: float
    offset: float = 0.0
    name: str = "boxcox"

    def forward(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        shifted = values + self.offset
        if np.any(shifted <= 0):
            raise DegenerateInput(
                "A Box-Cox transform needs positive values; the offset did not "
                "lift every sample above zero. Use 'nscore'."
            )
        if self.lam == 0.0:
            return np.log(shifted)
        return (np.power(shifted, self.lam) - 1.0) / self.lam

    def inverse(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        if self.lam == 0.0:
            return np.exp(values) - self.offset
        base = self.lam * values + 1.0
        # Negative bases are outside the transform's range — they arise when a
        # kriged value in transformed space falls below what any real value
        # maps to. Clipped rather than allowed to produce NaN: the honest
        # answer is "the smallest value this transform can represent".
        base = np.clip(base, a_min=np.finfo(float).tiny, a_max=None)
        return np.power(base, 1.0 / self.lam) - self.offset

    def parameters(self) -> dict[str, float]:
        return {"lambda": self.lam, "offset": self.offset}


@dataclass
class NormalScore(Transform):
    """Normal-score, with the forward and inverse tables kept.

    The tables *are* the transform: a normal-score map is defined by the data it
    was built from, and rebuilding it from a different sample gives a different
    map. §4.6 therefore needs them in the lineage record for a run to be
    reproducible.

    Ties are broken by averaging their scores, so two identical values stay
    identical — the alternative separates them arbitrarily, and a kriging that
    treats two equal measurements as different is one nobody can explain.
    """

    sorted_values: NDArray[np.float64]
    sorted_scores: NDArray[np.float64]
    name: str = "nscore"

    def forward(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.interp(values, self.sorted_values, self.sorted_scores)

    def inverse(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.interp(values, self.sorted_scores, self.sorted_values)

    def parameters(self) -> dict[str, float]:
        return {"table_size": float(self.sorted_values.size)}


def build_transform(
    kind: str | None,
    values: NDArray[np.float64],
    weights: NDArray[np.float64] | None = None,
    *,
    lam: float | None = None,
) -> Transform:
    """The transform named, fitted to these values where it needs fitting."""
    if kind in (None, "none"):
        return Identity()
    if kind == "log":
        return Log(offset=_log_offset(values))
    if kind == "boxcox":
        offset = _log_offset(values)
        return BoxCox(
            lam=lam if lam is not None else _fit_lambda(values + offset), offset=offset
        )
    if kind == "nscore":
        return _fit_nscore(values, weights)
    raise DegenerateInput(
        f"'{kind}' is not a target transform. Available: none, log, boxcox, nscore."
    )


def _log_offset(values: NDArray[np.float64]) -> float:
    """Enough to lift the data above zero, and no more.

    Zero when the data is already positive — an offset applied unnecessarily
    changes the shape of the distribution for no reason.
    """
    smallest = float(np.min(values))
    if smallest > 0:
        return 0.0
    positive = values[values > 0]
    step = float(np.min(positive)) if positive.size else 1.0
    return abs(smallest) + step * LOG_OFFSET_FRACTION


def _fit_lambda(values: NDArray[np.float64]) -> float:
    """λ by profile likelihood. SciPy's, which is the same estimator."""
    lam = float(stats.boxcox_normmax(values, method="mle"))
    # Clipped to the range where the transform is interpretable. Outside it the
    # likelihood surface is flat and the fitted value is noise, and a λ of 7 on
    # a porosity column is a transform nobody can read.
    return float(np.clip(lam, -2.0, 2.0))


def _fit_nscore(
    values: NDArray[np.float64], weights: NDArray[np.float64] | None
) -> NormalScore:
    """Build the forward table from the declustered distribution."""
    weights = np.ones_like(values) if weights is None else weights
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    sorted_weights = weights[order]

    cumulative = np.cumsum(sorted_weights) - 0.5 * sorted_weights
    cumulative /= sorted_weights.sum()
    scores = stats.norm.ppf(np.clip(cumulative, 1e-6, 1 - 1e-6))

    # Ties share a score, so two identical measurements stay identical.
    unique_values, index = np.unique(sorted_values, return_inverse=True)
    averaged = np.zeros(unique_values.size)
    np.add.at(averaged, index, scores)
    counts = np.bincount(index, minlength=unique_values.size)
    averaged /= counts

    return NormalScore(sorted_values=unique_values, sorted_scores=averaged)


__all__ = [
    "LOG_OFFSET_FRACTION",
    "BoxCox",
    "Identity",
    "Log",
    "NormalScore",
    "Transform",
    "build_transform",
]
