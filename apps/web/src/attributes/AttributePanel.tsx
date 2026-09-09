/**
 * The attribute table with its data source attached. `07-frontend.md` §5.2.
 *
 * Kept separate from `AttributeTable` so the table stays a pure component —
 * §5.5 wants panels detachable into a second window later, and a component
 * that fetches its own data assumes a query client and an API in scope.
 *
 * The interesting behaviour is what happens above the GeoJSON threshold. The
 * endpoint refuses rather than truncating (`06-rendering.md` §7.1), and that
 * refusal is passed on rather than worked around: a table showing the first
 * 5,000 of 500,000 features without saying so is a table someone will draw a
 * conclusion from.
 */

import { useMemo } from 'react';

import { AttributeTable } from './AttributeTable.js';
import type { AttributeColumn, AttributeRow } from './AttributeTable.js';
import { ApiError } from '../api/client.js';
import type { ApiClient } from '../api/client.js';
import { useFeatures } from '../api/sessions.js';

export interface AttributePanelProps {
  api: ApiClient;
  datasetId: string | null;
  layerName: string | null;
  /** From dataset metadata. Used to decide whether to ask at all. */
  featureCount: number | null;
  height?: number;
}

/** `06-rendering.md` §7.1, mirrored so the panel does not have to fail to
 *  learn the layer is too large. */
const GEOJSON_FEATURE_LIMIT = 5_000;

export function AttributePanel({
  api,
  datasetId,
  layerName,
  featureCount,
  height,
}: AttributePanelProps) {
  const tooLarge = featureCount !== null && featureCount >= GEOJSON_FEATURE_LIMIT;
  const features = useFeatures(api, datasetId, !tooLarge);

  const { columns, rows } = useMemo(() => derive(features.data?.features), [features.data]);

  if (!datasetId) {
    return <Message>Select a layer to see its attributes.</Message>;
  }
  if (tooLarge) {
    return (
      <Message>
        {layerName ?? 'This layer'} has {featureCount!.toLocaleString('en-US')} features,
        at or above the {GEOJSON_FEATURE_LIMIT.toLocaleString('en-US')} limit for loading
        attributes in the browser. Filter the layer, or export it to inspect the table.
      </Message>
    );
  }
  if (features.isLoading) return <Message>Loading attributes…</Message>;
  if (features.error) {
    // The API's own sentence, which for a permission failure names the owner.
    const message =
      features.error instanceof ApiError
        ? features.error.message
        : 'The attributes for this layer could not be loaded.';
    return <Message role="alert">{message}</Message>;
  }

  return (
    <AttributeTable
      columns={columns}
      rows={rows}
      totalCount={featureCount ?? rows.length}
      {...(height !== undefined ? { height } : {})}
    />
  );
}

/**
 * Columns and rows from a FeatureCollection's properties.
 *
 * Column order follows the first feature's key order, which is the order the
 * Parquet schema declares — so the table matches the file rather than
 * re-alphabetising it. Type is inferred from the first non-null value in each
 * column, because that decides alignment and a column of right-aligned strings
 * reads as broken.
 */
export function derive(
  features: Array<{ properties: Record<string, unknown> | null }> | undefined,
): { columns: AttributeColumn[]; rows: AttributeRow[] } {
  if (!features?.length) return { columns: [], rows: [] };

  const names: string[] = [];
  const seen = new Set<string>();
  for (const feature of features) {
    for (const key of Object.keys(feature.properties ?? {})) {
      if (!seen.has(key)) {
        seen.add(key);
        names.push(key);
      }
    }
  }

  const columns: AttributeColumn[] = names.map((name) => ({
    name,
    type: inferType(features, name),
  }));

  const rows: AttributeRow[] = features.map((feature) => {
    const row: AttributeRow = {};
    for (const name of names) {
      const value = feature.properties?.[name];
      row[name] =
        value === null || value === undefined
          ? null
          : typeof value === 'object'
            ? // A nested object or array — GeoJSON permits it and the table has
              // no column type for it. Rendered as JSON rather than as
              // "[object Object]", which tells the reader nothing.
              JSON.stringify(value)
            : (value as string | number | boolean);
    }
    return row;
  });

  return { columns, rows };
}

function inferType(
  features: Array<{ properties: Record<string, unknown> | null }>,
  name: string,
): AttributeColumn['type'] {
  for (const feature of features) {
    const value = feature.properties?.[name];
    if (value === null || value === undefined) continue;
    if (typeof value === 'number') return 'number';
    if (typeof value === 'boolean') return 'boolean';
    return 'string';
  }
  // Every value null. String, so the column left-aligns its em dashes —
  // right-aligned dashes read as a numeric column with no data.
  return 'string';
}

function Message({ children, role }: { children: React.ReactNode; role?: string }) {
  return (
    <p {...(role ? { role } : {})} style={{ fontSize: 12, opacity: 0.8, padding: 8, margin: 0 }}>
      {children}
    </p>
  );
}
