"""Format readers, writers, and connectors. `11-file-io.md`.

Connectors resolve and fetch; readers parse. Keeping the two separate means a
new format works across every connector, and a new connector works with every
format.
"""

from webmap_io._projenv import pin_proj_data

__version__ = "0.1.0"

# GDAL reads its coordinate database from ambient environment variables, and
# an unrelated PostGIS install on the same machine sets them. Pin them here,
# at package import, before any submodule imports rasterio. See _projenv.
pin_proj_data()
