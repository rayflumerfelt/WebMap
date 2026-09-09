# Shared style-compilation test vectors

`08-styling-palettes.md` §3.1. Every file here runs in **both** implementations
— `packages/style-model` under Vitest and `python/webmap_core/style` under
pytest — against the same expected output. A divergence fails CI in both
languages, which is the mechanism that keeps two compilers from drifting.

`expected_layers` is **hand-written**, not captured from either
implementation. Generating it from one would make the other's test a check
that two programs agree, which they can do while both being wrong; written by
hand it is a check that each matches the specification.

Shape of a vector:

```jsonc
{
  "name": "graduated_polygons",
  "description": "Why this case exists and what would break without it",
  "symbology": { /* the Symbology model from §2 */ },
  "palettes": { "<id>": { /* Palette */ } },
  "source_id": "wells",
  "source_layer": "features",   // omit for a GeoJSON source
  "expected_layers": [ /* MapLibre layers, in order */ ]
}
```

Order matters. A polygon's fill must precede its outline and a line's casing
must precede its line, because MapLibre paints in array order and the
difference between a road casing and a hairline outline is which one is on top.

## Rules a hand-written vector depends on

Three decisions are invisible in the specification but decide the last bit of a
number. They are recorded here because a vector cannot be written by hand
without them, and because they are the places the two languages would otherwise
differ silently.

**Channel rounding is half away from zero.** Interpolating between stops whose
channels differ by an odd number produces exact `.5` ties — two of the five
classes in `graduated_polygons` do. JavaScript's `Math.round` rounds half up;
Python's built-in `round` rounds half to *even*, so `round(50.5)` is `50` and
`Math.round(50.5)` is `51`. The Python compiler therefore uses
`math.floor(v + 0.5)` rather than `round`. This was found by these vectors on
their first run, which is a fair summary of why they exist.

**A stop sitting exactly on the sampled position wins, and the last such stop
wins.** A pair of coincident stops is how a hard break is expressed in an
otherwise continuous ramp, and this rule makes the break sharp: values at and
above it take the upper colour. It matches the `discrete` interpolation rule
rather than contradicting it, and it removes any divide-by-zero case from the
interpolation path.

**A reference-scale label's size stops are exact powers of two.**
`label_reference_scale` writes its stops *relative to the reference zoom* —
`ref-24`, `ref`, `ref+24` — so the three size values are `size/2**24`, `size`
and `size*2**24`. Every multiplier is exactly representable, and both languages
produce identical doubles.

Computing the stops as `pow(2, stop - reference)` would look equivalent and is
not: a non-integer exponent goes through each language's `libm`, which is not
required to round identically, and these vectors compare bytes. The span is 24
because `interpolate` clamps outside its stop range instead of extrapolating
and MapLibre's maximum zoom is 24, so the clamp is unreachable from any
reference zoom.
