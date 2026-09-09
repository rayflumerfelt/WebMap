/**
 * A docked, resizable panel. `07-frontend.md` §5.2.
 *
 * **Docked, not floating.** An overlay panel obscures the map, and the map is
 * what the geologist is reading. Everything docks around it and takes width
 * from it rather than covering it.
 *
 * Each panel collapses to a 40 px icon rail. Both the width and the collapsed
 * state persist per user — a geologist who works with the symbology panel shut
 * should not have to shut it again every morning.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

/** §5.2. Wide enough for the icon plus its hit slop, narrow enough to be a rail. */
export const RAIL_WIDTH = 40;

export interface DockedPanelProps {
  title: string;
  side: 'left' | 'right';
  width: number;
  collapsed: boolean;
  onWidthChange(width: number): void;
  onCollapsedChange(collapsed: boolean): void;
  minWidth?: number;
  maxWidth?: number;
  children: React.ReactNode;
}

export function DockedPanel({
  title,
  side,
  width,
  collapsed,
  onWidthChange,
  onCollapsedChange,
  minWidth = 200,
  maxWidth = 560,
  children,
}: DockedPanelProps) {
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  const clamp = useCallback(
    (value: number) => Math.max(minWidth, Math.min(maxWidth, value)),
    [maxWidth, minWidth],
  );

  useEffect(() => {
    if (!dragging) return;

    const onMove = (event: MouseEvent) => {
      const state = dragState.current;
      if (!state) return;
      // A left panel grows as the pointer moves right; a right panel grows as
      // it moves left. Getting this backwards makes the handle feel broken
      // rather than merely inverted.
      const delta = side === 'left' ? event.clientX - state.startX : state.startX - event.clientX;
      onWidthChange(clamp(state.startWidth + delta));
    };
    const onUp = () => {
      dragState.current = null;
      setDragging(false);
    };

    // Listeners on the document, not the handle: a fast drag outruns a 6 px
    // target and the pointer ends up over the map, where mousemove would
    // never reach the handle again and the panel would stick mid-resize.
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
  }, [clamp, dragging, onWidthChange, side]);

  if (collapsed) {
    return (
      <div
        style={{
          width: RAIL_WIDTH,
          flex: `0 0 ${RAIL_WIDTH}px`,
          borderRight: side === 'left' ? '1px solid var(--chrome-border)' : undefined,
          borderLeft: side === 'right' ? '1px solid var(--chrome-border)' : undefined,
          display: 'flex',
          justifyContent: 'center',
          paddingTop: 6,
        }}
      >
        <button
          type="button"
          aria-label={`Expand ${title} panel`}
          aria-expanded={false}
          onClick={() => onCollapsedChange(false)}
          style={railButtonStyle}
        >
          <span style={{ writingMode: 'vertical-rl', fontSize: 11 }}>{title}</span>
        </button>
      </div>
    );
  }

  const handle = (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${title} panel`}
      // A separator with a value is the only way a screen reader can tell the
      // panel was resized at all.
      aria-valuenow={Math.round(width)}
      aria-valuemin={minWidth}
      aria-valuemax={maxWidth}
      tabIndex={0}
      onMouseDown={(event) => {
        dragState.current = { startX: event.clientX, startWidth: width };
        setDragging(true);
      }}
      onKeyDown={(event) => {
        // §10: every pointer affordance has a keyboard path. 16 px a press,
        // which is coarse enough to cross the range in a few seconds.
        const step = event.shiftKey ? 64 : 16;
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          onWidthChange(clamp(width + (side === 'left' ? -step : step)));
        } else if (event.key === 'ArrowRight') {
          event.preventDefault();
          onWidthChange(clamp(width + (side === 'left' ? step : -step)));
        }
      }}
      style={{
        width: 6,
        flex: '0 0 6px',
        cursor: 'col-resize',
        // Painted as a hairline but 6 px wide: the hit area exceeds the
        // painted area, which at this size is the difference between usable
        // and infuriating.
        background: dragging ? 'var(--accent)' : 'transparent',
        borderLeft: side === 'right' ? '1px solid var(--chrome-border)' : undefined,
        borderRight: side === 'left' ? '1px solid var(--chrome-border)' : undefined,
      }}
    />
  );

  const body = (
    <section
      aria-label={title}
      style={{
        width,
        flex: `0 0 ${width}px`,
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
        background: 'var(--chrome-bg)',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          height: 'var(--control-h, 28px)',
          padding: '0 8px',
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: '0.04em',
          borderBottom: '1px solid var(--chrome-border)',
        }}
      >
        <span>{title}</span>
        <button
          type="button"
          aria-label={`Collapse ${title} panel`}
          aria-expanded
          onClick={() => onCollapsedChange(true)}
          style={railButtonStyle}
        >
          {side === 'left' ? '‹' : '›'}
        </button>
      </header>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 8 }}>{children}</div>
    </section>
  );

  return side === 'left' ? (
    <>
      {body}
      {handle}
    </>
  ) : (
    <>
      {handle}
      {body}
    </>
  );
}

const railButtonStyle: React.CSSProperties = {
  padding: 'var(--hit-slop, 4px)',
  border: 0,
  background: 'transparent',
  cursor: 'pointer',
  font: 'inherit',
  color: 'inherit',
  lineHeight: 1,
};
