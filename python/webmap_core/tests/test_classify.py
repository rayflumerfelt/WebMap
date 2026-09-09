"""Class-break computation. `08-styling-palettes.md` §4.

The property under test throughout is the one the compiler and the legend both
depend on: `classify()` returns exactly `n_classes - 1` strictly ascending
breaks, for every method and every distribution. Everything else is a detail
of one method; that invariant is what stops a 5-class map getting a 4-entry
legend.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from webmap_core.style.classify import (
    ClassificationError,
    classify,
    jenks_breaks,
    pretty_breaks,
    std_dev_breaks,
)

RNG = np.random.default_rng(20260908)

CONTINUOUS_METHODS = [
    "equal_interval",
    "quantile",
    "natural_breaks",
    "standard_deviation",
    "pretty",
]


# --- the invariant ----------------------------------------------------------


@pytest.mark.parametrize("method", CONTINUOUS_METHODS)
@pytest.mark.parametrize("n_classes", [2, 3, 5, 7, 12])
def test_every_method_returns_one_fewer_break_than_classes(method: str, n_classes: int) -> None:
    values = RNG.normal(loc=12.0, scale=3.5, size=2_000)

    breaks = classify(values, method, n_classes)

    assert len(breaks) == n_classes - 1


@pytest.mark.parametrize("method", CONTINUOUS_METHODS)
def test_breaks_ascend_strictly(method: str) -> None:
    """They compile to a MapLibre `step`, whose stops must ascend or the whole
    style is rejected — a blank map rather than a wrong one."""
    values = RNG.gamma(shape=2.0, scale=4.0, size=2_000)

    breaks = classify(values, method, 6)

    assert breaks == sorted(breaks)
    assert len(set(breaks)) == len(breaks)


@given(
    values=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=30,
        max_size=300,
    ),
    n_classes=st.integers(min_value=2, max_value=8),
)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_equal_interval_and_pretty_hold_the_invariant_for_any_distribution(
    values: list[float], n_classes: int
) -> None:
    """Hypothesis over the two methods that are pure functions of the range.

    Quantile and natural breaks legitimately refuse a degenerate distribution —
    twenty identical values cannot be split six ways — so the property they
    hold is "returns n-1 breaks or explains why not", asserted separately.
    """
    array = np.asarray(values, dtype=float)
    if float(array.min()) == float(array.max()):
        return

    for method in ("equal_interval", "pretty"):
        breaks = classify(array, method, n_classes)
        assert len(breaks) == n_classes - 1
        assert breaks == sorted(breaks)


# --- pretty -----------------------------------------------------------------


def test_pretty_reproduces_the_specified_porosity_example() -> None:
    """`08-styling-palettes.md` §4, verbatim: 4.1–21.8 into 5 classes gives
    5, 10, 15, 20 — not 7.64, 11.18, 14.72, 18.26."""
    assert pretty_breaks(4.1, 21.8, 5) == [5, 10, 15, 20]


@pytest.mark.parametrize(
    ("vmin", "vmax", "n_classes"),
    [
        (0.0, 100.0, 5),
        (0.0, 1.0, 4),
        (-50.0, 50.0, 5),
        (8_200.0, 9_600.0, 8),  # TVDSS in feet
        (0.02, 0.31, 6),  # a fraction rather than a percentage
    ],
)
def test_pretty_breaks_are_round_numbers(vmin: float, vmax: float, n_classes: int) -> None:
    """A geologist reads these off a legend. The test of "round" used here is
    that the gap between breaks has at most three significant digits, which
    admits 2.5 and 0.25 while rejecting 7.64."""
    breaks = pretty_breaks(vmin, vmax, n_classes)

    assert len(breaks) == n_classes - 1
    gap = breaks[1] - breaks[0]
    assert float(f"{gap:.3g}") == pytest.approx(gap, rel=1e-9)


def test_pretty_breaks_stay_inside_the_data_range() -> None:
    """A break at or beyond an end opens a class that matches nothing, which
    renders as a legend entry for an empty set."""
    for vmin, vmax, n in ((4.1, 21.8, 5), (0.0, 100.0, 5), (9.0, 11.0, 5), (-3.0, 7.0, 4)):
        breaks = pretty_breaks(vmin, vmax, n)
        assert all(vmin < b < vmax for b in breaks), (vmin, vmax, n, breaks)


def test_pretty_breaks_carry_no_floating_point_dust() -> None:
    """`3 * 0.1` is `0.30000000000000004`. A legend reading that instead of
    `0.3` defeats the entire purpose of this method."""
    breaks = pretty_breaks(0.0, 1.0, 10)

    assert all(repr(b) == repr(round(b, 6)) for b in breaks), breaks


# --- natural breaks ---------------------------------------------------------


def test_jenks_finds_the_gap_between_two_populations() -> None:
    """The reason this method exists.

    Two facies with distinct net-to-gross give a bimodal attribute. Jenks must
    put its boundary in the empty space between them; equal-interval would put
    it wherever the arithmetic lands, which is usually inside one of the modes.
    """
    low = RNG.normal(loc=10.0, scale=1.0, size=500)
    high = RNG.normal(loc=40.0, scale=1.0, size=500)
    values = np.concatenate([low, high])

    (boundary,) = jenks_breaks(values, 2)

    # Asserted against the data rather than against a midpoint: the whole gap
    # between the modes is empty, so every boundary in it describes the same
    # partition. The one returned is the first value of the upper class, which
    # is what `step` semantics require — so it sits at the bottom of the gap,
    # not in the middle of it.
    assert float(low.max()) < boundary <= float(high.min())
    assert boundary == pytest.approx(float(high.min()))


def test_jenks_beats_equal_interval_on_within_class_variance() -> None:
    """The definition of the method, asserted as a property rather than against
    a reference implementation: Fisher-Jenks is the *optimal* partition by
    within-class sum of squares, so it cannot lose to any other partition."""
    values = np.concatenate(
        [RNG.normal(5, 1, 300), RNG.normal(9, 0.5, 200), RNG.normal(30, 2, 400)]
    )

    def within_class_ssd(breaks: list[float]) -> float:
        edges = [-np.inf, *breaks, np.inf]
        total = 0.0
        for low, high in itertools.pairwise(edges):
            members = values[(values >= low) & (values < high)]
            if members.size:
                total += float(np.sum((members - members.mean()) ** 2))
        return total

    jenks = within_class_ssd(classify(values, "natural_breaks", 3))
    equal = within_class_ssd(classify(values, "equal_interval", 3))

    assert jenks <= equal


def test_jenks_breaks_are_values_that_occur_in_the_data() -> None:
    """A break is the first value of its class, so a feature sitting exactly on
    a boundary is filed upward — matching the `step` expression it compiles to."""
    values = RNG.normal(0.0, 1.0, 800)

    breaks = jenks_breaks(values, 4)

    assert all(np.isclose(values, b).any() for b in breaks)


def test_jenks_is_deterministic_and_independent_of_row_order() -> None:
    """**Why the subsample is systematic rather than seeded-random.**

    The same layer re-read from a differently sorted Parquet file must produce
    the same legend. A seeded `default_rng(0)` gives the first property and not
    the second, because the draw depends on the order it draws from.
    """
    values = RNG.normal(50.0, 12.0, 20_000)
    shuffled = RNG.permutation(values)

    assert jenks_breaks(values, 5) == jenks_breaks(shuffled, 5)
    assert jenks_breaks(values, 5) == jenks_breaks(values.copy(), 5)


def test_jenks_subsamples_a_large_layer_quickly() -> None:
    """250k values is an ordinary well set. Exact Fisher-Jenks on it is O(n²k)
    and would take hours; the cap is what makes the method offerable at all."""
    values = RNG.normal(0.0, 1.0, 250_000)

    breaks = jenks_breaks(values, 5)

    assert len(breaks) == 4


def test_jenks_refuses_more_classes_than_distinct_values() -> None:
    with pytest.raises(ClassificationError, match="Cannot form"):
        jenks_breaks(np.array([1.0, 2.0, 3.0]), 5)


# --- standard deviation -----------------------------------------------------


def test_std_dev_breaks_are_symmetric_about_the_mean() -> None:
    """What makes this the method for anomaly maps: the reader is being shown
    how unusual a value is, not where it sits in the range."""
    values = RNG.normal(loc=100.0, scale=10.0, size=5_000)
    mean = float(np.mean(values))

    breaks = std_dev_breaks(values, 5)

    offsets = [b - mean for b in breaks]
    assert offsets[0] == pytest.approx(-offsets[-1])
    assert offsets[1] == pytest.approx(-offsets[-2])


def test_std_dev_breaks_step_by_whole_standard_deviations() -> None:
    values = RNG.normal(loc=0.0, scale=4.0, size=5_000)
    sigma = float(np.std(values))

    breaks = std_dev_breaks(values, 6)

    gaps = np.diff(breaks)
    assert np.allclose(gaps, sigma)


# --- refusals ---------------------------------------------------------------


def test_an_all_nan_attribute_names_the_blanking_value() -> None:
    """The likely cause, so the message points at it. A Surfer grid arrives
    full of 1.70141e38 and NaN outside its blanking polygon."""
    with pytest.raises(ClassificationError, match="blanking value"):
        classify(np.full(100, np.nan), "quantile", 5)


def test_a_constant_attribute_suggests_a_single_symbol() -> None:
    with pytest.raises(ClassificationError, match="single symbol"):
        classify(np.full(100, 7.5), "equal_interval", 5)


def test_a_concentrated_attribute_refuses_rather_than_repeating_a_break() -> None:
    """Repeated breaks compile to a `step` with non-ascending stops, which
    MapLibre rejects — so the map goes blank and nothing says why. Caught here
    instead, with the two ways out named."""
    values = np.concatenate([np.zeros(900), np.array([1.0, 2.0, 3.0])])

    with pytest.raises(ClassificationError, match="too concentrated"):
        classify(values, "quantile", 5)


def test_a_single_class_is_refused_with_a_pointer_to_single_symbol() -> None:
    with pytest.raises(ClassificationError, match="single-symbol"):
        classify(RNG.normal(size=100), "quantile", 1)


def test_an_unknown_method_lists_the_known_ones() -> None:
    with pytest.raises(ClassificationError, match="natural_breaks"):
        classify(RNG.normal(size=100), "kmeans", 5)


def test_manual_classification_says_where_the_breaks_come_from() -> None:
    with pytest.raises(ClassificationError, match=r"Graduated\.breaks"):
        classify(RNG.normal(size=100), "manual", 5)


def test_non_finite_values_are_dropped_not_classified() -> None:
    """Infinities in an attribute are a data defect, not a class boundary."""
    values = np.concatenate([RNG.normal(10.0, 2.0, 500), [np.nan, np.inf, -np.inf]])

    breaks = classify(values, "equal_interval", 4)

    assert all(np.isfinite(breaks))
    assert breaks == classify(values[:-3], "equal_interval", 4)
