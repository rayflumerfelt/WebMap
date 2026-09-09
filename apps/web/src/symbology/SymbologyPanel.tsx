/**
 * Symbology editor. `08-styling-palettes.md` §2, Phase 2 scope: single symbol
 * and categorized.
 *
 * Edits the **symbology model**, never the compiled style. The model is what
 * the legend reads and what the session stores; a UI that wrote paint
 * properties directly would produce a map whose legend could not describe it
 * (`01-architecture.md` §4.2).
 *
 * The geometry-aware `SymbolSpec` union does real work here: a point symbol
 * has no `fillColor`, so the type system prevents this editor from offering
 * one. That is more reliable than a runtime check, and it is why the form is
 * a switch over geometry rather than one form with conditional fields.
 */

import type { Symbology } from '@webmap/style-model';
import { entryCount } from '@webmap/style-model';
import { useId } from 'react';

export interface SymbologyPanelProps {
  layerName: string | null;
  symbology: Symbology | null;
  onChange(symbology: Symbology): void;
  /** Attribute names for the categorize/graduate field picker. */
  fields?: string[];
}

export function SymbologyPanel({ layerName, symbology, onChange }: SymbologyPanelProps) {
  if (!symbology || !layerName) {
    return (
      <p style={{ fontSize: 12, opacity: 0.7, margin: 0 }}>
        Select a layer to edit how it is drawn.
      </p>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 12 }}>
      <div style={{ fontWeight: 600 }}>{layerName}</div>

      <Field label="Style">
        <select
          value={symbology.type}
          aria-label="Symbology type"
          onChange={(event) => onChange(convertTo(symbology, event.target.value))}
          style={controlStyle}
        >
          <option value="single">Single symbol</option>
          <option value="categorized">Categorized</option>
        </select>
      </Field>

      {symbology.type === 'single' ? (
        <SingleSymbolForm symbology={symbology} onChange={onChange} />
      ) : null}

      {symbology.type === 'categorized' ? (
        <CategorizedForm symbology={symbology} onChange={onChange} />
      ) : null}

      <div style={{ opacity: 0.7 }}>
        {/* Read from `entryCount`, the same function the legend and the
            compiler use, rather than counting anything locally — §8's
            single source of truth for how many entries exist. */}
        {entryCount(symbology)} legend {entryCount(symbology) === 1 ? 'entry' : 'entries'}
      </div>
    </div>
  );
}

function SingleSymbolForm({
  symbology,
  onChange,
}: {
  symbology: Extract<Symbology, { type: 'single' }>;
  onChange(symbology: Symbology): void;
}) {
  const symbol = symbology.symbol;

  const patch = (changes: Record<string, unknown>) =>
    onChange({ ...symbology, symbol: { ...symbol, ...changes } as typeof symbol });

  switch (symbol.geometry) {
    case 'point':
      return (
        <>
          <ColorField
            label="Fill"
            value={symbol.color}
            onChange={(color) => patch({ color })}
          />
          <NumberField
            label="Size"
            value={symbol.size}
            min={1}
            max={40}
            step={0.5}
            onChange={(size) => patch({ size })}
          />
          <ColorField
            label="Stroke"
            value={symbol.strokeColor}
            onChange={(strokeColor) => patch({ strokeColor })}
          />
        </>
      );
    case 'line':
      return (
        <>
          <ColorField label="Colour" value={symbol.color} onChange={(color) => patch({ color })} />
          <NumberField
            label="Width"
            value={symbol.width}
            min={0.25}
            max={20}
            step={0.25}
            onChange={(width) => patch({ width })}
          />
        </>
      );
    case 'polygon':
      return (
        <>
          <ColorField
            label="Fill"
            value={symbol.fillColor}
            onChange={(fillColor) => patch({ fillColor })}
          />
          <NumberField
            label="Fill opacity"
            value={symbol.fillOpacity}
            min={0}
            max={1}
            step={0.05}
            onChange={(fillOpacity) => patch({ fillOpacity })}
          />
          <ColorField
            label="Outline"
            value={symbol.outlineColor}
            onChange={(outlineColor) => patch({ outlineColor })}
          />
          <NumberField
            label="Outline width"
            value={symbol.outlineWidth}
            min={0}
            max={12}
            step={0.25}
            onChange={(outlineWidth) => patch({ outlineWidth })}
          />
        </>
      );
    case 'label':
      return (
        <>
          <ColorField label="Text" value={symbol.color} onChange={(color) => patch({ color })} />
          <NumberField
            label="Size"
            value={symbol.size}
            min={6}
            max={48}
            step={1}
            onChange={(size) => patch({ size })}
          />
        </>
      );
  }
}

