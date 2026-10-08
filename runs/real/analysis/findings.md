# Credit-risk analysis findings

**Data status: REAL.** Computed from the supplied applicant feature export.

The export contains 307,511 applicants; 24,825 have TARGET=1. The observed repayment-difficulty rate is 8.07% (95% Wilson interval 7.98%–8.17%). TARGET is a repayment-difficulty proxy, not a verified loss or an independently established default event.

## Model and risk drivers

The scorecard uses transparent train-only Weight of Evidence bins and unweighted regularized logistic regression. Strongest univariate train associations: ext_source_3 (IV 0.303), ext_source_2 (IV 0.278), ext_source_1 (IV 0.140), employed_years (IV 0.108), occupation_type (IV 0.082). Associations and odds ratios are descriptive and conditional, not causal. All feature selection, clipping, rare-level pooling and WoE mappings use only the training 60%; validation (20%) defines decile boundaries and approval thresholds. Test (20%) is reserved for evaluation.

On the untouched test split, ROC AUC is 0.7511, KS is 0.3701, Gini is 0.5023 and Brier score is 0.0685. Mean predicted PD is 8.11%, compared with observed TARGET rate 8.07%. Inspect the calibration curve and bin counts; rare events and small bands have wide intervals. These are point estimates on one random split, with no temporal validation. The test set was not used to calibrate, select features, tune the model, define bands or choose cutoffs.

Risk bands ascend in predicted risk by construction. Observed rates are audited rather than forced into monotonicity: validation: 0 observed adjacent nonempty-band reversals; test: 0 observed adjacent nonempty-band reversals. Repeated prediction thresholds can leave empty bands; all ten band numbers remain in the export. A reversal is reported transparently and does not trigger a refit using test outcomes.

## Stakeholder question: 10% more approvals

The illustrative baseline requests 50% approval on validation predictions. Ten percent more approvals **relative** requests 55%; ten **percentage points** requests 60%, and both scenarios are exported. On test, the fixed 50%/55% validation thresholds approve 49.71%/54.86% of the accepted cohort. The observed bad rate changes from 2.92% to 3.21%, a change of +0.29 percentage points. Scenario expected loss changes by 49,681,610.77 dataset currency units. Actual approvals can differ from requested approvals because threshold ties and population variation are retained; no test outcomes determine the thresholds. See cutoff_table.csv for rate confidence intervals and strategy_comparisons.csv for both interpretations.

## Interpretation limits

- **Reject inference:** outcomes for rejected current applicants are unavailable. These retrospective policies re-rank the observed accepted cohort; they do not estimate the outcome of accepting previously rejected customers. Historical refused applications supply behavior features, not outcomes of rejected current applications.
- **Expected loss:** PD × assumed LGD 45% × EAD. EAD uses nonnegative current credit amount as a proxy; missing/invalid EAD produces missing applicant EL and is excluded from known-EAD portfolio subtotals. The export has 0 missing EAD values. Cutoff and band tables report known/missing EAD counts and EL coverage. A nonempty portfolio with entirely missing EAD has unavailable EL, and its strategy comparison remains unavailable; an empty approved portfolio has zero EL. `expected_loss_per_approved` divides the known-EAD EL subtotal by all approved applicants, while `expected_loss_per_approved_known_ead` divides it by applicants with known EAD. Neither is a complete-portfolio estimate when coverage is below 100%. LGD is a configurable scenario assumption, not a value learned from recoveries. These figures are not calibrated accounting loss estimates or a claim about currency conversion.
- **Time and population:** the data do not provide a usable current-application calendar for this pipeline, so random stratified validation cannot establish future performance. Relative-day/month history must already be filtered at the application observation point by SQL.
- **Confidence intervals and tests:** Wilson intervals describe observed binomial TARGET rates; they do not include model, LGD or EAD uncertainty. Chi-square p-values use asymptotic assumptions; sparse expected counts are flagged and all exploratory feature tests use Benjamini–Hochberg correction. Train-selected continuous bins make these tests exploratory. Large samples can make weak effects significant; inspect Cramér's V.
- **Score units:** score 600 represents good:bad odds 50:1 and 20 points double those odds. Higher scores are safer. Odds ratios are per +1 WoE (log good-distribution/bad-distribution), not per year or currency unit. Positive/negative coefficients can be affected by correlated features.
- **Selection and fairness:** historical lending policy shapes this cohort. The model is an educational demonstration and has not been reviewed for fairness, credit-policy compliance or live lending use.

## Reproducibility

Input SHA-256: `e9729cd0faf80ccb960a7eceb446842260947bfa31961a91c96c05d085544f69`. Random seed: 42. Model: woe-logistic-v1. Training-selected features: 25. Preprocessor and model are saved together; model_manifest.json records split sizes, clipping rules, WoE interpretation and nine validation boundaries. No manual outcome-based reordering or rejection-outcome imputation was performed.
