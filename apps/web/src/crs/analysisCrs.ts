/**
 * Browser-side reprojection for the cursor readout. `07-frontend.md` §5.2.
 *
 * The status bar must show "live cursor coordinates in [the analysis] CRS".
 * A geologist on a Texas Central project reads and writes State Plane feet all
 * day; showing them −102.08, 31.99 means converting in their head every time
 * they want to check a location against a well file.
 *
 * **This is the one place in the browser that transforms coordinates, and it
 * transforms nothing but the cursor.** `adr/0003-geoprocessing-owns-crs.md`
 * puts reprojection at defined boundaries: geometry is reprojected server-side
 * at ingest and served in one frame, and the map draws in Web Mercator. A
 * pointer position is a display concern with no lineage and no stored
 * consequence, and round-tripping every mouse move to the API for it would be
 * absurd — so it is done here, and nothing else is.
 *
 * The definition comes from the server (`project.crs_wkt`), derived from the
 * project's `analysis_srid` by pyproj. It is not hardcoded and not looked up
 * from a client-side EPSG table: a table in the browser is a second source of
 * truth for what a CRS means, and the two would eventually disagree about a
 * datum shift. WKT rather than a PROJ string because the PROJ form is lossy —
 * see `webmap_geo.crs.crs_definition`.
 */

import proj4 from 'proj4';

/** WGS84 longitude/latitude — what MapLibre reports. */
const WGS84 = '+proj=longlat +datum=WGS84 +no_defs';

export interface ProjectedPoint {
  x: number;
  y: number;
}

export type CursorTransform = (lngLat: [number, number]) => ProjectedPoint | null;

/**
 * Build a lng/lat → analysis-CRS transform from a CRS definition.
 *
 * Returns a function that yields `null` rather than throwing on a point the
 * projection cannot represent. Panning outside a State Plane zone's domain is
 * an ordinary thing to do — the zone covers part of one state and the map does
 * not stop at its edge — and an exception on mouse move would take the app
 * down for it.
 */
export function cursorTransform(definition: string): CursorTransform {
  if (!definition.trim()) {
    throw new Error(
      'No definition for the analysis CRS. The status bar cannot show ' +
        'coordinates in a CRS it has no definition for — check that the project ' +
        'endpoint returns crs_wkt.',
    );
  }

  // proj4 accepts WKT and PROJ strings alike. The server sends WKT; the PROJ
  // form is accepted so a hand-written definition still works during
  // development.
  const converter = proj4(WGS84, definition);

  return ([lng, lat]) => {
    if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
    try {
      const [x, y] = converter.forward([lng, lat]);
      // proj4 returns Infinity rather than throwing for a point outside the
      // projection's domain, which would render as "E Infinity" in the bar.
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
      return { x, y };
    } catch {
      return null;
    }
  };
}
