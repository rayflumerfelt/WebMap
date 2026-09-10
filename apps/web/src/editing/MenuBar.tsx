/**
 * The menu bar. `09-editing.md` §9, rendering from the registry (§8).
 *
 * **A disabled command still appears here, greyed**, so it stays discoverable
 * and its shortcut stays visible. That is the difference from the palette,
 * which excludes them — a menu is where you go to find out what a tool can do,
 * and a palette is where you go when you already know.
 *
 * Keyboard behaviour is not an afterthought. `07` §10 requires every
 * mouse-reachable action to be keyboard-reachable, and a menu is the place
 * where that is usually half-done: arrow keys move within a menu, left and
 * right move between menus, `Esc` closes, and a shortcut is rendered beside
 * every item that has one so the menu teaches its own shortcuts.
 */

import type { CSSProperties } from 'react';
import { useEffect, useRef, useState } from 'react';

import { menus } from './registry.js';
import type { CommandDef, EditState } from './types.js';

export interface MenuBarProps {
  state: EditState;
  commands: CommandDef[];
  onRun(command: CommandDef): void;
  className?: string | undefined;
}

const bar: CSSProperties = {
  display: 'flex',
  alignItems: 'stretch',
  height: 28,
  borderBottom: '1px solid #d7dae0',
  background: '#fff',
  fontSize: 12,
};

const trigger: CSSProperties = {
  padding: '0 10px',
  border: 'none',
  background: 'transparent',
  fontSize: 12,
  cursor: 'pointer',
};

const openTrigger: CSSProperties = {
  ...trigger,
  background: '#eaf1fd',
};

const dropdown: CSSProperties = {
  position: 'absolute',
  top: '100%',
  left: 0,
  minWidth: 240,
  padding: '4px 0',
  border: '1px solid #c9ccd1',
  borderRadius: 3,
  background: '#fff',
  boxShadow: '0 4px 12px rgba(0, 0, 0, 0.12)',
  zIndex: 30,
};

const item: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 16,
  width: '100%',
  minHeight: 'var(--row-h, 26px)',
  padding: '0 10px',
  border: 'none',
  background: 'transparent',
  fontSize: 12,
  textAlign: 'left',
  cursor: 'pointer',
};

/**
 * `mod` is Cmd on macOS and Ctrl everywhere else — resolved once, here, at the
 * only place a shortcut is *shown* rather than matched.
 *
 * The **key** is uppercased, not the whole string: a menu that teaches its own
 * shortcuts has to spell them the way a keyboard does, and `Ctrl+z` reads as a
 * different key from the one on the keycap. Modifiers keep their own spelling
 * because `CTRL` shouts.
 */
export function renderShortcut(shortcut: string): string {
  const mac =
    typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform ?? '');

  const parts = shortcut.split('+').map((part) => {
    if (part === 'mod') return mac ? '⌘' : 'Ctrl';
    if (part === 'shift') return mac ? '⇧' : 'Shift';
    if (part === 'alt') return mac ? '⌥' : 'Alt';
    // A named key — `Delete`, `Escape` — already carries its own casing.
    return part.length === 1 ? part.toUpperCase() : part;
  });

  return parts.join(mac ? '' : '+');
}

export function MenuBar({ state, commands, onRun, className }: MenuBarProps) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const groups = menus(state, commands);

  useEffect(() => {
    if (openIndex === null) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpenIndex(null);
    };
    // Pointer-down rather than click: a click that lands on the map should
    // close the menu *before* the map handles it, or the first click after
    // opening a menu also starts drawing.
    window.addEventListener('pointerdown', onPointerDown);
    return () => window.removeEventListener('pointerdown', onPointerDown);
  }, [openIndex]);

  const move = (delta: number) => {
    if (openIndex === null) return;
    setOpenIndex((openIndex + delta + groups.length) % groups.length);
  };

  return (
    <div
      ref={container}
      className={className}
      style={bar}
      role="menubar"
      aria-label="Editing menus"
      onKeyDown={(event) => {
        if (event.key === 'Escape') setOpenIndex(null);
        if (event.key === 'ArrowRight') move(1);
        if (event.key === 'ArrowLeft') move(-1);
      }}
    >
      {groups.map((group, index) => (
        <div key={group.group} style={{ position: 'relative' }}>
          <button
            type="button"
            role="menuitem"
            aria-haspopup="menu"
            aria-expanded={openIndex === index}
            onClick={() => setOpenIndex(openIndex === index ? null : index)}
            style={openIndex === index ? openTrigger : trigger}
          >
            {group.label}
          </button>

          {openIndex === index ? (
            <div role="menu" aria-label={group.label} style={dropdown}>
              {group.items.map((command) => {
                const enabled = command.enabled(state);
                const checked = command.checked?.(state);
                return (
                  <button
                    key={command.id}
                    type="button"
                    role={checked === undefined ? 'menuitem' : 'menuitemcheckbox'}
                    aria-checked={checked}
                    disabled={!enabled}
                    title={command.description}
                    onClick={() => {
                      setOpenIndex(null);
                      onRun(command);
                    }}
                    style={{
                      ...item,
                      // Greyed, not hidden. The shortcut stays legible too,
                      // which is half of why a disabled item earns its place.
                      color: enabled ? 'inherit' : '#9aa0a8',
                      cursor: enabled ? 'pointer' : 'default',
                    }}
                  >
                    <span style={{ width: 12 }}>{checked ? '✓' : ''}</span>
                    <span style={{ flex: 1 }}>{command.label}</span>
                    {command.shortcut ? (
                      <kbd
                        style={{
                          fontSize: 11,
                          color: '#6b7078',
                          fontFamily: 'inherit',
                        }}
                      >
                        {renderShortcut(command.shortcut)}
                      </kbd>
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
