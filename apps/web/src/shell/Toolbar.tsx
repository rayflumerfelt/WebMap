/**
 * Toolbar. `07-frontend.md` §5.2 — "project · tools · view · render".
 *
 * Deliberately small. Every tool here also has a single-letter shortcut, and
 * §5.4 puts everything else behind the command palette rather than growing
 * this bar: a toolbar that accumulates buttons becomes a place to hunt through
 * rather than a place to reach for.
 *
 * Each button shows its shortcut in the tooltip, which is how anyone learns
 * the keyboard path without reading documentation.
 */

import { formatShortcut } from '../keyboard/shortcuts.js';
import type { Command, Shortcut } from '../keyboard/shortcuts.js';

export type ToolId = 'tool.select' | 'tool.identify' | 'tool.measure' | 'tool.edit';

interface ToolDef {
  id: ToolId;
  label: string;
  shortcut: Shortcut;
  glyph: string;
}

/** Order follows frequency of use, not alphabet. */
const TOOLS: ToolDef[] = [
  { id: 'tool.select', label: 'Select', shortcut: 'v', glyph: '⬉' },
  { id: 'tool.identify', label: 'Identify', shortcut: 'i', glyph: 'ⓘ' },
  { id: 'tool.measure', label: 'Measure', shortcut: 'm', glyph: '⟷' },
  { id: 'tool.edit', label: 'Edit', shortcut: 'e', glyph: '✎' },
];

export interface ToolbarProps {
  projectName: string;
  sessionName: string | null;
  activeTool: ToolId;
  onCommand(command: Command): void;
  /** Editing is a lazy-loaded bundle and a permission; the button is disabled
   *  rather than hidden, so its shortcut has something to explain itself. */
  canEdit?: boolean;
  platform?: 'mac' | 'other';
}

export function Toolbar({
  projectName,
  sessionName,
  activeTool,
  onCommand,
  canEdit = false,
  platform = 'other',
}: ToolbarProps) {
  return (
    <header
      // A toolbar role, so a screen reader user can jump to it and so arrow
      // navigation within it is expected behaviour rather than a surprise.
      role="toolbar"
      aria-label="Main toolbar"
      aria-orientation="horizontal"
      style={{
        height: 'var(--toolbar-h, 44px)',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '0 8px',
        borderBottom: '1px solid var(--chrome-border)',
        background: 'var(--chrome-bg)',
        fontSize: 12,
      }}
    >
      <span style={{ fontWeight: 600 }}>{projectName}</span>
      <span style={{ opacity: 0.7 }}>{sessionName ?? 'Untitled session'}</span>

      <Divider />

      <div style={{ display: 'flex', gap: 2 }}>
        {TOOLS.map((tool) => (
          <ToolButton
            key={tool.id}
            tool={tool}
            active={activeTool === tool.id}
            disabled={tool.id === 'tool.edit' && !canEdit}
            platform={platform}
            onSelect={() => onCommand(tool.id)}
          />
        ))}
      </div>

      <Divider />

      <BarButton
        label="Zoom to layer"
        shortcut="f"
        platform={platform}
        onClick={() => onCommand('view.zoomToLayer')}
      />

      <span style={{ flex: 1 }} />

      <BarButton
        label="Render"
        shortcut="mod+enter"
        platform={platform}
        onClick={() => onCommand('render.current')}
      />
      <BarButton
        label="Save"
        shortcut="mod+s"
        platform={platform}
        onClick={() => onCommand('session.save')}
      />
    </header>
  );
}

function ToolButton({
  tool,
  active,
  disabled,
  platform,
  onSelect,
}: {
  tool: ToolDef;
  active: boolean;
  disabled: boolean;
  platform: 'mac' | 'other';
  onSelect(): void;
}) {
  return (
    <button
      type="button"
      // `radio`, not a pressed toggle: exactly one tool is active, and a
      // screen reader should describe it as a choice among four rather than
      // as four independent switches.
      role="radio"
      aria-checked={active}
      aria-label={tool.label}
      title={`${tool.label} (${formatShortcut(tool.shortcut, platform)})`}
      disabled={disabled}
      onClick={onSelect}
      style={{
        height: 'var(--control-h, 28px)',
        minWidth: 28,
        padding: '0 6px',
        border: '1px solid transparent',
        borderRadius: 2,
        background: active ? 'var(--accent-soft)' : 'transparent',
        // Never colour alone (§10): the active tool is also outlined.
        borderColor: active ? 'var(--accent)' : 'transparent',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.4 : 1,
        font: 'inherit',
        color: 'inherit',
      }}
    >
      <span aria-hidden="true">{tool.glyph}</span>
    </button>
  );
}

function BarButton({
  label,
  shortcut,
  platform,
  onClick,
}: {
  label: string;
  shortcut: Shortcut;
  platform: 'mac' | 'other';
  onClick(): void;
}) {
  return (
    <button
      type="button"
      title={`${label} (${formatShortcut(shortcut, platform)})`}
      onClick={onClick}
      style={{
        height: 'var(--control-h, 28px)',
        padding: '0 8px',
        border: '1px solid var(--chrome-border)',
        borderRadius: 2,
        background: 'transparent',
        cursor: 'pointer',
        font: 'inherit',
        color: 'inherit',
      }}
    >
      {label}
    </button>
  );
}

function Divider() {
  return (
    <span
      aria-hidden="true"
      style={{ width: 1, height: 20, background: 'var(--chrome-border)' }}
    />
  );
}
