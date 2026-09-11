"""The estimator suite. `13-kriging.md` §11.1.

Each composes the pieces below it — preprocessing, variography, the kriging
engine, the trend — rather than reimplementing any of them. That is what keeps
two estimators of the same data from disagreeing for a reason nobody can find.
"""

from webmap_geo.estimators.rk import RegressionKrigingResult, regression_kriging

__all__ = ["RegressionKrigingResult", "regression_kriging"]
