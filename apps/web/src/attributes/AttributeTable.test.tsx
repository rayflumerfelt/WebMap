/**
 * Attribute table. `07-frontend.md` §5.2, §9.
 *
 * The load-bearing property is virtualization: 500k rows must not become 500k
 * DOM nodes. That is asserted by counting rendered rows against the data,
 * which is the only check that distinguishes "virtualized" from "fast today".
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AttributeTable, format } from './AttributeTable.js';
import type { AttributeColumn, AttributeRow } from './AttributeTable.js';

afterEach(cleanup);

// The virtualizer needs a measurable scroll element and a ResizeObserver,
// neither of which jsdom provides. Both are shimmed in `vitest.setup.ts`.

const COLUMNS: AttributeColumn[] = [
  { name: 'well_name', type: 'string' },
  { name: 'porosity', type: 'number', precision: 2 },
  { name: 'tvdss_ft', type: 'number' },
  { name: 'validated', type: 'boolean' },
];

function rows(count: number): AttributeRow[] {
  return Array.from({ length: count }, (_, i) => ({
    well_name: `Well ${i + 1}`,
    porosity: 4 + (i % 180) / 10,
    tvdss_ft: -8_200 - i,
    validated: i % 2 === 0,
  }));
}

describe('virtualization', () => {
  it('mounts a window of rows, not all of them', () => {
    // **The property this component exists for.** 500k rows as DOM nodes is
    // not slow, it is fatal: several kilobytes each, and the tab dies before
    // the table appears.
    render(<AttributeTable columns={COLUMNS} rows={rows(500_000)} />);

    const rendered = screen.getAllByRole('row').length;
    // One header row plus a window. The exact count depends on the
    // virtualizer's overscan; the assertion is the order of magnitude.
    expect(rendered).toBeLessThan(50);
    expect(rendered).toBeGreaterThan(1);
  });

  it('reports the true row count to assistive technology', () => {
    // The window is a rendering detail. A screen reader announcing "3 rows"
    // for a 500k-feature layer would be actively misleading.
    render(<AttributeTable columns={COLUMNS} rows={rows(1_000)} />);

    expect(screen.getByRole('grid').getAttribute('aria-rowcount')).toBe('1000');
  });

  it('says plainly when it is showing a page of a larger layer', () => {
    // A table showing 5,000 of 500,000 rows without saying so is a table
    // someone will draw a conclusion from.
    render(<AttributeTable columns={COLUMNS} rows={rows(5_000)} totalCount={500_000} />);

    expect(screen.getByRole('status').textContent).toContain('5,000 of 500,000');
  });

  it('says nothing when it is showing everything', () => {
    render(<AttributeTable columns={COLUMNS} rows={rows(20)} totalCount={20} />);

    expect(screen.queryByRole('status')).toBeNull();
  });
});

describe('formatting', () => {
  it('renders null as an em dash rather than as an empty cell', () => {
    // Null and "not measured here" are the same thing to a reader; a blank
    // cell reads as a rendering gap.
    expect(format(null, COLUMNS[0]!)).toBe('—');
    expect(format(undefined, COLUMNS[1]!)).toBe('—');
  });

  it('renders NaN as an em dash too', () => {
    // A NaN porosity is missing data that arrived by another route.
    expect(format(Number.NaN, COLUMNS[1]!)).toBe('—');
  });

  it('honours a column precision', () => {
    expect(format(12.3456, COLUMNS[1]!)).toBe('12.35');
  });

  it('separates thousands on a depth', () => {
    expect(format(-8_237, COLUMNS[2]!)).toBe('-8,237');
  });

  it('does not round a value the column gives no precision for', () => {
    // Silently rounding an attribute the geologist did not ask to round makes
    // the table disagree with the data it is showing.
    expect(format(12.345678, COLUMNS[2]!)).toBe('12.345678');
  });

  it('right-aligns numbers with tabular figures', () => {
    // A column of porosities is scanned for outliers by eye, and proportional
    // digits with ragged alignment defeat that completely.
    const { container } = render(<AttributeTable columns={COLUMNS} rows={rows(3)} />);

    const cells = [...container.querySelectorAll('[role="row"] span')] as HTMLElement[];
    const numeric = cells.find((cell) => cell.textContent?.includes('.'))!;
    expect(numeric.style.textAlign).toBe('right');
    expect(numeric.style.fontVariantNumeric).toBe('tabular-nums');
  });
});

describe('sorting', () => {
  it('sorts ascending on first click and descending on the second', () => {
    render(<AttributeTable columns={COLUMNS} rows={rows(50)} />);
    const header = screen.getByRole('columnheader', { name: /tvdss_ft/ });

    fireEvent.click(header);
    expect(header.getAttribute('aria-sort')).toBe('ascending');

    fireEvent.click(header);
    expect(header.getAttribute('aria-sort')).toBe('descending');
  });

  it('puts nulls last in both directions', () => {
    // A column sorted descending that opens with a screen of blanks looks
    // broken, and the reader wanted the largest values, not the missing ones.
    const sparse: AttributeRow[] = [
      { well_name: 'A', porosity: null, tvdss_ft: -8_000, validated: true },
      { well_name: 'B', porosity: 12, tvdss_ft: -8_100, validated: true },
      { well_name: 'C', porosity: 8, tvdss_ft: -8_200, validated: true },
    ];
    render(<AttributeTable columns={COLUMNS} rows={sparse} />);
    const header = screen.getByRole('columnheader', { name: /porosity/ });

    fireEvent.click(header);
    const ascending = screen.getAllByRole('row').slice(1).map((r) => r.textContent);
    fireEvent.click(header);
    const descending = screen.getAllByRole('row').slice(1).map((r) => r.textContent);

    expect(ascending.at(-1)).toContain('—');
    expect(descending.at(-1)).toContain('—');
  });
});

describe('interaction', () => {
  it('selects a row on click', () => {
    const onSelect = vi.fn();
    render(<AttributeTable columns={COLUMNS} rows={rows(10)} onSelect={onSelect} />);

    fireEvent.click(screen.getAllByRole('row')[1]!);

    expect(onSelect).toHaveBeenCalledWith(0);
  });

  it('zooms to a feature on double-click', () => {
    // §5.4: double-click on a layer zooms to its extent. The same gesture in
    // the table means the same thing for one feature.
    const onZoomTo = vi.fn();
    render(<AttributeTable columns={COLUMNS} rows={rows(10)} onZoomTo={onZoomTo} />);

    fireEvent.doubleClick(screen.getAllByRole('row')[1]!);

    expect(onZoomTo).toHaveBeenCalledWith(0);
  });

  it('zooms from the keyboard too', () => {
    // §10: every pointer affordance has a keyboard path. Double-click has
    // none by default.
    const onZoomTo = vi.fn();
    render(<AttributeTable columns={COLUMNS} rows={rows(10)} onZoomTo={onZoomTo} />);

    fireEvent.keyDown(screen.getAllByRole('row')[1]!, { key: 'Enter' });

    expect(onZoomTo).toHaveBeenCalledWith(0);
  });
});

describe('empty state', () => {
  it('says a layer has no attributes rather than drawing an empty grid', () => {
    render(<AttributeTable columns={[]} rows={[]} />);

    expect(screen.getByText(/no attributes/)).toBeDefined();
    expect(screen.queryByRole('grid')).toBeNull();
  });
});
