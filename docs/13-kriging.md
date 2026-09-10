# 13 — Kriging and Geostatistics

Variography, the kriging family, regression kriging, and **regression indicator kriging**
(RIK) with a global parametric trend.

This document specifies the geostatistics subsystem of `webmap_geo`. It is the deep end of
`05-geoprocessing.md` §6: that section specifies the two interpolators a structure map needs
and dispatches between them; this one specifies everything a geostatistician expects to find
underneath. Read `05` first — its rules on the analysis frame, fault handling and diagnostics
apply here without restatement.

The user-facing controls live in `07-frontend.md` §9.

---

## 0. Scope

**In scope.**

- Empirical and model variography — omnidirectional, directional, variogram maps.
- The kriging family: simple, ordinary, universal/KED, indicator, block.
- Regression kriging (RK) and regression indicator kriging (RIK).
- Sequential Gaussian simulation (SGS), as a comparison baseline and the source of
  realisation-based connectivity statistics.
- Declustering, target transforms, covariate screening.
- Diagnostics, spatially-blocked validation, and a structured flag system.
- Gridded output as arrays, and a per-node **local CDF** persisted as a multi-band grid.

**Out of scope, because WebMap already owns it.** This is the important half of the section:
each of these has a home, and building a second one here would produce two answers to the
same question.

| Concern | Owner |
|---|---|
| File ingest, column mapping, format parsing | `11-file-io.md` |
| Projection of raw uploads, CRS validation | `webmap_core.crs.CrsContext`, [`adr/0003`](adr/0003-geoprocessing-owns-crs.md) |
| Contouring, filled bands | `05` §7 |
| Map rendering, colour-filled grids, palettes | `06-rendering.md`, `08-styling-palettes.md` |
| Layer creation, export, permissions, lineage | `02-data-model.md`, `webmap_core.services` |
| Long-running execution, progress, cancellation | `10-jobs-async.md` |
| Figures | `07` §9.3 (ECharts) and the render service |

The toolkit produces arrays, numbers and flags. It does not draw anything and does not write
a file.

**Domain framing.** The core is domain-agnostic: covariates are arbitrary named columns and
the trend is an arbitrary callable. Petroleum knowledge — completion-response trend forms,
bottomhole resolution, per-foot intensity conventions, the lateral-length-normalisation trap
— lives in `webmap_geo.presets.petroleum` and is imported by nothing in the core (§15).

**Control stance.** Every geostatistical parameter has an automatic default computed from the
data, and every one is overridable. Auto-selection is the documented path; overrides sit
behind an explicit `advanced=` surface so a caller cannot change one by accident. When an
override is used, the lineage record stores **both** the auto-selected value and the
override, and the UI shows both (§14.2).

---

## 1. Design principles

Several of these are already house rules and are restated only because getting them wrong
here is expensive. The ones marked **new** are additions this subsystem brings.

| Principle | Why |
|---|---|
| Fail loudly on unsafe input | A silent bad default is worse than a loud failure. Already `CLAUDE.md` §8. |
| All distance math in the analysis frame | A variogram fitted on degrees has a range and an anisotropy that vary with latitude. Already `CLAUDE.md` §3.1. |
| Explicit `numpy.random.Generator` everywhere | Already `CLAUDE.md` §3.3. The integer seed goes in the lineage record. |
| **Every parameter auto-selected, every parameter overridable** | **New.** Non-experts get a defensible run; experts are not boxed in. |
| **Diagnostics are read-only** | **New, and load-bearing.** No diagnostic output feeds back into coefficient or parameter estimation. Otherwise the fit is circular and the run is not reproducible. |
| **Never estimate where there is no data** | **New.** Nodes beyond the mask distance are `NaN`, not interpolated. A masked hole is honest; a smooth one is not. This extends `05` §6.5's extrapolation reporting from a number to a rule. |
| **Structured flags, not just plots** | **New.** Every finding has a severity and a stable code (§16). The user who most needs a warning is least able to read the figure that carries it. |
| **The trend is global in RK/RIK** | **New, and the most consequential.** Coefficients are scalars for the whole field. Covariate-versus-spatial attribution is not identifiable from spatial data; locally varying coefficients produce confident, misleading maps. |

---

## 2. Where the code lives

Inside `webmap_geo`, as new subpackages beside the existing ones
([`adr/0012`](adr/0012-geostatistics-in-webmap-geo.md)). It reads and writes geometry and
gridded values, which is what `CLAUDE.md` §3.5 puts here, and it stays a leaf.

```
python/webmap_geo/src/webmap_geo/
  flags.py              # Flag, FlagCollection, the §16 registry

  prep/
    validate.py         # dtypes, NaN policy, duplicate resolution
    decluster.py        # cell declustering with a cell-size sweep
    transform.py        # log, Box-Cox, normal score (forward + inverse tables)
    screen.py           # collinearity, spatial-proxy, extrapolation-extent checks

  variogram/            # EXISTING, extended
    experimental.py     # + directional, equal-count binning, variogram maps
    model.py            # + nested structures, Matérn, stable, power
    fit.py              # WLS, renamed intent: raw-data and indicator variograms
    fit_reml.py         # NEW — for trend residuals only (adr/0011)
    aniso.py            # NEW — ellipse fit + bootstrap significance
    auto.py             # NEW — lag/maxlag/model selection

  interpolate/          # EXISTING, extended
    kriging.py          # + simple, universal/KED
    indicator.py        # NEW — per-threshold IK
    block.py            # NEW — block kriging by point discretisation
    neighborhood.py     # NEW — KD-tree search, ellipse, octant/quadrant, min/max

  trend/                # NEW
    forms.py            # preset parametric forms + registry
    parser.py           # sandboxed sympy expression -> callable
    gls.py              # nonlinear GLS with Cholesky whitening
    loop.py             # the GLS <-> REML iteration (§8.1)
    discover.py         # OPTIONAL PySR wrapper; offline, never a runtime dep

  cdf/                  # NEW
    local.py            # LocalCDF: per-node discrete CDF + quantile/prob/mean
    tails.py            # lower-linear, upper-hyperbolic/exponential/linear
    order.py            # order-relation correction

  estimators/           # NEW
    rk.py               # regression kriging (Gaussian residual)
    rik.py              # regression indicator kriging — the centrepiece
    sgs.py              # sequential Gaussian simulation (baseline)

  diag/                 # NEW — arrays and numbers, no figures
  validate/             # NEW — spatial splits, scores, baselines
  presets/petroleum.py  # NEW — imported by nothing in the core
```

---

## 3. Dependencies

Already present and used as-is: `numpy`, `scipy`, `shapely`, `gstools` (covariance models,
kriging backend, conditioned simulation), `scikit-gstat` (empirical and directional
variograms, variogram maps; `Variogram.to_gstools()` bridges the two).

**Added**, both load-bearing and both justified in
[`adr/0012`](adr/0012-geostatistics-in-webmap-geo.md):

