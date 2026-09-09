/**
 * Test environment shims. `07-frontend.md` §11.
 *
 * jsdom implements the DOM but not layout: every element measures 0×0 and
 * `ResizeObserver` does not exist. Neither matters for most components, but a
 * virtualizer is *made* of measurement — without these it computes a zero-high
 * window and renders no rows, which would make the attribute table's tests
 * fail for a reason that has nothing to do with the table.
 *
 * Deliberately here rather than inside one test file: a shim that changes how
 * the DOM behaves should be visible to anyone reading the suite, not hidden in
 * a `beforeAll` three hundred lines into a component test.
 */

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

/** A viewport-sized box for anything that asks. */
const RECT = { width: 800, height: 200, top: 0, left: 0, bottom: 200, right: 800, x: 0, y: 0 };

Object.defineProperty(HTMLElement.prototype, 'getBoundingClientRect', {
  configurable: true,
  value: () => ({ ...RECT, toJSON: () => RECT }),
});

for (const [property, value] of [
  ['clientHeight', RECT.height],
  ['clientWidth', RECT.width],
  ['offsetHeight', RECT.height],
  ['offsetWidth', RECT.width],
] as const) {
  Object.defineProperty(HTMLElement.prototype, property, {
    configurable: true,
    get: () => value,
  });
}
