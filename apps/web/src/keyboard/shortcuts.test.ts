/**
 * Keyboard shortcuts. `07-frontend.md` §5.4, and the §11 acceptance criterion
 * "every documented keyboard shortcut works".
 *
 * The criterion is asserted literally: the test iterates `SHORTCUTS` rather
 * than a hand-copied list, so adding one to the table without making it
 * resolve fails here.
 */

import { describe, expect, it } from 'vitest';

import { SHORTCUTS, commandFor, formatShortcut, isTypingTarget } from './shortcuts.js';
import type { KeyChord, Shortcut } from './shortcuts.js';

/** Turn a documented pattern back into the chord that should produce it. */
function chordFor(shortcut: string): KeyChord {
  const parts = shortcut.split('+');
  const key = parts[parts.length - 1]!;
  return {
    key: key === 'enter' ? 'Enter' : key,
    ctrlKey: parts.includes('mod'),
    shiftKey: parts.includes('shift'),
  };
}

describe('every documented shortcut resolves', () => {
  it.each(Object.entries(SHORTCUTS))('%s → %s', (shortcut, command) => {
    // The acceptance criterion, driven from the table itself: a shortcut added
    // to SHORTCUTS but not wired up fails here rather than in someone's hands.
    expect(commandFor(chordFor(shortcut))).toBe(command);
  });

  it('maps each shortcut to a distinct command', () => {
    const commands = Object.values(SHORTCUTS);
    expect(new Set(commands).size).toBe(commands.length);
  });
});

describe('modifiers', () => {
  it('accepts Cmd as well as Ctrl for mod', () => {
    // A Windows user on a Mac keyboard, and a remote session where the
    // modifier does not match the host, would otherwise lose every shortcut.
    expect(commandFor({ key: 's', metaKey: true })).toBe('session.save');
    expect(commandFor({ key: 's', ctrlKey: true })).toBe('session.save');
  });

  it('distinguishes mod+z from mod+shift+z', () => {
    expect(commandFor({ key: 'z', ctrlKey: true })).toBe('edit.undo');
    expect(commandFor({ key: 'z', ctrlKey: true, shiftKey: true })).toBe('edit.redo');
  });

  it('ignores Alt chords, which are reserved', () => {
    // Alt+Arrow reorders layers and Alt bypasses snapping while drawing
    // (§5.4). No shortcut in the table uses it.
    expect(commandFor({ key: 'ArrowUp', altKey: true })).toBeNull();
    expect(commandFor({ key: 'e', altKey: true })).toBeNull();
  });

  it('does not fire a bare-letter shortcut when mod is held', () => {
    // mod+e is not tool.edit. Browsers and the OS claim plenty of those, and
    // a stray Ctrl should never switch tools.
    expect(commandFor({ key: 'e', ctrlKey: true })).toBeNull();
  });

  it('is case-insensitive, so Shift-held letters still resolve', () => {
    expect(commandFor({ key: 'V' })).toBe('tool.select');
  });
});

describe('typing', () => {
  it('fires nothing while the user is typing', () => {
    // **The rule bare-letter shortcuts make load-bearing.** Renaming a layer
    // to "measured depth" would otherwise switch tools four times and open
    // identify, with whatever survived left in the field.
    for (const key of ['m', 'e', 'a', 's', 'u', 'r', 'd', 'v', 'i', 'f']) {
      expect(commandFor({ key }, { typing: true })).toBeNull();
    }
  });

  it('still fires Escape while typing, because that is what Escape means', () => {
    expect(commandFor({ key: 'Escape' }, { typing: true })).toBe('tool.cancel');
  });

  it('suppresses mod chords while typing too', () => {
    // mod+z in a text field is the browser's undo, not the app's.
    expect(commandFor({ key: 'z', ctrlKey: true }, { typing: true })).toBeNull();
  });
});

describe('isTypingTarget', () => {
  it.each(['input', 'textarea', 'select'])('recognises <%s>', (tag) => {
    expect(isTypingTarget(document.createElement(tag))).toBe(true);
  });

  it('recognises contenteditable', () => {
    // Easy to forget, and it is what a rich caption field will be.
    const element = document.createElement('div');
    element.contentEditable = 'true';
    // jsdom does not derive isContentEditable from the attribute.
    Object.defineProperty(element, 'isContentEditable', { value: true });

    expect(isTypingTarget(element)).toBe(true);
  });

  it.each(['combobox', 'textbox'])('recognises role="%s" on a div', (role) => {
    // A combobox built from divs takes typed input and announces itself
    // through its role rather than its tag.
    const element = document.createElement('div');
    element.setAttribute('role', role);

    expect(isTypingTarget(element)).toBe(true);
  });

  it('does not treat an ordinary button or the map as typing', () => {
    expect(isTypingTarget(document.createElement('button'))).toBe(false);
    expect(isTypingTarget(document.createElement('canvas'))).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });
});

describe('formatShortcut', () => {
  it('reads as a Mac user expects', () => {
    expect(formatShortcut('mod+s', 'mac')).toBe('⌘S');
    expect(formatShortcut('mod+shift+z', 'mac')).toBe('⌘⇧Z');
  });

  it('reads as a Windows user expects', () => {
    expect(formatShortcut('mod+s')).toBe('Ctrl+S');
    expect(formatShortcut('mod+enter')).toBe('Ctrl+Enter');
  });

  it('formats every documented shortcut without producing an empty label', () => {
    for (const shortcut of Object.keys(SHORTCUTS) as Shortcut[]) {
      expect(formatShortcut(shortcut).length).toBeGreaterThan(0);
      expect(formatShortcut(shortcut, 'mac').length).toBeGreaterThan(0);
    }
  });
});
