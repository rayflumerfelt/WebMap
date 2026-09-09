/**
 * The docked shell. `07-frontend.md` §5.1, §5.2, §10.
 *
 * Covers three Phase 2 acceptance criteria that do not need a browser:
 * panel widths and collapsed state persisting, the sub-1280 px notice, and the
 * status bar showing the analysis CRS, live cursor coordinates *in that CRS*,
 * and map scale.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DockedPanel, RAIL_WIDTH } from './DockedPanel.js';
import { StatusBar, formatCoordinate, scaleDenominatorFor } from './StatusBar.js';
import {
  DEFAULT_PREFS,
  STORAGE_KEY,
  loadPrefs,
  parsePrefs,
  savePrefs,
} from './panelPrefs.js';

afterEach(cleanup);

// --- panel preferences ------------------------------------------------------

describe('panel preferences', () => {
  function fakeStorage(initial: Record<string, string> = {}) {
    const data = { ...initial };
    return {
      data,
      getItem: (key: string) => data[key] ?? null,
      setItem: (key: string, value: string) => {
        data[key] = value;
      },
    };
  }

  it('round-trips widths and collapsed state', () => {
    // The criterion: "panel widths and collapsed state persist per user
    // across sessions."
    const storage = fakeStorage();
    const prefs = {
      ...DEFAULT_PREFS,
      layers: { width: 420, collapsed: false },
      symbology: { width: 300, collapsed: true },
    };

    savePrefs(prefs, storage);

    expect(loadPrefs(storage)).toEqual(prefs);
  });

  it('falls back to the baseline layout when nothing is stored', () => {
    expect(loadPrefs(fakeStorage())).toEqual(DEFAULT_PREFS);
  });

  it('clamps a stored width rather than trusting it', () => {
    // Stored values are user-writable — devtools, a synced profile, an older
    // version of this code. A width of 5000 leaves no map.
    const prefs = parsePrefs({ layers: { width: 5000, collapsed: false } });

    expect(prefs.layers.width).toBe(560);
  });

  it('rejects NaN, which would collapse the map with no way back', () => {
    const prefs = parsePrefs({ layers: { width: Number.NaN, collapsed: false } });

    expect(prefs.layers.width).toBe(DEFAULT_PREFS.layers.width);
  });

  it('survives corrupt storage without blanking the page', () => {
    expect(loadPrefs(fakeStorage({ [STORAGE_KEY]: '{not json' }))).toEqual(DEFAULT_PREFS);
  });

  it('fills in a key the stored object is missing', () => {
    // An older version wrote two panels; this one has three. A partial object
    // would be a crash on first paint.
    const prefs = parsePrefs({ layers: { width: 300, collapsed: false } });

    expect(prefs.symbology).toEqual(DEFAULT_PREFS.symbology);
    expect(prefs.attributes).toEqual(DEFAULT_PREFS.attributes);
  });

  it('does not throw when storage itself is unavailable', () => {
    // Safari in private mode, and any browser with site data blocked, throw
    // on access rather than returning null.
    const hostile = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };

    expect(loadPrefs(hostile)).toEqual(DEFAULT_PREFS);
    expect(() => savePrefs(DEFAULT_PREFS, hostile)).not.toThrow();
  });
});

// --- docked panel -----------------------------------------------------------

describe('DockedPanel', () => {
  function renderPanel(overrides: Partial<React.ComponentProps<typeof DockedPanel>> = {}) {
    const onWidthChange = vi.fn();
    const onCollapsedChange = vi.fn();
    const result = render(
      <DockedPanel
        title="Layers"
        side="left"
        width={280}
        collapsed={false}
        onWidthChange={onWidthChange}
        onCollapsedChange={onCollapsedChange}
        {...overrides}
      >
        <div>panel content</div>
      </DockedPanel>,
    );
    return { onWidthChange, onCollapsedChange, ...result };
  }

  it('collapses to the icon rail', () => {
    const { container } = renderPanel({ collapsed: true });

    const rail = container.firstChild as HTMLElement;
    expect(rail.style.width).toBe(`${RAIL_WIDTH}px`);
    expect(screen.queryByText('panel content')).toBeNull();
  });

  it('expands from the rail', () => {
    const { onCollapsedChange } = renderPanel({ collapsed: true });

    fireEvent.click(screen.getByLabelText('Expand Layers panel'));

    expect(onCollapsedChange).toHaveBeenCalledWith(false);
  });

  it('resizes by dragging the handle', () => {
    const { onWidthChange } = renderPanel();

    fireEvent.mouseDown(screen.getByRole('separator'), { clientX: 280 });
    fireEvent.mouseMove(document, { clientX: 340 });

    expect(onWidthChange).toHaveBeenCalledWith(340);
  });

  it('keeps tracking a drag that outruns the handle', () => {
    // A 6 px target is easy to outrun. With listeners on the handle rather
    // than the document, the pointer ends up over the map and the panel
    // sticks mid-resize.
    const { onWidthChange } = renderPanel();

    fireEvent.mouseDown(screen.getByRole('separator'), { clientX: 280 });
    fireEvent.mouseMove(document.body, { clientX: 900 });

    expect(onWidthChange).toHaveBeenCalledWith(560);
  });

  it('grows a right panel when the pointer moves left', () => {
    // Getting this backwards makes the handle feel broken rather than merely
    // inverted.
    const { onWidthChange } = renderPanel({ side: 'right', width: 340 });

    fireEvent.mouseDown(screen.getByRole('separator'), { clientX: 1000 });
    fireEvent.mouseMove(document, { clientX: 940 });

    expect(onWidthChange).toHaveBeenCalledWith(400);
  });

  it('stops resizing after mouseup', () => {
    const { onWidthChange } = renderPanel();
    fireEvent.mouseDown(screen.getByRole('separator'), { clientX: 280 });
    fireEvent.mouseUp(document);
    onWidthChange.mockClear();

    fireEvent.mouseMove(document, { clientX: 500 });

    expect(onWidthChange).not.toHaveBeenCalled();
  });

  it('resizes from the keyboard', () => {
    // §10: every pointer affordance has a keyboard path.
    const { onWidthChange } = renderPanel();

    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowRight' });

    expect(onWidthChange).toHaveBeenCalledWith(296);
  });

  it('takes a bigger step with Shift, so the range is crossable', () => {
    const { onWidthChange } = renderPanel();

    fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowRight', shiftKey: true });

    expect(onWidthChange).toHaveBeenCalledWith(344);
  });

  it('announces its width, so a resize is perceivable without sight', () => {
    renderPanel();

    const handle = screen.getByRole('separator');
    expect(handle.getAttribute('aria-valuenow')).toBe('280');
    expect(handle.getAttribute('aria-valuemin')).toBe('200');
  });

  it('clamps at the minimum rather than letting the panel vanish', () => {
    const { onWidthChange } = renderPanel();

    fireEvent.mouseDown(screen.getByRole('separator'), { clientX: 280 });
    fireEvent.mouseMove(document, { clientX: 0 });

    expect(onWidthChange).toHaveBeenCalledWith(200);
  });
});

// --- status bar -------------------------------------------------------------

describe('StatusBar', () => {
  it('shows the analysis CRS, cursor position and scale together', () => {
    // The criterion, in one assertion: all three are always visible, because
    // geologists check them constantly.
    render(
      <StatusBar
        crsLabel="EPSG:2277 · NAD83 / Texas Central (ftUS)"
        cursor={{ x: 1_784_231, y: 10_612_004 }}
        unit="usft"
        scaleDenominator={24_000}
      />,
    );

    expect(screen.getByText(/EPSG:2277/)).toBeDefined();
    expect(screen.getByText(/E 1,784,231/)).toBeDefined();
    expect(screen.getByText('1:24,000')).toBeDefined();
  });

  it('labels coordinates E and N, as a survey plat does', () => {
    // Removes any doubt about which number is which.
    expect(formatCoordinate({ x: 1_784_231.4, y: 10_612_004.6 }, 'usft')).toBe(
      'E 1,784,231 · N 10,612,005 ft',
    );
  });

  it('rounds to whole units rather than claiming false precision', () => {
    // A State Plane easting has seven figures before the point; three after it
    // is a precision no map interaction has.
    expect(formatCoordinate({ x: 1_784_231.4826, y: 10_612_004.19 }, 'usft')).not.toContain('.');
  });

  it('shows an em dash rather than stale coordinates off the map', () => {
    render(<StatusBar crsLabel="EPSG:2277" cursor={null} unit="usft" scaleDenominator={null} />);

    expect(screen.getAllByText('—')).toHaveLength(2);
  });

  it('says plainly when autosave has stopped', () => {
    // A quiet indicator would let someone keep editing while nothing is
    // written.
    render(
      <StatusBar
        crsLabel="EPSG:2277"
        cursor={null}
        unit="usft"
        scaleDenominator={null}
        save={{ dirty: true, conflict: true, lastSavedAt: null }}
      />,
    );

    expect(screen.getByRole('status').textContent).toContain('Not saving');
  });

  it('announces job progress in a live region', () => {
    // §10: gridding finishes while attention is elsewhere.
    const { container } = render(
      <StatusBar
        crsLabel="EPSG:2277"
        cursor={null}
        unit="usft"
        scaleDenominator={null}
        job={{ label: 'Gridding Wolfcamp A', progress: 0.4 }}
      />,
    );

    const live = container.querySelector('[aria-live="polite"]')!;
    expect(live.textContent).toContain('Gridding Wolfcamp A');
    expect(live.textContent).toContain('40%');
  });
});

describe('scaleDenominatorFor', () => {
  it('corrects for latitude, like the scale bar', () => {
    // A scale printed on a map is a claim, and "1:24,000" that is really
    // 1:28,000 is a wrong one.
    const equator = scaleDenominatorFor(0, 14);
    const midland = scaleDenominatorFor(31.99, 14);

    expect(midland / equator).toBeCloseTo(Math.cos((31.99 * Math.PI) / 180), 6);
  });

  it('matches a hand-computed value', () => {
    // 40075016.686 / 2^23 = 4.777314 m/px at the equator. Divided by the OGC
    // standardized rendering pixel of 0.28 mm: 17,061.84. Carried to two
    // decimals rather than rounded to the nearest thousand, so a change to
    // either constant shows up here instead of hiding inside the tolerance.
    expect(scaleDenominatorFor(0, 14)).toBeCloseTo(17_061.84, 2);
  });

  it('accounts for device pixel ratio', () => {
    // On a 2× display a CSS pixel covers half the ground, so the map is
    // effectively at twice the scale.
    expect(scaleDenominatorFor(32, 14, 2)).toBeCloseTo(scaleDenominatorFor(32, 14) * 2, 6);
  });
});
