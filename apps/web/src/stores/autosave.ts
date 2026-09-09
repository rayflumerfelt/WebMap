/**
 * Session autosave. `07-frontend.md` §3.
 *
 * Written as a plain function over a subscribe-like interface rather than as a
 * module-level side effect, so the timing rules below can be tested with a
 * fake clock instead of by waiting two seconds and hoping.
 *
 * Three rules, each earned:
 *
 * - **Trailing debounce.** The value worth saving is where a drag ended, not
 *   where it started. A leading-edge save writes the first frame of a pan.
 * - **One save in flight at a time.** Overlapping PATCHes to the same session
 *   race, and the loser's `expected_updated_at` is stale — so a burst of edits
 *   would produce a 409 for changes the user made themselves.
 * - **A conflict stops the loop.** Retrying a 409 with the same stale
 *   timestamp fails identically, forever, several times a minute.
 */

export interface AutosaveDeps<S> {
  /** Zustand's subscribe, or anything with the same shape. */
  subscribe(listener: (state: S) => void): () => void;
  getState(): S;
  /** Persist. Resolves with the new updated_at, or rejects. */
  save(state: S): Promise<void>;
  /** Whether this state has unsaved changes worth persisting. */
  isDirty(state: S): boolean;
  onSaved(): void;
  onConflict(): void;
  onError(error: unknown): void;
  /** Injected for tests. Defaults to the real timer. */
  setTimeout?: (handler: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
}

/** `07-frontend.md` §3: debounce at 2 s. */
export const AUTOSAVE_DELAY_MS = 2_000;

export interface AutosaveHandle {
  /** Save immediately, ignoring the debounce. For mod+s and for unload. */
  flush(): Promise<void>;
  stop(): void;
}

export function startAutosave<S>(deps: AutosaveDeps<S>): AutosaveHandle {
  const schedule = deps.setTimeout ?? ((fn, ms) => globalThis.setTimeout(fn, ms));
  const cancel = deps.clearTimeout ?? ((handle) => globalThis.clearTimeout(handle as number));

  let timer: unknown = null;
  let inFlight: Promise<void> | null = null;
  let pendingWhileInFlight = false;
  let stopped = false;

  async function run(): Promise<void> {
    if (stopped) return;
    const state = deps.getState();
    if (!deps.isDirty(state)) return;

    if (inFlight) {
      // Coalesce rather than queue: whatever is happening now will be
      // followed by exactly one more save carrying the latest state, however
      // many edits arrived meanwhile.
      pendingWhileInFlight = true;
      return;
    }

    inFlight = (async () => {
      try {
        await deps.save(state);
        deps.onSaved();
      } catch (error) {
        if (isConflict(error)) {
          // Stop. Retrying with the same stale expected_updated_at fails
          // identically, forever, several times a minute — and the user needs
          // to be told, not retried at.
          stopped = true;
          deps.onConflict();
          return;
        }
        // A transient failure leaves the state dirty, so the next edit — or a
        // flush — tries again. Not stopping, because a dropped Wi-Fi packet
        // should not end autosave for the session.
        deps.onError(error);
      }
    })();

    try {
      await inFlight;
    } finally {
      inFlight = null;
    }

    if (pendingWhileInFlight && !stopped) {
      pendingWhileInFlight = false;
      await run();
    }
  }

  const unsubscribe = deps.subscribe((state) => {
    if (stopped || !deps.isDirty(state)) return;
    if (timer !== null) cancel(timer);
    timer = schedule(() => {
      timer = null;
      void run();
    }, AUTOSAVE_DELAY_MS);
  });

  return {
    flush: async () => {
      if (timer !== null) {
        cancel(timer);
        timer = null;
      }
      await run();
    },
    stop: () => {
      stopped = true;
      if (timer !== null) cancel(timer);
      unsubscribe();
    },
  };
}

/** A 409 from the sessions API: someone else saved first. */
function isConflict(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'status' in error &&
    (error as { status: unknown }).status === 409
  );
}
