"""Geoprocessing for WebMap: interpolation, contouring, aggregation, data plane.

Pure computation. No database, no HTTP, no framework imports — see
`01-architecture.md` §3.2 for why this isolation is worth the friction.

The version string is stamped into every lineage record
(`02-data-model.md` §3.10), so a stored grid can say which algorithm package
produced it. Bump it deliberately.
"""

from webmap_geo.frame import AnalysisFrame

__version__ = "0.1.0"

__all__ = ["AnalysisFrame", "__version__"]
