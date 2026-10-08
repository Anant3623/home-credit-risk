# Feature dictionary and aggregation rules

The raw tables retain original Kaggle column names, values, and rows. The staging views select analytic fields, clean their names, and convert documented sentinel/category anomalies to `NULL`. Intermediate tables restrict history to applicants in `application_train`. Missing history and an observed clean history are different: mart joins may fill **absent count features** with zero, but should retain `NULL` for absent amounts, measured delinquency, and ratios. Never use applicant/loan IDs as predictors.

## Time and missingness

The current application is relative day/month **0**. `DAYS_*` and `MONTHS_BALANCE` are relative offsets; negative values are earlier observations. Historical decisions, loan originations, installment due dates, observed payments, and account monthly snapshots must be known at or before 0. Positive historical observations are excluded from intermediate score features and counted in `int_history_exclusion_audit`. Known future contractual maturity dates remain staging fields; they are not future outcomes and are not model inputs in this project.

`365243` is an employment/previous-application date sentinel, not a duration. Employment dates greater than 0 and invalid birth dates become `NULL`. The ordinary categories `XNA`, `XAP`, and empty strings become `NULL`; each source raw value remains available for the DQ report. Negative amounts become `NULL` except credit-card balances: a negative balance can represent an overpayment or account credit and remains visible. Credit-card debt utilization uses `MAX(balance, 0)` over a positive credit limit; a missing balance stays missing. Ratios with zero/missing denominators are `NULL`. Large positive incomes are flagged by DQ rather than removed by an arbitrary global threshold; any model winsorization must be learned from its training split.

All numeric comparisons and arithmetic explicitly cast source values to PostgreSQL `NUMERIC`, so conservative raw table type inference or valid-numeric `TEXT` fallback does not change feature semantics. Non-numeric corruption in a required numeric field must be resolved or explicitly quarantined; it is not silently coerced to zero.

## Application fields — `staging.application`

| Field | Definition |
|---|---|
| `applicant_id`, `target` | `SK_ID_CURR`, labelled repayment-difficulty indicator `TARGET`; target is an observed outcome, never a predictor. |
| `age_years` | `-DAYS_BIRTH / 365.25`; birth day must be in [-36525, -1]. |
| `employed_years`, `is_employed_missing` | `-DAYS_EMPLOYED / 365.25`; missing, sentinel, positive dates, and durations beyond 100 years produce missing duration and flag 1. |
| `is_age_invalid`, `is_income_invalid` | Invalid supplied birth day or nonpositive supplied income; raw missingness remains distinguishable. |
| `income_amount`, `credit_amount`, `annuity_amount`, `goods_amount` | Cleaned `AMT_INCOME_TOTAL`, `AMT_CREDIT`, `AMT_ANNUITY`, `AMT_GOODS_PRICE`; income must be positive and other amounts nonnegative. |
| `credit_to_income`, `annuity_to_income`, `credit_to_goods` | Credit/income, annuity/income, credit/goods. Amounts have dataset currency units; ratios are dimensionless. |
| `ext_source_1`, `ext_source_2`, `ext_source_3` | External normalized scores in [0, 1]. Provenance/production availability is not established by this dataset. |
| `contract_type`, `gender`, `owns_car`, `owns_realty` | Current application contract, sex category, and ownership flags; cleaned categorical inputs. |
| `income_type`, `education_type`, `family_status`, `housing_type`, `occupation_type` | Applicant profile categories; placeholders become missing. |
| `children_count`, `family_members_count` | Nonnegative child count and family count of at least one. |
| `region_rating`, `region_rating_city` | Region and city-aware region ratings, valid values 1–3. |
| `days_birth`, `days_employed`, `days_registration`, `days_id_publish`, `days_last_phone_change` | Cleaned relative day measures; original values remain raw. |

## Payment records → EMI → previous loan → applicant

`staging.installments_payments` has **one row per recorded payment**, not necessarily one EMI. The EMI key is `(applicant_id, previous_loan_id, installment_version, installment_number)`. Version is required because installment numbers can recur under revised schedules. Due day is audited as an attribute of this key; it does not split a conflicting EMI into two obligations.

