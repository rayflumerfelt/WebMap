/**
 * Label formatting. `07-frontend.md` §6.2, `08-styling-palettes.md` §2.4.
 *
 * Four things here are decisions rather than fields, and each is the reason a
 * geologist's map either reads or does not:
 *
 * **Size mode is the reference-scale control, and both behaviours exist.**
 * *Fixed* keeps 12 pt at 12 pt however far the map is zoomed — the default,
 * and what a screen reader of a map expects. *Scale with map* keeps 12 pt at a
 * **reference zoom** and doubles it per zoom level in, so the text always
 * covers the same distance on the ground: a 500 ft label is big zoomed in and
 * small zoomed out. Picking that mode reveals the reference-zoom control, which
 * is meaningless without it.
 *
 * **The zoom window is the only thinning control**, because collision
 * detection is off (`08` §2.4). That makes it not an advanced option but the
 * mechanism by which a section grid stops being a wall of text, so it sits in
 * the main flow with an explanation rather than behind a disclosure.
 *
 * **The halo starts at none**, and that is a default rather than a rule. Over a
 * colour-filled grid a halo is usually the only thing that keeps text legible,
 * so the control is present and prominent; it simply does not start switched
 * on, because on a plain background a halo is noise.
 *
 * **The preview zooms.** The difference between the two size modes is invisible
 * in a still, so the preview steps through two zoom levels rather than
 * rendering once. A user who cannot see the difference will pick the wrong one.
 */

import type { LabelSymbol } from '@webmap/style-model';
import { ColorPicker, FontPicker } from '@webmap/ui';
// `exactOptionalPropertyTypes` is on and `LabelSymbol.minZoom` is `number?`,
// so clearing the field to "no limit" needs a spread that omits the key rather
// than one that sets it to undefined.
import type { FontFamily } from '@webmap/ui';
import { useEffect, useId, useState } from 'react';
import { Section, fieldRow } from './dialogParts.js';

export interface LabelSectionProps {
  symbol: LabelSymbol;
  fonts: FontFamily[];
  fields: Array<{ name: string; type: 'text' | 'number' }>;
  onChange(symbol: LabelSymbol): void;
}

const control = { height: 'var(--control-h, 28px)', fontSize: 12 } as const;

export function LabelSection({ symbol, fonts, fields, onChange }: LabelSectionProps) {
  const id = useId();
  const patch = (changes: Partial<LabelSymbol>) => onChange({ ...symbol, ...changes });
  const scales = symbol.sizeMode.mode === 'scale-with-map';

  return (
    <>
      <Section title="Label">
        <div style={fieldRow}>
          <label htmlFor={`${id}-field`} style={{ fontSize: 11, minWidth: 88 }}>
            Column
          </label>
          <select
            id={`${id}-field`}
            value={symbol.field}
            onChange={(event) => patch({ field: event.target.value })}
            style={{ ...control, width: 168 }}
          >
            {fields.map((field) => (
              <option key={field.name} value={field.name}>
                {field.name}
              </option>
            ))}
          </select>
        </div>

        <FontPicker
          value={symbol.font}
          onChange={(font) => patch({ font })}
          families={fonts}
        />

        <div style={fieldRow}>
          <label htmlFor={`${id}-size`} style={{ fontSize: 11, minWidth: 88 }}>
            Size
          </label>
          <input
            id={`${id}-size`}
            type="number"
            min={4}
            max={72}
            step={0.5}
            value={symbol.size}
            onChange={(event) => patch({ size: Math.max(1, Number(event.target.value)) })}
            style={{ ...control, width: 72, textAlign: 'right' }}
          />
          <span style={{ fontSize: 11, color: '#6b7078' }}>pt</span>
        </div>

        <ColorPicker
          label="Colour"
          value={symbol.color}
          onChange={(color) => patch({ color })}
        />
      </Section>

      <Section title="Size mode">
        <div style={fieldRow}>
          <label htmlFor={`${id}-mode`} style={{ fontSize: 11, minWidth: 88 }}>
            Behaviour
          </label>
          <select
            id={`${id}-mode`}
            value={symbol.sizeMode.mode}
            onChange={(event) =>
              patch({
                sizeMode:
                  event.target.value === 'fixed'
                    ? { mode: 'fixed' }
                    : // Carrying the previous reference zoom across the switch,
                      // so toggling the mode to look at the preview and back
                      // does not silently reset it to 12.
                      { mode: 'scale-with-map', referenceZoom: referenceZoomOf(symbol) },
              })
            }
            style={{ ...control, width: 224 }}
          >
            <option value="fixed">Fixed — {symbol.size} pt stays {symbol.size} pt</option>
            <option value="scale-with-map">
              Scale with map — same size on the ground
            </option>
          </select>
        </div>

        {scales ? (
          <>
            <div style={fieldRow}>
              <label htmlFor={`${id}-reference`} style={{ fontSize: 11, minWidth: 88 }}>
                At zoom
              </label>
              <input
                id={`${id}-reference`}
                type="number"
                min={0}
                max={22}
                step={0.5}
                value={referenceZoomOf(symbol)}
                onChange={(event) =>
                  patch({
                    sizeMode: {
                      mode: 'scale-with-map',
                      referenceZoom: Number(event.target.value),
                    },
                  })
                }
                style={{ ...control, width: 72, textAlign: 'right' }}
              />
            </div>
            <p style={{ fontSize: 11, color: '#6b7078', margin: 0 }}>
              The label is {symbol.size} pt at this zoom, twice that one level in and half it
              one level out — so it always covers the same distance on the ground.
            </p>
          </>
        ) : null}

        <LabelPreview symbol={symbol} />
      </Section>

      <Section title="Halo">
        <ColorPicker
          label="Colour"
          value={symbol.haloColor}
          onChange={(haloColor) => patch({ haloColor })}
          disabled={symbol.haloWidth === 0}
        />
        <div style={fieldRow}>
          <label htmlFor={`${id}-halo`} style={{ fontSize: 11, minWidth: 88 }}>
            Width
          </label>
          <input
            id={`${id}-halo`}
            type="number"
            min={0}
            max={8}
            step={0.25}
            value={symbol.haloWidth}
            onChange={(event) => patch({ haloWidth: Math.max(0, Number(event.target.value)) })}
            style={{ ...control, width: 72, textAlign: 'right' }}
          />
          <span style={{ fontSize: 11, color: '#6b7078' }}>px</span>
        </div>
        {symbol.haloWidth === 0 ? (
          <p style={{ fontSize: 11, color: '#6b7078', margin: 0 }}>
            No halo. Over a colour-filled grid a halo is usually what makes the text readable at
            all — try 1 px in the map's background colour.
          </p>
        ) : null}
      </Section>

      <Section title="Zoom window">
        <p style={{ fontSize: 11, color: '#6b7078', margin: 0 }}>
          Labels overlap on purpose — collision detection is off, so this is the only control
          that thins them. A section grid needs a narrow window; a handful of field names needs
          none.
        </p>
        <div style={fieldRow}>
          <label htmlFor={`${id}-min`} style={{ fontSize: 11, minWidth: 88 }}>
            Show from
          </label>
          <input
            id={`${id}-min`}
            type="number"
            min={0}
            max={22}
            step={0.5}
            value={symbol.minZoom ?? ''}
            placeholder="any"
            onChange={(event) => onChange(withZoom(symbol, 'minZoom', event.target.value))}
            style={{ ...control, width: 72, textAlign: 'right' }}
          />
          <label htmlFor={`${id}-max`} style={{ fontSize: 11, minWidth: 44 }}>
            to
          </label>
          <input
            id={`${id}-max`}
            type="number"
            min={0}
            max={22}
            step={0.5}
            value={symbol.maxZoom ?? ''}
            placeholder="any"
            onChange={(event) => onChange(withZoom(symbol, 'maxZoom', event.target.value))}
            style={{ ...control, width: 72, textAlign: 'right' }}
          />
        </div>
      </Section>
    </>
  );
}