| Library | Role |
|---|---|
| `pandas` | The covariate table — named heterogeneous columns, selected, standardised, screened and carried alongside coordinates through declustering and subsetting. |
| `sympy` | Sandboxed parsing and `lambdify` of a user-supplied trend expression. The alternative is `eval`, which is not an alternative. |

**Optional extras**, never runtime dependencies:

| Extra | Library | Role |
|---|---|---|
| `[discover]` | `pysr` | Offline symbolic regression to *discover* a trend form (§9). Drags in Julia. |
| `[fast]` | `numba` | JIT for neighbourhood loops that resist vectorisation. |
| `[ref]` | `geostatspy` | Reference implementations for cross-checking declustering, MIK and order relations in tests. |

**Not added.** `matplotlib` — figures are the frontend's job. `rasterio` for a second COG
writer — `webmap_io` has one. `pykrige` — too slow for production grids, and a test oracle we
do not need given `[ref]`.

**Explicitly avoided.** Pure-Python geostatistics ports, unusable past a few thousand
samples. `sklearn.gaussian_process` — no variogram control, no indicator support, poor
scaling. Anything requiring a network call at runtime (`00-overview.md` §7).

---

## 4. Data contracts

Frozen dataclasses with validation in `__post_init__`, in `webmap_geo/types.py` beside the
existing `AnalysisFrame` and `GridDefinition`.

### 4.1 Samples

`webmap_geo.control.ControlPoints` already carries coordinates, values and a frame. The
geostatistics path needs three more fields, added there rather than in a parallel type:

```python
@dataclass(frozen=True)
class ControlPoints:
    coords: NDArray[np.float64]        # (n, 2), planar, in `frame`
    values: NDArray[np.float64]        # (n,)
    frame: AnalysisFrame
    ids: NDArray | None                # stable identifiers, for CV reporting

    # Added for §13:
    covariates: pd.DataFrame | None    # (n, p), named columns
    weights: NDArray | None            # (n,) declustering weights; None = uniform
    groups: NDArray | None             # (n,) grouping label (e.g. pad) for by-group CV
```

**No `crs` field and no `units` field.** `AnalysisFrame` carries both, it is already what
every `webmap_geo` entry point takes, and it is metadata rather than an instruction — nothing
here reprojects ([`adr/0003`](adr/0003-geoprocessing-owns-crs.md)).

### 4.2 Grid

`GridDefinition` as it stands (`05` §6), plus a `mask` and an `auto` constructor:

```python
mask: NDArray[np.bool_] | None    # True = estimate here

@classmethod
def auto(cls, points, variogram, *, max_cells=SOFT_CELL_LIMIT, extent="hull") -> GridDefinition
# extent: "bbox" | "hull" | "alpha_hull" | Polygon
```

`SOFT_CELL_LIMIT` is already 4,000,000 and already refuses with a suggested cell size.

### 4.3 Variogram model

Today's `FittedVariogram` is a single structure — one nugget, one sill, one range. That is
enough for `05` §6.2's ordinary kriging and not enough for anything here. It gains nesting
and a fit report:

```python
@dataclass(frozen=True)
class Structure:
    family: str              # "spherical"|"exponential"|"gaussian"|"matern"|"power"|"stable"
    sill: float
    range_: float
    nu: float | None = None      # Matérn smoothness
    alpha: float | None = None   # stable exponent

@dataclass(frozen=True)
class VariogramModel:
    nugget: float
    structures: list[Structure]
    anisotropy: Anisotropy | None      # ratio + azimuth, deg CW from N
    fit_method: str                    # "wls" | "reml" | "manual"
    fit_report: dict                   # objective, n_pairs per lag, condition diagnostics,
                                       # and for REML the likelihood of each candidate family
```

`FittedVariogram` becomes the single-structure special case, kept so `05` §6.2's signature
does not change under existing callers.

### 4.4 Local CDF

The RIK output. Not a surface — a distribution at every node.

```python
@dataclass
class LocalCDF:
    thresholds: NDArray[np.float64]    # (K,), in TARGET units after any trend shift
    probs: NDArray[np.float32]         # (n_nodes, K), monotone non-decreasing, in [0, 1]
    lower_tail: TailModel
    upper_tail: TailModel

    def quantile(self, q: float) -> NDArray: ...
    def prob_above(self, z: float) -> NDArray: ...
    def mean(self) -> NDArray: ...     # numerical integration, NOT a smearing factor
    def variance(self) -> NDArray: ...
    def tail_dependent(self, q: float) -> bool: ...   # §10.4
```

**Persisted as a multi-band grid**, one band per threshold, through the COG path that already
stores every grid — so it inherits storage, permissions, lineage, versioning and export with
no new machinery. `thresholds` and the tail parameters live in the dataset's lineage
parameters. Derived surfaces (P10, P50, P90, `P(Z > z)`, mean) are computed from it rather
than stored, because storing them guarantees they drift from the distribution they came from.

### 4.5 Flag

```python
@dataclass(frozen=True)
class Flag:
    severity: str                      # "ERROR" | "WARNING" | "INFO"
    code: str                          # stable, from the §16 registry
    msg: str                           # answers what happened, why, and what now
    detail: dict | None = None
    figure: str | None = None          # key into the result's figure-data dict
```

This **replaces `InterpolationResult.warnings: list[str]`**. Free text cannot be branched on,
cannot be counted, and cannot be linked to the parameter that caused it — and for an MCP tool
the message is the interface (`CLAUDE.md` §8). Existing warnings in `interpolate/dispatch.py`
migrate to codes rather than living alongside them.

### 4.6 Reproducibility

**There is no `RunManifest`.** The lineage record is it
([`adr/0012`](adr/0012-geostatistics-in-webmap-geo.md)). `lineage.parameters` carries
`webmap_geo.__version__`, every auto-selected value, every override paired with the auto
value it displaced, every integer seed, the fitted coefficients and variogram parameters, and
the flag list. `05` already requires a lineage record sufficient to re-run and reproduce the
identical grid, asserted by re-running and comparing arrays — that test now covers this too.

---

## 5. Preprocessing

Order matters. Each step assumes the previous one has run.

### 5.1 Validation (`prep/validate.py`)

- **The frame is already checked.** `CrsContext` refuses a geographic SRID at the boundary
  that prepares the arrays. Kriging on degrees produces anisotropy artefacts that vary with
  latitude, and the check belongs where the SRID is known rather than where coordinate
  magnitudes have to be sniffed.
- **NaN policy.** Drop rows with a missing target, or a missing covariate the trend uses.
  `INFO / ROWS_DROPPED` with the count.
- **Duplicate and near-collocated resolution.** Points closer than `dedup_tol` — default half
  the 1st-percentile inter-point distance, exposed — are resolved by policy: `"mean"`,
  `"first"`, `"error"`, or a caller-supplied callable. `INFO / DUPLICATES_RESOLVED`.
- **Degenerate geometry.** All points collinear, or fewer than `min_samples` (default 30) →
  `ERROR / INSUFFICIENT_DATA`.