`int_installment` sums valid, dated payments at or before day 0, groups same-day payments into one event, then computes cumulative payments in date order. The settlement day is the **first** day the cumulative amount meets the scheduled obligation, allowing a 0.01 currency-unit tolerance. It uses `MAX(scheduled_amount)` once per EMI, so repeated scheduled amounts on partial payment rows are not added. A zero obligation is settled on its due day. A partially paid EMI remains unsettled.

When conflicting due days/amounts share an EMI key, the table keeps one obligation with earliest valid due day and maximum scheduled amount, and exposes conflict flags. This conservative policy is visible in the audit; ambiguous schedules need source-level reconciliation before production use. No exact source duplicates are silently removed; raw and DQ retain them. A repeated payment record can therefore inflate payments, which is a source-quality limitation rather than an undocumented deduplication assumption.

`days_late` for a settled EMI is `MAX(settlement_day - due_day, 0)`. For an unpaid, already-due EMI it is `MAX(0 - due_day, 0)`: a **censored lower bound**, not its eventual final delay. Missing scheduled amounts produce missing measured lateness. Source `staging.installments_payments.days_late` is the individual payment offset; it is never averaged directly to form an EMI outcome. Missing payment amount/date and excluded future-payment counts are exposed separately. Undated payments cannot establish what was paid as of application date.

| Applicant feature (`int_applicant_repay`) | Definition |
|---|---|
| `prev_loans_with_installments` | Number of distinct previous loans with at least one due EMI. |
| `installment_count` | Due EMI count at application date, with installment versions retained. |
| `settled_installment_count`, `unpaid_installment_count` | Fully settled vs outstanding known obligations as of day 0. Unknown scheduled amounts are neither measurable paid nor unpaid obligations. |
| `late_installment_count`, `installment_late_rate` | Measured EMIs with positive lateness; divided by measured EMI count. Includes overdue unpaid lower bounds. |
| `max_days_late`, `mean_days_late` | Maximum and weighted-by-EMI mean measured lateness, in days. Applicant mean is not an unweighted mean of loan means. |
| `scheduled_installment_amount` | Sum of scheduled amount once per due EMI. |
| `installment_paid_amount`, `installment_outstanding_amount` | Sum of known dated paid amounts and `MAX(scheduled - paid, 0)`; overpayments remain in paid amounts. |
| `measured_installment_paid_amount`, `installment_paid_ratio` | Payments for EMIs with known scheduled amounts; their sum / known scheduled amount. Can exceed 1 with overpayments/source duplicate records. |
| `missing_payment_installment_count` | EMIs with any missing/invalid payment amount or date record. |
| `due_day_conflict_installment_count`, `amount_conflict_installment_count`, `missing_scheduled_installment_count` | Audit counts for ambiguous or unknown obligations. |
| `future_payment_row_count` | Audit count of excluded payments after the current application; **not a score predictor**. |

`int_prev_loan_repay` is keyed by previous loan and includes the same loan-level measures plus numerator/denominator totals required to form correctly weighted applicant features. Unique indexes and cross-source owner assertions enforce that one previous loan belongs to one applicant.

## Bureau history

The monthly bureau status is a bucket rather than an exact observed delinquency day:

| Source `STATUS` | `dpd_bucket` | `dpd_lower_bound_days` |
|---|---|---:|
| `0` | Current | 0 |
| `1` | 1-30 | 1 |
| `2` | 31-60 | 31 |
| `3` | 61-90 | 61 |
| `4` | 91-120 | 91 |
| `5` | 120+ | 121 |
| `C` | Closed | NULL |
| `X`, unsupported, missing | Unknown | NULL |

Closed/unknown states are not inferred to have zero delinquency. `int_bureau_month` has one row per bureau loan and relative month; repeated records choose the worst known DPD. If no known DPD exists, it chooses Closed only when all records are C; otherwise Unknown. Source row counts and mixed-status conflicts are audited. `int_bureau_loan` first aggregates months to one bureau loan, then `int_applicant_bureau` aggregates loans to applicants.

