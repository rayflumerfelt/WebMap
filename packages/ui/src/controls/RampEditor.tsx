/**
 * Gradient stops, over a histogram of the data. `07-frontend.md` §6.3, §5.
 *
 * A ramp is only right or wrong relative to the values it colours. Viridis
 * across 0–100 on a porosity layer whose values all sit between 6 and 14 gives
 * the whole map three neighbouring greens, and the editor that shows only the
 * ramp cannot say so. **The histogram underlay is the point of this control**,
 * not an embellishment: it puts the stops and the data on one axis, where a
 * ramp that misses the data is visible at a glance.
 *
 * Positions are normalised 0–1 and kept ascending. The editor sorts on every
 * change rather than constraining the drag, because a stop dragged past its
 * neighbour is an ordinary thing to do and refusing it mid-gesture feels
 * broken; what must not happen is a palette *stored* out of order, since
 * `colourAt` interpolates between adjacent entries and would run backwards.
 */

import type { Palette } from '@webmap/style-model';
import { colourAt } from '@webmap/style-model';
import { useId, useMemo, useRef, useState } from 'react';
import { ColorPicker } from './ColorPicker.js';
import { caution, cell, ghostButton, hint, iconButton, stack, table } from './styles.js';

export interface RampEditorProps {
  palette: Palette;
  onChange(palette: Palette): void;
  /**
   * Counts per equal-width bin over the layer's value range, as returned by
   * the attribute-summary endpoint. Omit and the strip renders without an
   * underlay — the ramp is still editable, it is just harder to judge.
   */
  histogram?: number[];
  /** The data range the histogram spans, for the axis labels. */
  domain?: [number, number];
  /** Shown under the strip. Use it for the perceptual caveat on a spectral
   *  ramp — `08` §5.1 keeps the option and warns rather than removing it. */
  note?: string;
  className?: string;
}

/** How many samples the preview strip draws. Enough to look continuous at any
 *  panel width without building a gradient string per pixel. */
const STRIP_SAMPLES = 96;

