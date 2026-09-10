/**
 * Interval bands: maximums and their colours. `07-frontend.md` §6.2, `08` §5.2.
 *
 * **The editor takes only maximums.** The minimum of each band is the maximum
 * of the one below, and the compiler extends the first band down to the data
 * floor and the last up to the ceiling, so a value outside the listed range
 * clamps to an end colour rather than falling through. Asking for both bounds
 * per band would let someone enter a gap, and an uncovered value renders
 * **transparent** — indistinguishable from no-data, which is the one thing the
 * extrapolation reporting exists to prevent.
 *
 * That is also why this is a different control from `RampEditor` rather than a
 * mode of it. A gradient is normalised 0–1 and comparable across no two grids;
 * a band list is in **raw data units** and says what it means, which is what
 * makes intervals the mode to use when two maps have to be read against each
 * other.
 */

import { useMemo } from 'react';
import { ColorPicker } from './ColorPicker.js';
import { caution, cell, ghostButton, hint, iconButton, stack, table } from './styles.js';

export interface IntervalBand {
  /** Upper bound, inclusive, in the data's own units. */
  max: number;
  color: string;
  /** Optional override for the legend text. Empty means "derive it". */
  label?: string;
}

export interface IntervalEditorProps {
  bands: IntervalBand[];
  onChange(bands: IntervalBand[]): void;
  /** The layer's data range, used for the derived labels and the warning. */
  domain?: [number, number] | undefined;
  /** Unit suffix for the derived labels — 'ft', '%', 'mD'. */
  unit?: string | undefined;
  className?: string | undefined;
}

export function IntervalEditor({
  bands,
  onChange,
  domain,
  unit,
  className,
}: IntervalEditorProps) {
  const sorted = useMemo(() => [...bands].sort((a, b) => a.max - b.max), [bands]);

  // Duplicated maximums make a zero-width band that can never match. Detected
  // and named rather than silently merged: the usual cause is a typo in one
  // digit, and merging hides which band was meant to be different.
  const duplicates = useMemo(() => {
    const seen = new Set<number>();
    const repeated = new Set<number>();
    for (const band of sorted) {
      if (seen.has(band.max)) repeated.add(band.max);
      seen.add(band.max);
    }
    return repeated;
  }, [sorted]);

  const uncovered =
    domain && sorted.length > 0 && sorted[sorted.length - 1]!.max < domain[1]
      ? domain[1]
      : null;

  const setBand = (index: number, patch: Partial<IntervalBand>) =>
    onChange(sorted.map((band, i) => (i === index ? { ...band, ...patch } : band)));

  const addBand = () => {
    const last = sorted[sorted.length - 1];
    const previous = sorted[sorted.length - 2];
    // One step above the top band, using the existing spacing so a regular
    // series stays regular. A new band at the same maximum would be dead on
    // arrival.
    const step = last && previous ? Math.abs(last.max - previous.max) || 1 : 1;
    onChange([
      ...sorted,
      { max: (last?.max ?? (domain?.[0] ?? 0)) + step, color: last?.color ?? '#cccccc' },
    ]);
  };

  return (
    <div className={className} style={stack}>
      <table style={table}>
        <thead>
          <tr>
            <th style={cell} scope="col">
              Range
            </th>
            <th style={cell} scope="col">
              Up to
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
          {sorted.map((band, index) => {
            const lower = index === 0 ? domain?.[0] : sorted[index - 1]!.max;
            return (
              <tr key={index}>
                <td style={{ ...cell, color: '#4a4f57' }}>
                  {describe(lower, band.max, unit, index === 0)}
                </td>
                <td style={cell}>
                  <input
                    type="number"
                    aria-label={`Band ${index + 1} maximum`}
                    value={band.max}
                    onChange={(event) => setBand(index, { max: Number(event.target.value) })}
                    style={{
                      width: 88,
                      height: 22,
                      fontSize: 12,
                      borderColor: duplicates.has(band.max) ? '#c46a00' : undefined,
                    }}
                  />
                </td>
                <td style={cell}>
                  <ColorPicker
                    value={band.color}
                    onChange={(color) => setBand(index, { color })}
                  />
                </td>
                <td style={cell}>
                  <button
                    type="button"
                    aria-label={`Remove band ${index + 1}`}
                    disabled={sorted.length <= 1}
                    onClick={() => onChange(sorted.filter((_, i) => i !== index))}
                    style={{ ...iconButton, height: 22, width: 22 }}
                  >
                    −
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div style={{ display: 'flex', gap: 6 }}>
        <button type="button" onClick={addBand} style={ghostButton}>
          Add band
        </button>
      </div>

      <p style={hint}>
        Each band starts where the one below it ends. The lowest extends down to the data and
        the highest extends up, so nothing is left uncoloured.
      </p>

      {duplicates.size > 0 ? (
        <p style={caution}>
          Two bands share the maximum {[...duplicates].map((v) => v.toLocaleString()).join(', ')}.
          One of them covers no values at all.
        </p>
      ) : null}

      {uncovered !== null ? (
        <p style={caution}>
          The data reaches {uncovered.toLocaleString()}
          {unit ? ` ${unit}` : ''}, above the highest band. Those values take the top colour.
        </p>
      ) : null}
    </div>
  );
}

function describe(
  lower: number | undefined,
  upper: number,
  unit: string | undefined,
  isFirst: boolean,
): string {
  const suffix = unit ? ` ${unit}` : '';
  const high = `${upper.toLocaleString()}${suffix}`;
  if (isFirst || lower === undefined) return `≤ ${high}`;
  return `${lower.toLocaleString()}${suffix} – ${high}`;
}