| Applicant feature (`int_applicant_bureau`) | Definition |
|---|---|
| `bureau_loan_count`, `bureau_active_loan_count`, `bureau_closed_loan_count` | Originated bureau loan counts; Active and Closed use bureau loan metadata. |
| `bureau_credit_sum`, `bureau_debt_sum`, `bureau_overdue_sum` | Sums of nonnegative loan credit, outstanding debt, and overdue balances. |
| `bureau_max_dpd` | Maximum of monthly worst lower-bound days and bureau metadata overdue days. |
| `bureau_months_31plus` | Count of loan-months with bucket 31-60 or worse. The 1-30 bucket cannot identify exactly 30-day arrears, so a precise 30+ count is unavailable. |
| `bureau_credit_to_debt_ratio` | Sum of credit amounts / sum of positive debt amounts; zero debt denominator is missing. |
| `bureau_observed_month_count`, `bureau_unknown_status_month_count`, `bureau_status_conflict_month_count` | History coverage and quality counts; measured at loan-month grain. |

Roll rates must use `LAG(relative_month)` **and** `LAG(dpd_bucket)` within each bureau loan and include only month differences of 1. A gap from month -3 to -1 is not a one-month transition. The marts build this from `int_bureau_month`; no applicant-level join is used to multiply monthly histories.

## Previous applications, POS and credit cards

| Table / features | Definition |
|---|---|
| `int_applicant_prev.previous_application_count`, `previous_approved_count`, `previous_refused_count`, `previous_refusal_rate` | Previous decisions known by day 0. Refusal rate = refused / all historical applications, including canceled/unused offers in the denominator. |
| `previous_credit_mean`, `previous_credit_sum`, `previous_annuity_mean`, `previous_latest_decision_day` | Previous offered credit/annuity aggregates and latest known decision day; past refusals are features, not current rejected-applicant outcome labels. |
| `int_applicant_pos.pos_loan_count`, `pos_month_count` | Distinct POS loans and account-month observations after account-month aggregation. `pos_month_count` counts loan-months, not distinct calendar months. |
| `pos_max_dpd`, `pos_max_dpd_def`, `pos_delinquent_month_rate` | Maximum POS overdue days/definitive overdue days; months with DPD > 0 / months with observed DPD. |
| `int_applicant_cc.cc_loan_count`, `cc_month_count` | Distinct card loans and loan-month observations. |
| `cc_max_dpd`, `cc_max_dpd_def` | Maximum card overdue days/definitive overdue days. |
| `cc_utilization_mean`, `cc_utilization_max` | Mean over observed loan-month utilization and maximum utilization. Utilization may exceed 1 with over-limit borrowing; zero limit is missing. |
| `cc_balance_sum_latest`, `cc_limit_sum_latest` | Sum of the latest known balance/limit **per account**, then across applicant accounts. A stale latest record is not guaranteed to represent the exact day-0 balance. |
| `cc_amount_conflict_month_count` | Duplicate account-month records with inconsistent balances/limits. Conservative maximum balance and maximum limit are retained separately and the ambiguity remains explicit. |

`int_pos_month` and `int_cc_month` aggregate account-month duplicates before loan/application summaries. POS DPD uses the maximum. Card account-month amounts use a conservative maximum rather than adding repeated balances/limits; these are stock values, not payment flows. Card mean utilization is weighted by the number of observed months, not equally by loan. All parent/account keys are unique and audited before application-level joins.

## Interpretation limits

These are observational applicant/history features. `TARGET` identifies the dataset's repayment-difficulty definition, not a full contractual lifetime loss amount. Loan credit is an EAD proxy and assumed LGD is a strategy scenario, not an observed loss estimate. Bureau `credit_currency` remains available, but aggregate monetary amounts assume comparable reporting units; anonymous currency codes supply no FX rates, so these sums need currency validation/conversion before production use. Current labelled applications have known outcomes within the dataset's selected borrower population; outcomes for current rejected applicants are unavailable. Previous refusals do not solve **reject inference**. Cut-off simulations describe this labelled population and should not be claimed as unbiased forecasts for previously rejected or future applicants.
