/**
 * Point shape, size, rotation and stroke. `07-frontend.md` §6.3.
 *
 * The shape set is `PointSymbol['marker']` — circle, square, triangle, cross
 * and sprite — so this control cannot offer a shape the compiler has no case
 * for. `sprite` reveals a name field, because a sprite marker without one
 * renders nothing and the failure is silent.
 *
 * **Rotation matters more than it looks.** Dip and strike symbols, well
 * deviation, and any oriented posting are a rotated marker, and MapLibre
 * measures `icon-rotate` clockwise from north — the same convention as a
 * strike reading, which is why the control is labelled in degrees from north
 * and not in mathematical degrees.
 */

import { useId } from 'react';
import { ColorPicker } from './ColorPicker.js';
import { control, hint, label, row, stack } from './styles.js';

export type MarkerShape = 'circle' | 'square' | 'triangle' | 'cross' | 'sprite';

export interface MarkerValue {
  marker: MarkerShape;
  size: number;
  color: string;
  strokeColor: string;
  strokeWidth: number;
  opacity: number;
  rotation?: number | undefined;
  /** Cleared to `undefined` when the select is emptied, which is why the type
   *  admits it under `exactOptionalPropertyTypes`. */
  spriteName?: string | undefined;
}

export interface MarkerPickerProps {
  value: MarkerValue;
  onChange(value: MarkerValue): void;
  /** Sprite names this deployment actually has, for the `sprite` shape. */
  sprites?: string[] | undefined;
  className?: string | undefined;
}

const SHAPES: Array<{ id: MarkerShape; name: string }> = [
  { id: 'circle', name: 'Circle' },
  { id: 'square', name: 'Square' },
  { id: 'triangle', name: 'Triangle' },
  { id: 'cross', name: 'Cross' },
  { id: 'sprite', name: 'Sprite' },
];

export function MarkerPicker({ value, onChange, sprites = [], className }: MarkerPickerProps) {
  const id = useId();
  const patch = (changes: Partial<MarkerValue>) => onChange({ ...value, ...changes });

  return (
    <div className={className} style={stack}>
      <div style={row}>
        <span style={label} id={`${id}-shape-label`}>
          Shape
        </span>
        <div role="radiogroup" aria-labelledby={`${id}-shape-label`} style={{ display: 'flex', gap: 4 }}>
          {SHAPES.map((shape) => (
            <button
              key={shape.id}
              type="button"
              role="radio"
              aria-checked={value.marker === shape.id}
              aria-label={shape.name}
              title={shape.name}
              onClick={() => patch({ marker: shape.id })}
              style={{
                ...control,
                width: 32,
                padding: 0,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
                borderColor: value.marker === shape.id ? '#1c5cc4' : '#c9ccd1',
                background: value.marker === shape.id ? '#eaf1fd' : '#fff',
              }}
            >
              <MarkerGlyph shape={shape.id} size={12} value={value} preview />
            </button>
          ))}
        </div>
      </div>

      {value.marker === 'sprite' ? (
        <div style={row}>
          <label htmlFor={`${id}-sprite`} style={label}>
            Sprite
          </label>
          <select
            id={`${id}-sprite`}
            value={value.spriteName ?? ''}
            onChange={(event) => patch({ spriteName: event.target.value || undefined })}
            style={{ ...control, width: 160 }}
          >
            <option value="">Choose a sprite…</option>
            {sprites.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          {!value.spriteName ? (
            <span style={hint}>A sprite marker with no name draws nothing.</span>
          ) : null}
        </div>
      ) : null}

      <div style={row}>
        <label htmlFor={`${id}-size`} style={label}>
          Size
        </label>
        <input
          id={`${id}-size`}
          type="number"
          min={1}
          max={64}
          step={0.5}
          value={value.size}
          onChange={(event) => patch({ size: Math.max(1, Number(event.target.value)) })}
          style={{ ...control, width: 72, textAlign: 'right' }}
        />
        <span style={hint}>px</span>
      </div>

      <ColorPicker
        label="Fill"
        value={value.color}
        onChange={(color) => patch({ color })}
        opacity={value.opacity}
        onOpacityChange={(opacity) => patch({ opacity })}
      />

      <ColorPicker
        label="Outline"
        value={value.strokeColor}
        onChange={(strokeColor) => patch({ strokeColor })}
      />

      <div style={row}>
        <label htmlFor={`${id}-stroke`} style={label}>
          Outline width
        </label>
        <input
          id={`${id}-stroke`}
          type="number"
          min={0}
          max={12}
          step={0.25}
          value={value.strokeWidth}
          onChange={(event) => patch({ strokeWidth: Math.max(0, Number(event.target.value)) })}
          style={{ ...control, width: 72, textAlign: 'right' }}
        />
      </div>

      <div style={row}>
        <label htmlFor={`${id}-rotation`} style={label}>
          Rotation
        </label>
        <input
          id={`${id}-rotation`}
          type="number"
          min={0}
          max={359}
          step={1}
          value={value.rotation ?? 0}
          onChange={(event) => patch({ rotation: Number(event.target.value) % 360 })}
          style={{ ...control, width: 72, textAlign: 'right' }}
        />
        <span style={hint}>° clockwise from north, as a strike reading</span>
      </div>

      <div
        style={{
          display: 'grid',
          placeItems: 'center',
          height: 72,
          border: '1px solid #eceef1',
          borderRadius: 3,
          background: '#fff',
        }}
      >
        <MarkerGlyph shape={value.marker} size={value.size} value={value} />
      </div>
    </div>
  );
}

function MarkerGlyph({
  shape,
  size,
  value,
  preview = false,
}: {
  shape: MarkerShape;
  size: number;
  value: MarkerValue;
  /** In the shape chooser, draw the outline only — the buttons are 32 px and
   *  a filled swatch at the layer's own colour makes five identical squares. */
  preview?: boolean;
}) {
  const box = Math.max(size, 8) + value.strokeWidth * 2 + 4;
  const half = box / 2;
  const r = size / 2;
  const fill = preview ? 'none' : value.color;
  const stroke = preview ? '#4a4f57' : value.strokeColor;
  const strokeWidth = preview ? 1.5 : value.strokeWidth;

  const common = {
    fill,
    stroke,
    strokeWidth,
    fillOpacity: preview ? 1 : value.opacity,
  };

  return (
    <svg
      width={box}
      height={box}
      viewBox={`0 0 ${box} ${box}`}
      role="img"
      aria-label={preview ? undefined : `${shape} marker preview`}
      aria-hidden={preview || undefined}
      style={{ transform: `rotate(${value.rotation ?? 0}deg)` }}
    >
      {shape === 'circle' || shape === 'sprite' ? (
        <circle cx={half} cy={half} r={r} {...common} />
      ) : null}
      {shape === 'square' ? (
        <rect x={half - r} y={half - r} width={size} height={size} {...common} />
      ) : null}
      {shape === 'triangle' ? (
        <polygon
          points={`${half},${half - r} ${half + r},${half + r} ${half - r},${half + r}`}
          {...common}
        />
      ) : null}
      {shape === 'cross' ? (
        <g stroke={stroke} strokeWidth={Math.max(1, strokeWidth)}>
          <line x1={half - r} y1={half} x2={half + r} y2={half} />
          <line x1={half} y1={half - r} x2={half} y2={half + r} />
        </g>
      ) : null}
    </svg>
  );
}
