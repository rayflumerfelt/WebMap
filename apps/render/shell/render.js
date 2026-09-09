/**
 * The render shell's entry point. `06-rendering.md` §4.
 *
 * Plain JavaScript on purpose: this file is loaded from `file://` by a browser
 * with no build step in front of it, and a bundler here would be one more
 * thing that can put the shell out of step with what the service expects.
 */

window.__mapReady = false;
window.__failedRequests = [];
window.__renderError = null;

window.renderMap = async function renderMap(spec) {
  try {
    const map = new maplibregl.Map({
      container: 'map',
      style: spec.style,
      interactive: false,
      attributionControl: false,
      // preserveDrawingBuffer is NOT set. The service screenshots the *page*,
      // not the canvas, so retaining the framebuffer would cost performance
      // for nothing — and screenshotting the page is what puts the HTML
      // overlays (legend, scale bar) into the image at all.
      preserveDrawingBuffer: false,
      // No cross-fade. The settled state is what is wanted, and a fade in
      // progress makes the same map render differently on consecutive runs.
      fadeDuration: 0,
      // The renderer must never invent a view. Every render names its own.
      center: [0, 0],
      zoom: 1,
    });

    // A failed tile produces a map with a hole, not an exception
    // (`06-rendering.md` §5.1). Recorded so the API can tell a geologist
    // "the tile server was down" rather than leaving them to conclude "the
    // data really is sparse there".
    map.on('error', (event) => {
      window.__failedRequests.push({
        url: (event && event.error && event.error.url) || null,
        message: (event && event.error && event.error.message) || String(event),
      });
    });

    if (spec.bounds) {
      map.fitBounds(spec.bounds, {
        padding: spec.padding == null ? 24 : spec.padding,
        // Instant. An animated fit would still be moving when 'idle' fires.
        duration: 0,
      });
    } else if (spec.center) {
      map.jumpTo({
        center: spec.center,
        zoom: spec.zoom == null ? 9 : spec.zoom,
        bearing: spec.bearing || 0,
        pitch: spec.pitch || 0,
      });
    }

    // Overlays are the app's own components, mounted into #overlay — the same
    // code path as the interactive map's legend (§9).
    if (spec.overlay && window.WebMapOverlay) {
      window.WebMapOverlay.mount(document.getElementById('overlay'), spec.overlay);
    }

    // 'idle' fires once tiles, glyphs and sprites have landed and no further
    // rendering is queued. Screenshotting before it yields a half-drawn map,
    // and the failure is *intermittent*, which makes it miserable to debug.
    // Do not substitute 'load'.
    //
    // The race against a timer is not belt-and-braces. A source that fails
    // outright can leave the map never reaching idle at all, and without this
    // the shell would sit there while the service waited on a flag that never
    // gets set — a partial map with a recorded reason is far more useful than
    // a request that never returns.
    const settled = new Promise((resolve) => {
      if (map.loaded() && map.areTilesLoaded()) {
        resolve('idle');
        return;
      }
      map.once('idle', () => resolve('idle'));
    });
    const expired = new Promise((resolve) =>
      setTimeout(() => resolve('timeout'), spec.settleTimeoutMs || 20000),
    );
    if ((await Promise.race([settled, expired])) === 'timeout') {
      window.__failedRequests.push({
        url: null,
        reason: 'settle_timeout',
        message: 'The map did not reach a settled state; the image may be incomplete.',
      });
    }

    // The overlay's fonts must be settled too. A legend screenshotted
    // mid-font-swap renders in a fallback face, which is a large enough
    // difference to fail a visual golden on its own.
    if (document.fonts && document.fonts.ready) {
      await document.fonts.ready;
    }

    window.__mapReady = true;
  } catch (error) {
    // Reported rather than thrown: the service waits on `__mapReady` and a
    // thrown error inside an evaluated promise would surface only as a
    // timeout, which says nothing about what went wrong.
    window.__renderError = String((error && error.message) || error);
    window.__mapReady = true;
  }
};
