/**
 * Family, weight and style — disabling what the family lacks.
 * `07-frontend.md` §6.2, §6.3, `08` §2.3.
 *
 * **Bold and italic are separate font stacks, not properties.** MapLibre reads
 * SDF glyphs from `GET /static/glyphs/{fontstack}/{range}.pbf`, so "Oswald
 * Bold" is a different stack from "Oswald" — there is no `text-weight`. This
 * control therefore edits a *stack name*, and its weight and style controls are
 * a way of composing one.
 *
 * That is also why the family list comes from the deployment rather than from
 * a constant: `GET /static/glyphs` lists what was actually built, so a font
 * whose build failed is **absent** rather than offered and then blank on the
 * map. And it is why the italic control is **disabled when the family has no
 * italic** — Oswald is the standing example — rather than offered and ignored.
 * A disabled control with a reason is information; one that silently does
 * nothing is a bug report.
 */

import { useId, useMemo } from 'react';
import { control, hint, label, row, stack } from './styles.js';

export interface FontFamily {
  /** The base stack name, e.g. `Inter`. */
  name: string;
  /** Stack names this deployment built for the family, e.g.
   *  `['Inter Regular', 'Inter Bold', 'Inter Italic']`. */
  stacks: string[];
}

export interface FontPickerProps {
  /** The MapLibre font stack, as stored on `LabelSymbol.font`. */
  value: string[];
  onChange(value: string[]): void;
  /** What `GET /static/glyphs` returned. */
  families: FontFamily[];
  className?: string | undefined;
}

/** Split a stack name into its family and the face words after it. */
export function splitStack(stackName: string, families: FontFamily[]): {
  family: string;
  bold: boolean;
  italic: boolean;
} {
  // Longest family name first, so "Noto Sans Bold" matches "Noto Sans" rather
  // than a hypothetical "Noto".
  const family =
    [...families]
      .map((entry) => entry.name)
      .sort((a, b) => b.length - a.length)
      .find((name) => stackName === name || stackName.startsWith(`${name} `)) ??
    stackName.replace(/\s+(Bold|Italic|Bold Italic|Regular)$/i, '');

  const face = stackName.slice(family.length).toLowerCase();
  return { family, bold: face.includes('bold'), italic: face.includes('italic') };
}

/** Compose the stack name for a family and face, or null if it was not built. */
export function composeStack(
  family: FontFamily,
  bold: boolean,
  italic: boolean,
): string | null {
  const wanted = [family.name, bold ? 'Bold' : '', italic ? 'Italic' : '']
    .filter(Boolean)
    .join(' ');
  const regular = `${family.name} Regular`;
  for (const candidate of [wanted, wanted === family.name ? regular : '']) {
    if (candidate && family.stacks.includes(candidate)) return candidate;
  }
  return null;
}

export function FontPicker({ value, onChange, families, className }: FontPickerProps) {
  const id = useId();
  const stackName = value[0] ?? '';
  const current = useMemo(() => splitStack(stackName, families), [stackName, families]);
  const family = families.find((entry) => entry.name === current.family) ?? families[0];

  const hasBold = family ? composeStack(family, true, current.italic) !== null : false;
  const hasItalic = family ? composeStack(family, current.bold, true) !== null : false;

  const set = (nextFamily: FontFamily, bold: boolean, italic: boolean) => {
    const composed = composeStack(nextFamily, bold, italic);
    // Falling back to the plain family rather than to nothing: switching from
    // Inter Bold Italic to Oswald should land on Oswald, not on an empty stack
    // that MapLibre resolves to its own default without saying so.
    onChange([composed ?? composeStack(nextFamily, false, false) ?? nextFamily.name]);
  };

  return (
    <div className={className} style={stack}>
      <div style={row}>
        <label htmlFor={`${id}-family`} style={label}>
          Font
        </label>
        <select
          id={`${id}-family`}
          value={family?.name ?? ''}
          onChange={(event) => {
            const chosen = families.find((entry) => entry.name === event.target.value);
            if (chosen) set(chosen, current.bold, current.italic);
          }}
          style={{ ...control, width: 168 }}
        >
          {families.map((entry) => (
            <option key={entry.name} value={entry.name}>
              {entry.name}
            </option>
          ))}
        </select>
      </div>

      <div style={row}>
        <span style={label}>Style</span>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <input
            type="checkbox"
            checked={current.bold}
            disabled={!family || !hasBold}
            onChange={(event) => family && set(family, event.target.checked, current.italic)}
          />
          <strong>Bold</strong>
        </label>
        <label
          style={{ display: 'flex', alignItems: 'center', gap: 4 }}
          title={
            family && !hasItalic
              ? `${family.name} has no italic in this deployment.`
              : undefined
          }
        >
          <input
            type="checkbox"
            checked={current.italic}
            disabled={!family || !hasItalic}
            onChange={(event) => family && set(family, current.bold, event.target.checked)}
          />
          <em>Italic</em>
        </label>
      </div>

      {family && !hasItalic ? (
        <p style={hint}>{family.name} has no italic face in this deployment.</p>
      ) : null}

      <div
        style={{
          border: '1px solid #eceef1',
          borderRadius: 3,
          padding: '6px 8px',
          fontSize: 14,
          fontWeight: current.bold ? 700 : 400,
          fontStyle: current.italic ? 'italic' : 'normal',
          fontFamily: `"${family?.name ?? 'inherit'}", system-ui, sans-serif`,
        }}
      >
        {/* The stack name, not lorem ipsum: what is stored is the string the
            tile server is asked for, and seeing it is how a missing glyph
            range gets diagnosed. */}
        {stackName || 'No font selected'}
      </div>
    </div>
  );
}
