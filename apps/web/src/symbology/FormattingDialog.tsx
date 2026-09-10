/**
 * The per-layer formatting dialog. `07-frontend.md` §6.2.
 *
 * One dialog per layer, composed from the §6.3 shared controls, with sections
 * that appear only where the geometry supports them. The `SymbolSpec` union
 * does that work at the type level rather than at runtime: a line symbol has no
 * `fillColor`, so the fill section is unreachable for it and the compiler says
 * so — which is why the body is a switch over the symbol's geometry rather than
 * one form with conditional fields.
 *
 * **This edits the symbology model, never the compiled style.** The model is
 * what the legend reads and what the session stores; a dialog that wrote paint
 * properties directly would produce a map whose legend could not describe it
 * (`01-architecture.md` §4.2).
 *
 * Two things here are not obvious from the section list:
 *
 * - **Size mode reveals its reference zoom.** A reference zoom is meaningless
 *   in fixed mode, and a control that is always visible but only sometimes
 *   effective is how people conclude a feature does not work.
 * - **The preview zooms.** The difference between "12 pt stays 12 pt" and
 *   "12 pt at zoom 12, doubling in" is invisible in a still, so the label
 *   preview animates through two zooms rather than rendering once.
 */

import type {
  Categorized,
  Graduated,
  LabelSymbol,
  LineSymbol,
  Palette,
  PointSymbol,
  PolygonSymbol,
  SingleSymbol,
  SymbolSpec,
  Symbology,
} from '@webmap/style-model';
import {
  CategoryTable,
  ColorPicker,
  IntervalEditor,
  LegendPreview,
  LinePicker,
  MarkerPicker,
  PaletteIO,
  RampEditor,
} from '@webmap/ui';
import type { CategoryRow, FontFamily, IntervalBand } from '@webmap/ui';
import { useId, useState } from 'react';
import { LabelSection } from './LabelSection.js';
import { Section, fieldRow, panel } from './dialogParts.js';

export interface AttributeSummary {
  /** Distinct values with counts, for a text column. Empty for a numeric one. */
  categories?: CategoryRow[] | undefined;
  /** Counts per equal-width bin, for the ramp editor's underlay. */
  histogram?: number[] | undefined;
  domain?: [number, number] | undefined;
  /** Set when the column's cardinality was past what the server enumerates. */
  refused?: { distinct: number; limit: number } | null | undefined;
  remaining?: number | undefined;
}

export interface FormattingDialogProps {
  layerName: string;
  symbology: Symbology;
  onChange(symbology: Symbology): void;
  /** Columns the layer has, for every field picker in the dialog. */
  fields: Array<{ name: string; type: 'text' | 'number' }>;
  /** Summary for the column currently being coloured or labelled by.
   *
   *  Absent while it loads, and absent for a fixed-colour layer where there is
   *  no column to summarise — both render the editors without an underlay
   *  rather than blocking on the request. */
  summary?: AttributeSummary | undefined;
  palettes: Record<string, Palette>;
  /** What `GET /static/glyphs` returned — see `FontPicker`. */
  fonts: FontFamily[];
  /** Interval bands, when the layer is coloured by interval rather than ramp. */
  bands?: IntervalBand[];
  onBandsChange?(bands: IntervalBand[]): void;
  /** Value shown where a numeric null or an out-of-range value lands. */
  nullColor?: string;
  onNullColorChange?(color: string): void;
  onImportPalette?(text: string, format: string, filename: string): Promise<void> | void;
  onExportPalette?(format: string): Promise<string> | string;
  className?: string;
}

/** Colour modes, as `07` §6.2 names them. */
type ColourMode = 'fixed' | 'gradient' | 'interval' | 'categories';

