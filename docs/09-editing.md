# 09 — Vector Editing

MapLibre has no built-in editing. We use **Terra Draw** — actively maintained, MapLibre-native
adapter, sane mode system. Not mapbox-gl-draw or its forks, which carry Mapbox-era assumptions.

---

## 1. Scope, honestly stated

Editing gets hard fast, and the hard parts are snapping and topology, not drawing.

**In scope:**

- Create, modify, delete point / line / polygon features
- Vertex-level editing: add, move, delete
- Snapping to vertices, edges, and intersections
- Attribute editing
- Geometry validation with actionable errors
- Undo/redo
- Optimistic locking with conflict detection

**Deferred, deliberately:**

- Real-time collaborative editing on the same layer
- Full planar topology (shared-boundary editing where moving a boundary updates both polygons
  automatically)
- Automatic gap and sliver removal across a polygon coverage

> **Why the deferral is explicit.** A polygon editor that silently permits slivers and gaps is
> worse than no editor for coverage work — the geologist trusts the output and the errors
> surface downstream in area calculations. We ship validation that *detects* slivers and
> refuses to save them, rather than topology that prevents them. That is an honest middle
> ground; pretending we have topology is not.

---

## 2. Terra Draw integration

```typescript
// packages/map/src/editing/EditingController.ts

import {
  TerraDraw, TerraDrawMapLibreGLAdapter,
  TerraDrawPointMode, TerraDrawLineStringMode, TerraDrawPolygonMode,
  TerraDrawSelectMode,
} from 'terra-draw';

export interface EditingConfig {
  datasetId: string;
  geometryKind: GeometryKind;
  snapping: SnappingConfig;
  onFeatureChange(change: FeatureChange): void;
  onValidationError(errors: ValidationError[]): void;
}

export class EditingController {
  private draw: TerraDraw;

  constructor(map: maplibregl.Map, config: EditingConfig) {
    this.draw = new TerraDraw({
      adapter: new TerraDrawMapLibreGLAdapter({ map }),
      modes: [
        new TerraDrawSelectMode({
          flags: {
            // Vertex-level editing on everything. Geologists expect to
            // drag individual points on a fault trace.
            point:      { feature: { draggable: true } },
            linestring: { feature: {
              draggable: true,
              coordinates: { midpoints: true, draggable: true, deletable: true },
            }},
            polygon:    { feature: {
              draggable: true,
              coordinates: { midpoints: true, draggable: true, deletable: true },
            }},
          },
        }),
        new TerraDrawPointMode(),
        new TerraDrawLineStringMode(),
        new TerraDrawPolygonMode(),
      ],
    });
  }
}
```

### 2.1 Coordinate handling

Terra Draw works in WGS84. Validation and snapping tolerances are in **analysis-CRS units**
(feet or metres). Convert at the boundary; never mix.

```typescript
/**
 * Snapping tolerance is specified in analysis-CRS units — a geologist says
 * "snap within 50 feet", not "snap within 0.00014 degrees". Convert per
 * operation at the current latitude, because degree length varies.
 */
function toleranceInDegrees(toleranceUnits: number, lat: number, crs: CrsInfo): number {
  const metres = toMetres(toleranceUnits, crs.horizontalUnit);
  return metres / (111_320 * Math.cos((lat * Math.PI) / 180));
}
```

---

## 3. Snapping

The feature that determines whether the editor is usable. Without it, every shared boundary is
a source of slivers.

```typescript
export interface SnappingConfig {
  enabled: boolean;
  /** In analysis-CRS units. */
  tolerance: number;
  targets: {
    vertices: boolean;
    edges: boolean;
    intersections: boolean;
    /** Snap to features in other visible layers, not just the edited one.
     *  Essential when digitising a polygon against an existing boundary. */
    otherLayers: string[];
  };
  /** Hold to temporarily disable — needed when a vertex must sit near but
   *  not on an existing one. */
  bypassKey: 'Alt' | 'Control';
}
```

```typescript
// packages/map/src/editing/snapping.ts

import RBush from 'rbush';

/**
 * Spatial index over candidate snap targets, rebuilt when the viewport
 * settles rather than on every mouse move.
 *
 * Naive nearest-vertex search over a 50,000-feature layer at 60 Hz is not
 * viable. The index is built from features currently in the viewport only —
 * you cannot snap to something you cannot see, so off-screen features are
 * irrelevant.
 */
export class SnapIndex {
  private vertexTree = new RBush<VertexEntry>();
  private edgeTree = new RBush<EdgeEntry>();

  rebuild(features: MapGeoJSONFeature[]): void { /* ... */ }

  /**
   * Nearest snap target within tolerance.
   * Priority: intersection > vertex > edge. An intersection is a more
   * meaningful place to land than an arbitrary point on an edge, and
   * geologists digitising fault networks depend on it.
   */
  query(lngLat: LngLat, toleranceDeg: number, targets: SnapTargets): SnapResult | null {
    /* ... */
  }
}
```

Visual feedback is mandatory: a distinct marker at the snap point, and a different marker for
intersections. Without it users cannot tell whether a snap occurred and lose trust in the
tool.

---

## 4. Validation

Runs on every geometry change, before save. Errors block; warnings do not.

