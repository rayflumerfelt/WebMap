/**
 * Keyboard shortcuts. `07-frontend.md` §5.4.
 *
 * Single-letter tool shortcuts follow the convention geologists already know
 * from QGIS and Surfer, which is why they are bare letters rather than
 * chorded: someone who has used those for a decade reaches for `v`, `i`, `m`
 * without thinking. A command palette (`mod+k`) covers everything else without
 * growing the toolbar.
 *
 * Bare letters make one rule load-bearing: **a shortcut never fires while the
 * user is typing.** Without that, renaming a layer to "measured depth" would
 * switch tools four times and open the identify tool, and the text field would
 * end up holding whatever survived.
 */

/** `mod` is Cmd on macOS and Ctrl everywhere else. */
export const SHORTCUTS = {
  'mod+s': 'session.save',
  'mod+z': 'edit.undo',
  'mod+shift+z': 'edit.redo',
  'mod+f': 'search.datasets',
  'mod+k': 'command.palette',
  'mod+1': 'panel.layers.toggle',
  'mod+2': 'panel.symbology.toggle',
  'mod+3': 'panel.attributes.toggle',
  'mod+enter': 'render.current',
  e: 'tool.edit',
  v: 'tool.select',
  i: 'tool.identify',
  m: 'tool.measure',
  f: 'view.zoomToLayer',
  Escape: 'tool.cancel',
} as const;

export type Shortcut = keyof typeof SHORTCUTS;
export type Command = (typeof SHORTCUTS)[Shortcut];

/** The subset of a KeyboardEvent this module needs. Keeps it testable without
 *  constructing DOM events, and documents exactly what is consulted. */
export interface KeyChord {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}

/**
 * Whether a key event should be treated as a shortcut at all.
 *
 * Returns false inside any text entry — including `contenteditable`, which is
 * easy to forget and is what a rich caption field will be. `Escape` is the
 * exception: cancelling out of a field is exactly what a user expects it to
 * do, and it is the one shortcut whose meaning does not change with focus.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    target.isContentEditable ||
    // A combobox or listbox built from divs takes typed input too, and
    // announces itself through its role rather than its tag.
    target.getAttribute('role') === 'combobox' ||
    target.getAttribute('role') === 'textbox'
  );
}

/**
 * The command a chord invokes, or null.
 *
 * `mod` matches Ctrl or Meta rather than branching on platform: a Windows user
 * on a Mac keyboard, and a remote session where the modifier does not match
 * the host, both otherwise lose every shortcut. Accepting either costs
 * nothing, because no shortcut here distinguishes them.
 */
export function commandFor(chord: KeyChord, options: { typing?: boolean } = {}): Command | null {
  const key = normaliseKey(chord.key);
  const mod = Boolean(chord.ctrlKey || chord.metaKey);

  if (options.typing) {
    // Escape still works while typing — it cancels the field. Everything else
    // would insert or replace characters instead.
    return key === 'escape' ? SHORTCUTS.Escape : null;
  }

  // Alt is reserved: Alt+Arrow reorders layers, Alt bypasses snapping while
  // drawing (§5.4). No shortcut in the table uses it, so an Alt chord reaching
  // here is meant for something else.
  if (chord.altKey) return null;

  const parts = [mod ? 'mod' : '', chord.shiftKey ? 'shift' : '', key].filter(Boolean);
  const lookup = parts.join('+');

  for (const [pattern, command] of Object.entries(SHORTCUTS)) {
    if (pattern.toLowerCase() === lookup) return command;
  }
  return null;
}

function normaliseKey(key: string): string {
  if (key === ' ') return 'space';
  return key.toLowerCase();
}

/** Human-readable, for a menu or a tooltip. `⌘S` on macOS, `Ctrl+S` elsewhere. */
export function formatShortcut(shortcut: Shortcut, platform: 'mac' | 'other' = 'other'): string {
  return shortcut
    .split('+')
    .map((part) => {
      if (part === 'mod') return platform === 'mac' ? '⌘' : 'Ctrl';
      if (part === 'shift') return platform === 'mac' ? '⇧' : 'Shift';
      if (part === 'enter') return platform === 'mac' ? '↩' : 'Enter';
      return part.length === 1 ? part.toUpperCase() : part;
    })
    .join(platform === 'mac' ? '' : '+');
}