export function FormattingDialog(props: FormattingDialogProps) {
  const {
    layerName,
    symbology,
    onChange,
    fields,
    summary,
    palettes,
    fonts,
    bands,
    onBandsChange,
    nullColor,
    onNullColorChange,
    onImportPalette,
    onExportPalette,
    className,
  } = props;

  const id = useId();
  const [mode, setMode] = useState<ColourMode>(() => initialMode(symbology, bands));

  const numericFields = fields.filter((field) => field.type === 'number');
  const textFields = fields.filter((field) => field.type === 'text');

  return (
    <div className={className} style={panel}>
      <header style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <h2 style={{ fontSize: 13, margin: 0 }}>{layerName}</h2>
        <span style={{ fontSize: 11, color: '#6b7078' }}>Formatting</span>
      </header>

      <Section title="Colour">
        <div style={fieldRow}>
          <label htmlFor={`${id}-mode`} style={{ fontSize: 11, minWidth: 88 }}>
            Colour by
          </label>
          <select
            id={`${id}-mode`}
            value={mode}
            onChange={(event) => {
              const next = event.target.value as ColourMode;
              setMode(next);
              onChange(toMode(symbology, next, numericFields[0]?.name, textFields[0]?.name));
            }}
            style={{ height: 'var(--control-h, 28px)', fontSize: 12, width: 168 }}
          >
            <option value="fixed">A fixed colour</option>
            <option value="gradient" disabled={numericFields.length === 0}>
              Column value — gradient
            </option>
            <option value="interval" disabled={numericFields.length === 0}>
              Column value — intervals
            </option>
            <option value="categories" disabled={textFields.length === 0}>
              Column value — categories
            </option>
          </select>
        </div>

        {mode !== 'fixed' ? (
          <FieldPicker
            id={`${id}-field`}
            label="Column"
            value={fieldOf(symbology) ?? ''}
            fields={mode === 'categories' ? textFields : numericFields}
            onChange={(field) => onChange(withField(symbology, field))}
          />
        ) : null}

        {mode === 'gradient' && symbology.type === 'graduated' ? (
          <>
            <RampEditor
              palette={palettes[symbology.paletteId] ?? fallbackPalette(symbology.paletteId)}
              onChange={() => {
                /* Palettes are shared objects; the dialog picks one and the
                   palette editor edits it. Editing in place here would change
                   every layer using it without saying so. */
              }}
              histogram={summary?.histogram}
              domain={summary?.domain}
              note={perceptualNote(palettes[symbology.paletteId])}
            />
            {onImportPalette && onExportPalette ? (
              <PaletteIO
                name={palettes[symbology.paletteId]?.name ?? 'palette'}
                onImport={(text, format, filename) => onImportPalette(text, format, filename)}
                onExport={(format) => onExportPalette(format)}
              />
            ) : null}
          </>
        ) : null}

        {mode === 'interval' && bands && onBandsChange ? (
          <IntervalEditor bands={bands} onChange={onBandsChange} domain={summary?.domain} />
        ) : null}

        {mode === 'categories' && symbology.type === 'categorized' ? (
          <CategoryTable
            rows={summary?.categories ?? categoriesAsRows(symbology)}
            onChange={(rows) => onChange(withCategories(symbology, rows))}
            otherColor={symbology.other ? swatchOf(symbology.other) : null}
            onOtherChange={(color) => onChange(withOther(symbology, color))}
            remaining={summary?.remaining}
            refused={summary?.refused ?? null}
          />
        ) : null}

        {onNullColorChange && nullColor !== undefined ? (
          <ColorPicker
            label="No data"
            value={nullColor}
            onChange={onNullColorChange}
          />
        ) : null}
        {onNullColorChange ? (
          <p style={{ fontSize: 11, color: '#6b7078', margin: 0 }}>
            <em>Other</em> catches unlisted text. It does not catch a numeric null, a value
            outside the range, or a blanked grid cell — and a well with no porosity reading is
            not zero.
          </p>
        ) : null}
      </Section>

      <SymbolSections symbology={symbology} onChange={onChange} fonts={fonts} fields={fields} />

      <Section title="Legend">
        <LegendPreview
          symbology={symbology}
          meta={{ name: layerName }}
          palettes={palettes}
        />
      </Section>
    </div>
  );
}

/**
 * The geometry-specific half of the dialog.
 *
 * A switch over the symbol's `geometry` discriminant, so the type system —
 * not a runtime check — is what stops a fill control appearing for a line.
 */
