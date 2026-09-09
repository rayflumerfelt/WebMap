/**
 * Attribute table. `07-frontend.md` §5.2, §9.
 *
 * **Virtualized, because some datasets have 500k rows.** Rendering those as
 * DOM nodes is not slow, it is fatal: the browser allocates several kilobytes
 * per row and the tab dies before the table appears. Only the visible window
 * is mounted.
 *
 * Docked *under* the map rather than over it, so it never hides the features
 * whose rows it is showing — which is the whole reason anyone opens it.
 *
 * Numbers are right-aligned with tabular figures. That is not decoration: a
 * column of porosities is scanned for outliers by eye, and proportional digits
 * with ragged alignment defeat that completely.
 */

import { useVirtualizer } from '@tanstack/react-virtual';
import { useRef, useState } from 'react';

export interface AttributeColumn {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'date';
  /** Decimal places for a number column. Absent means "as stored". */
  precision?: number;
}

export type AttributeRow = Record<string, string | number | boolean | null>;

export interface AttributeTableProps {
  columns: AttributeColumn[];
  rows: AttributeRow[];
  /** Total in the layer, which may exceed `rows.length` when only a page has
   *  been fetched. Shown so the reader knows they are seeing a subset. */
  totalCount?: number;
  selectedIndex?: number | null;
  onSelect?(index: number): void;
  /** Zoom the map to the row's feature. Double-click, and the context menu. */
  onZoomTo?(index: number): void;
  height?: number;
}

/** Matches `--row-h`. Fixed rather than measured: a uniform row height is what
 *  lets the virtualizer place rows without laying any of them out. */
const ROW_HEIGHT = 26;

export function AttributeTable({
  columns,
  rows,
  totalCount,
  selectedIndex = null,
  onSelect,
  onZoomTo,
  height = 200,
}: AttributeTableProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [sort, setSort] = useState<{ column: string; direction: 'asc' | 'desc' } | null>(null);

  const ordered = sort ? sortRows(rows, sort.column, sort.direction) : rows;

  const virtualizer = useVirtualizer({
    count: ordered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    // Five rows of slack above and below: enough that a fast scroll does not
    // show blank space, few enough that the DOM stays small.
    overscan: 5,
  });

  if (columns.length === 0) {
    return (
      <p style={{ fontSize: 12, opacity: 0.7, padding: 8, margin: 0 }}>
        This layer has no attributes to show.
      </p>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height, minHeight: 0 }}>
      <div
        role="row"
        style={{
          display: 'flex',
          height: ROW_HEIGHT,
          flex: `0 0 ${ROW_HEIGHT}px`,
          borderBottom: '1px solid var(--chrome-border)',
          fontSize: 11,
          fontWeight: 600,
          background: 'var(--chrome-bg)',
        }}
      >
        {columns.map((column) => (
          <button
            key={column.name}
            type="button"
            role="columnheader"
            aria-sort={
              sort?.column === column.name
                ? sort.direction === 'asc'
                  ? 'ascending'
                  : 'descending'
                : 'none'
            }
            onClick={() =>
              setSort((current) =>
                current?.column === column.name
                  ? { column: column.name, direction: current.direction === 'asc' ? 'desc' : 'asc' }
                  : { column: column.name, direction: 'asc' },
              )
            }
            style={{
              ...cellStyle(column),
              border: 0,
              borderRight: '1px solid var(--chrome-border)',
              background: 'transparent',
              font: 'inherit',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            {column.name}
            {sort?.column === column.name ? (sort.direction === 'asc' ? ' ▲' : ' ▼') : ''}
          </button>
        ))}
      </div>

      <div
        ref={scrollRef}
        role="grid"
        aria-rowcount={totalCount ?? ordered.length}
        aria-label="Attributes"
        tabIndex={0}
        style={{ flex: 1, minHeight: 0, overflow: 'auto', fontSize: 12 }}
      >
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualizer.getVirtualItems().map((item) => {
            const row = ordered[item.index]!;
            const selected = item.index === selectedIndex;
            return (
              <div
                key={item.key}
                role="row"
                aria-rowindex={item.index + 1}
                aria-selected={selected}
                tabIndex={-1}
                onClick={() => onSelect?.(item.index)}
                onDoubleClick={() => onZoomTo?.(item.index)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') onZoomTo?.(item.index);
                }}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  height: ROW_HEIGHT,
                  transform: `translateY(${item.start}px)`,
                  display: 'flex',
                  alignItems: 'center',
                  background: selected ? 'var(--accent-soft)' : 'transparent',
                  cursor: 'default',
                }}
              >
                {columns.map((column) => (
                  <span key={column.name} style={cellStyle(column)}>
                    {format(row[column.name], column)}
                  </span>
                ))}
              </div>
            );
          })}
        </div>
      </div>

      {totalCount !== undefined && totalCount > rows.length ? (
        <div
          role="status"
          style={{
            flex: '0 0 auto',
            padding: '2px 8px',
            fontSize: 11,
            opacity: 0.75,
            borderTop: '1px solid var(--chrome-border)',
          }}
        >
          {/* Said plainly: a table showing 5,000 of 500,000 rows without
              saying so is a table someone will draw a conclusion from. */}
          Showing {rows.length.toLocaleString('en-US')} of{' '}
          {totalCount.toLocaleString('en-US')} features
        </div>
      ) : null}
    </div>
  );
}

function cellStyle(column: AttributeColumn): React.CSSProperties {
  const numeric = column.type === 'number';
  return {
    flex: '1 1 0',
    minWidth: 0,
    padding: '0 6px',
    textAlign: numeric ? 'right' : 'left',
    // Tabular figures on numbers: a column of porosities is scanned for
    // outliers by eye, and proportional digits defeat that.
    fontVariantNumeric: numeric ? 'tabular-nums' : 'normal',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    lineHeight: `${ROW_HEIGHT}px`,
  };
}

export function format(
  value: string | number | boolean | null | undefined,
  column: AttributeColumn,
): string {
  // An em dash, not an empty cell: null and "not measured here" are the same
  // thing to a reader, and a blank cell reads as a rendering gap.
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '—';
    return column.precision === undefined
      ? value.toLocaleString('en-US', { maximumFractionDigits: 6 })
      : value.toLocaleString('en-US', {
          minimumFractionDigits: column.precision,
          maximumFractionDigits: column.precision,
        });
  }
  return value;
}

function sortRows(
  rows: AttributeRow[],
  column: string,
  direction: 'asc' | 'desc',
): AttributeRow[] {
  const sign = direction === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const left = a[column];
    const right = b[column];
    // Nulls last in both directions. A column sorted descending that opens
    // with a screen of blanks looks broken, and the reader wanted the largest
    // values, not the missing ones.
    if (left === null || left === undefined) return right === null || right === undefined ? 0 : 1;
    if (right === null || right === undefined) return -1;
    if (typeof left === 'number' && typeof right === 'number') return (left - right) * sign;
    return String(left).localeCompare(String(right)) * sign;
  });
}
