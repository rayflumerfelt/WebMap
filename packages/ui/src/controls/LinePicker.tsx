/**
 * Line colour, width, dash, cap and join, with a preview.
 * `07-frontend.md` §6.2, §6.3.
 *
 * **Dash type is a fixed choice, never by column value.** MapLibre's
 * `line-dasharray` accepts no property expressions (`08` §2.2), so a control
 * that offered "dash by category" would produce a style the map silently drops
 * — the layer draws solid and nothing says why. A layer that needs dash by
 * category uses rule-based symbology, which compiles to one layer per rule.
 *
 * The preview is not decoration either. `[2, 4]` and `[4, 2]` are the same
 * numbers in the same order to read and completely different lines to look at,
 * and dash arrays are in **line widths**, so the same array on a 1 px and a
 * 4 px line looks nothing alike. The preview draws at the chosen width for
 * exactly that reason.
 */

import { useId } from 'react';
import { ColorPicker } from './ColorPicker.js';
import { control, hint, label, row, stack } from './styles.js';

export interface LineValue {
  color: string;
  width: number;
  opacity?: number | undefined;
  /** In **line widths**, not pixels — MapLibre's own convention. `undefined`
   *  is solid, and is passed explicitly when the Solid pattern is chosen. */
  dashArray?: number[] | undefined;
  cap: 'butt' | 'round' | 'square';
  join: 'bevel' | 'round' | 'miter';
}

export interface LinePickerProps {
  value: LineValue;
  onChange(value: LineValue): void;
  className?: string;
}

/**
 * The dash patterns offered, in line widths.
 *
 * A named set rather than a free numeric array: the array is the storage
 * format and a terrible input control, and these five cover what a geological
 * map actually uses — solid for observed, dashed for inferred, dotted for
 * concealed, and the two dash-dot forms cartographers use for boundaries.
 * A caller with its own array still round-trips, because `patternName` falls
 * back to "Custom" rather than snapping it to the nearest preset.
 */
export const DASH_PATTERNS: Array<{ name: string; dash: number[] | undefined }> = [
  { name: 'Solid', dash: undefined },
  { name: 'Dashed', dash: [4, 2] },
  { name: 'Dotted', dash: [1, 2] },
  { name: 'Dash-dot', dash: [4, 2, 1, 2] },
  { name: 'Long dash', dash: [8, 3] },
];

export function patternName(dash: number[] | undefined): string {
  const match = DASH_PATTERNS.find(
    (pattern) =>
      (pattern.dash === undefined && (dash === undefined || dash.length === 0)) ||
      (pattern.dash !== undefined &&
        dash !== undefined &&
        pattern.dash.length === dash.length &&
        pattern.dash.every((value, i) => value === dash[i])),
  );
  return match ? match.name : 'Custom';
}

export function LinePicker({ value, onChange, className }: LinePickerProps) {
  const id = useId();
  const patch = (changes: Partial<LineValue>) => onChange({ ...value, ...changes });
  const current = patternName(value.dashArray);

  return (
    <div className={className} style={stack}>
      <ColorPicker
        label="Colour"
        value={value.color}
        onChange={(color) => patch({ color })}
        opacity={value.opacity}
        onOpacityChange={
          value.opacity === undefined ? undefined : (opacity) => patch({ opacity })
        }
      />

      <div style={row}>
        <label htmlFor={`${id}-width`} style={label}>
          Width
        </label>
        <input
          id={`${id}-width`}
          type="number"
          min={0}
          max={40}
          step={0.25}
          value={value.width}
          onChange={(event) => patch({ width: Math.max(0, Number(event.target.value)) })}
          style={{ ...control, width: 72, textAlign: 'right' }}
        />
        <span style={hint}>px</span>
      </div>

      <div style={row}>
        <label htmlFor={`${id}-dash`} style={label}>
          Type
        </label>
        <select
          id={`${id}-dash`}
          value={current}
          onChange={(event) => {
            const chosen = DASH_PATTERNS.find((p) => p.name === event.target.value);
            patch({ dashArray: chosen?.dash });
          }}
          style={{ ...control, width: 132 }}
        >
          {current === 'Custom' ? <option value="Custom">Custom</option> : null}
          {DASH_PATTERNS.map((pattern) => (
            <option key={pattern.name} value={pattern.name}>
              {pattern.name}
            </option>
          ))}
        </select>
      </div>

      <div style={row}>
        <label htmlFor={`${id}-cap`} style={label}>
          Ends
        </label>
        <select
          id={`${id}-cap`}
          value={value.cap}
          onChange={(event) => patch({ cap: event.target.value as LineValue['cap'] })}
          style={{ ...control, width: 96 }}
        >
          <option value="butt">Flat</option>
          <option value="round">Round</option>
          <option value="square">Square</option>
        </select>

        <label htmlFor={`${id}-join`} style={{ ...label, minWidth: 44 }}>
          Corners
        </label>
        <select
          id={`${id}-join`}
          value={value.join}
          onChange={(event) => patch({ join: event.target.value as LineValue['join'] })}
          style={{ ...control, width: 96 }}
        >
          <option value="miter">Mitre</option>
          <option value="round">Round</option>
          <option value="bevel">Bevel</option>
        </select>
      </div>

      <LinePreview value={value} />
    </div>
  );
}

function LinePreview({ value }: { value: LineValue }) {
  // Drawn as SVG at the chosen width, because a dash array is in line widths:
  // [4, 2] on a 1 px line and on a 4 px line are different pictures, and a
  // preview at a fixed width would show the wrong one.
  const width = Math.max(0.5, value.width);
  const dash = value.dashArray?.length
    ? value.dashArray.map((segment) => segment * width).join(' ')
    : undefined;

  return (
    <svg
      role="img"
      aria-label={`Preview: ${patternName(value.dashArray)} line, ${value.width} px`}
      width="100%"
      height={Math.max(24, width + 12)}
      style={{ border: '1px solid #eceef1', borderRadius: 3, background: '#fff' }}
    >
      <line
        x1="6"
        x2="98%"
        y1="50%"
        y2="50%"
        stroke={value.color}
        strokeWidth={width}
        strokeOpacity={value.opacity ?? 1}
        strokeDasharray={dash}
        strokeLinecap={value.cap === 'butt' ? 'butt' : value.cap}
        strokeLinejoin={value.join}
      />
    </svg>
  );
}
