/**
 * Turn font files into the SDF glyph ranges MapLibre fetches.
 *
 * Usage (via scripts/fetch_fonts.py, which mounts the directories):
 *   node build_glyphs.mjs /fonts /glyphs
 *
 * Each font file becomes one *stack* directory of `{start}-{end}.pbf` files.
 * MapLibre asks for the range covering each codepoint it needs to draw, so a
 * missing range is a missing label rather than an error.
 */

import { readdir, mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, extname, join } from 'node:path';
import fontnik from 'fontnik';

const [, , fontDir, glyphDir] = process.argv;
if (!fontDir || !glyphDir) {
  console.error('usage: build_glyphs.mjs <font-dir> <glyph-dir>');
  process.exit(2);
}

/**
 * Codepoints to build, as [start, end] inclusive.
 *
 * **Not the full 0–65535.** A complete build of Noto Sans alone is 256 ranges
 * and tens of megabytes, almost all of it CJK and scripts no label in this
 * system will ever use. These four cover Latin, Latin Extended A and B, the
 * IPA and spacing-modifier blocks, Greek and Cyrillic, and the punctuation,
 * superscript, currency and arrow blocks that carry degree signs, primes and
 * en-dashes — everything a well name, a formation name or a contour label
 * needs, in English and every European language.
 *
 * Extend it here if a deployment needs another script; the cost is build time
 * and disk, not correctness.
 */
const RANGES = [
  [0, 255],       // Basic Latin + Latin-1 Supplement
  [256, 767],     // Latin Extended A/B, IPA, spacing modifiers, diacritics
  [768, 1279],    // Greek and Cyrillic
  [8192, 8447],   // General punctuation, superscripts, currency, letterlike
];

const STEP = 256;

async function buildFont(file) {
  const stack = basename(file, extname(file));
  const target = join(glyphDir, stack);
  await mkdir(target, { recursive: true });

  const buffer = await readFile(join(fontDir, file));
  let written = 0;
  let bytes = 0;

  for (const [from, to] of RANGES) {
    for (let start = from; start <= to; start += STEP) {
      const end = Math.min(start + STEP - 1, to);
      const pbf = await new Promise((resolve, reject) => {
        fontnik.range({ font: buffer, start, end }, (err, data) =>
          err ? reject(err) : resolve(data),
        );
      });
      // fontnik returns a valid empty PBF for a range the font does not
      // cover. Writing it anyway is deliberate: MapLibre treats a 404 as a
      // load error and logs it on every tile, while an empty range is
      // simply "nothing to draw here".
      await writeFile(join(target, `${start}-${end}.pbf`), pbf);
      written += 1;
      bytes += pbf.length;
    }
  }

  console.log(`  ${stack.padEnd(34)} ${String(written).padStart(3)} ranges  ${(bytes / 1024).toFixed(0)} KB`);
  return { stack, written, bytes };
}

const files = (await readdir(fontDir)).filter((f) =>
  ['.ttf', '.otf'].includes(extname(f).toLowerCase()),
);
if (files.length === 0) {
  console.error(`No .ttf or .otf files in ${fontDir}`);
  process.exit(1);
}

console.log(`Building ${files.length} font stacks into ${glyphDir}`);
let total = 0;
for (const file of files.sort()) {
  const result = await buildFont(file);
  total += result.bytes;
}
console.log(`Done. ${files.length} stacks, ${(total / 1024 / 1024).toFixed(1)} MB total.`);
