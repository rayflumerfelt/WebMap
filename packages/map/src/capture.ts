/**
 * Capture the map canvas as a PNG. `06-rendering.md` §8, `07-frontend.md` §2.1.
 *
 * **Why this is not two lines.** A WebGL drawing buffer is cleared after every
 * composite unless `preserveDrawingBuffer` is set at construction — so a bare
 * `canvas.toBlob()` returns a transparent image, reliably, on a map that looks
 * perfectly fine on screen. The obvious fix is to set that flag, and it is the
 * wrong one: it forces the browser to keep a second full-size buffer and costs
 * real performance on every pan and zoom, permanently, for a feature used once
 * per export.
 *
 * Instead the buffer is read inside the render pass itself. `once('render')`
 * fires with the buffer still intact, and `triggerRepaint` asks for a pass on
 * demand. The cost lands on the export rather than on the interaction.
 */

import type maplibregl from 'maplibre-gl';

export function capture(map: maplibregl.Map): Promise<Blob> {
  return new Promise((resolve, reject) => {
    // A map that never settles would leave this promise pending forever, and
    // an export button that hangs with no error is worse than one that fails.
    const timeout = setTimeout(() => {
      map.off('render', onRender);
      reject(
        new Error(
          'The map did not finish rendering within 30 seconds, so there was ' +
            'nothing to capture. This usually means a tile or glyph request is ' +
            'hanging — check the network panel for a request that never ' +
            'returns.',
        ),
      );
    }, 30_000);

    function onRender(): void {
      // Read inside the render pass, before the buffer is cleared.
      map.getCanvas().toBlob((blob) => {
        clearTimeout(timeout);
        if (blob) {
          resolve(blob);
        } else {
          reject(
            new Error(
              'The browser produced no image data from the map canvas. This ' +
                'is usually a lost WebGL context — reload the page and try ' +
                'the export again.',
            ),
          );
        }
      }, 'image/png');
    }

    map.once('render', onRender);
    map.triggerRepaint();
  });
}
