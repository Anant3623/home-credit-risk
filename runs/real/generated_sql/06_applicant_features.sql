BEGIN;
CREATE SCHEMA IF NOT EXISTS marts;
DROP TABLE IF EXISTS marts.fact_application;
DROP TABLE IF EXISTS marts.applicant_features;
CREATE TABLE marts.applicant_features AS
SELECT a.*,
       COALESCE(r."prev_loans_with_installments", 0) AS "prev_loans_with_installments",
       COALESCE(r."installment_count", 0) AS "installment_count",
       COALESCE(r."settled_installment_count", 0) AS "settled_installment_count",
       COALESCE(r."unpaid_installment_count", 0) AS "unpaid_installment_count",
       COALESCE(r."late_installment_count", 0) AS "late_installment_count",
       r."max_days_late" AS "max_days_late",
       r."mean_days_late" AS "mean_days_late",
       r."scheduled_installment_amount" AS "scheduled_installment_amount",
       r."installment_paid_amount" AS "installment_paid_amount",
       r."measured_installment_paid_amount" AS "measured_installment_paid_amount",
       r."installment_outstanding_amount" AS "installment_outstanding_amount",
       r."installment_paid_ratio" AS "installment_paid_ratio",
       r."installment_late_rate" AS "installment_late_rate",
       COALESCE(r."missing_payment_installment_count", 0) AS "missing_payment_installment_count",
       COALESCE(r."due_day_conflict_installment_count", 0) AS "due_day_conflict_installment_count",
       COALESCE(r."amount_conflict_installment_count", 0) AS "amount_conflict_installment_count",
       COALESCE(r."missing_scheduled_installment_count", 0) AS "missing_scheduled_installment_count",
       COALESCE(r."future_payment_row_count", 0) AS "future_payment_row_count",
       COALESCE(b."bureau_loan_count", 0) AS "bureau_loan_count",
       COALESCE(b."bureau_active_loan_count", 0) AS "bureau_active_loan_count",
       COALESCE(b."bureau_closed_loan_count", 0) AS "bureau_closed_loan_count",
       b."bureau_credit_sum" AS "bureau_credit_sum",
       b."bureau_debt_sum" AS "bureau_debt_sum",
       b."bureau_overdue_sum" AS "bureau_overdue_sum",
       b."bureau_max_dpd" AS "bureau_max_dpd",
       COALESCE(b."bureau_months_31plus", 0) AS "bureau_months_31plus",
       b."bureau_credit_to_debt_ratio" AS "bureau_credit_to_debt_ratio",
       COALESCE(b."bureau_observed_month_count", 0) AS "bureau_observed_month_count",
       COALESCE(b."bureau_unknown_status_month_count", 0) AS "bureau_unknown_status_month_count",
       COALESCE(b."bureau_status_conflict_month_count", 0) AS "bureau_status_conflict_month_count",
       COALESCE(p."previous_application_count", 0) AS "previous_application_count",
       COALESCE(p."previous_approved_count", 0) AS "previous_approved_count",
       COALESCE(p."previous_refused_count", 0) AS "previous_refused_count",
       p."previous_refusal_rate" AS "previous_refusal_rate",
       p."previous_credit_mean" AS "previous_credit_mean",
       p."previous_credit_sum" AS "previous_credit_sum",
       p."previous_annuity_mean" AS "previous_annuity_mean",
       p."previous_latest_decision_day" AS "previous_latest_decision_day",
       COALESCE(o."pos_loan_count", 0) AS "pos_loan_count",
       COALESCE(o."pos_month_count", 0) AS "pos_month_count",
       o."pos_max_dpd" AS "pos_max_dpd",
       o."pos_max_dpd_def" AS "pos_max_dpd_def",
       o."pos_delinquent_month_rate" AS "pos_delinquent_month_rate",
       COALESCE(c."cc_loan_count", 0) AS "cc_loan_count",
       COALESCE(c."cc_month_count", 0) AS "cc_month_count",
       c."cc_max_dpd" AS "cc_max_dpd",
       c."cc_max_dpd_def" AS "cc_max_dpd_def",
       c."cc_utilization_mean" AS "cc_utilization_mean",
       c."cc_utilization_max" AS "cc_utilization_max",
       c."cc_balance_sum_latest" AS "cc_balance_sum_latest",
       c."cc_limit_sum_latest" AS "cc_limit_sum_latest",
       COALESCE(c."cc_amount_conflict_month_count", 0) AS "cc_amount_conflict_month_count"
FROM staging.application a
LEFT JOIN intermediate.int_applicant_repay r USING (applicant_id)
LEFT JOIN intermediate.int_applicant_bureau b USING (applicant_id)
LEFT JOIN intermediate.int_applicant_prev p USING (applicant_id)
LEFT JOIN intermediate.int_applicant_pos o USING (applicant_id)
LEFT JOIN intermediate.int_applicant_cc c USING (applicant_id);
CREATE UNIQUE INDEX applicant_features_pk ON marts.applicant_features(applicant_id);
DO $$ BEGIN
IF (SELECT count(*) FROM marts.applicant_features) <> (SELECT count(*) FROM raw.application_train) THEN
RAISE EXCEPTION 'Applicant join multiplied or lost rows'; END IF;
END $$;
ANALYZE marts.applicant_features;
COMMIT;
