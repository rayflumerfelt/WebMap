/**
 * Panel widths and collapsed state. `07-frontend.md` §5.2.
 *
 * "Widths persist per user in `user_preferences`. Each panel collapses to a
 * 40 px icon rail, and collapsed state persists too."
 *
 * Two tiers, deliberately:
 *
 * - **`localStorage` is the fast path.** Panel geometry has to be right on the
 *   first paint, and a round trip to the API would mean the panels visibly
 *   jump into place a moment after the page loads.
 * - **`user_preferences` is the durable one**, so the layout follows a
 *   geologist to a second workstation. Written behind the local copy, and a
 *   failure to write it is not worth interrupting anyone over.
 *
 * A stored value is always validated rather than trusted: it is user-writable
 * (devtools, a synced profile, an older version of this code), and a width of
 * `NaN` collapses the map to nothing with no way back except clearing site
 * data.
 */

export type PanelKey = 'layers' | 'symbology' | 'attributes';

export interface PanelPref {
  /** Width for a side panel; height for the attribute table. */
  width: number;
  collapsed: boolean;
}

export type PanelPrefs = Record<PanelKey, PanelPref>;

/** Matches `layout.css` at the 1440 px baseline. */
export const DEFAULT_PREFS: PanelPrefs = {
  layers: { width: 280, collapsed: false },
  symbology: { width: 340, collapsed: false },
  attributes: { width: 200, collapsed: true },
};

const LIMITS: Record<PanelKey, { min: number; max: number }> = {
  layers: { min: 200, max: 560 },
  symbology: { min: 240, max: 620 },
  attributes: { min: 120, max: 600 },
};

export const STORAGE_KEY = 'webmap.panels.v1';

/**
 * Coerce anything into usable preferences.
 *
 * Never throws and never returns a partial object: the caller renders with
 * whatever comes back, and a missing key would be a crash on first paint.
 */
export function parsePrefs(raw: unknown): PanelPrefs {
  const source = typeof raw === 'string' ? safeParse(raw) : raw;
  if (!isRecord(source)) return DEFAULT_PREFS;

  const result = {} as PanelPrefs;
  for (const key of Object.keys(DEFAULT_PREFS) as PanelKey[]) {
    const stored = source[key];
    const fallback = DEFAULT_PREFS[key];
    if (!isRecord(stored)) {
      result[key] = fallback;
      continue;
    }
    result[key] = {
      width: clampWidth(key, stored.width, fallback.width),
      collapsed: typeof stored.collapsed === 'boolean' ? stored.collapsed : fallback.collapsed,
    };
  }
  return result;
}

function clampWidth(key: PanelKey, value: unknown, fallback: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback;
  const { min, max } = LIMITS[key];
  return Math.max(min, Math.min(max, value));
}

function safeParse(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    // Corrupt storage is not an error worth surfacing — the defaults are a
    // perfectly good layout, and the alternative is a blank page.
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export interface PrefsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function loadPrefs(storage: PrefsStorage | undefined = safeLocalStorage()): PanelPrefs {
  if (!storage) return DEFAULT_PREFS;
  try {
    return parsePrefs(storage.getItem(STORAGE_KEY));
  } catch {
    // Storage can throw outright, not merely return null: Safari in private
    // mode, and any browser with site data blocked.
    return DEFAULT_PREFS;
  }
}

export function savePrefs(
  prefs: PanelPrefs,
  storage: PrefsStorage | undefined = safeLocalStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // A quota or a blocked store. The layout still works for this session;
    // losing it on reload is not worth an error dialog.
  }
}

function safeLocalStorage(): PrefsStorage | undefined {
  try {
    return globalThis.localStorage ?? undefined;
  } catch {
    return undefined;
  }
}