*Petroleum preset:* horizontal wells must be represented by bottomhole or lateral midpoint,
never surface location. `presets.petroleum.resolve_locations()` does this and warns when it
sees what looks like surface coordinates — several distinct targets at one point.

### 5.2 Declustering (`prep/decluster.py`)

Samples cluster in high-value areas by construction, so the naive sample CDF is biased high.
That bias propagates into the trend fit, the indicator threshold placement and the tail
model — three of the four things this document is about.

`05` §6.3 already declusters for the experimental variogram and already says why: *well
control is clustered by development history, not by geology*. This generalises it.

**Method:** cell declustering. Sweep cell size over a geometric range, compute the
declustered mean at each, and take the size that minimises it for a positively clustered
target — standard, and parameter-free. Offset the cell origin over several random shifts per
size (from the passed `Generator`) and average, so the answer does not depend on where the
grid happens to start. Weights `w_i` are normalised to sum to `n`.

`direction="min"|"max"` for the rare case where clustering is in low values; `cell_size=` as
an override.

Weights are consumed by the GLS trend fit (§8.2), indicator threshold placement (§10.1), the
global CDF behind tail extrapolation (§10.4), and histogram-reproduction diagnostics (§12.7).

### 5.3 Target transform (`prep/transform.py`)

`"log"`, `"boxcox"` (λ by profile likelihood on declustered data, or supplied), `"nscore"`
(forward and inverse tables; **required** for SGS or any Gaussian comparison run), or `None`.

All expose `forward`, `inverse` and `is_monotone` — always `True`, which is the property that
matters. **Back-transformation of quantiles is exact. Back-transformation of a mean is not**
(§6).

> **Petroleum preset warning.** Do **not** normalise the target by lateral length. Production
> scales sublinearly with lateral length — roughly `L^α`, α typically in [0.6, 0.9]. Dividing
> by `L` imposes α = 1 and leaves a systematic length artefact in the residual, which then
> propagates into the kriging. Pass `lateral_length` as a covariate and let the trend resolve
> the exponent. Proppant and fluid *should* be per-foot intensities, because those are
> genuine design choices. `WARNING / TARGET_LENGTH_NORMALIZED` fires when the preset sees a
> target that looks like a per-foot rate beside a `lateral_length` covariate.

### 5.4 Covariate screening (`prep/screen.py`)

Three automatic checks. All produce flags; none modify data.

1. **Collinearity.** Condition number of the standardised design matrix, plus per-covariate
   VIF. A high condition number means the coefficient vector is unstable and the fitted "law"
   changes between runs. `WARNING / COVAR_COLLINEAR` naming the implicated pair.
   *Petroleum:* proppant and fluid intensity are usually chosen together and strongly
   correlated; spacing correlates with vintage.
2. **Spatial proxies.** Correlate each covariate with the coordinates, and compare each
   covariate's own variogram range against the raw target's. A covariate that is effectively
   a spatial label — vintage, operator — lets the trend absorb the spatial signal.
   `WARNING / COVAR_SPATIAL_PROXY`.
3. **Extrapolation extent.** Convex hull of the covariate space. When a mapping scenario is
   supplied (§10.5), flag nodes whose covariate values fall outside it:
   `WARNING / COVAR_EXTRAPOLATION` with the fraction of nodes affected.

---

## 6. The RIK model statement

Let `Z(x)` be the target and `X(x)` the covariate vector.

```
Z(x) = f(X(x); θ) + R(x)
```

- `f` — user-supplied, may be nonlinear in θ, **globally constant**.
- `θ ∈ R^p` — scalar coefficients, estimated by GLS (§8).
- `R(x)` — zero-mean, spatially correlated residual. In **RK** it is kriged under a Gaussian
  model. In **RIK** it is estimated by multi-threshold indicator kriging, so its local
  distribution is recovered without a Gaussian assumption.

The RIK output is a **local CDF at each grid node**:

```
F̂_Z(z | x) = F̂_R( z − f(X(x); θ̂) | x )
```

The trend enters as a location shift, and shifts are monotone, so **quantiles map exactly**:
the q-quantile of `F̂_R` plus the trend *is* the q-quantile of `F̂_Z`. No correction term.

**Why quantiles and not a mean.** With a log-transformed target the mean does not
back-transform cleanly — `exp(E[log Z]) ≠ E[Z]` — and the usual smearing corrections rest on
assumptions nobody checks. Quantiles commute exactly with any monotone transform. Where a
mean is genuinely wanted, integrate the recovered CDF (`LocalCDF.mean()`).

**Why indicators at all.** Ordinary kriging has no mechanism for covariates, and its
multi-Gaussian structure actively *disconnects* extreme values — the opposite of the target
behaviour, where high-value regions are spatially connected, change slowly in their interiors
and fall off sharply at their margins. Regression kriging fixes the covariate problem;
indicator kriging fixes the connectivity problem. RIK is both.

### Known limitation

With a global trend, genuine regional differences in covariate *response* are absorbed into
the residual field and kriged as though they were spatial structure. This is an accepted
approximation, not an oversight. The standardised-residual diagnostic (§12.1) is how a user
detects it; the remedy is to split the field into separate runs, which is a deliberate user
decision and never an automatic one (§17).

---

## 7. Variography

### 7.1 Empirical (`variogram/experimental.py`)

Extends what `05` §6.3 already specifies — mandatory subsampling with declustered weights and
an explicit `Generator` stay exactly as they are.

- Omnidirectional and directional (azimuth, angular tolerance, bandwidth).
- **Equal-count lag binning by default**, not equal-width: it stabilises the tail where pair
  counts thin out. Equal-width available as an override.
- **Max lag = ½ the domain diagonal** by default; beyond that the estimator is unreliable.
- **Return pair counts per lag** alongside the semivariance. The WLS fitter weights by them
  and the workbench sizes its points by them (`07` §9.5) — a variogram plot without them
  invites a user to trust a tail built from nine pairs.
- Estimators: Matheron (default), Cressie–Hawkins (robust), Dowd.
- Variogram map for anisotropy detection.

### 7.2 Models (`variogram/model.py`)

Spherical, exponential, Gaussian, Matérn (ν free), power, stable. Nested structures with a
separate nugget. Geometric anisotropy as (ratio, azimuth), applied by rotating and scaling
coordinates before evaluating the isotropic model. Every model exposes `gamma(h)` and
`cov(h)` and converts to a `gstools.CovModel`.

### 7.3 WLS fitting (`variogram/fit.py`)

Weighted least squares against the empirical points, weights `n_pairs(h) / γ_model(h)²`
(Cressie weighting). Fast enough for a slider.

**This is the fitter for raw-data variograms** — OK, SK, and standalone IK — and for
indicators of an already-detrended residual, whose mean is again a single constant.

### 7.4 REML fitting (`variogram/fit_reml.py`)

**For the residual of a fitted trend, and only there.** The argument, the formula and the
three implementation rules are in [`adr/0011`](adr/0011-reml-for-trend-residual-variograms.md),
which exists because this is the single easiest thing in the document to "simplify" into a
subtly wrong map.

