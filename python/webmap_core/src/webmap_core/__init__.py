"""Shared domain code: models, permissions, settings, style compilation.

Imported by `webmap-api` and `webmap-worker`. Imports `webmap_geo` but never
the other way round — see `adr/0003-geoprocessing-owns-crs.md`.
"""

__version__ = "0.1.0"
