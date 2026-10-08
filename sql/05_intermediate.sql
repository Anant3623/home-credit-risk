-- Build upward from payment -> installment -> loan -> applicant, never raw many-to-many joins.
-- All histories are restricted to the labelled training cohort and day/month <= 0.
BEGIN;
CREATE SCHEMA IF NOT EXISTS intermediate;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM raw.application_train GROUP BY "SK_ID_CURR"::numeric
               HAVING count(*) > 1 OR "SK_ID_CURR"::numeric IS NULL) THEN
        RAISE EXCEPTION 'application_train must have one non-null key per applicant';
    END IF;
    IF EXISTS (SELECT 1 FROM staging.bureau GROUP BY bureau_loan_id
               HAVING count(*) > 1 OR bureau_loan_id IS NULL) THEN
        RAISE EXCEPTION 'bureau loan keys duplicate or missing: joining monthly history would multiply rows';
    END IF;
    IF EXISTS (SELECT 1 FROM staging.previous_application GROUP BY previous_loan_id
               HAVING count(*) > 1 OR previous_loan_id IS NULL) THEN
        RAISE EXCEPTION 'previous_application loan keys duplicate or missing';
    END IF;
    IF EXISTS (SELECT 1 FROM staging.installments_payments
               WHERE previous_loan_id IS NULL OR installment_version IS NULL
                  OR installment_number IS NULL) THEN
        RAISE EXCEPTION 'Installment business key is incomplete; repair or quarantine it explicitly';
    END IF;
END $$;

-- Drops are limited to this script's outputs, without CASCADE to protect other objects.
DROP TABLE IF EXISTS intermediate.int_applicant_repay;
DROP TABLE IF EXISTS intermediate.int_prev_loan_repay;
DROP TABLE IF EXISTS intermediate.int_installment;
DROP TABLE IF EXISTS intermediate.int_applicant_bureau;
DROP TABLE IF EXISTS intermediate.int_bureau_loan;
DROP TABLE IF EXISTS intermediate.int_bureau_month;
DROP TABLE IF EXISTS intermediate.int_applicant_prev;
DROP TABLE IF EXISTS intermediate.int_applicant_pos;
DROP TABLE IF EXISTS intermediate.int_pos_loan;
DROP TABLE IF EXISTS intermediate.int_pos_month;
DROP TABLE IF EXISTS intermediate.int_applicant_cc;
DROP TABLE IF EXISTS intermediate.int_cc_loan;
DROP TABLE IF EXISTS intermediate.int_cc_month;
DROP TABLE IF EXISTS intermediate.int_history_exclusion_audit;

CREATE TABLE intermediate.int_history_exclusion_audit AS
SELECT 'installment_future_due_rows'::text AS check_name, count(*)::bigint AS row_count
FROM staging.installments_payments WHERE due_day > 0
UNION ALL SELECT 'installment_missing_due_rows', count(*) FROM staging.installments_payments WHERE due_day IS NULL
UNION ALL SELECT 'installment_future_payment_rows', count(*) FROM staging.installments_payments WHERE payment_day > 0
UNION ALL SELECT 'previous_future_or_missing_decision_rows', count(*) FROM staging.previous_application WHERE days_decision > 0 OR days_decision IS NULL
UNION ALL SELECT 'bureau_future_or_missing_credit_day_rows', count(*) FROM staging.bureau WHERE days_credit > 0 OR days_credit IS NULL
UNION ALL SELECT 'bureau_future_or_missing_month_rows', count(*) FROM staging.bureau_balance WHERE relative_month > 0 OR relative_month IS NULL
UNION ALL SELECT 'pos_future_or_missing_month_rows', count(*) FROM staging.pos_cash_balance WHERE relative_month > 0 OR relative_month IS NULL
UNION ALL SELECT 'cc_future_or_missing_month_rows', count(*) FROM staging.credit_card_balance WHERE relative_month > 0 OR relative_month IS NULL;

