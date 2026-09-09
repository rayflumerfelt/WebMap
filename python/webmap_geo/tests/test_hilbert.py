"""Property tests for the Hilbert ordering used by the ingest writer.

The ordering is load-bearing for tile performance (`11-file-io.md` §6.1) and
a wrong curve fails silently — it still produces *an* order, just one that
does not cluster, and the only symptom is tiles that read the whole layer.
So the defining properties are asserted directly rather than against a
golden array.
"""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from webmap_geo.exceptions import DegenerateInput
from webmap_geo.hilbert import _xy_to_d, hilbert_index

UNIT_BBOX = (0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize("order", [1, 2, 3, 4, 5])
def test_is_a_bijection_over_the_grid(order: int) -> None:
    """Every cell gets exactly one index, and the indices are 0..n^2-1.

    A curve that visits a cell twice is not a curve, and the failure mode is
    two distant features landing on the same sort key.
    """
    side = 1 << order
    gx, gy = np.meshgrid(np.arange(side), np.arange(side), indexing="ij")
    d = _xy_to_d(gx.ravel().astype(np.int64), gy.ravel().astype(np.int64), order)

    assert np.array_equal(np.sort(d), np.arange(side * side, dtype=np.uint64))


@pytest.mark.parametrize("order", [1, 2, 3, 4, 5])
def test_consecutive_indices_are_adjacent_cells(order: int) -> None:
    """The Hilbert property, and the whole reason it was chosen over Morton.

    Consecutive indices must differ by one step in exactly one axis. Morton
    order satisfies the bijection test above and fails this one at every
    quadrant boundary — which is precisely the jump that would scatter a
    row group across the layer.
    """
    side = 1 << order
    gx, gy = np.meshgrid(np.arange(side), np.arange(side), indexing="ij")
    xs = gx.ravel().astype(np.int64)
    ys = gy.ravel().astype(np.int64)
    order_of_visit = np.argsort(_xy_to_d(xs, ys, order))

    steps = np.abs(np.diff(xs[order_of_visit])) + np.abs(np.diff(ys[order_of_visit]))
    assert np.all(steps == 1), (
        f"order {order}: {(steps != 1).sum()} of {steps.size} consecutive "
        f"indices are not adjacent cells; the curve has jumps."
    )


@given(
    xs=st.lists(st.floats(-1e6, 1e6), min_size=2, max_size=200),
)
def test_ordering_is_deterministic(xs: list[float]) -> None:
    """Same input, same order. Ingest must be reproducible across runs."""
    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(xs[::-1], dtype=np.float64)
    bbox = (float(x.min()), float(y.min()), float(x.max()), float(y.max()))

    first = hilbert_index(x, y, bbox)
    second = hilbert_index(x, y, bbox)
    assert np.array_equal(first, second)


def test_nearby_points_get_nearby_indices() -> None:
    """The property row-group pruning actually depends on.

    Compares the median gap between consecutive sorted indices rather than
    the total span. Span is the wrong measure: any cluster straddles a
    quadrant boundary at *some* level, and one straddle at a coarse level
    inflates the span without saying anything about how tightly the rest
    packs. The median gap is what determines how many features share a row
    group, which is the quantity that matters.
    """
    rng = np.random.default_rng(20260908)
    clustered = hilbert_index(
        rng.normal(0.5, 0.005, 500), rng.normal(0.5, 0.005, 500), UNIT_BBOX
    )
    spread = hilbert_index(rng.uniform(0.0, 1.0, 500), rng.uniform(0.0, 1.0, 500), UNIT_BBOX)

    clustered_gap = np.median(np.diff(np.sort(clustered.astype(np.float64))))
    spread_gap = np.median(np.diff(np.sort(spread.astype(np.float64))))

    assert clustered_gap < spread_gap / 100


def test_degenerate_extent_does_not_divide_by_zero() -> None:
    """Every feature at one location. Meaningless order, but not a crash."""
    x = np.full(10, 500_000.0)
    y = np.full(10, 3_500_000.0)

    d = hilbert_index(x, y, (500_000.0, 3_500_000.0, 500_000.0, 3_500_000.0))

    assert np.array_equal(d, np.zeros(10, dtype=np.uint64))


def test_mismatched_arrays_name_both_lengths() -> None:
    with pytest.raises(DegenerateInput, match=r"3 coordinates and y has 4"):
        hilbert_index(np.zeros(3), np.zeros(4), UNIT_BBOX)
