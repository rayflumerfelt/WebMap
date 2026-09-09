"""The CRS context threaded through orchestration.

A thin wrapper over `webmap_geo.crs`, which owns pyproj
(`adr/0003-geoprocessing-owns-crs.md`). This module holds the *validation*
that belongs at the boundary preparing arrays for analysis; it deliberately
does not re-implement transformation.

Three CRS roles, and conflating them is the most likely source of silently
wrong output in this system (`02-data-model.md` §1):

  storage   the CRS the data arrived in, preserved on ingest
  analysis  the projected CRS all geoprocessing runs in
  display   always EPSG:3857, applied at the last step
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from webmap_geo import crs as geo_crs

# Re-exported by the module that owns pyproj. Naming the type here without
# importing pyproj is what keeps adr/0003's "exactly one module" true — the
# rule is about ownership of the dependency, not about never naming its types.
from webmap_geo.crs import Transformer
from webmap_geo.frame import AnalysisFrame

WGS84 = geo_crs.WGS84
WEB_MERCATOR = geo_crs.WEB_MERCATOR


@dataclass(frozen=True)
class CrsContext:
    """Explicit CRS context threaded through every geoprocessing call.

    Constructing this is the only sanctioned way to obtain a transformer.
    Direct pyproj use outside `webmap_geo.crs` is an import-linter error.
    """

    storage_srid: int
    analysis_srid: int

    def __post_init__(self) -> None:
        if geo_crs.is_geographic(self.analysis_srid):
            raise ValueError(
                f"analysis_srid={self.analysis_srid} is geographic. "
                "Analysis requires a projected CRS — distances and areas in "
                "degrees are not meaningful. Choose a UTM zone or State Plane "
                "zone appropriate to the data extent."
            )

    @property
    def storage_to_analysis(self) -> Transformer:
        return geo_crs.transformer(self.storage_srid, self.analysis_srid)

    @property
    def analysis_to_storage(self) -> Transformer:
        return geo_crs.transformer(self.analysis_srid, self.storage_srid)

    @property
    def frame(self) -> AnalysisFrame:
        """The frame to hand `webmap_geo` entry points.

        Metadata declaring what the arrays are already in. It never causes a
        transformation — this context is what performs those, at the boundary.
        """
        return geo_crs.frame_for(self.analysis_srid)

    def to_analysis(
        self, x: NDArray[np.float64], y: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Storage -> analysis. One of the two defined reprojection boundaries."""
        return geo_crs.transform_points(x, y, self.storage_srid, self.analysis_srid)

    def bbox_4326_to_analysis(
        self, bbox: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        """EPSG:4326 bbox -> analysis-CRS bounds.

        `GridSpec.bbox` is documented in EPSG:4326 (`02-data-model.md` §5)
        while `cell_size` is in analysis-CRS units, so this conversion is
        unavoidable and is named as legitimate in `05-geoprocessing.md` §2.2.
        """
        return geo_crs.transform_bbox(bbox, WGS84, self.analysis_srid)
