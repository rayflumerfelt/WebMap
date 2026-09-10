/**
 * The persistent edit toolbar. `09-editing.md` §10.1.
 *
 * **About ten controls. Hold this line.** Anything that does not earn a slot
 * lives in the menu and the palette, which is what they are for — and this
 * component is where that budget is either kept or quietly spent. The count is
 * asserted in a test so a future addition has to be an argument rather than a
 * commit.
 *
 * It renders **from the command registry** (§8), which is the whole point of
 * having one: the toggles here and the same items in the menu bar cannot
 * disagree about whether snapping is on, because they read one `checked`.
 *
 * The layout is left–centre–right–far right, and the far right is the **active
 * layer selector**, deliberately prominent: it is the most consequential piece
 * of state in the subsystem, and switching it ends the session (§4).
 */

import type { CSSProperties } from 'react';

import type { CommandDef } from './types.js';
import type { EditState } from './types.js';
import type { EditMode, SelectTool } from './modes.js';

export interface LayerOption {
  id: string;
  name: string;
  /** A layer the user may not edit is still selectable — the tools grey
   *  instead, which says more than an absent layer would. */
  canEdit: boolean;
}

export interface EditToolbarProps {
  state: EditState;
  mode: EditMode;
  selectTool: SelectTool;
  commands: CommandDef[];
  /** Unsaved edits, for the dirty badge. */
  dirtyCount: number;
  /** Set when the snap tolerance is pixel-clamped — §6.3's badge. */
  snapClamped?: 'floor' | 'ceiling' | null;
  layers: LayerOption[];
  onMode(mode: EditMode): void;
  onSelectTool(tool: SelectTool): void;
  onRun(command: CommandDef): void;
  onActiveLayer(layerId: string): void;
  /** False while an operation is running — §4 blocks a mode switch, and the
   *  buttons grey rather than doing nothing when clicked. */
  canSwitchMode: boolean;
  className?: string | undefined;
}

const bar: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  height: 40,
  padding: '0 8px',
  borderBottom: '1px solid #d7dae0',
  background: '#f6f7f9',
  fontSize: 12,
};

const group: CSSProperties = { display: 'flex', alignItems: 'center', gap: 2 };

const button: CSSProperties = {
  height: 'var(--control-h, 28px)',
  minWidth: 28,
  padding: '0 8px',
  border: '1px solid #c9ccd1',
  borderRadius: 3,
  background: '#fff',
  fontSize: 12,
  cursor: 'pointer',
};

const activeButton: CSSProperties = {
  ...button,
  borderColor: '#1c5cc4',
  background: '#eaf1fd',
  fontWeight: 600,
};

const separator: CSSProperties = {
  width: 1,
  height: 20,
  background: '#d7dae0',
};

/** The four modes the segmented control offers. Draw's geometry type is a
 *  sub-choice, not four more slots — that is how the budget is kept. */
const MODES: Array<{ mode: EditMode; label: string; shortcut: string }> = [
  { mode: 'select', label: 'Select', shortcut: '1' },
  { mode: 'vertex', label: 'Vertex', shortcut: 'V' },
  { mode: 'draw-polygon', label: 'Draw', shortcut: 'D' },
  { mode: 'move', label: 'Move', shortcut: 'M' },
];

const SELECT_TOOLS: Array<{ tool: SelectTool; label: string }> = [
  { tool: 'click', label: 'Click' },
  { tool: 'rectangle', label: 'Rect' },
  { tool: 'lasso', label: 'Lasso' },
];

const DRAW_MODES: Array<{ mode: EditMode; label: string }> = [
  { mode: 'draw-polygon', label: 'Polygon' },
  { mode: 'draw-line', label: 'Line' },
  { mode: 'draw-point', label: 'Point' },
  { mode: 'draw-rectangle', label: 'Rectangle' },
  { mode: 'draw-freehand', label: 'Freehand' },
];

/** The three constraint toggles, by command id. */
const CONSTRAINTS = ['snap.enable', 'snap.topological', 'snap.angle'];

