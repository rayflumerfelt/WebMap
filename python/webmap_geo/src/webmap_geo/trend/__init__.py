"""Global trend estimation. `13-kriging.md` §8.

Stage 1 of regression kriging, shared by RK and RIK. One global coefficient
vector — the loop corrects the *weighting*, not the spatial variation, which is
the distinction §8.1 says the name obscures.
"""

from webmap_geo.trend.forms import ParsedExpression, TrendForm, parse_expression, preset
from webmap_geo.trend.gls import TrendFit, fit_trend, gls_fit, numeric_jacobian

__all__ = [
    "ParsedExpression",
    "TrendFit",
    "TrendForm",
    "fit_trend",
    "gls_fit",
    "numeric_jacobian",
    "parse_expression",
    "preset",
]
