# 0011 — Fit trend-residual variograms by REML, not by least squares

## Status

Accepted — 2026-09-09

## Context

`05-geoprocessing.md` §6.3 specifies one variogram fitter: weighted least squares against
the binned experimental points, short lags weighted more heavily. That is the right fitter,
and it stays the right fitter, for a variogram of **raw data** — ordinary kriging, indicator
kriging, anything whose mean is a single unknown constant.

`13-kriging.md` introduces regression kriging and regression indicator kriging, which fit a
global trend `f(X; θ)` first and then krige the residual `r = z − f(X; θ̂)`. Fitting the
residual's variogram by the same least-squares path is where this goes wrong, and it goes
wrong quietly.

**Residuals are not the errors.** They are the errors minus whatever the fitted trend
absorbed, and that absorption is systematic rather than random. The consequence is that a
method-of-moments or WLS variogram of a fitted residual **underestimates both sill and
range**, with the bias growing at long lags and with the number of coefficients `p`. This is
standard (Cressie 1993), and it is not a small effect at the coefficient counts a completion-
response trend uses.

Inside the GLS ↔ variogram loop of `13` §8.1 the bias is self-reinforcing:

> flattened variogram → covariance looks near-diagonal → GLS behaves like OLS → the trend
> absorbs more long-range structure → the next variogram is flatter still

The loop converges. It converges happily. It can converge onto a trend that has eaten the
spatial signal, and the output is a map driven by the regression with the geostatistics along
for the ride. Nothing in the run reports a problem: the fit residual is small, the
coefficients look sensible, and the map looks like a map.

The failure is invisible in exactly the way this repository keeps finding failures — it
produces plausible output, and a geologist puts it in a partner deck.

## Decision

**Two fitters, chosen by what the variogram is of, never by preference.**

| Variogram of | Fitter | Why |
|---|---|---|
| Raw data (OK, SK, standalone IK) | WLS (`variogram/fit.py`) | Mean is one unknown constant; no absorption to correct. Fast enough for interactive use. |
| Indicators of an already-detrended residual | WLS | The indicator mean is again a single constant, so §7.4's bias does not arise. |
| **Residual of a fitted trend** (RK, RIK stage 1) | **REML** (`variogram/fit_reml.py`) | Uses error contrasts; removes the absorption bias by construction. |

REML minimises, over covariance parameters `φ`:

```
ℓ(φ) = ½ log|C| + ½ log|Jᵀ C⁻¹ J| + ½ rᵀ P r
P    = C⁻¹ − C⁻¹ J (Jᵀ C⁻¹ J)⁻¹ Jᵀ C⁻¹
```

The middle term is the correction absent from method-of-moments. With a nonlinear trend,
profile it: at the current θ̂, linearise via the Jacobian `J = ∂f/∂θ` and treat `J` as the
design matrix.

**Model selection for a trend residual is by REML likelihood, not by least-squares fit to the
empirical points.** Choosing Matérn over exponential because it hugs the binned points better
is choosing on a statistic that is itself biased here.

Three implementation rules that are part of the decision rather than details:

1. **One Cholesky factorisation serves all three terms.** Never form `C⁻¹`; use `cho_solve`.
   Cache the factorisation keyed on `φ`, because the optimiser re-evaluates nearby points.
2. **Multi-start** from the WLS estimate plus two or three perturbations. REML surfaces are
   not reliably unimodal in range and nugget, and a single start lands in a local minimum
   often enough to matter.
3. **Above ~10,000 samples the dense `C` is the wall** — O(n²) memory, O(n³) time. Switch
   automatically to covariance tapering (Wendland at ~1.5× range, sparse Cholesky) or fit the
   trend on a declustered subset of ~5,000 and apply θ̂ to all samples. Record which path ran.

## Consequences

**A second fitter to maintain**, and a rule about which to use that a contributor can get
wrong. Mitigated by making the choice structural rather than a parameter: the residual path
calls `fit_reml` and there is no argument that switches it to WLS.

**Two tests in CI carry this decision, and they are not optional.**

- *Synthetic recovery.* Generate a field from a known `VariogramModel`, add a known trend
  `f(X; θ_true)`, and assert the §8.1 loop recovers θ_true within its standard errors and
  that REML recovers range, sill and nugget with **less bias than WLS** on the same data.
- *Bias demonstration.* The same setup, fitting the residual variogram by method of moments,
  asserting the range is materially underestimated relative to REML.

The second test exists to fail loudly if someone later "simplifies" this away. Without it,
deleting `fit_reml.py` and pointing the residual path at `fit.py` passes every other test
in the repository and produces subtly wrong maps forever.

**REML is slower than WLS** — an optimisation over two to four parameters, each evaluation a
Cholesky of an n×n matrix. This is why the interactive variogram workbench (`07` §9.5) fits
by WLS while dragging and only runs REML on Apply, and why the preview grid exists.

**Fault-aware kriging is unaffected.** `05` §6.2 already records that kriging is Euclidean
here and that minimum curvature is the fault-aware method. Nothing in this decision changes
that; a trend residual kriged across a fault is as wrong as any other kriged surface across a
fault, and for the same reason.

## References

- Patterson, H.D. & Thompson, R. (1971). Recovery of inter-block information when block sizes
  are unequal. *Biometrika* 58(3).
- Cressie, N. (1993). *Statistics for Spatial Data*, rev. ed. — bias of residual variograms
  under a fitted trend, and GLS estimation.
