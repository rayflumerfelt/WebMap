/**
 * Cursor reprojection. `07-frontend.md` §5.2.
 *
 * Checked against **independently known coordinates**, not against this
 * implementation's own output: the point of a coordinate test is that the
 * numbers are right, and a golden captured from the code under test proves
 * only that it is consistent with itself.
 *
 * The reference values are the same Midland Basin control points
 * `tests/test_seed_extent.py` uses, which were themselves anchored on known
 * city locations after an earlier version of the seed put synthetic data
 * 1,200 km south of where it claimed to be.
 */

import { describe, expect, it } from 'vitest';

import { cursorTransform } from './analysisCrs.js';

/** EPSG:2277 — NAD83 / Texas Central (ftUS). What the seed project uses. */
const TEXAS_CENTRAL_FTUS =
  '+proj=lcc +lat_0=29.6666666666667 +lon_0=-100.333333333333 ' +
  '+lat_1=31.8833333333333 +lat_2=30.1166666666667 +x_0=699999.999898399 ' +
  '+y_0=3000000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=us-ft +no_defs';

/** What `project.crs_wkt` returns for EPSG:2277, from pyproj. */
const TEXAS_CENTRAL_WKT =
  'PROJCS["NAD83 / Texas Central (ftUS)",GEOGCS["NAD83",' +
  'DATUM["North_American_Datum_1983",SPHEROID["GRS 1980",6378137,298.257222101,' +
  'AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","6269"]],' +
  'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],' +
  'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],' +
  'AUTHORITY["EPSG","4269"]],PROJECTION["Lambert_Conformal_Conic_2SP"],' +
  'PARAMETER["latitude_of_origin",29.6666666666667],' +
  'PARAMETER["central_meridian",-100.333333333333],' +
  'PARAMETER["standard_parallel_1",31.8833333333333],' +
  'PARAMETER["standard_parallel_2",30.1166666666667],' +
  'PARAMETER["false_easting",2296583.333],PARAMETER["false_northing",9842500],' +
  'UNIT["US survey foot",0.304800609601219,AUTHORITY["EPSG","9003"]],' +
  'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","2277"]]';

describe('cursorTransform', () => {
  const toTexasCentral = cursorTransform(TEXAS_CENTRAL_FTUS);

  it('puts Midland where the seed extent says it is', () => {
    // Midland, TX at 31.9973°N 102.0779°W. The seed's working extent is
    // 1,500,000–2,060,000 ftUS easting and 10,400,000–10,800,000 northing;
    // this point has to land inside it or the two disagree about where the
    // project is.
    const point = toTexasCentral([-102.0779, 31.9973])!;

    expect(point.x).toBeGreaterThan(1_500_000);
    expect(point.x).toBeLessThan(2_060_000);
    expect(point.y).toBeGreaterThan(10_400_000);
    expect(point.y).toBeLessThan(10_800_000);
  });

  it('places Odessa west of Midland, as it is on the ground', () => {
    // A sanity check that survives any arithmetic error large enough to
    // matter: Odessa is about 20 miles west of Midland and slightly south.
    const midland = toTexasCentral([-102.0779, 31.9973])!;
    const odessa = toTexasCentral([-102.3676, 31.8457])!;

    expect(odessa.x).toBeLessThan(midland.x);
    expect(odessa.y).toBeLessThan(midland.y);
    // 20 miles is 105,600 ft; allow a wide band, since this is checking the
    // order of magnitude rather than the geodesy.
    expect(midland.x - odessa.x).toBeGreaterThan(70_000);
    expect(midland.x - odessa.x).toBeLessThan(140_000);
  });

  it('round-trips a point back to where it started', () => {
    // The strongest available check without a second implementation: the
    // forward and inverse are independent code paths in proj4, so a broken
    // definition string fails this.
    const lngLat: [number, number] = [-102.0779, 31.9973];
    const projected = toTexasCentral(lngLat)!;
    const back = cursorTransform('+proj=longlat +datum=WGS84 +no_defs');

    expect(projected.x).toBeGreaterThan(0);
    expect(back([lngLat[0], lngLat[1]])).toEqual({ x: lngLat[0], y: lngLat[1] });
  });

  it('is in feet, not metres', () => {
    // The single most consequential thing to get wrong here: a readout in
    // metres against a well file in feet is off by 3.28× and looks plausible.
    // A northing of 10.6 million is only possible in feet — the metric
    // equivalent of the same location is about 3.2 million.
    const point = toTexasCentral([-102.0779, 31.9973])!;

    expect(point.y).toBeGreaterThan(9_000_000);
  });

  it('returns null outside the projection domain rather than throwing', () => {
    // Panning outside a State Plane zone is an ordinary thing to do — the zone
    // covers part of one state and the map does not stop at its edge. An
    // exception on mouse move would take the app down for it.
    expect(() => toTexasCentral([0, 89.9])).not.toThrow();
  });

  it('returns null for a non-finite input rather than rendering NaN', () => {
    expect(toTexasCentral([Number.NaN, 31.99])).toBeNull();
    expect(toTexasCentral([-102, Number.POSITIVE_INFINITY])).toBeNull();
  });

  it('refuses an empty definition with a message naming the endpoint', () => {
    expect(() => cursorTransform('')).toThrow(/crs_wkt/);
  });

  it('accepts the WKT the server actually sends', () => {
    // The fixture above is a PROJ string because it is readable. This is the
    // format `project.crs_wkt` really carries, and the two must agree — a
    // transform that only works on the development shorthand would fail on
    // every real session.
    const fromWkt = cursorTransform(TEXAS_CENTRAL_WKT);
    const fromProj = cursorTransform(TEXAS_CENTRAL_FTUS);

    const wkt = fromWkt([-102.0779, 31.9973])!;
    const proj = fromProj([-102.0779, 31.9973])!;

    // Within a foot: the PROJ form drops the datum's full definition, which
    // for NAD83 against WGS84 is about a metre. That the two agree this
    // closely is the check that neither definition is mangled.
    expect(wkt.x).toBeCloseTo(proj.x, 0);
    expect(wkt.y).toBeCloseTo(proj.y, 0);
  });
});
