/**
 * Text value → colour rows. `07-frontend.md` §6.2, §6.3.
 *
 * **Sorted by descending count of occurrences**, which is not a display
 * preference: a formation column has six values that matter and forty that
 * appear once, and alphabetical order buries the six. The counts come from a
 * distinct-values query — nothing MapLibre offers — and this control renders
 * what that query returned rather than computing anything itself.
 *
 * **`Other` is pinned to the bottom and defaults to light grey.** It is not a
 * category; it is what happens to every value not listed, including values
 * that appeared after the palette was made. A map with no *Other* row draws
 * those features invisibly, which reads as missing data.
 *
 * Two limits are surfaced rather than hidden. The list is capped at the top few
 * hundred and says how many values it did not show; and above a cardinality
 * threshold the endpoint refuses to enumerate at all — a well-name column on
 * 500k features has 500k distinct values and no useful colour mapping. The
 * refusal arrives here as `truncated`/`refused` and is stated in the control,
 * because a dialog that simply shows fewer rows looks like it lost some.
 */

import { useMemo, useState } from 'react';
import { ColorPicker } from './ColorPicker.js';
import { caution, cell, control, ghostButton, hint, iconButton, stack, table } from './styles.js';

export interface CategoryRow {
  value: string;
  color: string;
  /** How many features carry this value. Drives the sort. */
  count?: number;
  label?: string;
}

export interface CategoryTableProps {
  rows: CategoryRow[];
  onChange(rows: CategoryRow[]): void;
  /** The *Other* colour, or null when the layer has no *Other* row. */
  otherColor: string | null;
  onOtherChange(color: string | null): void;
  /** Distinct values the query found but did not return. */
  remaining?: number;
  /**
   * Set when the column's cardinality was too high to enumerate. The rows are
   * then whatever the user has added by hand, and the control says so.
   */
  refused?: { distinct: number; limit: number } | null;
  className?: string;
}

/** The default *Other* colour. Light grey: present, and obviously not a
 *  category anyone chose. */
export const OTHER_DEFAULT = '#d9d9d9';

export function CategoryTable(props: CategoryTableProps) {
  const { rows, onChange, otherColor, onOtherChange, remaining, refused, className } = props;
  const [filter, setFilter] = useState('');

  const ordered = useMemo(
    () =>
      [...rows].sort((a, b) => {
        const byCount = (b.count ?? 0) - (a.count ?? 0);
        // Alphabetical only to break a tie, so a column with no counts at all
        // is still in a stable, readable order rather than in query order.
        return byCount !== 0 ? byCount : a.value.localeCompare(b.value);
      }),
    [rows],
  );

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? ordered.filter((row) => row.value.toLowerCase().includes(needle)) : ordered;
  }, [ordered, filter]);

  const setRow = (value: string, patch: Partial<CategoryRow>) =>
    onChange(rows.map((row) => (row.value === value ? { ...row, ...patch } : row)));

  return (
    <div className={className} style={stack}>
      {ordered.length > 12 ? (
        <input
          type="search"
          value={filter}
          placeholder="Filter values"
          aria-label="Filter values"
          onChange={(event) => setFilter(event.target.value)}
          style={{ ...control, width: '100%' }}
        />
      ) : null}

      <table style={table}>
        <thead>
          <tr>
            <th style={cell} scope="col">
              Value
            </th>
            <th style={{ ...cell, width: 64, textAlign: 'right' }} scope="col">
              Features
            </th>
            <th style={cell} scope="col">
              Colour
            </th>
            <th style={{ ...cell, width: 32 }} scope="col">
              <span style={{ position: 'absolute', left: -9999 }}>Remove</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((row) => (
            <tr key={row.value}>
              <td style={cell}>{row.label ?? row.value}</td>
              <td style={{ ...cell, textAlign: 'right', color: '#6b7078' }}>
                {row.count === undefined ? '—' : row.count.toLocaleString()}
              </td>
              <td style={cell}>
                <ColorPicker
                  value={row.color}
                  onChange={(color) => setRow(row.value, { color })}
                />
              </td>
              <td style={cell}>
                <button
                  type="button"
                  aria-label={`Remove ${row.value}`}
                  onClick={() => onChange(rows.filter((other) => other.value !== row.value))}
                  style={{ ...iconButton, height: 22, width: 22 }}
                >
                  −
                </button>
              </td>
            </tr>
          ))}

          {/* Pinned last, and outside the filter: hiding the fallback while
              searching would make it look as though the layer had none. */}
          <tr>
            <td style={{ ...cell, fontStyle: 'italic' }}>Other</td>
            <td style={{ ...cell, textAlign: 'right', color: '#6b7078' }}>
              {remaining ? remaining.toLocaleString() : '—'}
            </td>
            <td style={cell}>
              {otherColor === null ? (
                <button
                  type="button"
                  onClick={() => onOtherChange(OTHER_DEFAULT)}
                  style={{ ...ghostButton, height: 22 }}
                >
                  Add Other
                </button>
              ) : (
                <ColorPicker value={otherColor} onChange={onOtherChange} />
              )}
            </td>
            <td style={cell}>
              {otherColor === null ? null : (
                <button
                  type="button"
                  aria-label="Remove Other"
                  onClick={() => onOtherChange(null)}
                  style={{ ...iconButton, height: 22, width: 22 }}
                >
                  −
                </button>
              )}
            </td>
          </tr>
        </tbody>
      </table>

      {otherColor === null ? (
        <p style={caution}>
          With no <em>Other</em> colour, any value not listed draws invisibly — which on the map
          is indistinguishable from missing data.
        </p>
      ) : null}

      {remaining ? (
        <p style={hint}>
          {remaining.toLocaleString()} more distinct value{remaining === 1 ? '' : 's'} are not
          listed; they take the <em>Other</em> colour.
        </p>
      ) : null}

      {refused ? (
        <p style={caution}>
          This column has {refused.distinct.toLocaleString()} distinct values, past the{' '}
          {refused.limit.toLocaleString()} the server will enumerate. Add the few you want to
          pick out by hand, or colour by a column with fewer values.
        </p>
      ) : null}
    </div>
  );
}
