/**
 * The contextual operation bar. `09-editing.md` §10.2, §5.1.
 *
 * Appears **only** while a modal operation runs, and disappears the instant it
 * resolves. Operation name, its parameters, Apply and Cancel, and a one-line
 * hint that says what to do next.
 *
 * **Visually distinct from the persistent toolbar — a different background —
 * so the two levels of commitment are never confused.** That is not decoration:
 * §5 keeps operation state out of the dirty buffer entirely, so Cancel here
 * loses nothing and Discard on the toolbar above loses an afternoon. A bar that
 * looked the same as the toolbar would make those two buttons look like
 * variations of each other.
 *
 * `Esc` cancels with no confirmation, because nothing is lost; `Enter` applies.
 * Both are bound here rather than in the global hotkey map: while an operation
 * is running they mean *this* operation, and a global binding would have to ask
 * what is on screen.
 */

import type { CSSProperties, ReactNode } from 'react';
import { useEffect, useRef } from 'react';

export interface OperationBarProps {
  /** The operation's name — "Split", "Smooth", "Offset". */
  title: string;
  /**
   * One line saying what to do next: *"Click to add points along the cut line.
   * Enter to apply, Esc to cancel."*
   *
   * Required, not optional. A modal mode with no instruction is the state
   * where a user clicks once, nothing visible happens, and they leave.
   */
  hint: string;
  /** The operation's parameters — a slider, a distance field, a checkbox. */
  children?: ReactNode;
  /** False while the parameters are incomplete: a buffer with no distance, a
   *  split with one point. Apply greys rather than failing on click. */
  canApply?: boolean;
  onApply(): void;
  onCancel(): void;
  className?: string | undefined;
}

const bar: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  minHeight: 40,
  padding: '4px 8px',
  // The different background §10.2 asks for. Warm rather than alarming: an
  // operation in progress is a normal state, not an error.
  background: '#fdf6e3',
  borderBottom: '1px solid #e0d3a8',
  fontSize: 12,
};

const button: CSSProperties = {
  height: 'var(--control-h, 28px)',
  padding: '0 12px',
  border: '1px solid #c9ccd1',
  borderRadius: 3,
  background: '#fff',
  fontSize: 12,
  cursor: 'pointer',
};

const primary: CSSProperties = {
  ...button,
  borderColor: '#1c5cc4',
  background: '#1c5cc4',
  color: '#fff',
  fontWeight: 600,
};

export function OperationBar(props: OperationBarProps) {
  const { title, hint, children, canApply = true, onApply, onCancel, className } = props;
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCancel();
        return;
      }
      // Enter applies — unless the focus is in a textarea, where Enter is a
      // newline and hijacking it would make a comment field unusable.
      if (event.key === 'Enter' && canApply) {
        const target = event.target as HTMLElement | null;
        if (target?.tagName === 'TEXTAREA') return;
        event.preventDefault();
        onApply();
      }
    };
    // On the window rather than on the bar: the pointer is usually over the
    // map while an operation runs, so the bar rarely holds focus, and a
    // listener that needed it would make Enter work only after a click.
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [canApply, onApply, onCancel]);

  return (
    <div
      ref={container}
      className={className}
      style={bar}
      role="region"
      aria-label={`${title} operation`}
    >
      <strong style={{ fontSize: 12 }}>{title}</strong>

      {children ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>{children}</div>
      ) : null}

      <span style={{ color: '#6b5b2a', marginLeft: 4 }}>{hint}</span>

      <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
        <button type="button" onClick={onCancel} style={button}>
          Cancel
        </button>
        <button type="button" disabled={!canApply} onClick={onApply} style={primary}>
          Apply
        </button>
      </div>
    </div>
  );
}