/**
 * A two-zoom animation, because a still cannot show the difference.
 *
 * The point is not fidelity — it is one comparison: in *fixed* mode the text
 * stays the same size while the ground scale changes, and in *ground* mode it
 * grows with it. Anyone watching for two seconds knows which they wanted.
 */
function LabelPreview({ symbol }: { symbol: LabelSymbol }) {
  const [step, setStep] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setStep((current) => (current + 1) % 2), 1400);
    return () => clearInterval(timer);
  }, []);

  const zoomedIn = step === 1;
  // One zoom level in doubles the ground scale; a ground-sized label doubles
  // with it, and a fixed one does not.
  const pixels =
    symbol.size * (96 / 72) * (symbol.sizeMode.mode === 'fixed' ? 1 : zoomedIn ? 2 : 1);
  const groundWidth = zoomedIn ? '70%' : '35%';

  return (
    <div
      aria-label="Label size preview"
      role="img"
      style={{
        position: 'relative',
        height: 78,
        border: '1px solid #eceef1',
        borderRadius: 3,
        background: '#f2efe9',
        overflow: 'hidden',
        display: 'grid',
        placeItems: 'center',
      }}
    >
      {/* A section square, standing in for the ground. It is what changes size
          between the two frames; whether the text changes with it is the whole
          question. */}
      <div
        style={{
          position: 'absolute',
          width: groundWidth,
          height: groundWidth,
          border: '1px solid #b9ada0',
          transition: 'width 400ms, height 400ms',
        }}
      />
      <span
        style={{
          position: 'relative',
          fontSize: pixels,
          lineHeight: 1,
          color: symbol.color,
          transition: 'font-size 400ms',
          textShadow:
            symbol.haloWidth > 0
              ? `0 0 ${symbol.haloWidth * 2}px ${symbol.haloColor}`
              : undefined,
        }}
      >
        Sec 14
      </span>
      <span
        style={{
          position: 'absolute',
          bottom: 3,
          right: 6,
          fontSize: 10,
          color: '#6b7078',
        }}
      >
        {zoomedIn ? 'zoomed in' : 'zoomed out'}
      </span>
    </div>
  );
}

/**
 * The reference zoom the scaling arm carries, or the default for a label that
 * is currently fixed.
 *
 * 12 as the default because it is roughly township scale on a Permian map —
 * the zoom at which somebody choosing "scale with map" is usually looking, so
 * the size they set is the size they see rather than a number they then have
 * to correct.
 */
export const DEFAULT_REFERENCE_ZOOM = 12;

/**
 * Set or clear one end of the zoom window.
 *
 * Clearing **deletes the key** rather than setting it to undefined: the
 * compiler reads an absent `minZoom` as "no limit" and `exactOptionalPropertyTypes`
 * makes the difference a type error rather than a subtle one, which is the
 * point of having it on.
 */
function withZoom(
  symbol: LabelSymbol,
  key: 'minZoom' | 'maxZoom',
  raw: string,
): LabelSymbol {
  if (raw === '') {
    const next = { ...symbol };
    delete next[key];
    return next;
  }
  return { ...symbol, [key]: Number(raw) };
}

function referenceZoomOf(symbol: LabelSymbol): number {
  return symbol.sizeMode.mode === 'scale-with-map'
    ? symbol.sizeMode.referenceZoom
    : DEFAULT_REFERENCE_ZOOM;
}
