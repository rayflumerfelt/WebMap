/**
 * Bind the shortcut table to the document. `07-frontend.md` §5.4.
 *
 * On the document rather than on the shell element, because the map canvas,
 * the panels and the toolbar all take focus and a shortcut has to work from
 * any of them. `isTypingTarget` is what makes that safe.
 */

import { useEffect, useRef } from 'react';

import { commandFor, isTypingTarget } from './shortcuts.js';
import type { Command } from './shortcuts.js';

export function useShortcuts(onCommand: (command: Command) => void): void {
  // Held in a ref so the listener is registered once. Re-registering on every
  // render of the app is the same leak the map component avoids.
  const handler = useRef(onCommand);
  handler.current = onCommand;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const command = commandFor(event, { typing: isTypingTarget(event.target) });
      if (!command) return;
      // Only prevent default for a shortcut that matched. Swallowing keys
      // indiscriminately breaks the browser's own Ctrl+F and Ctrl+S for
      // anyone who expected them.
      event.preventDefault();
      handler.current(command);
    };

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);
}