export function EditToolbar(props: EditToolbarProps) {
  const {
    state,
    mode,
    selectTool,
    commands,
    dirtyCount,
    snapClamped,
    layers,
    onMode,
    onSelectTool,
    onRun,
    onActiveLayer,
    canSwitchMode,
    className,
  } = props;

  const byId = new Map(commands.map((command) => [command.id, command]));
  const drawing = mode.startsWith('draw-');

  return (
    <div className={className} style={bar} role="toolbar" aria-label="Editing tools">
      {/* Left — mode. */}
      <div style={group} role="radiogroup" aria-label="Edit mode">
        {MODES.map((entry) => {
          const active =
            entry.mode === 'draw-polygon' ? drawing : mode === entry.mode;
          return (
            <button
              key={entry.mode}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={!canSwitchMode || !state.canEdit}
              title={`${entry.label} (${entry.shortcut})`}
              onClick={() => onMode(entry.mode)}
              style={active ? activeButton : button}
            >
              {entry.label}
            </button>
          );
        })}
      </div>

      {/* The sub-choice for whichever mode is active. Only one is ever shown,
          so it costs one slot rather than eight. */}
      {mode === 'select' ? (
        <div style={group} role="radiogroup" aria-label="Selection tool">
          {SELECT_TOOLS.map((entry) => (
            <button
              key={entry.tool}
              type="button"
              role="radio"
              aria-checked={selectTool === entry.tool}
              onClick={() => onSelectTool(entry.tool)}
              style={selectTool === entry.tool ? activeButton : button}
            >
              {entry.label}
            </button>
          ))}
        </div>
      ) : null}

      {drawing ? (
        <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ color: '#4a4f57' }}>Shape</span>
          <select
            aria-label="Draw shape"
            value={mode}
            onChange={(event) => onMode(event.target.value as EditMode)}
            style={{ ...button, cursor: 'pointer' }}
          >
            {DRAW_MODES.map((entry) => (
              <option key={entry.mode} value={entry.mode}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      <span style={separator} aria-hidden="true" />

      {/* Centre — constraints. Each reads `checked` from the registry, so the
          toolbar and the Snap menu cannot disagree about whether snapping is
          on. */}
      <div style={group}>
        {CONSTRAINTS.map((id) => {
          const command = byId.get(id);
          if (!command) return null;
          const on = command.checked?.(state) ?? false;
          const badged = id === 'snap.enable' && on && snapClamped != null;
          return (
            <button
              key={id}
              type="button"
              aria-pressed={on}
              disabled={!command.enabled(state)}
              title={
                badged
                  ? `${command.label} — the ${snapClamped === 'floor' ? 'minimum' : 'maximum'} ` +
                    `pixel tolerance is in force at this zoom, not the distance you set.`
                  : `${command.label}${command.shortcut ? ` (${command.shortcut})` : ''}`
              }
              onClick={() => onRun(command)}
              style={on ? activeButton : button}
            >
              {command.label.replace('Enable ', '')}
              {badged ? (
                <span
                  aria-label="pixel tolerance in force"
                  style={{ marginLeft: 4, color: '#8a5a00' }}
                >
                  ●
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      <span style={separator} aria-hidden="true" />

      {/* Right — history and session. */}
      <div style={group}>
        {['edit.undo', 'edit.redo', 'session.save', 'session.discard'].map((id) => {
          const command = byId.get(id);
          if (!command) return null;
          return (
            <button
              key={id}
              type="button"
              disabled={!command.enabled(state)}
              title={`${command.label}${command.shortcut ? ` (${command.shortcut})` : ''}`}
              onClick={() => onRun(command)}
              style={button}
            >
              {command.label}
            </button>
          );
        })}
        {dirtyCount > 0 ? (
          <span
            aria-label={`${dirtyCount} unsaved edits`}
            style={{
              marginLeft: 4,
              padding: '1px 6px',
              borderRadius: 9,
              background: '#c46a00',
              color: '#fff',
              fontSize: 11,
            }}
          >
            {dirtyCount}
          </span>
        ) : null}
      </div>

      {/* Far right — the active layer. Prominent because it is the most
          consequential piece of state here: switching it ends the session. */}
      <div style={{ ...group, marginLeft: 'auto', gap: 6 }}>
        <label htmlFor="edit-active-layer" style={{ color: '#4a4f57' }}>
          Editing
        </label>
        <select
          id="edit-active-layer"
          value={state.activeLayerId ?? ''}
          onChange={(event) => onActiveLayer(event.target.value)}
          style={{ ...button, minWidth: 160, fontWeight: 600, cursor: 'pointer' }}
        >
          <option value="">No layer</option>
          {layers.map((layer) => (
            <option key={layer.id} value={layer.id}>
              {layer.name}
              {layer.canEdit ? '' : ' (read-only)'}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

/**
 * The controls this toolbar puts on screen at once, for the §10.1 budget test.
 *
 * Counted as *slots*, not as buttons: the four mode buttons are one segmented
 * control and the sub-choice that follows the mode is one more, because that is
 * what a user scanning the bar sees. Counting buttons would let the budget be
 * met by splitting one control into two.
 */
export const TOOLBAR_SLOTS = [
  'mode',
  'mode sub-choice',
  'snap',
  'topology',
  'angle',
  'undo',
  'redo',
  'save',
  'discard',
  'active layer',
] as const;
