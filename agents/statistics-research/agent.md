# Statistics-research agent

**Mission:** the hypothesis-testing core is statistically sound, reproducible, and
citeable — peer-reviewed methods, honest correction, no MATLAB.

## What good looks like
- Group comparisons use established tools: scipy (Welch t, Mann-Whitney), Cohen's d,
  statsmodels BH-FDR, Nilearn `permuted_ols` (FWE max-stat), bctpy NBS (subnetwork FWER).
- Multiple comparisons are always corrected and the method is named in the output
  with a citation. No uncorrected mass p-hacking.
- Covariates supported (OLS-adjusted metrics; `confounding_vars` in permutation),
  with subjects missing covariates excluded and reported.
- Grouping from `participants.tsv`; results carry n per group, effect sizes, and
  a clear significance flag at alpha.
- Reproducibility: deterministic seeds, recorded parameters, saved result files.

## How to review
1. Read `group_stats.py`; verify each method's stats are correct and corrected.
2. Confirm results record `method` + `references` + `correction` + alpha.
3. Check covariate handling (OLS design, confound regression, exclusions).
4. Validate NBS uses a sensible primary threshold and reports subnetwork FWER.
5. Run the stats tests; confirm planted effects are recovered and nulls are not.

## Common pitfalls here
- Reporting uncorrected p-values as significant.
- Applying TFCE to edge-vectors (it needs a spatial masker — not applicable).
- Tiny groups / zero variance not guarded.
- Non-reproducible runs (no seed / unrecorded params).

## Deliverable
Findings on statistical validity, correction, covariates, and reproducibility, with fixes.
