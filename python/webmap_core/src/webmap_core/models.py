"""The API and MCP contract. `02-data-model.md` §5.

Pydantic for anything crossing a boundary. `extra="forbid"` is deliberate:
Claude supplies these parameters, and a silently-ignored typo produces a map
built with defaults the geologist did not ask for and cannot see.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Visibility is the permission model's vocabulary, defined once there so the
# resolution rule and the wire contract cannot drift apart.
from webmap_core.permissions import Visibility

__all__ = [
    "AttributeField",
    "Bbox",
    "DatasetDetail",
    "DatasetKind",
    "DatasetSummary",
    "GeometryKind",
    "GridSpec",
    "InterpolationMethod",
    "InterpolationRequest",
    "JobState",
    "LengthUnit",
    "LineageRecord",
    "ProjectSummary",
    "RenderMetadata",
    "RenderResult",
    "SizePreset",
    "SyncState",
    "VariogramModel",
    "VariogramSpec",
    "Visibility",
    "WebMapModel",
]


class DatasetKind(StrEnum):
    VECTOR = "vector"
    GRID = "grid"
    POINTSET = "pointset"
    FAULT_NETWORK = "fault_network"


class GeometryKind(StrEnum):
    POINT = "point"
    LINESTRING = "linestring"
    POLYGON = "polygon"
    MIXED = "mixed"


class SyncState(StrEnum):
    PENDING = "pending"
    SYNCING = "syncing"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"


class LengthUnit(StrEnum):
    M = "m"
    FT = "ft"
    USFT = "usft"


class ConnectorKind(StrEnum):
    UPLOAD = "upload"
    FILESHARE = "fileshare"
    POSTGIS = "postgis"
    DERIVED = "derived"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


Bbox = Annotated[list[float], Field(min_length=4, max_length=4)]
"""[west, south, east, north] in EPSG:4326."""


class WebMapModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",  # Catch typos in Claude-supplied params loudly
        use_enum_values=False,
        populate_by_name=True,
    )


class AttributeField(WebMapModel):
    name: str
    type: Literal["string", "integer", "number", "boolean", "date", "datetime"]
    nullable: bool = True
    description: str | None = None


class LineageRecord(WebMapModel):
    operation: str
    parameters: dict[str, Any]
    input_dataset_ids: list[UUID]
    webmap_geo_version: str
    created_at: datetime


class ProjectSummary(WebMapModel):
    """The container that fixes the analysis CRS and units (`02` §3.4)."""

    id: UUID
    slug: str
    name: str
    description: str | None = None
    analysis_srid: int
    horizontal_unit: LengthUnit
    vertical_unit: LengthUnit
    depth_positive_down: bool
    default_extent: Bbox | None = None
    visibility: Visibility


class DatasetSummary(WebMapModel):
    """Compact form returned by list/search. Keep small — Claude reads many."""

    id: UUID
    name: str
    kind: DatasetKind
    geometry_kind: GeometryKind | None = None
    feature_count: int | None = None
    bbox_4326: Bbox | None = None
    sync_state: SyncState
    synced_at: datetime | None = None
    caption: str | None = None


class DatasetDetail(DatasetSummary):
    """Full form. Includes everything Claude needs for an accurate caption."""

    description: str | None = None
    project_id: UUID | None = None
    storage_srid: int
    attribute_schema: list[AttributeField] | None = None
    value_min: float | None = None
    value_max: float | None = None
    value_unit: str | None = None
    vertical_unit: LengthUnit | None = None
    grid_nx: int | None = None
    grid_ny: int | None = None
    grid_cell_size: float | None = None
    data_vintage: date | None = None
    owner: str  # display name
    visibility: Visibility
    created_at: datetime
    updated_at: datetime
    lineage: LineageRecord | None = None


# --- Interpolation ------------------------------------------------------


class VariogramModel(StrEnum):
    SPHERICAL = "spherical"
    EXPONENTIAL = "exponential"
    GAUSSIAN = "gaussian"
    MATERN = "matern"
    LINEAR = "linear"


class VariogramSpec(WebMapModel):
    model: VariogramModel = VariogramModel.EXPONENTIAL
    range: float | None = Field(
        None,
        gt=0,
        description="Correlation range in analysis-CRS units. None = auto-fit.",
    )
    sill: float | None = Field(None, gt=0)
    nugget: float | None = Field(None, ge=0)
    anisotropy_ratio: float = Field(
        1.0,
        ge=1.0,
        description="Major/minor axis ratio. 1.0 = isotropic.",
    )
    anisotropy_angle: float = Field(
        0.0,
        ge=-180,
        le=180,
        description="Azimuth of major axis, degrees clockwise from north.",
    )


class GridSpec(WebMapModel):
    cell_size: float | None = Field(
        None,
        gt=0,
        description="Cell size in analysis-CRS units. None = derived from "
        "data density (median nearest-neighbour distance / 2).",
    )
    bbox: Bbox | None = Field(
        None,
        description="Output extent in EPSG:4326. None = data extent + 5% margin.",
    )
    max_cells: int = Field(4_000_000, le=16_000_000)

    @model_validator(mode="after")
    def _check(self) -> GridSpec:
        if self.bbox and (self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]):
            raise ValueError(
                "bbox must be [west, south, east, north] with west<east, south<north"
            )
        return self


class InterpolationMethod(StrEnum):
    ORDINARY_KRIGING = "ordinary_kriging"
    UNIVERSAL_KRIGING = "universal_kriging"
    MINIMUM_CURVATURE = "minimum_curvature"
    CUBIC_SPLINE = "cubic_spline"
    IDW = "idw"
    NEAREST = "nearest"


class InterpolationRequest(WebMapModel):
    dataset_id: UUID
    value_field: str
    method: InterpolationMethod = InterpolationMethod.ORDINARY_KRIGING
    grid: GridSpec = GridSpec()
    variogram: VariogramSpec | None = None  # kriging only
    fault_dataset_id: UUID | None = Field(
        None,
        description="Fault network to honor. Features with "
        "constraint_kind='fault' block interpolation; "
        "'breakline' allows continuity but breaks gradient.",
    )
    n_neighbors: int = Field(48, ge=8, le=256)
    max_search_radius: float | None = Field(None, gt=0)
    detrend: Literal["none", "linear", "quadratic"] = "none"
    declustering: bool = Field(
        True,
        description="Cell-declustering weights for variogram estimation. "
        "Strongly recommended with clustered well control.",
    )
    output_name: str


# --- Rendering ----------------------------------------------------------


class SizePreset(StrEnum):
    SLIDE_FULL = "slide_full"  # 16:9 full bleed,  2560x1440 @2x
    SLIDE_HALF = "slide_half"  # half slide,       1280x1440 @2x
    SLIDE_QUARTER = "slide_quarter"  # quarter,          1280x720  @2x
    SQUARE = "square"  #                   1600x1600 @2x
    THUMBNAIL = "thumbnail"  #                   640x360   @1x


class RenderMetadata(WebMapModel):
    title: str
    value_range: dict[str, Any] | None = None
    method: str | None = None
    method_params: dict[str, Any] | None = None
    grid: dict[str, Any] | None = None
    crs: dict[str, int]
    extent: Bbox
    data_vintage: date | None = None
    feature_counts: dict[str, int] = Field(default_factory=dict)


class RenderResult(WebMapModel):
    render_id: UUID
    url: str
    width: int
    height: int
    metadata: RenderMetadata
    caption: str
    session_url: str | None = None
    warnings: list[str] = Field(default_factory=list)
