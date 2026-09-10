/**
 * Colour, with optional opacity. `07-frontend.md` §6.3.
 *
 * Used by the fixed-colour mode, every ramp stop, every category row, the
 * *Other* row and the no-data colour — which is why it takes a colour and an
 * `onChange` and knows nothing about any of them.
 *
 * **The native `<input type="color">` does the picking.** It is the one
 * control browsers give us that geologists already know, it is keyboard
 * reachable, and it opens the operating system's picker with its eyedropper —
 * which on a colour tool is worth more than any custom wheel. What it cannot
 * do is alpha, so opacity is a separate slider beside it, shown only where the
 * caller says the property has one.
 *
 * The hex field is not decoration. A palette from a partner's deck arrives as
 * `#1f78b4` in an email, and typing it is faster than hunting for it in a
 * picker; it also makes the current value copyable, which the native input
 * alone does not.
 */

import { useEffect, useId, useState } from 'react';
import { CHECKERBOARD, CONTROL_HEIGHT, control, label, row } from './styles.js';

export interface ColorPickerProps {
  /** `#rrggbb`. Case-insensitive on the way in, lowercased on the way out. */
  value: string;
  onChange(value: string): void;
  /** Opacity 0–1. Omit to hide the slider entirely.
   *
   *  `| undefined` explicitly, because `exactOptionalPropertyTypes` is on and
   *  callers legitimately pass `undefined` to mean "this property has no
   *  opacity" — a line casing does not, a fill does. */
  opacity?: number | undefined;
  onOpacityChange?: ((opacity: number) => void) | undefined;
  label?: string | undefined;
  disabled?: boolean | undefined;
  className?: string | undefined;
}

const HEX = /^#[0-9a-f]{6}$/i;

/** `#abc` → `#aabbcc`, and anything already long left alone. */
export function expandHex(text: string): string | null {
  const trimmed = text.trim();
  const withHash = trimmed.startsWith('#') ? trimmed : `#${trimmed}`;
  if (/^#[0-9a-f]{3}$/i.test(withHash)) {
    const [r, g, b] = [withHash[1], withHash[2], withHash[3]];
    return `#${r}${r}${g}${g}${b}${b}`.toLowerCase();
  }
  return HEX.test(withHash) ? withHash.toLowerCase() : null;
}

export function ColorPicker(props: ColorPickerProps) {
  const { value, onChange, opacity, onOpacityChange, disabled, className } = props;
  const id = useId();

  // The typed hex is local until it parses. Committing on every keystroke
  // makes `#1f78b4` pass through `#1`, `#1f`, `#1f7` — three invalid colours
  // that each repaint the map before the fourth character arrives.
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const commit = (text: string) => {
    const parsed = expandHex(text);
    if (parsed) onChange(parsed);
    else setDraft(value);
  };

  return (
    <div className={className} style={{ ...row, gap: 6 }}>
      {props.label ? (
        <label htmlFor={`${id}-hex`} style={label}>
          {props.label}
        </label>
      ) : null}

      <span style={{ ...CHECKERBOARD, display: 'inline-flex', borderRadius: 3 }}>
        <input
          type="color"
          aria-label={props.label ? `${props.label} colour` : 'Colour'}
          value={HEX.test(value) ? value.toLowerCase() : '#000000'}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value.toLowerCase())}
          style={{
            width: 34,
            height: CONTROL_HEIGHT,
            padding: 0,
            border: '1px solid #c9ccd1',
            borderRadius: 3,
            background: 'transparent',
            cursor: disabled ? 'default' : 'pointer',
            // Opacity is applied to the preview so the checkerboard shows
            // through it — the control has to show what it is setting.
            opacity: opacity ?? 1,
          }}
        />
      </span>

      <input
        id={`${id}-hex`}
        type="text"
        inputMode="text"
        spellCheck={false}
        aria-label={props.label ? `${props.label} hex value` : 'Hex value'}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={(event) => commit(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') commit((event.target as HTMLInputElement).value);
          if (event.key === 'Escape') setDraft(value);
        }}
        style={{ ...control, width: 84, fontFamily: 'ui-monospace, monospace' }}
      />

      {opacity !== undefined && onOpacityChange ? (
        <>
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={opacity}
            disabled={disabled}
            aria-label={props.label ? `${props.label} opacity` : 'Opacity'}
            onChange={(event) => onOpacityChange(Number(event.target.value))}
            style={{ width: 72 }}
          />
          <span style={{ fontSize: 11, color: '#4a4f57', width: 32, textAlign: 'right' }}>
            {Math.round(opacity * 100)}%
          </span>
        </>
      ) : null}
    </div>
  );
}
