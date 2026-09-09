/**
 * Deriving a table from a FeatureCollection.
 *
 * The rendering half lives in `AttributeTable.test.tsx`; this covers the
 * translation, where the decisions that affect a reader are made — column
 * order, inferred type, and what happens to a value the table has no column
 * type for.
 */

import { describe, expect, it } from 'vitest';

import { derive } from './AttributePanel.js';

function features(...properties: Array<Record<string, unknown> | null>) {
  return properties.map((p) => ({ properties: p }));
}

describe('columns', () => {
  it('follows the order the data declares, not the alphabet', () => {
    // The key order is the Parquet schema's order. Re-sorting would make the
    // table disagree with the file it came from, and with every other tool
    // that reads it.
    const { columns } = derive(
      features({ well_name: 'A', tvdss_ft: -8_200, porosity: 12.4 }),
    );

    expect(columns.map((c) => c.name)).toEqual(['well_name', 'tvdss_ft', 'porosity']);
  });

  it('picks up a column that only later features have', () => {
    // Sparse attributes are ordinary — a field added partway through a
    // campaign. Taking the first feature's keys alone would hide it.
    const { columns } = derive(
      features({ well_name: 'A' }, { well_name: 'B', operator: 'Someone' }),
    );

    expect(columns.map((c) => c.name)).toEqual(['well_name', 'operator']);
  });

  it('infers type from the first non-null value', () => {
    // Type decides alignment, and a column of right-aligned strings reads as
    // broken.
    const { columns } = derive(
      features({ porosity: null, name: null }, { porosity: 12.4, name: 'A' }),
    );

    expect(columns.find((c) => c.name === 'porosity')!.type).toBe('number');
    expect(columns.find((c) => c.name === 'name')!.type).toBe('string');
  });

  it('treats an all-null column as text', () => {
    // Right-aligned em dashes read as a numeric column with no data, which
    // invites the reader to wonder what happened to the numbers.
    const { columns } = derive(features({ vintage: null }, { vintage: null }));

    expect(columns[0]!.type).toBe('string');
  });
});

describe('rows', () => {
  it('keeps null as null rather than as an empty string', () => {
    // The table renders null as an em dash; an empty string would render as a
    // blank cell, which reads as a rendering gap.
    const { rows } = derive(features({ porosity: null }));

    expect(rows[0]!.porosity).toBeNull();
  });

  it('treats a missing key as null, not as undefined', () => {
    const { rows } = derive(features({ a: 1 }, { b: 2 }));

    expect(rows[0]!.b).toBeNull();
    expect(rows[1]!.a).toBeNull();
  });

  it('renders a nested value as JSON rather than [object Object]', () => {
    // GeoJSON permits nested properties and the table has no column type for
    // them. "[object Object]" tells the reader nothing at all.
    const { rows } = derive(features({ lineage: { method: 'kriging', nugget: 0.1 } }));

    expect(rows[0]!.lineage).toBe('{"method":"kriging","nugget":0.1}');
  });

  it('handles a feature with no properties at all', () => {
    // Valid GeoJSON: `properties` may be null.
    expect(() => derive(features(null, { a: 1 }))).not.toThrow();
    expect(derive(features(null, { a: 1 })).rows).toHaveLength(2);
  });

  it('returns nothing for an empty collection', () => {
    expect(derive([])).toEqual({ columns: [], rows: [] });
    expect(derive(undefined)).toEqual({ columns: [], rows: [] });
  });
});