The short version: a residual is the error minus what the trend absorbed, the absorption is
systematic, so a least-squares variogram of a residual underestimates sill and range — and
inside the §8.1 loop that bias feeds itself until the trend has eaten the spatial signal and
nothing reports a problem.

### 7.5 Nugget guard

Fit the nugget freely. If nugget/sill > 0.6, `WARNING / HIGH_NUGGET`: there is little spatial
structure to exploit and the map will be close to the trend alone.

### 7.6 Anisotropy (`variogram/aniso.py`)

Directional variograms on a fixed azimuth set — 0°, 45°, 90°, 135°, plus 22.5° offsets where
data density allows — then fit an anisotropy ellipse.

**Accept the anisotropic model only if the range ratio is significant against a bootstrap
null**: permute values across locations (from the passed `Generator`), refit, and compare the
ratio distribution. Otherwise fall back to isotropic and emit
`INFO / ANISO_NOT_SIGNIFICANT`. The p-value goes in the lineage record and on screen
(`07` §9.5), because an anisotropy azimuth quoted without it looks like a measurement.

This replaces the current "detect anisotropy in 8 azimuths" behaviour in `05` §6.3, which
accepts whatever ellipse comes back.

### 7.7 Auto-selection and overrides (`variogram/auto.py`)

`auto_variogram(points, purpose=...)` returns a fitted `VariogramModel`, the empirical points,
and every intermediate choice. Overrides: lag count, lag width, max lag, binning scheme,
estimator, model family, nugget (fix or free), each structure's sill and range, anisotropy
ratio and azimuth, and a fully manual model (`fit_method="manual"`).

**A cheap re-evaluation path is required**, not optional: given fixed empirical points,
evaluating a candidate model is pure arithmetic and must not re-bin. The variogram workbench
drags against it.

---

## 8. Stage 1 — global trend estimation

Shared by RK and RIK.

### 8.1 The loop (`trend/loop.py`)

```
theta <- theta0                                  # preset, expression, or discovery output
repeat (max 5 iterations):
    r     <- z - f(X; theta)
    phi   <- fit_covariance_REML(r, coords, J)   # §7.4
    C     <- covariance_matrix(phi, coords)
    theta <- gls_fit(z, X, f, C, theta)          # whitened nonlinear least squares
until ||theta - theta_prev||_rel < tol           # default 1e-4
```

Two to four iterations typically suffice. Failure to converge by five indicates an
identifiability problem — usually collinear covariates — not a numerical one (§8.4).

**This loop does not introduce spatial variation in θ.** It produces one global coefficient
vector. What iterating corrects is the *weighting*: without it, samples in dense clusters are
effectively counted many times over, biasing the global law toward whatever practice prevails
there. It fixes a clustering-bias problem, not a spatial-variation problem — a distinction
worth keeping straight, because the name suggests otherwise.

A simpler fallback, OLS with declustering weights, gets close on the point estimates. The
loop mainly removes clustering bias and tightens standard errors. Exposed as `method="ols"`
for speed and as the documented fallback when the loop fails.

### 8.2 GLS step (`trend/gls.py`)

Whiten by the Cholesky factor and hand to nonlinear least squares:

```python
def gls_fit(z, X, f, C, theta0, weights=None):
    L = cholesky(C, lower=True)
    def resid(theta):
        e = z - f(X, *theta)
        if weights is not None:
            e = e * np.sqrt(weights)
        return solve_triangular(L, e, lower=True)
    return least_squares(resid, theta0, method="trf")
```

One Cholesky per outer iteration; negligible for a few thousand samples.

**Above ~10,000 samples** the dense `C` is the bottleneck — O(n²) memory, O(n³) time. Switch
automatically to covariance tapering (Wendland at ~1.5× range, sparse Cholesky) or fit the
trend on a stratified declustered subset of ~5,000 and apply θ̂ to all samples. Record which
path ran; emit `INFO / TREND_SUBSET_FIT` when subsetting.

**Coefficient uncertainty.** Return `cov(θ̂) ≈ (Jᵀ C⁻¹ J)⁻¹` from the final iterate. Needed
for the optional trend-uncertainty term in the RK variance (§11.2) and for reporting standard
errors.

### 8.3 Trend forms (`trend/forms.py`, `trend/parser.py`)

Three ways to supply `f`:

1. **Preset registry.** Named parametric forms with sensible `theta0` and bounds:
   `"linear"`, `"loglinear"`, `"power"` (`a·∏ xᵢ^bᵢ`), `"saturating"`
   (`a·∏(1 − exp(−xᵢ/bᵢ))`), `"additive_saturating"`, `"cobb_douglas"`. Petroleum presets
   compose these into completion-response forms (§15).
2. **Sandboxed expression.** A string parsed with `sympy.parse_expr` against an allow-list of
   functions (`exp`, `log`, `sqrt`, `Abs`, `Pow`, arithmetic) and the declared covariate
   symbols, then `lambdify`'d to NumPy. **Never `eval`.** Reject any expression containing a
   symbol outside the declared covariates and parameters. Parsing happens **server-side**; a
   client-side parse is a convenience, never the source of truth (`07` §9.4).
3. **A raw Python callable** `f(X, *theta) -> ndarray`. The caller's responsibility, and not
   reachable from the API or MCP surfaces — only from a notebook.

All three go through the same validation: finite output over the observed covariate range,
correct output shape, and a finite-difference check against any analytic Jacobian supplied.

### 8.4 Failure handling

| Condition | Action |
|---|---|
| Coefficients oscillate between iterations | Do **not** return the last iterate silently. Fall back to declustered OLS, `WARNING / TREND_NOT_CONVERGED`, and attach the coefficient trajectory. |
| `JᵀJ` ill-conditioned | `WARNING / TREND_UNIDENTIFIED`, naming *which* coefficients are unidentified (smallest singular vectors) and suggesting a reduced feature set. |
| `f` returns NaN/Inf for in-range covariates | `ERROR / TREND_NONFINITE` with the offending values. |
| `f` non-monotone in a covariate over its observed range | `WARNING / TREND_NONMONOTONE`. Evaluate `f` on a per-covariate grid holding others at their median and flag sign reversals in the numerical partial derivative. Symbolic-regression expressions turn over inside the data range constantly. |

### 8.5 Secondary check

Compute the variogram of raw `z` and of `r`. The residual variogram should be lower but
should retain a comparable **range**. A collapsed range means the trend has taken spatial
structure — raise the variance-budget warning (§12.4).

---

## 9. Optional — trend discovery (`trend/discover.py`)

Offline, **not** part of the estimation loop, and not a runtime dependency. Symbolic
regression proposes a functional form; a human accepts it; the form is then frozen.

Notes an implementer must preserve:

- Custom operators are **Julia strings**, not Python callables. A Python callback would cross
  the Python/Julia boundary on every one of millions of expression evaluations.
- Saturating operators matter: diminishing-returns responses are expensive to build out of
  `+`, `*` and `exp`, and consume the complexity budget.
