/**
 * The command palette. `09-editing.md` §8, `07-frontend.md` §5.4 (`mod+k`).
 *
 * **Disabled commands are excluded**, unlike the menu. A palette is a
 * do-this-now surface: you type three letters and press Enter, and a greyed row
 * you can land on and not run is noise on the one surface where speed is the
 * whole point.
 *
 * What makes it worth having beyond the menu is the **description and the
 * keywords**. `09` §9.1 forbids labelling two commands "Merge", because the
 * ambiguity between multi-part wrapping and true union produces silent data
 * loss — so a user arriving from another tool types "merge" and this is what
 * has to return Combine *and* Dissolve, each with the sentence that says what
 * happens to the geometry.
 */

import type { CSSProperties } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { paletteEntries } from './registry.js';
import type { CommandDef, EditState } from './types.js';

export interface CommandPaletteProps {
  open: boolean;
  state: EditState;
  commands: CommandDef[];
  onRun(command: CommandDef): void;
  onClose(): void;
  className?: string | undefined;
}

/** Rows rendered at once. Beyond this the list is a scroll rather than a
 *  choice, and the answer is a better query. */
const MAX_ROWS = 12;

const backdrop: CSSProperties = {
  position: 'fixed',
  inset: 0,
  display: 'grid',
  // Not centred vertically: a palette that opens under the cursor's usual
  // resting place covers the map. A fifth of the way down keeps the map's
  // middle visible while you type.
  alignContent: 'start',
  justifyItems: 'center',
  paddingTop: '18vh',
  background: 'rgba(20, 24, 30, 0.25)',
  zIndex: 60,
};

const panel: CSSProperties = {
  width: 520,
  maxWidth: '90vw',
  border: '1px solid #c9ccd1',
  borderRadius: 4,
  background: '#fff',
  boxShadow: '0 12px 32px rgba(0, 0, 0, 0.22)',
  overflow: 'hidden',
};

const input: CSSProperties = {
  width: '100%',
  height: 40,
  padding: '0 12px',
  border: 'none',
  borderBottom: '1px solid #eceef1',
  fontSize: 14,
  outline: 'none',
  boxSizing: 'border-box',
};

const row: CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  gap: 10,
  width: '100%',
  padding: '6px 12px',
  border: 'none',
  background: 'transparent',
  fontSize: 12,
  textAlign: 'left',
  cursor: 'pointer',
};

export function CommandPalette(props: CommandPaletteProps) {
  const { open, state, commands, onRun, onClose, className } = props;
  const [query, setQuery] = useState('');
  const [highlighted, setHighlighted] = useState(0);
  const field = useRef<HTMLInputElement>(null);

  const matches = useMemo(
    () => paletteEntries(state, query, commands).slice(0, MAX_ROWS),
    [state, query, commands],
  );

  useEffect(() => {
    if (!open) return;
    setQuery('');
    setHighlighted(0);
    field.current?.focus();
  }, [open]);

  // The highlight is clamped rather than reset as the query narrows: a user
  // typing one more letter should keep their place near the top, not be sent
  // back to row zero on every keystroke.
  useEffect(() => {
    setHighlighted((current) => Math.min(current, Math.max(0, matches.length - 1)));
  }, [matches.length]);

  if (!open) return null;

  const run = (command: CommandDef | undefined) => {
    if (!command) return;
    onClose();
    onRun(command);
  };

  return (
    <div
      className={className}
      style={backdrop}
      role="presentation"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div style={panel} role="dialog" aria-modal="true" aria-label="Command palette">
        <input
          ref={field}
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls="command-palette-list"
          aria-label="Search commands"
          placeholder="Search commands…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              onClose();
            }
            if (event.key === 'ArrowDown') {
              event.preventDefault();
              setHighlighted((current) => Math.min(current + 1, matches.length - 1));
            }
            if (event.key === 'ArrowUp') {
              event.preventDefault();
              setHighlighted((current) => Math.max(current - 1, 0));
            }
            if (event.key === 'Enter') {
              event.preventDefault();
              run(matches[highlighted]);
            }
          }}
          style={input}
        />

        <div id="command-palette-list" role="listbox" aria-label="Commands">
          {matches.length === 0 ? (
            <p style={{ margin: 0, padding: '10px 12px', fontSize: 12, color: '#6b7078' }}>
              {/* Says *why*, because the usual reason is a state the user can
                  fix — nothing selected, no active layer — rather than a
                  command that does not exist. */}
              Nothing matches “{query}” that can run right now. Commands that need a
              selection or an active layer are hidden until they apply.
            </p>
          ) : (
            matches.map((command, index) => (
              <button
                key={command.id}
                type="button"
                role="option"
                aria-selected={index === highlighted}
                onPointerEnter={() => setHighlighted(index)}
                onClick={() => run(command)}
                style={{
                  ...row,
                  background: index === highlighted ? '#eaf1fd' : 'transparent',
                }}
              >
                <span style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{command.label}</span>
                {command.description ? (
                  <span style={{ flex: 1, color: '#6b7078' }}>{command.description}</span>
                ) : (
                  <span style={{ flex: 1 }} />
                )}
                {command.shortcut ? (
                  <kbd style={{ fontSize: 11, color: '#6b7078', fontFamily: 'inherit' }}>
                    {command.shortcut}
                  </kbd>
                ) : null}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
