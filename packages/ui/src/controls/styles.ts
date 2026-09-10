/**
 * Shared appearance for the §6.3 controls. `07-frontend.md` §5.1, §5.3.
 *
 * Style objects rather than a stylesheet, because this package builds with
 * `tsc` and ships `dist/index.js` — a `.css` import would need a bundler in
 * every consumer, and §6.3's whole premise is that these components can be
 * lifted into another application unchanged.
 *
 * **Desktop is the base case.** Every size here is a pointer target, not a
 * fingertip one: 26 px rows and 28 px controls, so a formatting dialog shows
 * its whole form without scrolling. The values read from the CSS custom
 * properties in `apps/web/src/styles/layout.css` where the host defines them
 * and fall back to the same numbers where it does not, so a lifted component
 * looks right on its own and picks up the host's density when there is one.
 */

import type { CSSProperties } from 'react';

/** The row height a dense list uses. `--row-h` in the host application. */
export const ROW_HEIGHT = 'var(--row-h, 26px)';
/** The height of an input, select or button. `--control-h`. */
export const CONTROL_HEIGHT = 'var(--control-h, 28px)';
/** Invisible padding that makes a small target hittable. `--hit-slop`. */
export const HIT_SLOP = 'var(--hit-slop, 4px)';

/**
 * A checkerboard, drawn in CSS, behind anything that can be translucent.
 *
 * Without it a 20%-opacity swatch on a white panel is indistinguishable from
 * an 80%-white one, and the whole point of a transparency control is that its
 * effect is visible in the control.
 */
export const CHECKERBOARD: CSSProperties = {
  backgroundImage:
    'linear-gradient(45deg, #c8c8c8 25%, transparent 25%),' +
    'linear-gradient(-45deg, #c8c8c8 25%, transparent 25%),' +
    'linear-gradient(45deg, transparent 75%, #c8c8c8 75%),' +
    'linear-gradient(-45deg, transparent 75%, #c8c8c8 75%)',
  backgroundSize: '8px 8px',
  backgroundPosition: '0 0, 0 4px, 4px -4px, -4px 0',
  backgroundColor: '#fff',
};

export const control: CSSProperties = {
  height: CONTROL_HEIGHT,
  fontSize: 12,
  padding: '0 6px',
  border: '1px solid #c9ccd1',
  borderRadius: 3,
  background: '#fff',
  color: 'inherit',
  boxSizing: 'border-box',
};

export const numberControl: CSSProperties = {
  ...control,
  width: 72,
  textAlign: 'right',
};

export const row: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  minHeight: ROW_HEIGHT,
};

export const stack: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  fontSize: 12,
};

export const label: CSSProperties = {
  fontSize: 11,
  color: '#4a4f57',
  minWidth: 88,
};

export const iconButton: CSSProperties = {
  ...control,
  width: CONTROL_HEIGHT,
  padding: 0,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  cursor: 'pointer',
};

export const ghostButton: CSSProperties = {
  ...control,
  cursor: 'pointer',
  background: '#f6f7f9',
};

export const table: CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: 12,
};

export const cell: CSSProperties = {
  padding: '2px 4px',
  height: ROW_HEIGHT,
  borderBottom: '1px solid #eceef1',
  textAlign: 'left',
  fontWeight: 400,
};

export const hint: CSSProperties = {
  fontSize: 11,
  color: '#6b7078',
  margin: 0,
};

/**
 * The warning tone. Deliberately not red: none of the §6.3 controls warns
 * about anything that failed, only about choices with consequences — a
 * perceptually poor ramp, a cardinality cap, an overshoot. Red would put those
 * on the same footing as an error, and then real errors stop being read.
 */
export const caution: CSSProperties = {
  ...hint,
  color: '#8a5a00',
};