- Pass declustering weights.
- **Select from the Pareto front using spatial-block CV** (§13), not the in-sample front.
  With clustered samples the in-sample front overfits.
- Run 5–10 seeds. If the structure is not stable across seeds the interpretability is
  illusory — reduce the feature set or accept a black-box trend. `WARNING / SR_UNSTABLE`
  with the per-seed expressions.

The discovered `f` is **frozen** before it enters §8. Structure is never searched inside the
estimation loop.

---

## 10. Stage 2 — kriging the residual

Fix θ̂. All remaining work is on `r_i = z_i − f(X_i; θ̂)`.

### 10.0 RK path — Gaussian residual (`estimators/rk.py`)

1. Fit a continuous variogram to `r` by REML (§7.4).
2. Ordinary kriging of `r` onto the grid → `R̂(x)`, `σ²_K(x)`.
3. Add the trend at each node: `Ẑ(x) = f(X(x); θ̂) + R̂(x)`.
4. Back-transform through §5.3 if a transform was applied — **quantiles only**, never the mean
   directly.

This is the "krige the residuals" workflow. It shares Stage 1 entirely with RIK and differs
only in Stage 2. It is also baseline 3 in §13.

### 10.1 Threshold selection (`interpolate/indicator.py`)

- 5–9 thresholds at **declustered quantiles** of `r`, not round numbers. Default 7.
- `threshold_bias="upper"|"even"|"lower"`, or explicit thresholds, for when high-value
  delineation is the decision being made.
- **Minimum-count guard:** at least ~30 samples on the minority side of each threshold. Below
  that the indicator variogram is noise. Drop failing thresholds and emit
  `INFO / THRESHOLD_DROPPED` naming them.
- Fewer than 3 thresholds surviving → `ERROR / TOO_FEW_THRESHOLDS`, recommending RK.

### 10.2 Per-threshold estimation

