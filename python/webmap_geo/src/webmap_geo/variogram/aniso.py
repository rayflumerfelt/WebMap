"""Anisotropy, and whether to believe it. `13-kriging.md` §7.6.

**The ellipse is not the hard part. Deciding it is real is.** Directional
variograms on four azimuths always produce four different ranges, because four
subsets of the same pairs always differ — so an ellipse fitted to them always
exists, always has a ratio above 1, and always has an azimuth. Quote that
azimuth on a map and it reads as a measurement of the field's geology.

So §7.6 requires a null: permute the values across the locations, refit, and see
how often chance alone produces a ratio this large. The permutation destroys any
spatial structure while keeping the sampling geometry and the value distribution
exactly — which is what makes it the right null. A clustered survey along a
river valley produces directional ranges that differ for reasons that have
nothing to do with the field, and the permutation reproduces exactly that
artefact.

**This replaces the "detect anisotropy in 8 azimuths" behaviour in `05` §6.3**,
which accepts whatever ellipse comes back.

The p-value goes in the lineage record and on screen, because an anisotropy
azimuth quoted without one looks like a measurement rather than a hypothesis.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.flags import FlagList
from webmap_geo.variogram.experimental import estimate_experimental
from webmap_geo.variogram.fit import fit

#: Azimuths tested, degrees clockwise from north. §7.6's fixed set; the 22.5°
#: offsets are added where the data supports them.
BASE_AZIMUTHS = (0.0, 45.0, 90.0, 135.0)
FINE_AZIMUTHS = (22.5, 67.5, 112.5, 157.5)

#: Pairs per direction below which a directional variogram is noise. Splitting
#: 200 points across eight azimuths leaves too few pairs per lag to fit
#: anything, and the ellipse that comes back is a picture of the sampling.
MIN_PAIRS_PER_DIRECTION = 200

#: Permutations for the null. 99 gives a p-value resolution of 0.01, which is
#: as fine as a decision between "use the ellipse" and "do not" needs.
PERMUTATIONS = 99

#: Below this p-value the ratio is accepted as real. Conventional, and the
#: conventional number is right here because the cost of a wrong "yes" — a
#: quoted azimuth that is an artefact — is higher than a wrong "no", which is
#: only a slightly less sharp map.
SIGNIFICANCE = 0.05

#: Below this ratio the anisotropy is not worth modelling whatever its p-value:
#: a 1.2:1 ellipse changes a kriged surface less than the choice of model
#: family does.
MEANINGFUL_RATIO = 1.3


@dataclass(frozen=True)
class AnisotropyResult:
    """What the directional analysis found, and whether to use it."""

    ratio: float
    azimuth: float
    #: Fraction of permutations whose ratio was at least this large.
    p_value: float
    significant: bool
    #: Range fitted per azimuth, for the rose diagram (`07` §9.5).
    ranges: dict[float, float]

    @property
    def use(self) -> bool:
        """Whether the estimator should apply this ellipse."""
        return self.significant and self.ratio >= MEANINGFUL_RATIO


def detect_anisotropy(
    points: NDArray[np.float64],
    values: NDArray[np.float64],
    rng: np.random.Generator,
    *,
    flags: FlagList | None = None,
    permutations: int = PERMUTATIONS,
    azimuth_tolerance: float = 22.5,
) -> AnisotropyResult:
    """Fit an anisotropy ellipse and test it against a permutation null.

    The `Generator` is required, per `CLAUDE.md` §3.3 — the null is random, and
    §4.6 wants the seed in the lineage record so the p-value is reproducible.
    """
    azimuths = _azimuths_for(points)
    ranges = _directional_ranges(points, values, azimuths, azimuth_tolerance)

    if len(ranges) < 2:
        result = AnisotropyResult(
            ratio=1.0, azimuth=0.0, p_value=1.0, significant=False, ranges=ranges
        )
        _flag(flags, result, "too few directions had enough pairs to compare")
        return result

    ratio, azimuth = _ellipse_from(ranges)

    null = _null_ratios(points, values, rng, azimuths, azimuth_tolerance, permutations)
    # `>=` and the +1: the observed value is one of the possible arrangements,
    # so a p-value that could reach zero would claim more certainty than a
    # permutation test can give.
    at_least = int(np.sum(null >= ratio))
    p_value = (at_least + 1) / (len(null) + 1)

    result = AnisotropyResult(
        ratio=ratio,
        azimuth=azimuth,
        p_value=float(p_value),
        significant=bool(p_value < SIGNIFICANCE),
        ranges=ranges,
    )

    if not result.use:
        reason = (
            f"p = {p_value:.2f}, which is not below {SIGNIFICANCE}"
            if not result.significant
            else f"the ratio is {ratio:.2f}, below the {MEANINGFUL_RATIO} worth modelling"
        )
        _flag(flags, result, reason)
    return result


def _flag(flags: FlagList | None, result: AnisotropyResult, reason: str) -> None:
    if flags is None:
        return
    flags.add(
        "ANISO_NOT_SIGNIFICANT",
        f"Directional variograms suggested {result.ratio:.2f}:1 anisotropy at "
        f"{result.azimuth:.0f}°, but {reason}. An isotropic model is used. "
        f"Four subsets of the same pairs always differ, so an ellipse always "
        f"exists — quoting its azimuth without the test makes an artefact look "
        f"like a measurement.",
        ratio=result.ratio,
        azimuth=result.azimuth,
        p_value=result.p_value,
    )


def _azimuths_for(points: NDArray[np.float64]) -> tuple[float, ...]:
    """Four azimuths, or eight where the data supports them.

    §7.6 adds the 22.5° offsets "where data density allows". The rule: eight
    directions need twice the pairs to say the same thing, and a rose built on
    too few is a picture of where the wells are.
    """
    count = len(points)
    pairs = count * (count - 1) // 2
    if pairs >= MIN_PAIRS_PER_DIRECTION * len(BASE_AZIMUTHS + FINE_AZIMUTHS):
        return BASE_AZIMUTHS + FINE_AZIMUTHS
    return BASE_AZIMUTHS


def _directional_ranges(
    points: NDArray[np.float64],
    values: NDArray[np.float64],
    azimuths: tuple[float, ...],
    tolerance: float,
) -> dict[float, float]:
    """The fitted range in each direction, skipping the ones with too few pairs."""
    ranges: dict[float, float] = {}
    for azimuth in azimuths:
        # `DegenerateInput` only: a direction with no pairs in it is a direction
        # with nothing to say, and skipping it is right. Catching everything
        # would hide a real failure — it hid a `TypeError` from a wrong call
        # here, and every direction came back empty with no explanation.
        try:
            experimental = estimate_experimental(
                points, values, azimuth=azimuth, azimuth_tolerance=tolerance
            )
        except DegenerateInput:
            continue
        if int(np.sum(experimental.counts)) < MIN_PAIRS_PER_DIRECTION:
            continue
        try:
            fitted = fit(experimental)
        except DegenerateInput:
            continue
        ranges[azimuth] = fitted.range_
    return ranges


def _ellipse_from(ranges: dict[float, float]) -> tuple[float, float]:
    """Ratio and azimuth from the per-direction ranges.

    The longest range is the major axis. Not a least-squares ellipse fit: with
    four or eight noisy ranges the fit is dominated by whichever direction
    happened to sample a lineament, and the argmax is both simpler and no worse
    — it is the same answer the fit lands on when the ellipse is real, and it
    does not manufacture an azimuth between two directions when it is not.
    """
    azimuth = max(ranges, key=lambda key: ranges[key])
    major = ranges[azimuth]
    minor = min(ranges.values())
    if minor <= 0:
        return 1.0, azimuth
    return float(major / minor), float(azimuth)


def _null_ratios(
    points: NDArray[np.float64],
    values: NDArray[np.float64],
    rng: np.random.Generator,
    azimuths: tuple[float, ...],
    tolerance: float,
    permutations: int,
) -> NDArray[np.float64]:
    """Ratios from values shuffled across the same locations.

    The permutation keeps the sampling geometry and the value distribution and
    destroys the spatial structure, which is exactly the null wanted: it asks
    "how anisotropic does this survey look when the field is not anisotropic at
    all?" A survey strung along a river valley answers "quite".
    """
    ratios = np.empty(permutations, dtype=np.float64)
    for index in range(permutations):
        shuffled = rng.permutation(values)
        ranges = _directional_ranges(points, shuffled, azimuths, tolerance)
        ratios[index] = _ellipse_from(ranges)[0] if len(ranges) >= 2 else 1.0
    return ratios


__all__ = [
    "BASE_AZIMUTHS",
    "FINE_AZIMUTHS",
    "MEANINGFUL_RATIO",
    "MIN_PAIRS_PER_DIRECTION",
    "PERMUTATIONS",
    "SIGNIFICANCE",
    "AnisotropyResult",
    "detect_anisotropy",
]
