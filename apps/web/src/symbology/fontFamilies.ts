/**
 * The font families this deployment actually built. `08` §2.3, `07` §6.2.
 *
 * `GET /static/glyphs` returns flat **stack names** — `Inter Regular`,
 * `Inter Bold`, `Oswald Regular` — because a stack is what MapLibre asks for
 * and bold is a different stack, not a property. The picker wants them grouped
 * by family, so this is where the grouping happens: at the boundary, once,
 * rather than in the control that has to stay liftable.
 *
 * **The list is read, never hard-coded.** A font whose build failed is absent
 * from the endpoint and therefore absent from the picker, instead of being
 * offered and then rendering blank on the map — which is the failure mode this
 * whole arrangement exists to avoid.
 */

import type { FontFamily } from '@webmap/ui';

/** Face words that follow a family name in a stack. Longest first, so
 *  "Bold Italic" is stripped before "Bold" leaves "Italic" stranded. */
const FACES = ['Bold Italic', 'Italic Bold', 'Regular', 'Bold', 'Italic', 'Medium', 'Light'];

/** Group flat stack names into families, preserving the exact stack strings. */
export function groupIntoFamilies(stacks: string[]): FontFamily[] {
  const families = new Map<string, string[]>();

  for (const stack of stacks) {
    const family = familyOf(stack);
    const existing = families.get(family);
    if (existing) existing.push(stack);
    else families.set(family, [stack]);
  }

  return [...families.entries()]
    .map(([name, entries]) => ({ name, stacks: entries.sort() }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function familyOf(stack: string): string {
  for (const face of FACES) {
    if (stack.endsWith(` ${face}`)) return stack.slice(0, -face.length - 1);
  }
  return stack;
}

/**
 * Fetch and group, or return an empty list.
 *
 * An empty list is a legitimate answer — a deployment that built no glyphs has
 * no fonts — and the picker renders as an empty select rather than throwing.
 * It is not the same as a failed request, which the caller sees as a rejected
 * promise.
 */
export async function loadFontFamilies(
  get: (path: string) => Promise<{ stacks: string[] }>,
): Promise<FontFamily[]> {
  const response = await get('/static/glyphs');
  return groupIntoFamilies(response.stacks ?? []);
}
