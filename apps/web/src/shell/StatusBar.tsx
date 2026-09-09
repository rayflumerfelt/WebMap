/**
 * Status bar. `07-frontend.md` §5.2.
 *
 * "Always visible and always shows the analysis CRS, live cursor coordinates
 * in that CRS, and the current map scale. Geologists check these constantly;
 * burying them in a menu is a daily irritation."
 *
 * **Coordinates are shown in the analysis CRS, not in longitude/latitude.**
 * A geologist working a Texas Central project reads and writes State Plane
 * feet all day; showing them −102.08, 31.99 means converting in their head
 * every time they want to check a location against a well file. The map's
 * native frame is Web Mercator and that is an implementation detail of the
 * renderer, not something to put on screen.
 */

export interface StatusBarProps {
  /** e.g. "EPSG:2277 · NAD83 / Texas Central (ftUS)". */
  crsLabel: string;
  /** Cursor position already transformed into the analysis CRS, or null when
   *  the pointer is off the map. */
  cursor: { x: number; y: number } | null;
  /** Horizontal unit of the analysis CRS, for the coordinate readout. */
  unit: 'ft' | 'usft' | 'm';
  /** Denominator of the representative fraction: 24000 renders "1:24,000". */
  scaleDenominator: number | null;
  /** In-flight job, if any. Gridding finishes while attention is elsewhere. */
  job?: { label: string; progress: number | null } | null;
  /** Autosave state, so "did my change save" is answerable without asking. */
  save?: { dirty: boolean; conflict: boolean; lastSavedAt: number | null };
}

export function StatusBar({
  crsLabel,
  cursor,
  unit,
  scaleDenominator,
  job,
  save,
}: StatusBarProps) {
  return (
    <footer
      style={{
        height: 'var(--statusbar-h, 24px)',
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        padding: '0 8px',
        fontSize: 11,
        borderTop: '1px solid var(--chrome-border)',
        background: 'var(--chrome-bg)',
        // Tabular figures: the coordinate readout updates on every mouse move,
        // and proportional digits make it jitter horizontally, which is
        // genuinely hard to read.
        fontVariantNumeric: 'tabular-nums',
      }}
    >
      <span title="Analysis coordinate reference system">{crsLabel}</span>

      <span aria-live="off" style={{ minWidth: 200 }}>
        {cursor ? formatCoordinate(cursor, unit) : '—'}
      </span>

      <span>{scaleDenominator ? `1:${Math.round(scaleDenominator).toLocaleString('en-US')}` : '—'}</span>

      <span style={{ flex: 1 }} />

      {save ? <SaveStatus {...save} /> : null}

      {job ? (
        // §10: a live region, because gridding finishes while attention is
        // elsewhere and a silent completion is a job nobody collects.
        <span aria-live="polite" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {job.label}
          {job.progress !== null ? ` ${Math.round(job.progress * 100)}%` : ''}
        </span>
      ) : null}
    </footer>
  );
}

function SaveStatus({
  dirty,
  conflict,
  lastSavedAt,
}: {
  dirty: boolean;
  conflict: boolean;
  lastSavedAt: number | null;
}) {
  if (conflict) {
    // Not a quiet indicator: autosave has stopped, and continuing to edit
    // builds up changes that will not be written.
    return (
      <span role="status" style={{ color: '#a33', fontWeight: 600 }}>
        Not saving — this session changed elsewhere. Reload to continue.
      </span>
    );
  }
  if (dirty) return <span role="status">Unsaved changes…</span>;
  if (lastSavedAt) return <span role="status">Saved</span>;
  return null;
}

/**
 * A projected coordinate, rounded to the precision the unit justifies.
 *
 * Feet to the nearest foot and metres to the nearest metre: a State Plane
 * easting has seven significant figures before the decimal point, and showing
 * three after it claims a precision no map interaction has.
 */
export function formatCoordinate(
  cursor: { x: number; y: number },
  unit: 'ft' | 'usft' | 'm',
): string {
  const suffix = unit === 'm' ? 'm' : 'ft';
  const round = (value: number) => Math.round(value).toLocaleString('en-US');
  // E/N rather than X/Y: that is how a survey plat labels them, and it removes
  // any doubt about which number is which.
  return `E ${round(cursor.x)} · N ${round(cursor.y)} ${suffix}`;
}

/**
 * Representative-fraction denominator for a Web Mercator view.
 *
 * Uses the same 1/cos(latitude) correction as the scale bar — a scale printed
 * on a map is a claim, and "1:24,000" that is really 1:28,000 is a wrong one.
 */
export function scaleDenominatorFor(
  latitude: number,
  zoom: number,
  devicePixelRatio = 1,
): number {
  const EQUATORIAL_CIRCUMFERENCE_M = 40_075_016.686;
  const metersPerPixel =
    (EQUATORIAL_CIRCUMFERENCE_M * Math.cos((latitude * Math.PI) / 180)) / 2 ** (zoom + 9);
  // 0.28 mm per pixel is the OGC standardized rendering pixel size, which is
  // what makes a printed scale mean anything.
  const METERS_PER_CSS_PIXEL = 0.00028;
  return (metersPerPixel * devicePixelRatio) / METERS_PER_CSS_PIXEL;
}