For threshold `r_k`, `I_k(x) = 1{R(x) ≤ r_k}`. Fit and krige each indicator
**independently**: empirical indicator variogram auto-fit by **WLS** (§7.3 — the indicator
mean is a single constant, so §7.4's bias does not apply), then ordinary kriging onto the
grid.

Because each threshold gets its own variogram, the method can represent **longer correlation
ranges at high thresholds** — the empirical signature of connected high-value regions. That is
the entire reason for using indicators rather than a Gaussian residual, and §12.4 checks it
survived the trend fit.

Parallelise across thresholds; they are independent.

### 10.3 Order-relation correction (`cdf/order.py`)

Kriged indicators are not guaranteed monotone in `k`. Correct by an upward pass
(`p̂_k ← max(p̂_k, p̂_{k−1})`), a downward pass (`p̂_k ← min(p̂_k, p̂_{k+1})`), averaging the two,
then enforcing monotonicity by cumulative maximum, then clipping to [0, 1].

Log the frequency and magnitude of violations. Frequent large violations mean inconsistent
indicator variograms — usually too many thresholds for the data.
`WARNING / ORDER_RELATION_SEVERE` above a configurable rate (default: >5% of nodes with a
violation exceeding 0.05).

### 10.4 Tail extrapolation (`cdf/tails.py`)

Indicator kriging provides **no information** beyond the outermost thresholds. The tails are
an **assumption** and must be labelled as one in every output that depends on them.

- **Lower tail:** linear to the declustered minimum.
- **Upper tail:** hyperbolic with ω ∈ [1.5, 3] — `1 − F(z) = λ z^(−ω)`, with
  `λ = (1 − F(z_K)) · z_K^ω`. ω = 1.5 optimistic, ω = 3 conservative, default 2, exposed.

> **Implementation trap.** The hyperbolic model requires **positive support**. Residuals `r`
> are centred at zero and routinely negative, so applying it to `F̂_R` directly is
> ill-defined. Assemble the node CDF in `Z` units first — shift by the trend, back-transform
> through §5.3 — and *then* apply the hyperbolic upper tail. If the back-transformed target
> can still be non-positive, fall back to an exponential tail and emit
> `INFO / TAIL_MODEL_FALLBACK`. Offer `upper_tail = "hyperbolic" | "exponential" | "linear"`.

Any reported P95 or P99 is dominated by this choice. `LocalCDF.tail_dependent(q)` exists so
that every surface derived from a tail-dependent quantile can say so — in the layer name, in
the legend, and in the MCP caption (`07` §9.10).

### 10.5 Assembling the output CDF (`cdf/local.py`)

```
F̂_Z(z | x) = F̂_R( z − f(X(x); θ̂) | x )
```

Evaluating the trend at grid nodes needs covariates at grid nodes. Two supported modes:

1. **Reference scenario** — the common case. Fix user-specified covariate values across all
   nodes, isolating the spatial signal from the covariate effect: *"the target at a reference
   configuration"*. For acreage evaluation this is usually the deliverable.
2. **Covariate rasters** — per-node covariate grids, for a planned design that varies by area.
   Run §5.4's extrapolation check against these.

Back-transform quantiles through §5.3. Exact, no correction.

---

## 11. Grid, neighbourhood, and the estimator suite

### 11.0 Auto-selection

| Parameter | Auto rule | Override |
|---|---|---|
| Cell size | ¼ to ⅕ of the shortest fitted range, capped by `SOFT_CELL_LIMIT` | `GridDefinition(cell_size=)` |
| Extent | Convex hull of samples, buffered by one range | `"bbox"`, `"alpha_hull"`, or a Polygon |
| Search radius | 1.0–1.5 × the longest range | `neighborhood(radius=)` |
| Min / max neighbours | 8 / 40 | `neighborhood(min_n=, max_n=)` |
| Search ellipse | From the fitted anisotropy (§7.6) | explicit ratio/azimuth |
| Sector search | Off; quadrant/octant available | `sectors=4|8`, `per_sector=` |
| Extrapolation mask | Nodes further than `mask_distance` (default 1 × range) from any sample | `mask_distance=` |

Below `min_n` neighbours a node is **unestimated**, not extrapolated. `05` §6.5 already
reports the extrapolated fraction; this makes it a mask rather than only a number.

### 11.1 Estimators

All share the neighbourhood assembly and solve path: KD-tree search, per-node system,
Cholesky with a fallback to `lstsq` on singular systems — emitting `INFO / SINGULAR_SYSTEM`
with a **count**, not one flag per node.

| Estimator | Notes |
|---|---|
| Simple kriging | Known mean. Needed for the dual-kriging LOO identity (§12.1). |
| Ordinary kriging | Default. Already exists (`05` §6.2). |
| Universal / KED | Polynomial drift in coordinates and/or an external drift raster. **Distinct from RK:** UK solves trend and residual jointly per neighbourhood; RK fits a global trend first. Callers conflate them, so both docstrings say so. |
| Indicator kriging | Standalone, and as RIK Stage 2. |
| Block kriging | Point discretisation of the block (default 4×4), averaging covariances. Continuous estimators only — a block average of indicators is not an indicator, so `ERROR / BLOCK_INDICATOR_UNSUPPORTED`. |
| RK | §10.0. |
| RIK | §8 + §10. |
| SGS | Via `gstools` on normal-score-transformed data. Baseline 4 in §13 and the source of realisation-based connectivity statistics. |

**None of these is fault-aware.** `05` §6.2 already records that kriging is Euclidean here,
that supplying a fault network with a kriging method warns rather than blocks, and that
minimum curvature is the fault-aware method. That applies to every row above.

### 11.2 Kriging variance for RK

Report the residual kriging variance `σ²_K(x)` by default. Optionally add the trend
contribution by the delta method:

```
Var[Ẑ(x)] ≈ σ²_K(x) + g(x)ᵀ cov(θ̂) g(x),    g(x) = ∂f/∂θ at X(x)
```

Off by default — it is an approximation for nonlinear `f` — exposed as
`include_trend_uncertainty=True`, and labelled approximate wherever it is reported.

### 11.3 Performance

- Neighbourhood kriging is O(n_nodes · m³) with `m ≤ max_n`, trivially parallel over nodes.
  Threads are fine; the solve releases the GIL through BLAS.
- **A coarse preview mode** (`preview=True`, default 64×64) that runs in well under a second,
  so the frontend can re-render while a user drags a variogram parameter. Full-resolution runs
  go through the job queue (`10-jobs-async.md`), which already owns progress and cancellation.
- Cache: empirical variogram points keyed on (data hash, binning params); Cholesky
  factorisations keyed on φ; KD-tree keyed on coords hash.
- `SOFT_CELL_LIMIT` already refuses oversized grids with the cell size that would fit.

---

## 12. Diagnostics

All **read-only**. Nothing here modifies θ̂ or the estimated CDFs. Their purpose is to show
where the global trend is working and where it is straining.

Each returns arrays and numbers with a stable `figure` key; the frontend draws them (§0).

### 12.1 Standardised residual map — *"where is the trend optimistic or conservative?"*

Under a correct trend the residual field is mean-zero everywhere. Persistent, spatially
coherent departures mean trend misspecification.

```
T(x) = R̂(x) / σ_K(x)
```

`T > 0`: the trend under-predicts — the area outperforms what its covariates explain, so the
model is **conservative** there. `T < 0`: **optimistic**. `|T| > 2` over an area larger than
the variogram range: the global law is systematically off there.

Two implementation rules:

- **Use leave-one-out kriged residuals.** In-sample residuals collapse to zero at sample
  locations and produce a falsely clean map. LOO comes cheaply from the dual-kriging system —
  one factorisation, `e_i = α_i / (C⁻¹)_ii`. **That identity is exact for simple kriging with
  a global neighbourhood.** With ordinary kriging or a moving neighbourhood it is an
  approximation; label it as such and offer exact k-fold as an override for small `n`.
- **Threshold on area, not only magnitude.** A single anomalous sample is noise; a patch
  spanning several correlation lengths is signal. Label connected components of `|T| > 2` and
  discard those smaller than about one range².

### 12.2 Covariate spread map — *"where can we not tell?"*

§12.1 shows where the trend is wrong. It does not show where you **cannot determine** whether
it is wrong, and that is the map that matters for attribution.

Per node, compute the spread of covariate values among the `k` nearest samples — condition
number of the local design matrix, or (simpler and adequate) the IQR of the primary covariate
relative to the field-wide IQR.

Where spread ≈ 0, every nearby sample had the same covariate values. The trend's covariate
terms are being **extrapolated** into that area, not tested there, and any apparent covariate
effect is imported from elsewhere in the field.

### 12.3 Joint interpretation panel — the headline

Overlay §12.1 and §12.2 into a four-way classification:

| | Low covariate spread | Good covariate spread |
|---|---|---|
| **Small \|T\|** | Trend fits, but untested — extrapolated | **Trust it** — fits and is tested |
| **Large \|T\|** | Ambiguous — spatial signal or unmodelled covariate effect | Real spatial effect the covariates cannot explain |

Only the bottom-right cell supports a confident statement about the field net of covariates.
The **top-left** is where users will most confidently misread the map, and is the most
valuable region to shade.

### 12.4 Variance budget — the single most consequential check

Whatever the trend explains, the spatial model does not see. If the covariate set contains
proxies for location or for the underlying property, the trend absorbs the signal, the
residual comes back near-white, and the map is driven by the regression rather than by the
geostatistics.

**Test:** compute indicator variograms of raw `z` and of `r` at the **same quantile
thresholds**, and compare the **range contrast** between the high threshold and the median
threshold.

- Contrast preserved in `r` → the split is working.
- Contrast collapses → `WARNING / TREND_ABSORBED`: *"trend absorbed spatial structure — check
  whether covariates are proxies for location."*

A non-expert will never detect this unaided. It belongs in the text output at high severity,
with the paired variogram plot as supporting evidence — not the other way round.

### 12.5 Trend-versus-residual variance ratio — *"covariates or field?"*

Map `Var(f(X; θ̂))` and `Var(R̂)` locally in the same units, as two panels or one ratio map
centred at 1. Trend variance dominant → variation here is driven by the covariates
(engineering and design). Residual variance dominant → driven by the underlying spatial
property (geology). This answers the question users actually have more directly than the
target map does.

### 12.6 Covariate–residual confound report

For each covariate, correlate it with the kriged residual field at sample locations. A strong
correlation means covariate intensity is spatially aligned with the underlying property —
*operators complete good rock harder* — and **the coefficient split between covariates and
spatial signal is not trustworthy**.

This confound **cannot be resolved by any diagnostic**. It can only be measured and displayed.
One number per covariate, prominently: `WARNING / COVAR_CONFOUND` with
`detail={"prop_per_ft": 0.61, ...}`.

### 12.7 Standard panels

Empirical and fitted variograms (continuous and per-threshold indicator) on one figure; the
coefficient trajectory across loop iterations (§8.4's diagnosis, free because it is recorded
anyway); histogram reproduction, declustered sample CDF against grid CDF; swath plots N–S and
E–W, data against map; P10/P50/P90 and `P(Z > threshold)` maps; **tail-dependence annotation**
on any quantile flagged by `LocalCDF.tail_dependent(q)`; and connectivity — largest connected
component of the thresholded map against the same statistic from the samples and from SGS
realisations, which directly tests the motivation for using indicators at all.

---

## 13. Validation

**RMSE is the wrong headline metric** and will not detect any of the failure modes above. It
also penalises exactly the de-smoothing that indicator methods are designed to produce.
Compute it; never lead with it.

**Holdout is spatial-block or by-group, never random.** Clustered samples make random holdout
leak badly and every configuration look excellent. This is a **hard default, not a tunable** —
`splits.random()` exists and emits `WARNING / RANDOM_SPLIT_USED` every time it is called.

| Metric | What it detects |
|---|---|
| **CRPS** at held-out samples | Overall distributional accuracy; the proper scoring rule for a CDF output. Computed from the discrete `LocalCDF` by exact piecewise integration, not by sampling. |
| **PIT histogram** | Calibration. Uniform = calibrated; U-shaped = overconfident; dome = underconfident. |
| **Threshold accuracy** | Of nodes assigned `P(Z > z_k) ≈ 0.8`, do ~80% of held-out samples exceed `z_k`? Reported as a reliability curve. |
| **Krige's regression slope** | Conditional bias. Regress true on estimated in LOO; kriging gives slope < 1, you want ≈ 1. |
| **Connectivity statistic** | Whether connected-high structure is reproduced. |
| RMSE / MAE / ME | Reported, not headlined. |

**Baselines, all run on the same splits:** trend-only (does spatial modelling help at all?),
kriging-only (do the covariates help at all?), RK with a Gaussian residual (do indicators
help?), and SGS (reproduces histogram and variogram by construction).

**If the trend-only baseline wins, say so at the top.** `WARNING / TREND_ONLY_WINS`. That is
the most important thing the tool can tell a user and it must not be buried under a map that
looks impressive.

---

## 14. API surface

### 14.1 In-process

`webmap_geo` must be fully usable from a notebook with no API and no database present
(`CLAUDE.md` §3.5), so this is the primary surface and the job wraps it.

```python
from webmap_geo.estimators import RegressionIndicatorKriging
from webmap_geo.control import ControlPoints
from webmap_geo.grid import GridDefinition

rik = RegressionIndicatorKriging(
    trend="saturating",                # preset name | expression string | callable
    covariates=["prop_per_ft", "fluid_per_ft", "spacing_ft", "lat_len_ft"],
    target_transform="log",
    n_thresholds=7,
    upper_tail="hyperbolic",
    upper_tail_omega=2.0,
    rng=np.random.default_rng(20260909),
)

fitted = rik.fit(points)                                   # Stage 1 + Stage 2 variograms
result = fitted.predict(
    GridDefinition.auto(points, fitted.variogram),
    scenario={"prop_per_ft": 2000, "lat_len_ft": 10000},
)

result.surface("p50")            # "p10" | "p90" | any q | "mean" | "kriging_std" | "T"
result.surface("prob_above", threshold=1.2e6)
result.local_cdf                 # LocalCDF, the thing everything else derives from
result.flags                     # FlagCollection
result.diagnostics               # arrays + numbers + figure keys
result.validate(splits="spatial_block", baselines="all")
```

Every estimator in §11.1 shares `fit / predict / surface / flags / diagnostics / validate`.

### 14.2 Overrides

```python
rik.advanced.variogram(model="matern", nu=1.5, nugget=0.02, range_=8500,
                       anisotropy=(0.45, 35.0), lags=18, max_lag=45000)
rik.advanced.neighborhood(min_n=12, max_n=48, radius=12000, sectors=4)
rik.advanced.thresholds([...])
rik.advanced.trend(method="ols", max_iter=3, tol=1e-5)
```

Any `advanced.*` call records the override **alongside the auto value it displaced**, and both
reach the lineage record and the UI.

### 14.3 As a job

Full-resolution runs go through the existing job path (`10-jobs-async.md`): a
`KrigingRequest` submitted at `POST /api/v1/jobs/krige`, executed by the worker as the
requesting principal, with progress phases weighted by measured duration and cancellation
checked before anything becomes visible.

The phases are real and known in advance, which is what makes progress here informative
rather than a spinner:

```
validate → decluster → transform → trend iteration 1..n → indicator variogram k of K
→ kriging → diagnostics → validation
```

The result document carries the output `dataset_id` (the multi-band local-CDF grid), the
derived surfaces requested, the flags, and the validation summary.

### 14.4 Output

Grids leave as arrays for the existing storage path — float32, `NaN` where unestimated, with
the frame and bounds — and are registered as datasets with lineage by `webmap_core`. The
local CDF is a multi-band grid (§4.4). Nothing here writes a file.

`surface()` always returns a masked array with `NaN` where unestimated, and the tile endpoint
already renders `NaN` transparent (`08` §5.2) — which is why §1's "never estimate where there
is no data" costs nothing to display, provided the masked area is drawn distinctly from a low
value rather than at the bottom of the ramp (`07` §9.10).

---

## 15. Petroleum presets

The only place domain knowledge lives. Nothing in the core imports it.

- **Trend forms** for completion response: sublinear lateral-length scaling `L^α` with α
  bounded to [0.5, 1.0], saturating proppant and fluid intensity terms, and a spacing term,
  with sensible `theta0` and bounds.
- **`resolve_locations()`** — bottomhole or lateral-midpoint resolution, warning when surface
  locations are detected.
- **Unit conventions** — proppant and fluid as per-foot intensities; the target *not*
  normalised by lateral length (§5.3), with `WARNING / TARGET_LENGTH_NORMALIZED` if violated.
- **Named covariate hints** so §5.4 can raise a more specific `COVAR_SPATIAL_PROXY` when
  `vintage` or `operator` appear.
- **Reference-completion helper** for building the §10.5 scenario.
- Default reporting labels — "EUR at reference completion", "P(EUR > X)".

---

## 16. Flag registry

Stable codes. Each has a severity, a message template, a docs anchor, and optionally a figure
key. This is the single registry for the whole of `webmap_geo`; the free-text warnings in
`interpolate/dispatch.py` migrate into it rather than living beside it.

| Code | Severity | Trigger |
|---|---|---|
| `COORD_DEGREES` | ERROR | Geographic frame reached geoprocessing (`CrsContext`, §5.1) |
| `INSUFFICIENT_DATA` | ERROR | Below `min_samples`, or degenerate geometry |
| `TREND_NONFINITE` | ERROR | `f` returns NaN/Inf in range (§8.4) |
| `TOO_FEW_THRESHOLDS` | ERROR | Fewer than 3 thresholds survive the count guard (§10.1) |
| `BLOCK_INDICATOR_UNSUPPORTED` | ERROR | Block kriging requested for indicators (§11.1) |
| `TREND_ABSORBED` | WARNING | Indicator range contrast collapses (§12.4) |
| `COVAR_CONFOUND` | WARNING | Covariate correlates with the kriged residual (§12.6) |
| `COVAR_COLLINEAR` | WARNING | High condition number (§5.4) |
| `COVAR_SPATIAL_PROXY` | WARNING | Covariate is effectively a spatial label (§5.4) |
| `COVAR_EXTRAPOLATION` | WARNING | Scenario outside the covariate hull (§5.4) |
| `TREND_NOT_CONVERGED` | WARNING | Loop hit the iteration cap or oscillated (§8.4) |
| `TREND_UNIDENTIFIED` | WARNING | `JᵀJ` ill-conditioned (§8.4) |
| `TREND_NONMONOTONE` | WARNING | Sign reversal in ∂f/∂x within range (§8.4) |
| `HIGH_NUGGET` | WARNING | nugget/sill > 0.6 (§7.5) |
| `ORDER_RELATION_SEVERE` | WARNING | Violation rate above threshold (§10.3) |
| `TREND_ONLY_WINS` | WARNING | The trend-only baseline beats the full model (§13) |
| `RANDOM_SPLIT_USED` | WARNING | Non-spatial CV requested (§13) |
| `SR_UNSTABLE` | WARNING | Discovered structure varies across seeds (§9) |
| `TARGET_LENGTH_NORMALIZED` | WARNING | Petroleum unit trap (§5.3) |
| `EXTRAPOLATED_FRACTION` | WARNING | Existing `05` §6.5 diagnostic, now a coded flag |
| `THRESHOLD_DROPPED` | INFO | Count guard removed a threshold (§10.1) |
| `ANISO_NOT_SIGNIFICANT` | INFO | Bootstrap test failed; isotropic used (§7.6) |
| `DUPLICATES_RESOLVED` | INFO | Collocated points merged (§5.1) |
| `ROWS_DROPPED` | INFO | NaN policy removed rows (§5.1) |
| `TREND_SUBSET_FIT` | INFO | Trend fitted on a subset (§8.2) |
| `TAIL_MODEL_FALLBACK` | INFO | Hyperbolic tail unusable; fell back (§10.4) |
| `SINGULAR_SYSTEM` | INFO | Kriging systems solved by `lstsq`, with a count (§11.1) |

---

## 17. Automation boundary

**Safe to automate.** Variogram model selection by REML likelihood; declustering cell size;
threshold placement at declustered quantiles with a count guard; anisotropy detection with a
significance test; grid and neighbourhood sizing; multi-start initialisation of θ; the
GLS/REML loop with a convergence check and iteration cap; order-relation correction.

**Must be surfaced, never hidden.** Variance-budget collapse (§12.4); non-monotone trend
response (§8.4); covariate extrapolation (§5.4); loop non-convergence (§8.4); high
nugget-to-sill ratio (§7.5); dropped thresholds (§10.1); tail-extrapolation dependence of
extreme quantiles (§10.4); the trend-only baseline winning (§13).

**Cannot be automated.**

*Whether the covariates are exogenous.* If operators complete good rock harder, the global law
attributes rock quality to proppant, and no diagnostic distinguishes that from a real proppant
effect. The obligation is to **measure and display** the confound (§12.6), not to resolve it.

*Whether to split the field.* Large coherent `|T|` regions (§12.1) indicate the global-trend
assumption straining. Splitting into separate runs is a deliberate user decision informed by
domain knowledge, never something the algorithm does silently.

---

## 18. Testing

`CLAUDE.md` §6.1 puts `webmap_geo` at the highest bar in the repository: reference comparison
plus property tests. That applies here in full.

| Layer | Tests |
|---|---|
| **Analytic** | Kriging a known covariance field reproduces exact weights for small hand-computable systems. SK with a global neighbourhood interpolates exactly at data points, with zero variance. |
| **Synthetic recovery** | A field from a known `VariogramModel` plus a known trend `f(X; θ_true)`: the §8.1 loop recovers θ_true within its standard errors, and REML recovers range/sill/nugget with **less bias than WLS**. Required by [`adr/0011`](adr/0011-reml-for-trend-residual-variograms.md); in CI. |
| **Bias demonstration** | The same setup fitted by method of moments, asserting the range is materially underestimated relative to REML. This test exists to fail if someone later "simplifies" §7.4 away. |
| **Order relations** | Random non-monotone probability vectors → corrected output is monotone, in [0, 1], and within a bounded distance of the input. Property test. |
| **Tails** | The hyperbolic tail integrates to 1, matches at `z_K`, and is monotone. Negative-support input triggers the fallback. |
| **CRPS / PIT** | Against closed-form Gaussian CRPS; PIT of a correctly specified Gaussian model is uniform by KS test. |
| **Declustering** | Cross-checked against `geostatspy` on a GSLIB dataset (`[ref]` extra). |
| **Splits** | No block-CV fold has a training point within the block buffer of a test point. |
| **Determinism** | Two runs from the same lineage parameters produce bit-identical arrays. |
| **Golden runs** | One petroleum-shaped and one environmental-shaped synthetic dataset with checked-in expected flags and summary statistics. `tests/fixtures/` is synthetic only (`CLAUDE.md` §7.5). |

---

## 19. Build order

Ordered so that each step is independently testable and so nothing unsafe is reachable early.

1. **Foundations** — `flags.py` and the §16 registry; `prep/` (validate, decluster, transform,
   screen); the `ControlPoints` and `VariogramModel` extensions of §4. Cross-check
   declustering against a reference.
2. **Variography** — directional variograms and variogram maps, nested and Matérn models,
   auto-selection, anisotropy bootstrap, and the cheap re-evaluation path §7.7 requires.
3. **Kriging engine** — neighbourhood search, simple kriging, `GridDefinition.auto`, masks,
   block kriging. Analytic tests.
4. **REML** — `fit_reml.py` plus the synthetic-recovery and bias-demonstration tests. **Before
   the trend loop**; the loop is unsafe without it.
5. **Trend** — forms, sandboxed parser, GLS, the loop, failure handling.
6. **RK** — Stage 1 + OK of residuals + scenario evaluation + back-transform. **First
   end-to-end estimator and the first genuinely useful deliverable.**
7. **Indicator machinery** — thresholds, per-threshold IK, order relations, tails, `LocalCDF`,
   and the multi-band grid it persists as.
8. **RIK** — compose 5 and 7. **The target.**
9. **Validation** — spatial splits, CRPS, PIT, threshold accuracy, Krige slope, baselines.
10. **Diagnostics** — §12 in order; §12.4 before any cosmetic panel.
11. **UK/KED, SGS, connectivity statistics.**
12. **The job, the API route and the MCP tool.**
13. **Petroleum presets.**
14. **Performance** — parallel node loop, preview mode, tapering and the subset path above
    10k samples, caching.

---

## 20. References

- Journel, A.G. (1983). Nonparametric estimation of spatial distributions. *Mathematical
  Geology* — indicator kriging.
- Deutsch, C.V. & Journel, A.G. (1998). *GSLIB*, 2nd ed. — declustering, order-relation
  correction, tail models.
- Goovaerts, P. (1997). *Geostatistics for Natural Resources Evaluation* — indicator
  formalism, local CDF assembly.
- Yamamoto, J.K. (2005). Correcting the smoothing effect of ordinary kriging estimates.
  *Mathematical Geology*.
- Patterson, H.D. & Thompson, R. (1971). Recovery of inter-block information when block sizes
  are unequal. *Biometrika* — REML.
- Cressie, N. (1993). *Statistics for Spatial Data*, rev. ed. — bias of residual variograms
  under fitted trends; GLS estimation.
- Gneiting, T. & Raftery, A.E. (2007). Strictly proper scoring rules, prediction, and
  estimation. *JASA* — CRPS.
- Roberts, D.R. et al. (2017). Cross-validation strategies for data with spatial, temporal,
  phylogenetic or spatial-temporal structure. *Ecography* — spatial block CV.
- Cranmer, M. (2023). Interpretable machine learning for science with PySR and
  SymbolicRegression.jl.
