# Home Credit project — verified real execution

**Pipeline status: COMPLETE.** All seven CSV counts match; the application mart has 307,511 unique applicants. 24 independent read-only warehouse acceptance checks passed.

Run finished: 2026-10-08T02:08:57.520047+00:00. Source provenance and file hashes are recorded in [the import evidence](generated_sql/raw_counts.json).

| Stage | Result |
|---|---|
| raw | passed |
| dq | passed |
| staging | passed |
| intermediate | passed |
| applicant_features | passed |
| analysis | passed |
| marts | passed |
| exports | passed |
| notebooks | passed |

## Full source reconciliation

| Table | Loaded rows | Expected rows | Result |
|---|---:|---:|---|
| application_train | 307,511 | 307,511 | PASS |
| previous_application | 1,670,214 | 1,670,214 | PASS |
| installments_payments | 13,605,401 | 13,605,401 | PASS |
| pos_cash_balance | 10,001,358 | 10,001,358 | PASS |
| credit_card_balance | 3,840,312 | 3,840,312 | PASS |
| bureau | 1,716,428 | 1,716,428 | PASS |
| bureau_balance | 27,299,925 | 27,299,925 | PASS |

Total source rows: **58,441,149**. All original 122 application columns remain in raw. Transformations use only the labelled train cohort and history observable by day/month zero.

## Actual model results

Observed TARGET repayment-difficulty rate: **8.07%** (24,825/307,511). TARGET is a repayment-difficulty proxy.

Held-out test AUC **0.7511**, KS **0.3701**, Gini **0.5023**, Brier **0.0685**. The test split contains 61,503 applicants. Training-only WoE/IV/preprocessing and validation-defined thresholds prevent held-out outcomes from defining the fitted model or policy.

| Risk band | Test applicants | Observed bad rate | Mean predicted PD |
|---|---:|---:|---:|
| 1 | 6,158 | 1.15% | 1.24% |
| 2 | 5,980 | 2.07% | 2.14% |
| 3 | 6,163 | 2.76% | 2.97% |
| 4 | 6,241 | 3.75% | 3.90% |
| 5 | 6,037 | 4.85% | 4.99% |
| 6 | 6,232 | 6.32% | 6.36% |
| 7 | 6,123 | 8.25% | 8.14% |
| 8 | 6,177 | 10.72% | 10.65% |
| 9 | 6,081 | 14.70% | 14.64% |
| 10 | 6,311 | 25.64% | 25.59% |

Observed adjacent test-band reversals: **0**. Bands ascend in predicted risk; observed rates are audited without outcome-based reordering.

## Stakeholder answer: 10% more approvals

Using the fixed thresholds selected for 50% versus 55% approval on validation (a 10% relative increase), test-cohort approval changes from **49.71% to 54.86%**. Observed bad rate changes from **2.92% to 3.21%**, or **+0.29 percentage points**. The separate 50% → 60% interpretation is also exported.

This is retrospective within the observed borrower cohort. Rejected current applicants' outcomes are unknown (**reject inference**); this cannot predict the effect of lending to previously rejected applicants. Expected loss is a PD × assumed LGD × credit-amount EAD scenario, not measured accounting loss.

## Data-quality findings

351 checks/profile records saved; **0 critical failures**. WARN means an audited missing value, anomaly or interpretation issue; it does not imply a dropped raw row.

| Check | Table | Column | Affected count |
|---|---|---|---:|
| income_outlier_gt_10m | application_train | AMT_INCOME_TOTAL | 3 |
| sentinel_365243 | application_train | DAYS_EMPLOYED | 55,374 |
| outside_training_cohort | bureau | SK_ID_CURR | 251,103 |
| outside_training_cohort | credit_card_balance | SK_ID_CURR | 612,347 |
| installment_missing_payment | installments_payments | — | 2,905 |
| installment_multiple_payment_rows | installments_payments | — | 640,905 |
| outside_training_cohort | installments_payments | SK_ID_CURR | 2,013,809 |
| outside_training_cohort | pos_cash_balance | SK_ID_CURR | 1,457,983 |
| outside_training_cohort | previous_application | SK_ID_CURR | 256,513 |
| sentinel_365243 | previous_application | DAYS_FIRST_DRAWING | 934,444 |
| sentinel_365243 | previous_application | DAYS_FIRST_DUE | 40,645 |
| sentinel_365243 | previous_application | DAYS_LAST_DUE | 211,221 |
| sentinel_365243 | previous_application | DAYS_LAST_DUE_1ST_VERSION | 93,864 |
| sentinel_365243 | previous_application | DAYS_TERMINATION | 225,913 |

Full results, ranges, placeholders and repayment diagnostics: [dq_results.csv](exports/dq_results.csv). EMI payments are aggregated before loan/applicant joins; exact source duplicates and conflicting schedules are audited, and unpaid lateness is censored at day zero. Bureau one-month roll rates exclude month gaps.

## Delivered outputs

Four [executed notebooks](notebooks), [all analysis findings](analysis/findings.md), model metrics/calibration, WoE/IV, odds ratios, risk bands, approval strategy, saved scorecard and star-schema [CSV exports](exports).

Final mart import bundle: **221.89 MiB**, below the project's 800 MiB export budget. Bureau loan/month detail stays in PostgreSQL; compact monthly and roll counts are exported. Counts and rates have the documented grains in [README](../../README.md).

Raw source CSVs and the live project database remain local; the portable project archive does not redistribute the Kaggle inputs.