export function RampEditor({
  palette,
  onChange,
  histogram,
  domain,
  note,
  className,
}: RampEditorProps) {
  const id = useId();
  const stripRef = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState(0);

  const strip = useMemo(
    () =>
      Array.from({ length: STRIP_SAMPLES }, (_, i) =>
        colourAt(palette, i / (STRIP_SAMPLES - 1)),
      ),
    [palette],
  );

  const commit = (stops: Palette['stops']) =>
    onChange({ ...palette, stops: [...stops].sort((a, b) => a.position - b.position) });

  const setStop = (index: number, patch: Partial<Palette['stops'][number]>) => {
    const next = palette.stops.map((stop, i) => (i === index ? { ...stop, ...patch } : stop));
    commit(next);
  };

  const addStop = () => {
    // Halfway between the selected stop and the next one, taking that
    // midpoint's current colour — so adding a stop changes nothing until it is
    // moved, which is what makes it safe to add one to look at.
    const here = palette.stops[Math.min(selected, palette.stops.length - 1)];
    const next = palette.stops[Math.min(selected + 1, palette.stops.length - 1)];
    const position = here && next ? (here.position + next.position) / 2 : 0.5;
    commit([...palette.stops, { position, color: colourAt(palette, position) }]);
  };

  const removeStop = (index: number) => {
    if (palette.stops.length <= 2) return;
    commit(palette.stops.filter((_, i) => i !== index));
    setSelected((current) => Math.max(0, Math.min(current, palette.stops.length - 2)));
  };

  const positionFromPointer = (clientX: number): number => {
    const box = stripRef.current?.getBoundingClientRect();
    if (!box || box.width === 0) return 0;
    return Math.min(1, Math.max(0, (clientX - box.left) / box.width));
  };

  const peak = histogram && histogram.length ? Math.max(...histogram, 1) : 1;

  return (
    <div className={className} style={stack}>
      <div style={{ position: 'relative' }}>
        {histogram && histogram.length ? (
          <div
            aria-hidden="true"
            style={{
              display: 'flex',
              alignItems: 'flex-end',
              gap: 1,
              height: 40,
              padding: '0 0 2px',
            }}
          >
            {histogram.map((count, i) => (
              <div
                key={i}
                style={{
                  flex: 1,
                  // A square-root scale, not linear. A well-control histogram
                  // is routinely one tall bin and forty short ones, and on a
                  // linear axis the forty are invisible — which is exactly the
                  // part of the distribution a ramp has to cover.
                  height: `${Math.sqrt(count / peak) * 100}%`,
                  minHeight: count > 0 ? 1 : 0,
                  background: '#b8bec7',
                }}
              />
            ))}
          </div>
        ) : null}

        <div
          ref={stripRef}
          role="img"
          aria-label="Colour ramp preview"
          style={{
            display: 'flex',
            height: 20,
            border: '1px solid #c9ccd1',
            borderRadius: 3,
            overflow: 'hidden',
          }}
        >
          {strip.map((colour, i) => (
            <div key={i} style={{ flex: 1, background: colour }} />
          ))}
        </div>

        <div style={{ position: 'relative', height: 14 }}>
          {palette.stops.map((stop, index) => (
            <button
              key={index}
              type="button"
              aria-label={`Stop ${index + 1} at ${(stop.position * 100).toFixed(0)}%`}
              aria-pressed={index === selected}
              onPointerDown={(event) => {
                setSelected(index);
                event.currentTarget.setPointerCapture(event.pointerId);
              }}
              onPointerMove={(event) => {
                if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
                setStop(index, { position: positionFromPointer(event.clientX) });
              }}
              onKeyDown={(event) => {
                // Keyboard is not the fallback path here; a stop at exactly
                // 0.25 is easier to type than to drag, and §10 requires every
                // pointer gesture to have one.
                const step = event.shiftKey ? 0.1 : 0.01;
                if (event.key === 'ArrowLeft') {
                  setStop(index, { position: Math.max(0, stop.position - step) });
                  event.preventDefault();
                }
                if (event.key === 'ArrowRight') {
                  setStop(index, { position: Math.min(1, stop.position + step) });
                  event.preventDefault();
                }
                if (event.key === 'Delete' || event.key === 'Backspace') {
                  removeStop(index);
                  event.preventDefault();
                }
              }}
              style={{
                position: 'absolute',
                left: `calc(${stop.position * 100}% - 6px)`,
                top: 0,
                width: 12,
                height: 12,
                padding: 0,
                borderRadius: '50%',
                border: `2px solid ${index === selected ? '#1c5cc4' : '#4a4f57'}`,
                background: stop.color,
                cursor: 'ew-resize',
              }}
            />
          ))}
        </div>

        {domain ? (
          <div style={{ ...hint, display: 'flex', justifyContent: 'space-between' }}>
            <span>{formatValue(domain[0])}</span>
            <span>{formatValue(domain[1])}</span>
          </div>
        ) : null}
      </div>

      <table style={table}>
        <thead>
          <tr>
            <th style={cell} scope="col">
              Position
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
          {palette.stops.map((stop, index) => (
            <tr key={index} onFocus={() => setSelected(index)}>
              <td style={cell}>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  aria-label={`Stop ${index + 1} position, percent`}
                  value={Math.round(stop.position * 100)}
                  onChange={(event) =>
                    setStop(index, {
                      position: Math.min(1, Math.max(0, Number(event.target.value) / 100)),
                    })
                  }
                  style={{ width: 60, height: 22, fontSize: 12 }}
                />
              </td>
              <td style={cell}>
                <ColorPicker
                  value={stop.color}
                  onChange={(color) => setStop(index, { color })}
                />
              </td>
              <td style={cell}>
                <button
                  type="button"
                  aria-label={`Remove stop ${index + 1}`}
                  disabled={palette.stops.length <= 2}
                  onClick={() => removeStop(index)}
                  style={{ ...iconButton, height: 22, width: 22 }}
                  title={
                    palette.stops.length <= 2
                      ? 'A ramp needs at least two stops'
                      : 'Remove this stop'
                  }
                >
                  −
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <button type="button" onClick={addStop} style={ghostButton}>
          Add stop
        </button>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
          <input
            type="checkbox"
            id={`${id}-discrete`}
            checked={palette.interpolation === 'discrete'}
            onChange={(event) =>
              onChange({
                ...palette,
                interpolation: event.target.checked ? 'discrete' : 'linear',
                isContinuous: !event.target.checked,
              })
            }
          />
          Hard breaks
        </label>
      </div>

      {note ? <p style={caution}>{note}</p> : null}
    </div>
  );
}

function formatValue(value: number): string {
  const magnitude = Math.abs(value);
  if (magnitude >= 10000 || (magnitude > 0 && magnitude < 0.01)) {
    return value.toExponential(2);
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