CREATE TABLE intermediate.int_installment AS
WITH source AS MATERIALIZED (
    SELECT * FROM staging.installments_payments
), schedules AS (
    -- A conflicting due date must not become a second EMI. Choose earliest date and audit.
    -- Include all rows of an EMI if its earliest valid due date is already due at day 0.
    SELECT applicant_id, previous_loan_id, installment_version, installment_number,
           min(due_day) AS due_day, max(scheduled_amount) AS scheduled_amount,
           count(*)::bigint AS source_row_count,
           count(DISTINCT due_day)::integer AS distinct_due_day_count,
           count(DISTINCT scheduled_amount)::integer AS distinct_scheduled_amount_count,
           (count(DISTINCT due_day) > 1)::integer AS is_due_day_conflict,
           (count(DISTINCT scheduled_amount) > 1)::integer AS is_scheduled_amount_conflict,
           count(*) FILTER (WHERE payment_day > 0)::bigint AS future_payment_row_count,
           count(*) FILTER (WHERE is_payment_amount_missing = 1 OR is_payment_day_missing = 1)::bigint AS missing_payment_row_count,
           coalesce(sum(payment_amount) FILTER (WHERE payment_day <= 0), 0) AS paid_amount_asof,
           min(payment_day) FILTER (WHERE payment_day <= 0 AND payment_amount IS NOT NULL) AS first_payment_day,
           max(payment_day) FILTER (WHERE payment_day <= 0 AND payment_amount IS NOT NULL) AS last_payment_day
    FROM source
    GROUP BY applicant_id, previous_loan_id, installment_version, installment_number
    HAVING min(due_day) <= 0
), daily_payments AS (
    -- Same-day partial payments form one event before computing a running total.
    SELECT applicant_id, previous_loan_id, installment_version, installment_number,
           payment_day, sum(payment_amount) AS daily_amount
    FROM source
    WHERE payment_day <= 0 AND payment_amount IS NOT NULL
    GROUP BY applicant_id, previous_loan_id, installment_version, installment_number, payment_day
), cumulative AS (
    SELECT d.*, sum(daily_amount) OVER (
        PARTITION BY applicant_id, previous_loan_id, installment_version, installment_number
        ORDER BY payment_day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS cumulative_amount
    FROM daily_payments d
), completion AS (
    SELECT s.applicant_id, s.previous_loan_id, s.installment_version, s.installment_number,
           min(c.payment_day) FILTER (
               WHERE c.cumulative_amount >= s.scheduled_amount - 0.01
           ) AS settlement_day,
           coalesce(sum(c.daily_amount) FILTER (WHERE c.payment_day <= s.due_day), 0) AS paid_by_due_amount
    FROM schedules s LEFT JOIN cumulative c USING (
        applicant_id, previous_loan_id, installment_version, installment_number
    )
    GROUP BY s.applicant_id, s.previous_loan_id, s.installment_version, s.installment_number
), status AS (
    SELECT s.*, c.paid_by_due_amount,
           CASE WHEN s.scheduled_amount = 0 THEN s.due_day ELSE c.settlement_day END AS settlement_day,
           (s.scheduled_amount IS NOT NULL AND
               (s.scheduled_amount = 0 OR c.settlement_day IS NOT NULL))::integer AS is_settled,
           (s.missing_payment_row_count > 0)::integer AS has_missing_payment,
           (s.scheduled_amount IS NULL)::integer AS is_scheduled_amount_missing
    FROM schedules s JOIN completion c USING (
        applicant_id, previous_loan_id, installment_version, installment_number
    )
)
SELECT status.*,
       CASE WHEN is_settled = 1 THEN greatest(settlement_day - due_day, 0)
            WHEN scheduled_amount IS NOT NULL THEN greatest(-due_day, 0) END AS days_late,
       (is_settled = 0 AND scheduled_amount IS NOT NULL)::integer AS is_unpaid_asof,
       CASE WHEN scheduled_amount > 0 THEN coalesce(paid_amount_asof, 0) / scheduled_amount END AS paid_ratio,
       CASE WHEN scheduled_amount IS NOT NULL
            THEN greatest(scheduled_amount - coalesce(paid_amount_asof, 0), 0) END AS outstanding_amount_asof
FROM status;
CREATE UNIQUE INDEX int_installment_key ON intermediate.int_installment
    (applicant_id, previous_loan_id, installment_version, installment_number);
CREATE INDEX int_installment_previous_loan ON intermediate.int_installment (previous_loan_id);

CREATE TABLE intermediate.int_prev_loan_repay AS
SELECT applicant_id, previous_loan_id,
       count(*)::bigint AS installment_count,
       sum(is_settled)::bigint AS settled_installment_count,
       sum(is_unpaid_asof)::bigint AS unpaid_installment_count,
       count(*) FILTER (WHERE days_late > 0)::bigint AS late_installment_count,
       max(days_late) AS max_days_late,
       sum(days_late) AS total_days_late,
       count(days_late)::bigint AS measured_installment_count,
       avg(days_late) AS mean_days_late,
       sum(scheduled_amount) AS scheduled_installment_amount,
       sum(coalesce(paid_amount_asof, 0)) AS installment_paid_amount,
       sum(coalesce(paid_amount_asof, 0)) FILTER (WHERE scheduled_amount IS NOT NULL) AS measured_installment_paid_amount,
       sum(outstanding_amount_asof) AS installment_outstanding_amount,
       sum(coalesce(paid_amount_asof, 0)) FILTER (WHERE scheduled_amount IS NOT NULL)
           / NULLIF(sum(scheduled_amount), 0) AS installment_paid_ratio,
       sum(has_missing_payment)::bigint AS missing_payment_installment_count,
       sum(is_due_day_conflict)::bigint AS due_day_conflict_installment_count,
       sum(is_scheduled_amount_conflict)::bigint AS amount_conflict_installment_count,
       sum(is_scheduled_amount_missing)::bigint AS missing_scheduled_installment_count,
       sum(future_payment_row_count)::bigint AS future_payment_row_count
FROM intermediate.int_installment
GROUP BY applicant_id, previous_loan_id;
-- A loan must never belong to two applicants.
CREATE UNIQUE INDEX int_prev_loan_repay_key ON intermediate.int_prev_loan_repay (previous_loan_id);
CREATE INDEX int_prev_loan_repay_applicant ON intermediate.int_prev_loan_repay (applicant_id);

CREATE TABLE intermediate.int_applicant_repay AS
SELECT applicant_id, count(*)::bigint AS prev_loans_with_installments,
       sum(installment_count)::bigint AS installment_count,
       sum(settled_installment_count)::bigint AS settled_installment_count,
       sum(unpaid_installment_count)::bigint AS unpaid_installment_count,
       sum(late_installment_count)::bigint AS late_installment_count,
       max(max_days_late) AS max_days_late,
       sum(total_days_late) / NULLIF(sum(measured_installment_count), 0) AS mean_days_late,
       sum(scheduled_installment_amount) AS scheduled_installment_amount,
       sum(installment_paid_amount) AS installment_paid_amount,
       sum(measured_installment_paid_amount) AS measured_installment_paid_amount,
       sum(installment_outstanding_amount) AS installment_outstanding_amount,
       sum(measured_installment_paid_amount) / NULLIF(sum(scheduled_installment_amount), 0) AS installment_paid_ratio,
       sum(late_installment_count)::numeric / NULLIF(sum(measured_installment_count), 0) AS installment_late_rate,
       sum(missing_payment_installment_count)::bigint AS missing_payment_installment_count,
       sum(due_day_conflict_installment_count)::bigint AS due_day_conflict_installment_count,
       sum(amount_conflict_installment_count)::bigint AS amount_conflict_installment_count,
       sum(missing_scheduled_installment_count)::bigint AS missing_scheduled_installment_count,
       sum(future_payment_row_count)::bigint AS future_payment_row_count
FROM intermediate.int_prev_loan_repay GROUP BY applicant_id;
CREATE UNIQUE INDEX int_applicant_repay_key ON intermediate.int_applicant_repay (applicant_id);

CREATE TABLE intermediate.int_bureau_month AS
SELECT b.applicant_id, bb.bureau_loan_id, bb.relative_month,
       max(bb.dpd_lower_bound_days) AS dpd_lower_bound_days,
       CASE max(bb.dpd_lower_bound_days)
           WHEN 121 THEN '120+' WHEN 91 THEN '91-120' WHEN 61 THEN '61-90'
           WHEN 31 THEN '31-60' WHEN 1 THEN '1-30' WHEN 0 THEN 'Current'
           ELSE CASE WHEN bool_and(bb.status_code = 'C') THEN 'Closed' ELSE 'Unknown' END
       END::text AS dpd_bucket,
       count(*)::bigint AS source_row_count,
       (count(DISTINCT bb.status_code) > 1)::integer AS is_status_conflict
FROM staging.bureau_balance bb
JOIN staging.bureau b USING (bureau_loan_id)
WHERE bb.relative_month <= 0 AND b.days_credit <= 0
GROUP BY b.applicant_id, bb.bureau_loan_id, bb.relative_month;
CREATE UNIQUE INDEX int_bureau_month_key ON intermediate.int_bureau_month (bureau_loan_id, relative_month);
CREATE INDEX int_bureau_month_applicant ON intermediate.int_bureau_month (applicant_id);

CREATE TABLE intermediate.int_bureau_loan AS
WITH monthly AS (
    SELECT bureau_loan_id, count(*)::bigint AS observed_month_count,
           count(dpd_lower_bound_days)::bigint AS known_dpd_month_count,
           max(dpd_lower_bound_days) AS worst_dpd_lower_bound_days,
           count(*) FILTER (WHERE dpd_lower_bound_days >= 31)::bigint AS months_31plus,
           count(*) FILTER (WHERE dpd_bucket = 'Unknown')::bigint AS unknown_status_month_count,
           sum(is_status_conflict)::bigint AS status_conflict_month_count
    FROM intermediate.int_bureau_month GROUP BY bureau_loan_id
)
SELECT b.*, coalesce(m.observed_month_count, 0)::bigint AS observed_month_count,
       coalesce(m.known_dpd_month_count, 0)::bigint AS known_dpd_month_count,
       m.worst_dpd_lower_bound_days,
       coalesce(m.months_31plus, 0)::bigint AS months_31plus,
       coalesce(m.unknown_status_month_count, 0)::bigint AS unknown_status_month_count,
       coalesce(m.status_conflict_month_count, 0)::bigint AS status_conflict_month_count
FROM staging.bureau b LEFT JOIN monthly m USING (bureau_loan_id)
WHERE b.days_credit <= 0;
CREATE UNIQUE INDEX int_bureau_loan_key ON intermediate.int_bureau_loan (bureau_loan_id);
CREATE INDEX int_bureau_loan_applicant ON intermediate.int_bureau_loan (applicant_id);

CREATE TABLE intermediate.int_applicant_bureau AS
SELECT applicant_id, count(*)::bigint AS bureau_loan_count,
       count(*) FILTER (WHERE credit_status = 'Active')::bigint AS bureau_active_loan_count,
       count(*) FILTER (WHERE credit_status = 'Closed')::bigint AS bureau_closed_loan_count,
       sum(credit_amount) AS bureau_credit_sum,
       sum(debt_amount) AS bureau_debt_sum,
       sum(overdue_amount) AS bureau_overdue_sum,
       greatest(max(worst_dpd_lower_bound_days), max(credit_days_overdue)) AS bureau_max_dpd,
       sum(months_31plus)::bigint AS bureau_months_31plus,
       sum(credit_amount) / NULLIF(sum(debt_amount), 0) AS bureau_credit_to_debt_ratio,
       sum(observed_month_count)::bigint AS bureau_observed_month_count,
       sum(unknown_status_month_count)::bigint AS bureau_unknown_status_month_count,
       sum(status_conflict_month_count)::bigint AS bureau_status_conflict_month_count
FROM intermediate.int_bureau_loan GROUP BY applicant_id;
CREATE UNIQUE INDEX int_applicant_bureau_key ON intermediate.int_applicant_bureau (applicant_id);

CREATE TABLE intermediate.int_applicant_prev AS
SELECT applicant_id, count(*)::bigint AS previous_application_count,
       count(*) FILTER (WHERE application_status = 'Approved')::bigint AS previous_approved_count,
       count(*) FILTER (WHERE application_status = 'Refused')::bigint AS previous_refused_count,
       count(*) FILTER (WHERE application_status = 'Refused')::numeric / NULLIF(count(*), 0) AS previous_refusal_rate,
       avg(credit_amount) AS previous_credit_mean,
       sum(credit_amount) AS previous_credit_sum,
       avg(annuity_amount) AS previous_annuity_mean,
       max(days_decision) AS previous_latest_decision_day
FROM staging.previous_application
WHERE days_decision <= 0
GROUP BY applicant_id;
CREATE UNIQUE INDEX int_applicant_prev_key ON intermediate.int_applicant_prev (applicant_id);

CREATE TABLE intermediate.int_pos_month AS
SELECT applicant_id, previous_loan_id, relative_month,
       max(dpd) AS dpd, max(dpd_def) AS dpd_def,
       max(installment_count) AS installment_count,
       min(future_installment_count) AS future_installment_count,
       count(*)::bigint AS source_row_count,
       (count(DISTINCT contract_status) > 1)::integer AS is_status_conflict
FROM staging.pos_cash_balance WHERE relative_month <= 0
GROUP BY applicant_id, previous_loan_id, relative_month;
CREATE UNIQUE INDEX int_pos_month_key ON intermediate.int_pos_month (previous_loan_id, relative_month);

CREATE TABLE intermediate.int_pos_loan AS
SELECT applicant_id, previous_loan_id, count(*)::bigint AS observed_month_count,
       count(dpd)::bigint AS known_dpd_month_count,
       max(dpd) AS max_dpd, max(dpd_def) AS max_dpd_def,
       count(*) FILTER (WHERE dpd > 0)::bigint AS delinquent_month_count
FROM intermediate.int_pos_month GROUP BY applicant_id, previous_loan_id;
CREATE UNIQUE INDEX int_pos_loan_key ON intermediate.int_pos_loan (previous_loan_id);
CREATE INDEX int_pos_loan_applicant ON intermediate.int_pos_loan (applicant_id);

CREATE TABLE intermediate.int_applicant_pos AS
SELECT applicant_id, count(*)::bigint AS pos_loan_count,
       sum(observed_month_count)::bigint AS pos_month_count,
       max(max_dpd) AS pos_max_dpd, max(max_dpd_def) AS pos_max_dpd_def,
       sum(delinquent_month_count)::numeric / NULLIF(sum(known_dpd_month_count), 0) AS pos_delinquent_month_rate
FROM intermediate.int_pos_loan GROUP BY applicant_id;
CREATE UNIQUE INDEX int_applicant_pos_key ON intermediate.int_applicant_pos (applicant_id);

CREATE TABLE intermediate.int_cc_month AS
WITH monthly AS (
    -- Multiple snapshots of one account/month are ambiguous: keep a conservative maximum,
    -- audit conflicts, and do not sum repeated account balances or limits.
    SELECT applicant_id, previous_loan_id, relative_month,
           max(balance_amount) AS balance_amount,
           max(credit_limit_amount) AS credit_limit_amount,
           max(dpd) AS dpd, max(dpd_def) AS dpd_def,
           count(*)::bigint AS source_row_count,
           (count(DISTINCT balance_amount) > 1 OR count(DISTINCT credit_limit_amount) > 1)::integer AS is_amount_conflict,
           max(is_credit_balance) AS has_credit_balance
    FROM staging.credit_card_balance WHERE relative_month <= 0
    GROUP BY applicant_id, previous_loan_id, relative_month
)
SELECT monthly.*,
       CASE WHEN balance_amount IS NOT NULL THEN greatest(balance_amount, 0) / NULLIF(credit_limit_amount, 0) END AS utilization
FROM monthly;
CREATE UNIQUE INDEX int_cc_month_key ON intermediate.int_cc_month (previous_loan_id, relative_month);

CREATE TABLE intermediate.int_cc_loan AS
WITH aggregates AS (
    SELECT applicant_id, previous_loan_id, count(*)::bigint AS observed_month_count,
           max(dpd) AS max_dpd, max(dpd_def) AS max_dpd_def,
           sum(utilization) AS utilization_sum,
           count(utilization)::bigint AS known_utilization_month_count,
           max(utilization) AS utilization_max,
           sum(is_amount_conflict)::bigint AS amount_conflict_month_count
    FROM intermediate.int_cc_month GROUP BY applicant_id, previous_loan_id
), latest AS (
    SELECT DISTINCT ON (previous_loan_id) previous_loan_id, relative_month AS latest_relative_month,
           balance_amount AS latest_balance_amount, credit_limit_amount AS latest_limit_amount
    FROM intermediate.int_cc_month ORDER BY previous_loan_id, relative_month DESC
)
SELECT a.*, l.latest_relative_month, l.latest_balance_amount, l.latest_limit_amount
FROM aggregates a JOIN latest l USING (previous_loan_id);
CREATE UNIQUE INDEX int_cc_loan_key ON intermediate.int_cc_loan (previous_loan_id);
CREATE INDEX int_cc_loan_applicant ON intermediate.int_cc_loan (applicant_id);

CREATE TABLE intermediate.int_applicant_cc AS
SELECT applicant_id, count(*)::bigint AS cc_loan_count,
       sum(observed_month_count)::bigint AS cc_month_count,
       max(max_dpd) AS cc_max_dpd, max(max_dpd_def) AS cc_max_dpd_def,
       sum(utilization_sum) / NULLIF(sum(known_utilization_month_count), 0) AS cc_utilization_mean,
       max(utilization_max) AS cc_utilization_max,
       sum(latest_balance_amount) AS cc_balance_sum_latest,
       sum(latest_limit_amount) AS cc_limit_sum_latest,
       sum(amount_conflict_month_count)::bigint AS cc_amount_conflict_month_count
FROM intermediate.int_cc_loan GROUP BY applicant_id;
CREATE UNIQUE INDEX int_applicant_cc_key ON intermediate.int_applicant_cc (applicant_id);

DO $$ DECLARE t text; has_duplicates boolean; BEGIN
    -- Cross-source parent mapping guard, after reducing child tables to loan grain.
    IF EXISTS (
        SELECT previous_loan_id FROM (
            SELECT previous_loan_id, applicant_id FROM staging.previous_application
            UNION SELECT previous_loan_id, applicant_id FROM intermediate.int_prev_loan_repay
            UNION SELECT previous_loan_id, applicant_id FROM intermediate.int_pos_loan
            UNION SELECT previous_loan_id, applicant_id FROM intermediate.int_cc_loan
        ) owners GROUP BY previous_loan_id HAVING count(DISTINCT applicant_id) > 1
    ) THEN RAISE EXCEPTION 'A previous loan maps to different applicants across source tables'; END IF;
    FOREACH t IN ARRAY ARRAY['int_applicant_repay', 'int_applicant_bureau', 'int_applicant_prev',
                            'int_applicant_pos', 'int_applicant_cc'] LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM intermediate.%I GROUP BY applicant_id HAVING count(*) <> 1 OR applicant_id IS NULL)', t)
        INTO has_duplicates;
        IF has_duplicates THEN RAISE EXCEPTION 'Duplicate/missing applicant key in %', t; END IF;
    END LOOP;
END $$;

ANALYZE intermediate.int_installment;
ANALYZE intermediate.int_prev_loan_repay;
ANALYZE intermediate.int_bureau_month;
ANALYZE intermediate.int_bureau_loan;
ANALYZE intermediate.int_applicant_repay;
ANALYZE intermediate.int_applicant_bureau;
ANALYZE intermediate.int_applicant_prev;
ANALYZE intermediate.int_applicant_pos;
ANALYZE intermediate.int_applicant_cc;
COMMIT;