function SymbolSections({
  symbology,
  onChange,
  fonts,
  fields,
}: {
  symbology: Symbology;
  onChange(symbology: Symbology): void;
  fonts: FontFamily[];
  fields: Array<{ name: string; type: 'text' | 'number' }>;
}) {
  const symbol = baseSymbolOf(symbology);
  if (!symbol) return null;

  const replace = (next: SymbolSpec) => onChange(withBaseSymbol(symbology, next));

  switch (symbol.geometry) {
    case 'point':
      return (
        <Section title="Marker">
          <MarkerPicker
            value={{
              marker: symbol.marker,
              size: symbol.size,
              color: symbol.color,
              strokeColor: symbol.strokeColor,
              strokeWidth: symbol.strokeWidth,
              opacity: symbol.opacity,
              rotation: typeof symbol.rotation === 'number' ? symbol.rotation : undefined,
              spriteName: symbol.spriteName,
            }}
            onChange={(value) => replace({ ...symbol, ...value } as PointSymbol)}
          />
        </Section>
      );

    case 'line':
      return (
        <Section title="Line">
          <LinePicker
            value={{
              color: symbol.color,
              width: symbol.width,
              opacity: symbol.opacity,
              dashArray: symbol.dashArray,
              cap: symbol.cap,
              join: symbol.join,
            }}
            onChange={(value) => replace({ ...symbol, ...value } as LineSymbol)}
          />
        </Section>
      );

    case 'polygon':
      return (
        <>
          <Section title="Fill">
            <ColorPicker
              label="Colour"
              value={symbol.fillColor}
              onChange={(fillColor) => replace({ ...symbol, fillColor })}
              opacity={symbol.fillOpacity}
              onOpacityChange={(fillOpacity) => replace({ ...symbol, fillOpacity })}
            />
          </Section>
          <Section title="Outline">
            <LinePicker
              value={{
                color: symbol.outlineColor,
                width: symbol.outlineWidth,
                dashArray: symbol.outlineDashArray,
                cap: 'butt',
                join: 'miter',
              }}
              onChange={(value) =>
                replace({
                  ...symbol,
                  outlineColor: value.color,
                  outlineWidth: value.width,
                  outlineDashArray: value.dashArray,
                } as PolygonSymbol)
              }
            />
          </Section>
        </>
      );

    case 'label':
      return (
        <LabelSection
          symbol={symbol}
          fonts={fonts}
          fields={fields}
          onChange={(next: LabelSymbol) => replace(next)}
        />
      );
  }
}

function FieldPicker({
  id,
  label,
  value,
  fields,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  fields: Array<{ name: string }>;
  onChange(field: string): void;
}) {
  return (
    <div style={fieldRow}>
      <label htmlFor={id} style={{ fontSize: 11, minWidth: 88 }}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        style={{ height: 'var(--control-h, 28px)', fontSize: 12, width: 168 }}
      >
        {fields.map((field) => (
          <option key={field.name} value={field.name}>
            {field.name}
          </option>
        ))}
      </select>
    </div>
  );
}

// --- model helpers -----------------------------------------------------------
//
// Kept here rather than in `@webmap/style-model`: they are about what a *form*
// does when a control changes, not about what a symbology means, and the model
// package is shared with the Python compiler's test vectors.

function initialMode(symbology: Symbology, bands?: IntervalBand[]): ColourMode {
  if (symbology.type === 'categorized') return 'categories';
  if (symbology.type === 'graduated') return bands?.length ? 'interval' : 'gradient';
  return 'fixed';
}

/**
 * The column a layer is coloured by, or null for a fixed colour.
 *
 * Exported because the container fetches a summary *for that column* rather
 * than for the layer: a distinct-values query over half a million features is
 * expensive, and thirty-nine of a layer's forty columns are not being looked
 * at.
 */
export function colouredColumn(symbology: Symbology): string | null {
  return fieldOf(symbology);
}

export function fieldOf(symbology: Symbology): string | null {
  if (symbology.type === 'categorized' || symbology.type === 'graduated') {
    return symbology.field;
  }
  return null;
}

export function baseSymbolOf(symbology: Symbology): SymbolSpec | null {
  switch (symbology.type) {
    case 'single':
      return symbology.symbol;
    case 'graduated':
      return symbology.baseSymbol;
    case 'categorized':
      return symbology.categories[0]?.symbol ?? symbology.other ?? null;
    default:
      return null;
  }
}

function withBaseSymbol(symbology: Symbology, symbol: SymbolSpec): Symbology {
  switch (symbology.type) {
    case 'single':
      return { ...symbology, symbol } satisfies SingleSymbol;
    case 'graduated':
      return { ...symbology, baseSymbol: symbol } satisfies Graduated;
    case 'categorized':
      // Every category keeps its own colour and takes the shared shape. A
      // categorized layer varies one property; changing the marker or the line
      // width should change all of them, or the legend stops being readable as
      // a set.
      return {
        ...symbology,
        categories: symbology.categories.map((category) => ({
          ...category,
          symbol: mergeAppearance(symbol, category.symbol),
        })),
      } satisfies Categorized;
    default:
      return symbology;
  }
}

