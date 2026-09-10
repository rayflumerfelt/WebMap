/**
 * The editing hotkey layer. `09-editing.md` §8.
 *
 * **The registry is the single registrar.** Nothing here defines a shortcut;
 * this resolves a key press against what the registry already declares, which
 * is what makes "two commands share a key" a checkable property rather than a
 * hope.
 *
 * The interesting case is exactly that sharing. `Delete` is Delete Feature
 * under Edit and Delete Selected Vertices under Vertices, and both are legal
 * because no state enables both — `conflicts(state)` asserts it. So the
 * resolution is: find every command claiming the chord, keep the ones the
 * current state enables, and run it if exactly one survives.
 *
 * If two survive, **nothing runs and the caller is told**. That is a registry
 * bug rather than a runtime one, and picking the first would make the key mean
 * whichever command happened to be defined earlier.
 */

import { bindings, byId } from './registry.js';
import type { CommandDef, EditState } from './types.js';

/** The subset of a `KeyboardEvent` this module reads. Keeps it testable
 *  without constructing DOM events, and documents what is consulted. */
export interface KeyChord {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}

export type Resolution =
  | { kind: 'none' }
  | { kind: 'command'; command: CommandDef }
  /** Two enabled commands claim the chord. A registry bug; the caller logs it
   *  rather than guessing. */
  | { kind: 'ambiguous'; commands: CommandDef[] };

/**
 * Normalise a chord to the registry's spelling.
 *
 * `mod` matches **Ctrl or Meta** rather than branching on platform, for the
 * same reason `keyboard/shortcuts.ts` does: a Windows user on a Mac keyboard,
 * and a remote session where the modifier does not match the host, both
 * otherwise lose every shortcut. No editing shortcut distinguishes the two, so
 * accepting either costs nothing.
 *
 * A named key keeps its own casing — `Delete`, `Escape` — because that is what
 * the registry writes and what `KeyboardEvent.key` reports.
 */
export function chordString(chord: KeyChord): string {
  const key = chord.key.length === 1 ? chord.key.toLowerCase() : chord.key;
  return [
    chord.ctrlKey || chord.metaKey ? 'mod' : '',
    chord.shiftKey ? 'shift' : '',
    chord.altKey ? 'alt' : '',
    key,
  ]
    .filter(Boolean)
    .join('+');
}

/**
 * What a chord means in this state.
 *
 * `typing` comes from the caller's focus check. **Escape is the exception**:
 * cancelling out of a field is exactly what a user expects, and it is the one
 * key whose meaning does not change with focus. Everything else would insert
 * or replace characters in the field instead of running a command.
 */
export function resolve(
  chord: KeyChord,
  state: EditState,
  options: { typing?: boolean } = {},
  commands?: CommandDef[],
): Resolution {
  if (options.typing && chord.key !== 'Escape') return { kind: 'none' };

  // Alt is reserved: Alt+Arrow reorders layers and Alt bypasses snapping while
  // drawing (`09` §5.4). An Alt chord reaching here is meant for something
  // else, and swallowing it would break the thing it was meant for.
  if (chord.altKey) return { kind: 'none' };

  const lookup = byId(commands);
  const candidates = (bindings(commands).get(chordString(chord)) ?? [])
    .map((id) => lookup.get(id))
    .filter((command): command is CommandDef => command !== undefined);

  const live = candidates.filter(
    (command) => (!command.visible || command.visible(state)) && command.enabled(state),
  );

  if (live.length === 1) return { kind: 'command', command: live[0]! };
  if (live.length > 1) return { kind: 'ambiguous', commands: live };
  return { kind: 'none' };
}

/**
 * Whether a resolved chord should have its default prevented.
 *
 * Only when something actually ran. Swallowing keys indiscriminately breaks
 * the browser's own Ctrl+F and Ctrl+S for anyone who expected them — and an
 * *ambiguous* chord is not handled either, so the browser keeps it rather than
 * having it eaten by a bug.
 */
export function shouldPreventDefault(resolution: Resolution): boolean {
  return resolution.kind === 'command';
}