function CategorizedForm({
  symbology,
  onChange,
}: {
  symbology: Extract<Symbology, { type: 'categorized' }>;
  onChange(symbology: Symbology): void;
}) {
  return (
    <>
      <Field label="Field">
        <input
          value={symbology.field}
          aria-label="Category field"
          onChange={(event) => onChange({ ...symbology, field: event.target.value })}
          style={controlStyle}
        />
      </Field>

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <caption style={{ captionSide: 'top', textAlign: 'left', paddingBottom: 4 }}>
          Categories
        </caption>
        <thead>
          <tr>
            <th style={cellStyle}>Value</th>
            <th style={cellStyle}>Label</th>
            <th style={cellStyle}>Colour</th>
          </tr>
        </thead>
        <tbody>
          {symbology.categories.map((category, index) => (
            <tr key={String(category.value)} style={{ height: 'var(--row-h, 26px)' }}>
              <td style={cellStyle}>{String(category.value ?? '(null)')}</td>
              <td style={cellStyle}>
                <input
                  value={category.label}
                  aria-label={`Label for ${String(category.value)}`}
                  onChange={(event) => {
                    const categories = [...symbology.categories];
                    categories[index] = { ...category, label: event.target.value };
                    onChange({ ...symbology, categories });
                  }}
                  style={{ ...controlStyle, width: '100%' }}
                />
              </td>
              <td style={cellStyle}>
                <input
                  type="color"
                  value={colourOf(category.symbol)}
                  aria-label={`Colour for ${String(category.value)}`}
                  onChange={(event) => {
                    const categories = [...symbology.categories];
                    categories[index] = {
                      ...category,
                      symbol: withColour(category.symbol, event.target.value),
                    };
                    onChange({ ...symbology, categories });
                  }}
                  style={{ width: 28, height: 20, padding: 0, border: 0 }}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

/**
 * Switch symbology type, keeping what can be kept.
 *
 * A geologist changing from single to categorized has already chosen a colour
 * and a size; discarding them and starting from a default would make the type
 * switch feel like losing work, so the existing symbol becomes the base.
 */
function convertTo(current: Symbology, type: string): Symbology {
  if (type === current.type) return current;

  if (type === 'single') {
    const symbol =
      current.type === 'categorized'
        ? (current.categories[0]?.symbol ?? current.other)
        : current.type === 'graduated'
          ? current.baseSymbol
          : current.type === 'rules'
            ? current.rules[0]?.symbol
            : undefined;
    if (!symbol) {
      throw new Error(
        `Cannot convert ${current.type} symbology to a single symbol: it has no ` +
          `symbol to keep. Add a category or a rule first.`,
      );
    }
    return { type: 'single', symbol };
  }

  if (type === 'categorized') {
    const base =
      current.type === 'single'
        ? current.symbol
        : current.type === 'graduated'
          ? current.baseSymbol
          : undefined;
    if (!base) {
      throw new Error(
        `Cannot convert ${current.type} symbology to categorized: it has no base ` +
          `symbol to derive categories from.`,
      );
    }
    // No categories yet — the field has not been chosen, so there is nothing
    // to enumerate. `other` carries the appearance forward so the map does not
    // go blank the instant the type changes.
    return { type: 'categorized', field: '', categories: [], other: base };
  }

  throw new Error(
    `Symbology type '${type}' is not editable in this panel yet. Graduated, ` +
      `rule-based, and continuous raster styling arrive in Phase 5 ` +
      `(08-styling-palettes.md §2).`,
  );
}

type AnySymbol = Extract<Symbology, { type: 'single' }>['symbol'];

function colourOf(symbol: AnySymbol): string {
  return symbol.geometry === 'polygon' ? symbol.fillColor : symbol.color;
}

function withColour(symbol: AnySymbol, colour: string): AnySymbol {
  return symbol.geometry === 'polygon'
    ? { ...symbol, fillColor: colour }
    : { ...symbol, color: colour };
}

// --- small form pieces ------------------------------------------------------

const controlStyle: React.CSSProperties = {
  height: 'var(--control-h, 28px)',
  fontSize: 12,
  padding: '0 4px',
};

const cellStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '0 4px',
  fontWeight: 400,
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  const id = useId();
  return (
    <label htmlFor={id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ width: 90, flex: '0 0 90px' }}>{label}</span>
      <span style={{ flex: 1 }}>
        {/* The control carries its own aria-label; the id wires the visible
            text to it so clicking the label focuses the control. */}
        {children}
      </span>
    </label>
  );
}

function ColorField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange(value: string): void;
}) {
  return (
    <Field label={label}>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input
          type="color"
          value={value}
          aria-label={label}
          onChange={(event) => onChange(event.target.value)}
          style={{ width: 28, height: 20, padding: 0, border: 0 }}
        />
        {/* The hex alongside the swatch: colour is never the only channel
            (§10), and a geologist matching a partner's map needs the value. */}
        <code style={{ fontSize: 11 }}>{value}</code>
      </span>
    </Field>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange(value: number): void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        aria-label={label}
        onChange={(event) => {
          const raw = event.target.value;
          // **`Number('')` is 0, not NaN.** Clearing the field to retype a
          // value would otherwise set outline width to zero — and zero is a
          // legitimate width, so a range check cannot catch it. The empty
          // string has to be rejected on its own.
          if (raw.trim() === '') return;
          const parsed = Number(raw);
          if (Number.isFinite(parsed) && parsed >= min && parsed <= max) onChange(parsed);
        }}
        style={{ ...controlStyle, width: 80 }}
      />
    </Field>
  );
}
