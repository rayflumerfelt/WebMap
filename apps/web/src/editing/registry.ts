/**
 * The command registry. `09-editing.md` §8, §9.
 *
 * **Every command is defined exactly once**, and every surface renders from
 * here: the menu bar, the command palette, the toolbar and the context menu.
 * `09` §8 calls this the highest-leverage structural decision in the editing UI
 * and says it must exist before any surface, because retrofitting it means
 * finding four copies of every command's enabled-state logic.
 *
 * Three rules the surfaces depend on, stated here so they are not re-decided:
 *
 * - **A disabled command still appears in the menu**, greyed, so it stays
 *   discoverable and its shortcut is visible. It is **excluded from the
 *   palette**, where a disabled row is noise (`09` §8).
 * - **Shortcuts derive from the registry.** Nothing registers a hotkey
 *   independently — `bindings()` is what the hotkey layer reads, and a second
 *   registrar is how two commands come to share a key.
 * - **The labels in `09` §9.1 are not renameable.** Combine wraps parts and
 *   leaves geometry alone; Dissolve unions and changes it. Calling either
 *   "Merge" produces silent data loss in whichever direction the user did not
 *   intend, which is why both carry `merge` as a *keyword* and neither as a
 *   label: a search for it finds both, with the descriptions that distinguish
 *   them.
 *
 * `run` is a dispatch to the handler the editing session installs. The registry
 * knows *what* the commands are; it deliberately does not know how any of them
 * work.
 */

import {
  areal,
  editable,
  exactlyOneFeature,
  featureScope,
  hasEditableVertices,
  hasSelection,
  hasVertices,
  ready,
  severalFeatures,
} from './predicates.js';
import type { CommandDef, CommandGroup, ContextScope, EditState } from './types.js';

/**
 * What a command does when it runs.
 *
 * The registry dispatches by id into a handler map the session supplies, so
 * this file stays a description of the command surface rather than an
 * implementation of the editor. It also means a surface can be rendered and
 * tested with no editor at all — which is how the menu is checked against `09`
 * §9 without a map on screen.
 */
export type CommandHandler = (state: EditState) => void | Promise<void>;
export type Handlers = Partial<Record<string, CommandHandler>>;

/** Menu order, as `09` §9 lists it. Not alphabetical: the order is the spec. */
export const GROUP_ORDER: CommandGroup[] = [
  'file',
  'layers',
  'select',
  'edit',
  'vertices',
  'transform',
  'snap',
  'view',
];

export const GROUP_LABELS: Record<CommandGroup, string> = {
  file: 'File',
  layers: 'Layers',
  select: 'Select',
  edit: 'Edit',
  vertices: 'Vertices',
  transform: 'Transform',
  snap: 'Snap',
  view: 'View',
};

interface Spec {
  id: string;
  label: string;
  group: CommandGroup;
  description?: string;
  shortcut?: string;
  keywords?: string[];
  enabled?: (state: EditState) => boolean;
  visible?: (state: EditState) => boolean;
  checked?: (state: EditState) => boolean;
  contextMenu?: ContextScope[];
}