/** Take the shape from `shared` and the colour from `existing`. */
function mergeAppearance(shared: SymbolSpec, existing: SymbolSpec): SymbolSpec {
  if (shared.geometry !== existing.geometry) return shared;
  switch (shared.geometry) {
    case 'point':
      return { ...shared, color: (existing as PointSymbol).color };
    case 'line':
      return { ...shared, color: (existing as LineSymbol).color };
    case 'polygon':
      return { ...shared, fillColor: (existing as PolygonSymbol).fillColor };
    default:
      return shared;
  }
}

function withField(symbology: Symbology, field: string): Symbology {
  if (symbology.type === 'categorized' || symbology.type === 'graduated') {
    return { ...symbology, field };
  }
  return symbology;
}

function categoriesAsRows(symbology: Categorized): CategoryRow[] {
  return symbology.categories.map((category) => ({
    value: String(category.value ?? ''),
    color: swatchOf(category.symbol),
    label: category.label,
  }));
}

function withCategories(symbology: Categorized, rows: CategoryRow[]): Categorized {
  const byValue = new Map(rows.map((row) => [row.value, row]));
  return {
    ...symbology,
    categories: symbology.categories
      .filter((category) => byValue.has(String(category.value ?? '')))
      .map((category) => {
        const row = byValue.get(String(category.value ?? ''))!;
        return { ...category, symbol: withSwatch(category.symbol, row.color) };
      }),
  };
}

function withOther(symbology: Categorized, color: string | null): Categorized {
  if (color === null) {
    // Deleted rather than set to undefined: `exactOptionalPropertyTypes` makes
    // the two different types, and the compiler reads an absent `other` as
    // "unlisted values are not drawn".
    const rest = { ...symbology };
    delete rest.other;
    return rest;
  }
  const template = symbology.categories[0]?.symbol ?? symbology.other;
  return template ? { ...symbology, other: withSwatch(template, color) } : symbology;
}

/** The one colour that stands for a symbol in a swatch. */
export function swatchOf(symbol: SymbolSpec): string {
  switch (symbol.geometry) {
    case 'polygon':
      return symbol.fillColor;
    case 'label':
      return symbol.color;
    default:
      return symbol.color;
  }
}

function withSwatch(symbol: SymbolSpec, color: string): SymbolSpec {
  return symbol.geometry === 'polygon'
    ? { ...symbol, fillColor: color }
    : { ...symbol, color };
}

function toMode(
  symbology: Symbology,
  mode: ColourMode,
  numericField: string | undefined,
  textField: string | undefined,
): Symbology {
  const symbol = baseSymbolOf(symbology);
  if (!symbol) return symbology;

  switch (mode) {
    case 'fixed':
      return { type: 'single', symbol };
    case 'categories':
      return symbology.type === 'categorized'
        ? symbology
        : {
            type: 'categorized',
            field: textField ?? '',
            categories: [],
            other: symbol,
          };
    case 'gradient':
    case 'interval':
      return symbology.type === 'graduated'
        ? symbology
        : {
            type: 'graduated',
            field: numericField ?? '',
            method: 'quantile',
            classCount: 5,
            breaks: [],
            paletteId: 'viridis',
            vary: 'color',
            baseSymbol: symbol,
          };
  }
}

function fallbackPalette(id: string): Palette {
  // A palette id the session does not carry is a loading state, not an error.
  // Rendering a grey ramp keeps the dialog laid out rather than collapsing a
  // section as the palettes arrive.
  return {
    id,
    name: id,
    isContinuous: true,
    interpolation: 'linear',
    stops: [
      { position: 0, color: '#eeeeee' },
      { position: 1, color: '#888888' },
    ],
  };
}

/**
 * `08` §5.1 keeps the spectral ramp and warns rather than removing it —
 * geologists expect it on a structure map even though it is perceptually poor.
 */
function perceptualNote(palette: Palette | undefined): string | undefined {
  if (!palette) return undefined;
  const spectral = /spectral|rainbow|jet|turbo/i.test(palette.name);
  return spectral
    ? `${palette.name} is not perceptually uniform: equal steps in value are not equal steps ` +
        `in apparent colour, so it invents boundaries the data does not have. Viridis or ` +
        `cividis is the safer choice for a property map.`
    : undefined;
}