```python
# python/webmap_geo/validate.py

from dataclasses import dataclass
from enum import StrEnum

from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity


class Severity(StrEnum):
    ERROR = "error"      # blocks save
    WARNING = "warning"  # allows save, surfaces in the UI


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    location: tuple[float, float] | None = None
    feature_id: int | None = None


def validate_geometry(
    geom: BaseGeometry,
    analysis_srid: int,
    sliver_threshold: float = 0.05,
    min_segment_length: float | None = None,
) -> list[ValidationIssue]:
    """Validate a single geometry in the analysis CRS.

    Checks:
      ERROR   self-intersection (invalid per OGC)
      ERROR   unclosed ring
      ERROR   fewer than 3 distinct vertices in a polygon ring
      ERROR   zero-length linestring
      ERROR   NaN or infinite coordinate
      WARNING sliver polygon (thinness ratio below threshold)
      WARNING duplicate consecutive vertices
      WARNING segment shorter than min_segment_length
      WARNING self-touching ring (valid, but usually a mistake)
    """
    issues: list[ValidationIssue] = []

    if not geom.is_valid:
        reason = explain_validity(geom)
        issues.append(ValidationIssue(
            Severity.ERROR, "invalid_geometry",
            f"Geometry is invalid: {reason}",
            location=_parse_location(reason),
        ))

    if geom.geom_type in ("Polygon", "MultiPolygon"):
        thinness = 4 * 3.14159 * geom.area / (geom.length ** 2) if geom.length else 0
        if 0 < thinness < sliver_threshold:
            issues.append(ValidationIssue(
                Severity.WARNING, "sliver",
                f"Polygon is very thin (thinness {thinness:.3f}). This is "
                f"usually an accidental sliver from digitising against an "
                f"existing boundary. Enable snapping to avoid it.",
                location=geom.representative_point().coords[0],
            ))
    return issues
```

### 4.1 Fault network validation

Fault layers get the extra checks from `05-geoprocessing.md` §3, run on save rather than at
grid time. Catching a dangling fault while the geologist is looking at it is far better than
failing a kriging job forty minutes later.

---

## 5. Persistence

### 5.1 Optimistic locking

```sql
UPDATE feat.ds_9f3a...
SET geom = ST_GeomFromGeoJSON(:geom),
    props = :props,
    version = version + 1,
    updated_at = now(),
    updated_by = :user_id
WHERE id = :feature_id AND version = :expected_version
RETURNING version;
```

Zero rows means someone else changed it. The API returns 409 with the current server state so
the client can show a diff and let the user choose. Never silently overwrite.

### 5.2 Edit batching

```typescript
/**
 * Edits accumulate in memory and flush on a debounce or on explicit save.
 *
 * A vertex drag fires dozens of change events; one PATCH per event would
 * overwhelm the API and make undo semantics incoherent. The batch is the
 * unit of undo.
 */
export class EditBuffer {
  private pending = new Map<string, FeatureChange>();

  stage(change: FeatureChange): void {
    // Coalesce by feature id — the last state wins within a batch.
    this.pending.set(change.featureId, change);
    this.scheduleFlush();
  }

  async flush(): Promise<FlushResult> { /* PATCH /datasets/{id}/features */ }
}
```

### 5.3 Never write in place

Editing a dataset sourced from a file share creates a new versioned output. The source file is
never modified (`03-auth-security.md` §8). A geologist losing a partner-delivered shapefile is
unrecoverable, and no editing convenience is worth that risk.

---

## 6. Undo/redo

Command pattern. The unit is the flushed batch, so undo restores a state the user recognises
rather than an intermediate drag position.

```typescript
export interface EditCommand {
  readonly label: string;               // shown in the UI: "Move 3 vertices"
  apply(): Promise<void>;
  invert(): EditCommand;
}

export class EditHistory {
  private undoStack: EditCommand[] = [];
  private redoStack: EditCommand[] = [];
  private readonly limit = 50;

  /** Server-side conflict clears the redo stack — replaying forward over
   *  someone else's change would produce a state nobody authored. */
  onConflict(): void { this.redoStack = []; }
}
```

---

## 7. Attribute editing

```typescript
export interface AttributeEditorProps {
  schema: AttributeField[];
  features: EditableFeature[];         // multi-select supported
  onChange(featureIds: number[], updates: Record<string, unknown>): void;
}
```

- Field types come from `dataset.attribute_schema` and drive the control (number input, date
  picker, select for enumerated fields).
- Multi-feature edit sets a field across the selection; mixed values show as indeterminate,
  not as blank.
- Schema changes (adding a field) are a dataset-level operation, not an attribute edit.

**Shapefile warning on export.** If the dataset will be exported to shapefile, warn at
*schema-edit* time that field names longer than 10 characters will be truncated — not at
export time when it is too late to choose a shorter name. See `11-file-io.md` §4.

---

## 8. Layer source switching

Editing needs GeoJSON sources for immediate visual feedback; large layers need MVT for
performance. Reconcile explicitly.

```typescript
/**
 * Editing a large layer:
 *   1. Switch the edit target to a GeoJSON source containing only features
 *      in the current viewport (capped at 5,000).
 *   2. Keep the MVT source visible for context, filtered to exclude the
 *      features now in the GeoJSON source, so nothing renders twice.
 *   3. On flush, invalidate the affected MVT tiles and switch back.
 *
 * Panning during an edit session re-queries the viewport. Unflushed edits
 * are retained in the buffer and re-applied to the new working set.
 */
```

If the viewport contains more than 5,000 features, the UI requires the user to zoom in before
editing. Say so plainly rather than degrading silently.

---

## 9. Testing

| Concern | Approach |
|---|---|
| Validation rules | pytest, geometry fixtures including known-bad shapes |
| Snapping correctness | Vitest, synthetic geometries with exact expected snap points |
| Snap index performance | Benchmark: 50k features, query under 2 ms |
| Optimistic locking | Integration test with two concurrent sessions |
| Undo/redo | Property test — random command sequences, assert `undo(apply(s)) == s` |
| Edit → render | E2E: edit a fault, re-grid, confirm the surface changed at the fault |

That last test is the one that matters most. It verifies the whole chain — edit, persist,
invalidate, re-interpolate with the new constraint geometry — and it is the chain most likely
to break silently.