/** Every command, once. */
const SPECS: Spec[] = [
  // --- File -------------------------------------------------------------------
  {
    id: 'session.save',
    label: 'Save',
    group: 'file',
    shortcut: 'mod+s',
    description: 'Commit every applied edit in one transaction.',
    keywords: ['commit', 'persist'],
    // **Disabled while an operation bar is showing** — `09` §5.2 says to pick
    // that or auto-apply and hold it, because alternating is what makes a tool
    // feel unpredictable. Disabled is the one that cannot surprise anybody.
    enabled: (state) => state.dirty && !state.operationActive,
  },
  {
    id: 'session.discard',
    label: 'Discard Changes',
    group: 'file',
    description: 'Throw away every unsaved edit. Asks first.',
    keywords: ['revert', 'abandon'],
    enabled: (state) => state.dirty,
  },
  {
    id: 'layer.export',
    label: 'Export Active Layer…',
    group: 'file',
    enabled: (state) => state.activeLayerId !== null,
  },

  // --- Layers -----------------------------------------------------------------
  { id: 'layer.add.blank', label: 'Add Blank Layer', group: 'layers', enabled: () => true },
  { id: 'layer.add.file', label: 'Add Layer From File…', group: 'layers', enabled: () => true },
  {
    id: 'layer.add.buffer',
    label: 'Add Layer From Buffer…',
    group: 'layers',
    enabled: (state) => state.activeLayerId !== null,
  },
  { id: 'layer.add.grid', label: 'Add Grid Layer…', group: 'layers', enabled: () => true },
  {
    id: 'layer.duplicate',
    label: 'Duplicate Layer',
    group: 'layers',
    description: 'A second layer over the same dataset — styling, not data.',
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'layer.delete',
    label: 'Delete Layer…',
    group: 'layers',
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'layer.style',
    label: 'Style…',
    group: 'layers',
    description: 'How the layer is drawn: colours, lines, labels.',
    keywords: ['symbology', 'colour', 'color', 'format'],
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'layer.properties',
    label: 'Properties…',
    group: 'layers',
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'layer.attributes',
    label: 'Attribute Table',
    group: 'layers',
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'layer.validate',
    label: 'Validate Topology…',
    group: 'layers',
    description: 'Whole-layer check for self-intersections, slivers and gaps.',
    enabled: (state) => state.activeLayerId !== null && !state.operationActive,
  },
  {
    id: 'layer.align',
    label: 'Align to Layer…',
    group: 'layers',
    enabled: (state) => ready(state) && areal(state),
  },

  // --- Select -----------------------------------------------------------------
  {
    id: 'select.mode.click',
    label: 'Click',
    group: 'select',
    shortcut: '1',
    enabled: (state) => state.activeLayerId !== null,
    checked: (state) => state.selectMode === 'click',
  },
  {
    id: 'select.mode.rectangle',
    label: 'Rectangle',
    group: 'select',
    shortcut: '2',
    enabled: (state) => state.activeLayerId !== null,
    checked: (state) => state.selectMode === 'rectangle',
  },
  {
    id: 'select.mode.lasso',
    label: 'Lasso',
    group: 'select',
    shortcut: '3',
    enabled: (state) => state.activeLayerId !== null,
    checked: (state) => state.selectMode === 'lasso',
  },
  {
    id: 'select.all',
    label: 'Select All',
    group: 'select',
    shortcut: 'mod+a',
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'select.none',
    label: 'Select None',
    group: 'select',
    shortcut: 'mod+shift+a',
    enabled: hasSelection,
  },
  {
    id: 'select.invert',
    label: 'Invert Selection',
    group: 'select',
    shortcut: 'mod+i',
    enabled: (state) => state.activeLayerId !== null,
  },
  {
    id: 'select.zoomTo',
    label: 'Zoom to Selection',
    group: 'select',
    shortcut: 'z',
    enabled: hasSelection,
    contextMenu: ['selection'],
  },

  // --- Edit -------------------------------------------------------------------
  { id: 'edit.undo', label: 'Undo', group: 'edit', shortcut: 'mod+z', enabled: (s) => s.canUndo },
  {
    id: 'edit.redo',
    label: 'Redo',
    group: 'edit',
    shortcut: 'mod+shift+z',
    enabled: (state) => state.canRedo,
  },
  {
    id: 'edit.cut',
    label: 'Cut',
    group: 'edit',
    shortcut: 'mod+x',
    enabled: (state) => ready(state) && hasSelection(state),
    contextMenu: ['feature', 'selection'],
  },
  {
    id: 'edit.copy',
    label: 'Copy',
    group: 'edit',
    shortcut: 'mod+c',
    // Copy needs no edit permission: it reads. A viewer copying a boundary
    // into their own layer is the ordinary way work gets reused.
    enabled: hasSelection,
    contextMenu: ['feature', 'selection'],
  },
  {
    id: 'edit.paste',
    label: 'Paste',
    group: 'edit',
    shortcut: 'mod+v',
    enabled: (state) => ready(state) && state.clipboardCount > 0,
    contextMenu: ['empty'],
  },
  {
    id: 'edit.delete',
    label: 'Delete',
    group: 'edit',
    shortcut: 'Delete',
    // `featureScope`, not `hasSelection`: with vertices selected this shares
    // its key with Delete Selected Vertices, and Delete has to mean the
    // vertices there. `conflicts` is what caught the ambiguity.
    enabled: (state) => ready(state) && featureScope(state),
    contextMenu: ['feature', 'selection'],
  },
  {
    id: 'edit.split',
    label: 'Split…',
    group: 'edit',
    description: 'Draw a line across a feature to divide it.',
    // Lines and polygons only: a point has nothing to divide, and splitting a
    // raster is a clip.
    enabled: (state) =>
      ready(state) && exactlyOneFeature(state) && hasEditableVertices(state),
    contextMenu: ['feature'],
  },
  {
    id: 'edit.reshape',
    label: 'Reshape…',
    group: 'edit',
    description: 'Redraw part of a boundary between two crossings.',
    enabled: (state) => ready(state) && exactlyOneFeature(state) && areal(state),
    contextMenu: ['feature'],
  },
  {
    id: 'edit.combine',
    label: 'Combine',
    group: 'edit',
    // `09` §9.1. The description is what stops this being confused with
    // Dissolve, and the keyword is what makes a search for "merge" find both.
    description: 'Wrap several features into one multi-part feature. Geometry unchanged.',
    keywords: ['merge', 'multipart', 'group'],
    enabled: (state) => ready(state) && severalFeatures(state),
    contextMenu: ['selection'],
  },
  {
    id: 'edit.explode',
    label: 'Explode',
    group: 'edit',
    description: 'Split a multi-part feature into one feature per part.',
    keywords: ['multipart', 'separate'],
    enabled: (state) => ready(state) && hasSelection(state),
    contextMenu: ['feature', 'selection'],
  },
  {
    id: 'edit.dissolve',
    label: 'Dissolve',
    group: 'edit',
    description: 'True union. Shared interior boundaries are removed. Geometry changes.',
    keywords: ['merge', 'union'],
    enabled: (state) => ready(state) && severalFeatures(state) && areal(state),
    contextMenu: ['selection'],
  },
  {
    id: 'edit.clip',
    label: 'Clip…',
    group: 'edit',
    enabled: (state) => ready(state) && hasSelection(state) && areal(state),
  },
  {
    id: 'edit.erase',
    label: 'Erase…',
    group: 'edit',
    enabled: (state) => ready(state) && hasSelection(state) && areal(state),
  },
  {
    id: 'edit.intersect',
    label: 'Intersect…',
    group: 'edit',
    enabled: (state) => ready(state) && severalFeatures(state) && areal(state),
  },
  {
    id: 'edit.attributes',
    label: 'Attributes…',
    group: 'edit',
    description: 'Edit the selected features’ attribute values, including in bulk.',
    enabled: (state) => ready(state) && hasSelection(state),
    contextMenu: ['feature', 'selection'],
  },

  // --- Vertices ---------------------------------------------------------------
  {
    id: 'vertex.mode',
    label: 'Edit Vertices',
    group: 'vertices',
    shortcut: 'v',
    enabled: (state) => ready(state) && hasEditableVertices(state) && hasSelection(state),
    checked: (state) => state.selection === 'vertex' || state.selection === 'vertices',
    contextMenu: ['feature'],
  },
  {
    id: 'vertex.add',
    label: 'Add Vertex',
    group: 'vertices',
    shortcut: 'shift+v',
    enabled: (state) => ready(state) && hasEditableVertices(state),
    contextMenu: ['edge'],
  },
  {
    id: 'vertex.delete',
    label: 'Delete Selected Vertices',
    group: 'vertices',
    shortcut: 'Delete',
    enabled: (state) => ready(state) && hasVertices(state),
    contextMenu: ['vertex'],
  },
  {
    id: 'vertex.selectAll',
    label: 'Select All Vertices',
    group: 'vertices',
    enabled: (state) => ready(state) && hasEditableVertices(state) && hasSelection(state),
  },
  {
    id: 'vertex.enterCoordinates',
    label: 'Enter Coordinates…',
    group: 'vertices',
    description: 'Type an exact position for the selected vertex.',
    keywords: ['numeric', 'exact', 'type'],
    enabled: (state) => ready(state) && state.selectedVertexCount === 1,
    contextMenu: ['vertex'],
  },

  // --- Transform --------------------------------------------------------------
  {
    id: 'transform.move',
    label: 'Move',
    group: 'transform',
    shortcut: 'm',
    // `09` §9.1: under Transform, beside Rotate and Scale, because that is
    // what it is.
    enabled: (state) => ready(state) && hasSelection(state),
    contextMenu: ['feature', 'selection'],
  },
  {
    id: 'transform.rotate',
    label: 'Rotate…',
    group: 'transform',
    shortcut: 'r',
    enabled: (state) => ready(state) && hasSelection(state),
  },
  {
    id: 'transform.scale',
    label: 'Scale…',
    group: 'transform',
    enabled: (state) => ready(state) && hasSelection(state),
  },
  {
    id: 'transform.buffer',
    label: 'Offset / Buffer…',
    group: 'transform',
    keywords: ['offset', 'expand', 'shrink'],
    enabled: (state) => ready(state) && hasSelection(state),
  },
  {
    id: 'transform.smooth',
    label: 'Smooth…',
    group: 'transform',
    enabled: (state) => ready(state) && hasSelection(state) && hasEditableVertices(state),
  },
  {
    id: 'transform.simplify',
    label: 'Simplify…',
    group: 'transform',
    enabled: (state) => ready(state) && hasSelection(state) && hasEditableVertices(state),
  },
  {
    id: 'transform.reverse',
    label: 'Reverse Direction',
    group: 'transform',
    description: 'Flip the vertex order of a line — which end is the start.',
    enabled: (state) => ready(state) && hasSelection(state) && state.activeLayerGeometry === 'line',
  },
  {
    id: 'transform.trimExtend',
    label: 'Trim / Extend to Feature',
    group: 'transform',
    enabled: (state) =>
      ready(state) && exactlyOneFeature(state) && state.activeLayerGeometry === 'line',
  },

  // --- Snap -------------------------------------------------------------------
  {
    id: 'snap.enable',
    label: 'Enable Snapping',
    group: 'snap',
    shortcut: 's',
    description: 'Without it, every shared boundary is a source of slivers.',
    enabled: editable,
    checked: (state) => state.snapEnabled,
  },
  {
    id: 'snap.topological',
    label: 'Topological Editing',
    group: 'snap',
    shortcut: 't',
    description: 'Move a shared boundary once and both features follow.',
    enabled: editable,
    checked: (state) => state.topologicalEditing,
  },
  {
    id: 'snap.angle',
    label: 'Angle Constraint',
    group: 'snap',
    shortcut: 'a',
    enabled: editable,
    checked: (state) => state.angleConstraint,
  },
  { id: 'snap.settings', label: 'Snap Settings…', group: 'snap', enabled: editable },

  // --- View -------------------------------------------------------------------
  {
    id: 'view.showVertices',
    label: 'Show Vertices',
    group: 'view',
    enabled: () => true,
    checked: (state) => state.showVertices,
  },
  {
    id: 'view.showMeasurements',
    label: 'Show Measurements',
    group: 'view',
    enabled: () => true,
    checked: (state) => state.showMeasurements,
  },
  {
    id: 'view.showValidationErrors',
    label: 'Show Validation Errors',
    group: 'view',
    enabled: () => true,
    checked: (state) => state.showValidationErrors,
  },
  { id: 'view.basemap', label: 'Basemap…', group: 'view', enabled: () => true },
];

