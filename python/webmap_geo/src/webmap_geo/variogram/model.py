"""Variogram models. `05-geoprocessing.md` §6.3.

A variogram model is the statement "points this far apart differ by about this
much". Kriging weights come entirely from it, so **kriging without variogram
analysis is kriging with made-up parameters** — and the made-up answer looks
exactly as plausible as the real one, which is what makes it dangerous.

The three parameters have geological meaning, which is why they are named
rather than fitted anonymously (`CLAUDE.md` §13):

- **nugget** — variance at zero lag. Measurement noise plus real variability
  below the sample spacing. A nugget that is most of the sill says the data
  cannot support interpolation at this scale.
- **sill** — the plateau. Total variance; beyond the range, points are
  uncorrelated.
- **range** — the lag at which the sill is reached. The distance over which
  the property is spatially organised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput

#: The model families worth offering. Each says something different about how
#: a property behaves at short lags, which is where kriging weights are
#: decided:
#:
#: - `spherical` reaches the sill exactly at the range. The default in mining
#:   and the one most geologists picture.
#: - `exponential` approaches it asymptotically — a rougher surface, common
#:   for porosity and permeability.
#: - `gaussian` is smooth at the origin, which suits structure but produces an
#:   ill-conditioned kriging system without a nugget.
MODELS = ("spherical", "exponential", "gaussian", "power", "matern", "stable")

#: Matérn smoothness. ν = 0.5 is exponential and ν → ∞ is Gaussian, so the
#: default sits between them: smoother than an exponential, without the
#: Gaussian's infinitely differentiable field, which produces a surface that
#: looks more confident near the data than the data supports.
DEFAULT_MATERN_NU = 1.5

#: Stable exponent. 2 is Gaussian and 1 is exponential; 1.5 is the usual
#: compromise for a surface with some short-range smoothness.
DEFAULT_STABLE_ALPHA = 1.5


@dataclass(frozen=True)
class FittedVariogram:
    """A fitted model, with everything needed to reproduce it.

    `05` §9 requires a lineage record sufficient to re-run and reproduce an
    identical grid. That means the fit itself has to be reportable, not just
    its output — hence the residual and the pair count, which say how much to
    trust it.
    """

    model: str
    nugget: float
    sill: float
    range_: float
    #: Ratio of major to minor axis. 1.0 is isotropic.
    anisotropy_ratio: float = 1.0
    #: Azimuth of the major axis, degrees clockwise from north.
    anisotropy_angle: float = 0.0
    fit_residual: float = 0.0
    n_pairs_used: int = 0
    #: Matérn smoothness ν, or the stable exponent α. Ignored by the other
    #: models. One field rather than two because a model has at most one shape
    #: parameter and two would let a caller set the wrong one silently.
    shape: float | None = None
    #: Additional structures, each `(model, partial_sill, range)`. §7.2's nested
    #: variograms: a short structure for the facies and a long one for the
    #: regional trend is the ordinary case, not an exotic one.
    nested: tuple[tuple[str, float, float], ...] = ()

    def __post_init__(self) -> None:
        if self.model not in MODELS:
            raise DegenerateInput(
                f"Unknown variogram model '{self.model}'. Available models: "
                f"{', '.join(MODELS)}."
            )
        if self.nugget < 0 or self.sill < 0:
            raise DegenerateInput(
                f"A variogram cannot have a negative nugget ({self.nugget:g}) or "
                f"sill ({self.sill:g}) — both are variances."
            )
        if self.range_ <= 0:
            raise DegenerateInput(
                f"Variogram range must be positive; got {self.range_:g}. A range "
                f"of zero says every pair of points is uncorrelated, which makes "
                f"kriging equivalent to assigning the global mean everywhere."
            )
        if self.anisotropy_ratio < 1.0:
            raise DegenerateInput(
                f"Anisotropy ratio is major/minor and so is at least 1; got "
                f"{self.anisotropy_ratio:g}. To express a shorter range in the "
                f"stated azimuth, rotate the azimuth by 90 degrees instead."
            )

    @property
    def total_sill(self) -> float:
        """Every structure's contribution plus the nugget.

        The number a nugget-to-sill ratio should be taken against: with a nested
        model, `sill` is the first structure's plateau and not the variance the
        surface actually reaches.
        """
        return self.sill + sum(partial for _, partial, _ in self.nested)

    @property
    def partial_sill(self) -> float:
        """Sill above the nugget. The part that varies with distance."""
        return max(0.0, self.sill - self.nugget)

    @property
    def nugget_fraction(self) -> float:
        """How much of the total variance is unstructured.

        Above about 0.5 the data barely supports interpolation: most of the
        variability happens below the sample spacing, and a kriged surface
        will be close to flat with high variance everywhere. Worth reporting
        rather than hiding.
        """
        return self.nugget / self.sill if self.sill > 0 else 0.0

    def gamma(self, h: NDArray[np.floating]) -> NDArray[np.float64]:
        """Semivariance at lag distance `h`.

        `gamma(0) == 0` exactly, for every model. The nugget is a *limit* as
        h approaches zero, not a value at zero — and a kriging system built
        with gamma(0) = nugget on its diagonal is singular, which surfaces as
        a LinAlgError deep inside the solver rather than as anything about
        variograms.
        """
        lag = np.asarray(h, dtype=float)
        result = np.empty_like(lag)

        # The distinction that keeps the kriging matrix invertible.
        at_zero = lag <= 0.0
        positive = ~at_zero
        result[at_zero] = 0.0

        if not np.any(positive):
            return result

        far = lag[positive]
        scaled = far / self.range_
        partial = self.partial_sill

        structured = _structure(self.model, far, self.range_, partial, self.shape)
        for model, nested_sill, nested_range in self.nested:
            structured = structured + _structure(model, far, nested_range, nested_sill, None)

        result[positive] = self.nugget + structured
        return result

    def covariance(self, h: NDArray[np.floating]) -> NDArray[np.float64]:
        """Covariance at lag `h`. `C(h) = sill - gamma(h)`.

        Kriging is solved in covariance form rather than semivariance form
        because the matrix is then positive definite, which lets a Cholesky
        factorisation catch a degenerate system as a clean failure instead of
        returning nonsense from a general solver.
        """
        return np.asarray(self.sill - self.gamma(h), dtype=np.float64)

    def describe(self) -> str:
        """One line for a caption or a lineage record.

        The form `04-mcp-server.md` §6.1 shows in a render's metadata, so a
        figure caption can state the parameters without anyone re-deriving
        them from the image.
        """
        text = (
            f"{self.model} variogram (range {self.range_:,.0f}, "
            f"nugget {self.nugget:.3g}, sill {self.sill:.3g})"
        )
        if self.anisotropy_ratio > 1.05:
            text += (
                f", anisotropy {self.anisotropy_ratio:.1f}:1 at {self.anisotropy_angle:03.0f}°"
            )
        return text


def _structure(
    model: str,
    lag: NDArray[np.float64],
    range_: float,
    partial: float,
    shape: float | None,
) -> NDArray[np.float64]:
    """One structure's contribution to the semivariance.

    Split out because a nested variogram is a sum of these, and `13` §7.2 makes
    nesting ordinary rather than exotic — a short structure for the facies and a
    long one for the regional trend is what most real surfaces look like.
    """
    scaled = lag / range_

    if model == "spherical":
        return np.asarray(
            np.where(scaled >= 1.0, partial, partial * (1.5 * scaled - 0.5 * scaled**3)),
            dtype=np.float64,
        )
    if model == "exponential":
        # 3/range so that `range_` means the *practical* range — the lag at
        # 95% of the sill. Without the 3, "range" would mean the e-folding
        # distance, which is a third as far and would silently make every
        # fitted range look three times too short.
        return np.asarray(partial * (1.0 - np.exp(-3.0 * scaled)), dtype=np.float64)
    if model == "gaussian":
        return np.asarray(partial * (1.0 - np.exp(-3.0 * scaled**2)), dtype=np.float64)
    if model == "matern":
        return _matern(scaled, partial, shape if shape is not None else DEFAULT_MATERN_NU)
    if model == "stable":
        alpha = shape if shape is not None else DEFAULT_STABLE_ALPHA
        return np.asarray(
            partial * (1.0 - np.exp(-3.0 * np.power(scaled, alpha))), dtype=np.float64
        )

    # power: no sill, variance grows without bound. Legitimate for a surface
    # with regional trend, and the reason `sill` is read as a scale factor here
    # rather than as a plateau.
    return np.asarray(partial * np.power(scaled, min(range_, 1.99)), dtype=np.float64)


def _matern(scaled: NDArray[np.float64], partial: float, nu: float) -> NDArray[np.float64]:
    """Matérn semivariance, with ν free.

    The family the other models are corners of: ν = 0.5 is exponential, ν → ∞ is
    Gaussian. It earns its place because ν *is* the smoothness of the field, and
    a geologist choosing between "exponential" and "Gaussian" is really choosing
    between two points on this axis without being able to say where between
    them the surface actually sits.

    Evaluated through `scipy.special.kv`, which is undefined at zero — handled
    by the caller's `h > 0` mask, and again here for the scaled lag, because a
    nested structure can reach this with a lag that rounds to zero.
    """
    from scipy import special

    # The scaling that keeps `range_` meaning the practical range across ν, so
    # switching model families does not silently change what "range" means.
    factor = np.sqrt(2.0 * nu) * 3.0 * scaled
    correlation = np.ones_like(scaled)

    positive = factor > 0
    if np.any(positive):
        value = factor[positive]
        correlation[positive] = (
            np.power(2.0, 1.0 - nu)
            / special.gamma(nu)
            * np.power(value, nu)
            * special.kv(nu, value)
        )
    # `kv` underflows to zero for a large argument, which is the right limit;
    # it can also return NaN there, which is not.
    correlation = np.nan_to_num(correlation, nan=0.0)
    return np.asarray(partial * (1.0 - correlation), dtype=np.float64)


def anisotropy_transform(ratio: float, angle_deg: float) -> NDArray[np.float64]:
    """The 2x2 matrix that maps coordinates into the variogram's frame.

    Applied before any distance is measured, so a neighbourhood search becomes
    elliptical rather than circular and lag distances respect the direction of
    continuity. Rotate so the major axis lies along x, then compress y by the
    ratio — after which an isotropic distance in the transformed frame is the
    anisotropic distance in the original one.

    **The azimuth is clockwise from north**, which is how a geologist states a
    structural trend, and it is not the mathematical convention. 035° means
    north-northeast; in the usual counter-clockwise-from-east frame that is
    055°. Getting this backwards produces a variogram whose major axis is
    across the structural grain rather than along it — and the resulting map
    looks plausible, which is the problem.
    """
    if ratio < 1.0:
        raise DegenerateInput(
            f"Anisotropy ratio is major/minor and so is at least 1; got {ratio:g}."
        )

    # Azimuth (CW from north) to the standard CCW-from-east angle.
    theta = math.radians(90.0 - angle_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    rotation = np.array([[cos_t, sin_t], [-sin_t, cos_t]], dtype=np.float64)
    scaling = np.array([[1.0, 0.0], [0.0, ratio]], dtype=np.float64)
    return np.asarray(scaling @ rotation, dtype=np.float64)


__all__ = [
    "DEFAULT_MATERN_NU",
    "DEFAULT_STABLE_ALPHA",
    "MODELS",
    "FittedVariogram",
    "anisotropy_transform",
]
