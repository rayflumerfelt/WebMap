/**
 * The attribute table with its data source attached. `07-frontend.md` §5.2.
 *
 * Kept separate from `AttributeTable` so the table stays a pure component —
 * §5.5 wants panels detachable into a second window later, and a component
 * that fetches its own data assumes a query client and an API in scope.
 *
 * Reads the paged attributes endpoint rather than the GeoJSON one. The GeoJSON
 * path refuses above 5,000 features rather than truncating
 * (`06-rendering.md` §7.1) — right for a map source, and it left a
 * 500k-feature layer with no attribute view at all. Paging is honest about
 * being a page: the table says how many of how many it is showing.
 */

import { useMemo, useState } from 'react';

import { AttributeTable } from './AttributeTable.js';
import type { AttributeColumn, AttributeRow } from './AttributeTable.js';
import { ApiError } from '../api/client.js';
import type { ApiClient } from '../api/client.js';
import { useAttributes } from '../api/sessions.js';

export interface AttributePanelProps {
  api: ApiClient;
  datasetId: string | null;
  layerName: string | null;
  /** From dataset metadata. Used to decide whether to ask at all. */
  featureCount: number | null;
  height?: number;
}

export function AttributePanel({
  api,
  datasetId,
  layerName,
  featureCount,
  height,
}: AttributePanelProps) {
  const [offset, setOffset] = useState(0);
  const page = useAttributes(api, datasetId, { offset, limit: PAGE_SIZE });

  const { columns, rows } = useMemo(() => derive(page.data?.items), [page.data]);

  if (!datasetId) {
    return <Message>Select a layer to see its attributes.</Message>;
  }
  if (page.isLoading) return <Message>Loading attributes…</Message>;
  if (page.error) {
    // The API's own sentence, which for a permission failure names the owner.
    const message =
      page.error instanceof ApiError
        ? page.error.message
        : 'The attributes for this layer could not be loaded.';
    return <Message role="alert">{message}</Message>;
  }

  const total = page.data?.total ?? featureCount ?? rows.length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height, minHeight: 0 }}>
      <AttributeTable
        columns={columns}
        rows={rows}
        totalCount={total}
        {...(height !== undefined ? { height: height - PAGER_HEIGHT } : {})}
      />
      {total > PAGE_SIZE ? (
        <Pager
          offset={offset}
          shown={rows.length}
          total={total}
          layerName={layerName}
          onOffsetChange={setOffset}
        />
      ) : null}
    </div>
  );
}

/** Matches the endpoint's default. A page of 500 covers a fast scroll without
 *  a round trip; above a few thousand the JSON itself becomes the cost. */
const PAGE_SIZE = 500;
const PAGER_HEIGHT = 22;

function Pager({
  offset,
  shown,
  total,
  layerName,
  onOffsetChange,
}: {
  offset: number;
  shown: number;
  total: number;
  layerName: string | null;
  onOffsetChange(offset: number): void;
}) {
  const first = total === 0 ? 0 : offset + 1;
  const last = offset + shown;

  return (
    <div
      style={{
        flex: `0 0 ${PAGER_HEIGHT}px`,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '0 8px',
        fontSize: 11,
        borderTop: '1px solid var(--chrome-border)',
      }}
    >
      <button
        type="button"
        aria-label={`Previous page of ${layerName ?? 'attributes'}`}
        disabled={offset === 0}
        onClick={() => onOffsetChange(Math.max(0, offset - PAGE_SIZE))}
        style={pagerButton}
      >
        &lsaquo;
      </button>
      <span role="status">
        {first.toLocaleString('en-US')}&ndash;{last.toLocaleString('en-US')} of{' '}
        {total.toLocaleString('en-US')}
      </span>
      <button
        type="button"
        aria-label={`Next page of ${layerName ?? 'attributes'}`}
        disabled={last >= total}
        onClick={() => onOffsetChange(offset + PAGE_SIZE)}
        style={pagerButton}
      >
        &rsaquo;
      </button>
    </div>
  );
}

const pagerButton: React.CSSProperties = {
  padding: '0 var(--hit-slop, 4px)',
  border: '1px solid var(--chrome-border)',
  borderRadius: 2,
  background: 'transparent',
  cursor: 'pointer',
  font: 'inherit',
};

/**
 * Columns and rows from a page of attribute records.
 *
 * Column order follows the first feature's key order, which is the order the
 * Parquet schema declares — so the table matches the file rather than
 * re-alphabetising it. Type is inferred from the first non-null value in each
 * column, because that decides alignment and a column of right-aligned strings
 * reads as broken.
 */
export function derive(items: Array<Record<string, unknown>> | undefined): {
  columns: AttributeColumn[];
  rows: AttributeRow[];
} {
  if (!items?.length) return { columns: [], rows: [] };

  const names: string[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    for (const key of Object.keys(item)) {
      if (!seen.has(key)) {
        seen.add(key);
        names.push(key);
      }
    }
  }

  const columns: AttributeColumn[] = names.map((name) => ({
    name,
    type: inferType(items, name),
  }));

  const rows: AttributeRow[] = items.map((item) => {
    const row: AttributeRow = {};
    for (const name of names) {
      const value = item[name];
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
  items: Array<Record<string, unknown>>,
  name: string,
): AttributeColumn['type'] {
  for (const item of items) {
    const value = item[name];
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