/**
 * Build the registry, binding each command to its handler.
 *
 * A command with no handler is **still registered and still enabled**: it is
 * defined, its shortcut is reserved, and it appears in every surface. Running
 * it is a no-op that logs, which is the honest state of a command whose
 * implementation has not landed — hiding it instead would make the menu a
 * moving target as features arrive, and disabling it would say the state is
 * wrong when the state is fine.
 */
export function buildRegistry(handlers: Handlers = {}): CommandDef[] {
  return SPECS.map((spec) => {
    const { enabled, ...rest } = spec;
    return {
      ...rest,
      enabled: enabled ?? (() => true),
      run: (state: EditState) => handlers[spec.id]?.(state),
    } satisfies CommandDef;
  });
}

/** The registry with no handlers — for rendering and for tests. */
export const COMMANDS: CommandDef[] = buildRegistry();

export function byId(commands: CommandDef[] = COMMANDS): Map<string, CommandDef> {
  return new Map(commands.map((command) => [command.id, command]));
}

/**
 * Shortcut → command id, for the hotkey layer.
 *
 * **The single registrar.** `09` §8: shortcuts derive from the registry and
 * nothing registers a hotkey independently.
 *
 * A key may repeat *across groups* — `Delete` is Delete Feature in the Edit
 * menu and Delete Vertices in the Vertices menu — because only one of the two
 * is ever enabled: vertex deletion needs selected vertices and feature deletion
 * does not apply while they are. So the map is from key to *candidates*, and
 * the hotkey layer picks the enabled one. A key with two candidates enabled at
 * once is a registry bug, and `conflicts` finds it.
 */
