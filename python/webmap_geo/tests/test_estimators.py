"""Simple and block kriging. `13-kriging.md` §11.1.

`CLAUDE.md` §6.1: analytic tests, because kriging has exact properties that can
be checked rather than eyeballed. An estimator that interpolates its own data,
a block estimate that is smoother than the point estimate it averages, and a
variance that is lowest at the control — all three are statements the maths
makes, and all three break silently.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from webmap_geo.flags import FlagList, GeostatError
from webmap_geo.frame import AnalysisFrame
from webmap_geo.grid import GridDefinition
from webmap_geo.interpolate.estimators import block_kriging, simple_kriging
from webmap_geo.interpolate.kriging import ordinary_kriging
from webmap_geo.variogram.model import FittedVariogram

TEXAS_CENTRAL = 2277
FRAME = AnalysisFrame(srid=TEXAS_CENTRAL, units="usft")

#: A well-behaved model: some nugget, a range a third of the field.
MODEL = FittedVariogram(
    model="spherical", nugget=0.1, sill=1.0, range_=3_000.0, n_pairs_used=100
)


def field(rng: np.random.Generator, count: int = 120) -> tuple[Any, Any]:
    """Scattered samples of a smooth field over a 10,000 ft square."""
    points = rng.uniform(0.0, 10_000.0, size=(count, 2))
    values = np.sin(points[:, 0] / 2_000.0) + np.cos(points[:, 1] / 2_500.0)
    return points, values


def grid(nx: int = 25, ny: int = 25, cell: float = 400.0) -> GridDefinition:
    return GridDefinition(xmin=0.0, ymin=0.0, cell_size=cell, nx=nx, ny=ny, frame=FRAME)


class TestSimpleKriging:
    def test_reproduces_a_value_at_its_own_control_point(self) -> None:
        """The exactness property. With a nugget the estimator is no longer an
        exact interpolator, so this is checked with a nugget of zero — where the
        maths says the estimate at a sample *is* that sample."""
        rng = np.random.default_rng(3)
        points, values = field(rng, 60)
        exact = FittedVariogram(model="spherical", nugget=0.0, sill=1.0, range_=4_000.0)

        # A one-cell grid centred on the first control point.
        # The smallest grid the definition allows, with its south-west cell
        # centred on the control point.
        target = GridDefinition(
            xmin=float(points[0, 0]),
            ymin=float(points[0, 1]),
            cell_size=10.0,
            nx=2,
            ny=2,
            frame=FRAME,
        )
        result = simple_kriging(
            points, values, target, exact, mean=float(values.mean()), min_neighbours=1
        )

        # Row 0 is the north, so the south-west cell — the one on the control
        # point — is the last row's first column.
        assert result.estimate[-1, 0] == pytest.approx(values[0], abs=1e-6)

    def test_falls_back_to_the_mean_far_from_any_control(self) -> None:
        """What distinguishes it from ordinary kriging: the mean is known, so
        beyond the range the estimate is that mean rather than a local average
        of whatever happens to be nearest."""
        points = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
        values = np.array([5.0, 5.0, 5.0])
        far = GridDefinition(
            xmin=50_000.0, ymin=50_000.0, cell_size=100.0, nx=2, ny=2, frame=FRAME
        )

        result = simple_kriging(
            points,
            values,
            far,
            MODEL,
            mean=0.0,
            min_neighbours=1,
            max_radius=1e9,
        )

        assert result.estimate[0, 0] == pytest.approx(0.0, abs=0.2)

    def test_a_sparse_node_is_unestimated_not_extrapolated(self) -> None:
        """§11.0: below `min_n` neighbours the node is left empty. A mean drawn
        across a gap looks like data and is not."""
        points = np.array([[0.0, 0.0], [100.0, 0.0]])
        values = np.array([1.0, 2.0])

        result = simple_kriging(
            points, values, grid(3, 3, 500.0), MODEL, mean=1.5, min_neighbours=8
        )

        assert np.isnan(result.estimate).all()
        assert result.n_extrapolated == 9

    def test_the_variance_is_lowest_where_the_control_is(self) -> None:
        """The property that makes the variance worth drawing: it says where the
        surface is interpolated and where it is invented."""
        rng = np.random.default_rng(5)
        points, values = field(rng, 80)

        result = simple_kriging(
            points, values, grid(), MODEL, mean=float(values.mean()), min_neighbours=1
        )

        centres = grid().cell_centres()
        from scipy.spatial import cKDTree

        distance, _ = cKDTree(points).query(centres)
        variance = result.variance.ravel()
        usable = np.isfinite(variance)

        near = variance[usable][distance[usable] < 300.0]
        far = variance[usable][distance[usable] > 1_500.0]
        assert near.size and far.size
        assert float(near.mean()) < float(far.mean())


class TestBlockKriging:
    def test_is_smoother_than_the_point_estimate(self) -> None:
        """A block average has less variability than the point values it
        averages, by the block's own dispersion variance. That smoothing is
        exactly what a volumetric calculation should have."""
        rng = np.random.default_rng(7)
        points, values = field(rng, 150)
        target = grid(15, 15, 800.0)

        point = ordinary_kriging(points, values, target, MODEL)
        block = block_kriging(points, values, target, MODEL, min_neighbours=1)

        both = np.isfinite(point.estimate) & np.isfinite(block.estimate)
        assert float(np.nanstd(block.estimate[both])) <= float(np.nanstd(point.estimate[both]))

    def test_has_a_lower_variance_than_the_point_estimate(self) -> None:
        """The block is better known than any point inside it, which is the
        reason to use it when the quantity reported is per unit area."""
        rng = np.random.default_rng(11)
        points, values = field(rng, 150)
        target = grid(15, 15, 800.0)

        point = ordinary_kriging(points, values, target, MODEL)
        block = block_kriging(points, values, target, MODEL, min_neighbours=1)

        both = np.isfinite(point.variance) & np.isfinite(block.variance)
        assert float(np.nanmean(block.variance[both])) < float(np.nanmean(point.variance[both]))

    def test_agrees_with_the_point_estimate_on_a_tiny_block(self) -> None:
        """A block far smaller than the range is a point, and the two estimators
        must agree there — otherwise the discretisation is doing something other
        than averaging."""
        rng = np.random.default_rng(13)
        points, values = field(rng, 100)
        tiny = GridDefinition(
            xmin=2_000.0, ymin=2_000.0, cell_size=1.0, nx=3, ny=3, frame=FRAME
        )

        point = ordinary_kriging(points, values, tiny, MODEL)
        block = block_kriging(points, values, tiny, MODEL, min_neighbours=1)

        assert block.estimate == pytest.approx(point.estimate, rel=1e-3)

    def test_refuses_an_indicator_and_explains_the_error(self) -> None:
        """A block average of a 0/1 field is a proportion, and feeding that into
        an indicator CDF gives a plausible map and a wrong volume."""
        points = np.array([[0.0, 0.0], [500.0, 0.0], [0.0, 500.0]])
        values = np.array([0.0, 1.0, 1.0])

        with pytest.raises(GeostatError, match="not an exceedance probability"):
            block_kriging(points, values, grid(2, 2), MODEL, is_indicator=True)

    def test_a_simple_block_krige_uses_the_known_mean(self) -> None:
        points = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
        values = np.array([5.0, 5.0, 5.0])
        far = GridDefinition(
            xmin=50_000.0, ymin=50_000.0, cell_size=100.0, nx=2, ny=2, frame=FRAME
        )

        result = block_kriging(
            points, values, far, MODEL, mean=0.0, min_neighbours=1, max_radius=1e9
        )

        assert result.estimate[0, 0] == pytest.approx(0.0, abs=0.2)


class TestSingularSystems:
    def test_counts_them_once_rather_than_flagging_each_node(self) -> None:
        """§11.1: one flag with a count. A flag per node would bury every other
        flag on a grid with a few thousand duplicate-adjacent samples."""
        rng = np.random.default_rng(17)
        points, values = field(rng, 40)
        # Exact duplicates: two rows of the covariance matrix are identical.
        points = np.vstack([points, points])
        values = np.concatenate([values, values])
        flags = FlagList()

        simple_kriging(
            points,
            values,
            grid(6, 6, 1_000.0),
            FittedVariogram(model="spherical", nugget=0.0, sill=1.0, range_=5_000.0),
            mean=0.0,
            min_neighbours=1,
            flags=flags,
        )

        singular = [item for item in flags if item.code == "SINGULAR_SYSTEM"]
        assert len(singular) <= 1
        if singular:
            assert singular[0].detail is not None
            assert singular[0].detail["count"] >= 1

    def test_produces_finite_estimates_anyway(self) -> None:
        """The fallback is least squares, not a NaN — a NaN here spreads through
        contouring and turns one bad node into a hole in the map."""
        points = np.array([[0.0, 0.0], [0.0, 0.0], [1_000.0, 0.0], [0.0, 1_000.0]])
        values = np.array([1.0, 1.0, 2.0, 3.0])

        result = simple_kriging(
            points,
            values,
            GridDefinition(xmin=0.0, ymin=0.0, cell_size=500.0, nx=2, ny=2, frame=FRAME),
            FittedVariogram(model="spherical", nugget=0.0, sill=1.0, range_=5_000.0),
            mean=2.0,
            min_neighbours=1,
        )

        assert np.isfinite(result.estimate).all()
