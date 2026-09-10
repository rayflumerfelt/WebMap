/**
 * The editing status strip. `09-editing.md` §10.4.
 *
 * **Telemetry, not controls** — which is what keeps it out of §10.1's
 * ten-control budget. Bottom-left, read-only, with one exception noted below.
 *
 * The snap readout is the item that earns its place: *"⊾ vertex · Leases"*
 * versus *"no snap"* is the difference between "the tool is broken" and "I am
 * snapped to the wrong layer", and a user who cannot tell those apart stops
 * trusting the editor.
 *
 * The live measurement is the second: in land and lease work **acreage is the
 * number everyone is actually watching**, so a selection summary that says
 * `3 features · 640.2 ac` answers the question before it is asked.
 *
 * The selection count carries a clear button — the exception to read-only —
 * because it doubles as Select None and because it explains why half the
 * commands are greyed.
 */

import type { CSSProperties } from 'react';

import type { SnapType } from './snap.js';

export interface SnapReadout {
  type: SnapType;
  layerName: string;
  /** False while still tile-derived (§6.6). Rendered hollow, as the map
   *  indicator is, so the two agree. */
  isExact: boolean;
}

export interface EditStatusStripProps {
  /** Cursor position in the working CRS, already formatted by the caller —
   *  this component does no coordinate maths and no reprojection. */
  cursor: string | null;
  crsLabel: string;
  snap: SnapReadout | null;
  /** True when snapping is switched off, which is a different message from
   *  "snapping is on and nothing is in range". */
  snapDisabled?: boolean;
  /** While drawing: the running length or area. Idle: the selection summary. */
  measurement: string | null;
  selectedCount: number;
  onClearSelection(): void;
  className?: string | undefined;
}

const strip: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 16,
  height: 'var(--statusbar-h, 24px)',
  padding: '0 8px',
  borderTop: '1px solid #d7dae0',
  background: '#f6f7f9',
  fontSize: 11,
  color: '#4a4f57',
  fontVariantNumeric: 'tabular-nums',
};

const GLYPH: Record<SnapType, string> = {
  // The same shapes §6.7 gives the map indicator, so the strip and the map say
  // the same thing in the same alphabet.
  vertex: '▪',
  edge: '●',
  intersection: '✕',
  midpoint: '▲',
};

export function EditStatusStrip(props: EditStatusStripProps) {
  const {
    cursor,
    crsLabel,
    snap,
    snapDisabled = false,
    measurement,
    selectedCount,
    onClearSelection,
    className,
  } = props;

  return (
    <div className={className} style={strip} role="status" aria-label="Editing status">
      <span title={crsLabel}>
        {cursor ?? '—'} <span style={{ opacity: 0.7 }}>{crsLabel}</span>
      </span>

      <span aria-label="Snap state">
        {snapDisabled ? (
          <span style={{ opacity: 0.7 }}>snapping off</span>
        ) : snap ? (
          <span style={{ color: snap.isExact ? '#1c5cc4' : '#6b7078' }}>
            {GLYPH[snap.type]} {snap.type} · {snap.layerName}
            {snap.isExact ? '' : ' (approx)'}
          </span>
        ) : (
          <span style={{ opacity: 0.7 }}>no snap</span>
        )}
      </span>

      {measurement ? <span aria-label="Measurement">{measurement}</span> : null}

      {selectedCount > 0 ? (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          {selectedCount} selected
          <button
            type="button"
            aria-label="Clear selection"
            title="Clear selection"
            onClick={onClearSelection}
            style={{
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
              color: 'inherit',
              // A hit area larger than the glyph: at 11 px the ✕ alone is a
              // target nobody can hit twice in a row.
              padding: 'var(--hit-slop, 4px)',
              lineHeight: 1,
            }}
          >
            ✕
          </button>
        </span>
      ) : null}
    </div>
  );
}

/**
 * `3 features · 640.2 ac` — the idle selection summary.
 *
 * Acreage to one decimal place: a lease is quoted to the tenth of an acre and
 * carrying more digits implies a precision the geometry does not have, while
 * carrying fewer loses the number people compare against a lease document.
 */
export function selectionSummary(count: number, acres: number | null): string | null {
  if (count === 0) return null;
  const features = `${count} feature${count === 1 ? '' : 's'}`;
  if (acres === null) return features;
  return `${features} · ${acres.toLocaleString(undefined, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })} ac`;
}