export function bindings(commands: CommandDef[] = COMMANDS): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const command of commands) {
    if (!command.shortcut) continue;
    const existing = map.get(command.shortcut);
    if (existing) existing.push(command.id);
    else map.set(command.shortcut, [command.id]);
  }
  return map;
}

/**
 * Shortcuts that resolve to more than one *enabled* command in a given state.
 *
 * The check that makes sharing a key safe: two commands may claim `Delete` as
 * long as no state enables both. Anything this returns is ambiguous at the
 * moment the user presses the key, which is a registry bug rather than a
 * runtime one.
 */
export function conflicts(state: EditState, commands: CommandDef[] = COMMANDS): string[] {
  const ambiguous: string[] = [];
  for (const [shortcut, ids] of bindings(commands)) {
    const lookup = byId(commands);
    const live = ids.filter((id) => lookup.get(id)?.enabled(state));
    if (live.length > 1) ambiguous.push(`${shortcut}: ${live.join(', ')}`);
  }
  return ambiguous;
}

/**
 * What the command palette shows. `09` §8: **disabled commands are excluded**,
 * where a disabled row is noise — unlike the menu, which greys them so they
 * stay discoverable.
 *
 * Matching runs over the label, the description and the keywords, which is
 * what makes a search for "merge" return Combine and Dissolve with the
 * sentences that distinguish them rather than nothing at all.
 */
export function paletteEntries(
  state: EditState,
  query: string,
  commands: CommandDef[] = COMMANDS,
): CommandDef[] {
  const needle = query.trim().toLowerCase();
  return commands.filter((command) => {
    if (command.visible && !command.visible(state)) return false;
    if (!command.enabled(state)) return false;
    if (!needle) return true;
    const haystack = [command.label, command.description ?? '', ...(command.keywords ?? [])]
      .join(' ')
      .toLowerCase();
    return haystack.includes(needle);
  });
}

/**
 * What a context menu shows for a scope. Disabled entries are dropped here too:
 * a context menu is opened *at* something, and a greyed list of things that do
 * not apply to it is a worse answer than a short list that does.
 */
export function contextEntries(
  scope: ContextScope,
  state: EditState,
  commands: CommandDef[] = COMMANDS,
): CommandDef[] {
  return commands.filter(
    (command) =>
      command.contextMenu?.includes(scope) &&
      (!command.visible || command.visible(state)) &&
      command.enabled(state),
  );
}

/**
 * The menu bar. `09` §9's structure, in its order, **including disabled
 * commands** — greyed, so they stay discoverable and their shortcuts visible.
 */
export function menus(
  state: EditState,
  commands: CommandDef[] = COMMANDS,
): Array<{ group: CommandGroup; label: string; items: CommandDef[] }> {
  return GROUP_ORDER.map((group) => ({
    group,
    label: GROUP_LABELS[group],
    items: commands.filter(
      (command) => command.group === group && (!command.visible || command.visible(state)),
    ),
  })).filter((menu) => menu.items.length > 0);
}
