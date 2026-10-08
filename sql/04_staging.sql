-- Raw remains unchanged. Staging selects model/report inputs and preserves quality flags.
-- All relative dates use application date = day 0; positive historical dates are excluded downstream.
BEGIN;
CREATE SCHEMA IF NOT EXISTS staging;

CREATE OR REPLACE FUNCTION staging.clean_category(value text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN value IS NULL OR upper(btrim(value)) IN ('', 'XNA', 'XAP')
                THEN NULL ELSE btrim(value) END
$$;

CREATE OR REPLACE VIEW staging.application AS
WITH cleaned AS (
    SELECT "SK_ID_CURR"::numeric::bigint AS applicant_id,
           "TARGET"::numeric::integer AS target,
           staging.clean_category("NAME_CONTRACT_TYPE"::text) AS contract_type,
           staging.clean_category("CODE_GENDER"::text) AS gender,
           staging.clean_category("FLAG_OWN_CAR"::text) AS owns_car,
           staging.clean_category("FLAG_OWN_REALTY"::text) AS owns_realty,
           CASE WHEN "CNT_CHILDREN"::numeric >= 0 THEN "CNT_CHILDREN"::numeric END AS children_count,
           CASE WHEN "CNT_FAM_MEMBERS"::numeric >= 1 THEN "CNT_FAM_MEMBERS"::numeric END AS family_members_count,
           staging.clean_category("NAME_INCOME_TYPE"::text) AS income_type,
           staging.clean_category("NAME_EDUCATION_TYPE"::text) AS education_type,
           staging.clean_category("NAME_FAMILY_STATUS"::text) AS family_status,
           staging.clean_category("NAME_HOUSING_TYPE"::text) AS housing_type,
           staging.clean_category("OCCUPATION_TYPE"::text) AS occupation_type,
           CASE WHEN "REGION_RATING_CLIENT"::numeric BETWEEN 1 AND 3 THEN "REGION_RATING_CLIENT"::numeric::integer END AS region_rating,
           CASE WHEN "REGION_RATING_CLIENT_W_CITY"::numeric BETWEEN 1 AND 3 THEN "REGION_RATING_CLIENT_W_CITY"::numeric::integer END AS region_rating_city,
           CASE WHEN "AMT_INCOME_TOTAL"::numeric > 0 THEN "AMT_INCOME_TOTAL"::numeric END AS income_amount,
           CASE WHEN "AMT_CREDIT"::numeric >= 0 THEN "AMT_CREDIT"::numeric END AS credit_amount,
           CASE WHEN "AMT_ANNUITY"::numeric >= 0 THEN "AMT_ANNUITY"::numeric END AS annuity_amount,
           CASE WHEN "AMT_GOODS_PRICE"::numeric >= 0 THEN "AMT_GOODS_PRICE"::numeric END AS goods_amount,
           CASE WHEN "DAYS_BIRTH"::numeric BETWEEN -36525 AND -1 THEN "DAYS_BIRTH"::numeric END AS days_birth,
           CASE WHEN "DAYS_EMPLOYED"::numeric BETWEEN -36525 AND 0 THEN "DAYS_EMPLOYED"::numeric END AS days_employed,
           ("DAYS_EMPLOYED"::numeric IS NULL OR "DAYS_EMPLOYED"::numeric = 365243 OR "DAYS_EMPLOYED"::numeric > 0 OR "DAYS_EMPLOYED"::numeric < -36525)::integer AS is_employed_missing,
           ("DAYS_BIRTH"::numeric IS NOT NULL AND ("DAYS_BIRTH"::numeric < -36525 OR "DAYS_BIRTH"::numeric >= 0))::integer AS is_age_invalid,
           ("AMT_INCOME_TOTAL"::numeric IS NOT NULL AND "AMT_INCOME_TOTAL"::numeric <= 0)::integer AS is_income_invalid,
           CASE WHEN "EXT_SOURCE_1"::numeric BETWEEN 0 AND 1 THEN "EXT_SOURCE_1"::numeric END AS ext_source_1,
           CASE WHEN "EXT_SOURCE_2"::numeric BETWEEN 0 AND 1 THEN "EXT_SOURCE_2"::numeric END AS ext_source_2,
           CASE WHEN "EXT_SOURCE_3"::numeric BETWEEN 0 AND 1 THEN "EXT_SOURCE_3"::numeric END AS ext_source_3,
           CASE WHEN "DAYS_REGISTRATION"::numeric <= 0 AND "DAYS_REGISTRATION"::numeric <> -365243 THEN "DAYS_REGISTRATION"::numeric END AS days_registration,
           CASE WHEN "DAYS_ID_PUBLISH"::numeric <= 0 AND "DAYS_ID_PUBLISH"::numeric <> -365243 THEN "DAYS_ID_PUBLISH"::numeric END AS days_id_publish,
           CASE WHEN "DAYS_LAST_PHONE_CHANGE"::numeric <= 0 AND "DAYS_LAST_PHONE_CHANGE"::numeric <> -365243 THEN "DAYS_LAST_PHONE_CHANGE"::numeric END AS days_last_phone_change
    FROM raw.application_train
)
SELECT cleaned.*, -days_birth / 365.25 AS age_years, -days_employed / 365.25 AS employed_years,
       credit_amount / NULLIF(income_amount, 0) AS credit_to_income,
       annuity_amount / NULLIF(income_amount, 0) AS annuity_to_income,
       credit_amount / NULLIF(goods_amount, 0) AS credit_to_goods
FROM cleaned;

CREATE OR REPLACE VIEW staging.bureau AS
SELECT b."SK_ID_CURR"::numeric::bigint AS applicant_id,
       b."SK_ID_BUREAU"::numeric::bigint AS bureau_loan_id,
       staging.clean_category(b."CREDIT_ACTIVE"::text) AS credit_status,
       staging.clean_category(b."CREDIT_CURRENCY"::text) AS credit_currency,
       staging.clean_category(b."CREDIT_TYPE"::text) AS credit_type,
       CASE WHEN b."DAYS_CREDIT"::numeric <= 0 AND b."DAYS_CREDIT"::numeric <> -365243 THEN b."DAYS_CREDIT"::numeric END AS days_credit,
       CASE WHEN abs(b."DAYS_CREDIT_ENDDATE"::numeric) <> 365243 THEN b."DAYS_CREDIT_ENDDATE"::numeric END AS days_credit_enddate,
       CASE WHEN b."DAYS_ENDDATE_FACT"::numeric <= 0 AND abs(b."DAYS_ENDDATE_FACT"::numeric) <> 365243 THEN b."DAYS_ENDDATE_FACT"::numeric END AS days_enddate_fact,
       CASE WHEN b."DAYS_CREDIT_UPDATE"::numeric <= 0 AND abs(b."DAYS_CREDIT_UPDATE"::numeric) <> 365243 THEN b."DAYS_CREDIT_UPDATE"::numeric END AS days_credit_update,
       CASE WHEN b."CREDIT_DAY_OVERDUE"::numeric >= 0 THEN b."CREDIT_DAY_OVERDUE"::numeric END AS credit_days_overdue,
       CASE WHEN b."AMT_CREDIT_SUM"::numeric >= 0 THEN b."AMT_CREDIT_SUM"::numeric END AS credit_amount,
       CASE WHEN b."AMT_CREDIT_SUM_DEBT"::numeric >= 0 THEN b."AMT_CREDIT_SUM_DEBT"::numeric END AS debt_amount,
       CASE WHEN b."AMT_CREDIT_SUM_OVERDUE"::numeric >= 0 THEN b."AMT_CREDIT_SUM_OVERDUE"::numeric END AS overdue_amount,
       CASE WHEN b."AMT_CREDIT_SUM_LIMIT"::numeric >= 0 THEN b."AMT_CREDIT_SUM_LIMIT"::numeric END AS credit_limit_amount,
       CASE WHEN b."AMT_ANNUITY"::numeric >= 0 THEN b."AMT_ANNUITY"::numeric END AS annuity_amount
FROM raw.bureau b
WHERE EXISTS (SELECT 1 FROM raw.application_train a WHERE a."SK_ID_CURR"::numeric = b."SK_ID_CURR"::numeric);

CREATE OR REPLACE VIEW staging.bureau_balance AS
SELECT bb."SK_ID_BUREAU"::numeric::bigint AS bureau_loan_id,
       bb."MONTHS_BALANCE"::numeric::integer AS relative_month,
       btrim(bb."STATUS"::text) AS status_code,
       CASE btrim(bb."STATUS"::text)
           WHEN 'C' THEN 'Closed' WHEN 'X' THEN 'Unknown' WHEN '0' THEN 'Current'
           WHEN '1' THEN '1-30' WHEN '2' THEN '31-60' WHEN '3' THEN '61-90'
           WHEN '4' THEN '91-120' WHEN '5' THEN '120+' ELSE 'Unknown' END AS dpd_bucket,
       CASE btrim(bb."STATUS"::text)
           WHEN '0' THEN 0 WHEN '1' THEN 1 WHEN '2' THEN 31 WHEN '3' THEN 61
           WHEN '4' THEN 91 WHEN '5' THEN 121 END::integer AS dpd_lower_bound_days
FROM raw.bureau_balance bb
WHERE EXISTS (SELECT 1 FROM staging.bureau b WHERE b.bureau_loan_id = bb."SK_ID_BUREAU"::numeric);

CREATE OR REPLACE VIEW staging.previous_application AS
SELECT p."SK_ID_CURR"::numeric::bigint AS applicant_id,
       p."SK_ID_PREV"::numeric::bigint AS previous_loan_id,
       staging.clean_category(p."NAME_CONTRACT_TYPE"::text) AS contract_type,
       staging.clean_category(p."NAME_CONTRACT_STATUS"::text) AS application_status,
       staging.clean_category(p."NAME_PAYMENT_TYPE"::text) AS payment_type,
       staging.clean_category(p."CODE_REJECT_REASON"::text) AS reject_reason,
       staging.clean_category(p."NAME_CLIENT_TYPE"::text) AS client_type,
       staging.clean_category(p."NAME_GOODS_CATEGORY"::text) AS goods_category,
       staging.clean_category(p."NAME_YIELD_GROUP"::text) AS yield_group,
       CASE WHEN p."AMT_APPLICATION"::numeric >= 0 THEN p."AMT_APPLICATION"::numeric END AS application_amount,
       CASE WHEN p."AMT_CREDIT"::numeric >= 0 THEN p."AMT_CREDIT"::numeric END AS credit_amount,
       CASE WHEN p."AMT_ANNUITY"::numeric >= 0 THEN p."AMT_ANNUITY"::numeric END AS annuity_amount,
       CASE WHEN p."AMT_GOODS_PRICE"::numeric >= 0 THEN p."AMT_GOODS_PRICE"::numeric END AS goods_amount,
       CASE WHEN p."AMT_DOWN_PAYMENT"::numeric >= 0 THEN p."AMT_DOWN_PAYMENT"::numeric END AS down_payment_amount,
       CASE WHEN p."CNT_PAYMENT"::numeric >= 0 THEN p."CNT_PAYMENT"::numeric END AS scheduled_payment_count,
       CASE WHEN p."DAYS_DECISION"::numeric <= 0 AND abs(p."DAYS_DECISION"::numeric) <> 365243 THEN p."DAYS_DECISION"::numeric END AS days_decision,
       CASE WHEN abs(p."DAYS_FIRST_DRAWING"::numeric) <> 365243 THEN p."DAYS_FIRST_DRAWING"::numeric END AS days_first_drawing,
       CASE WHEN abs(p."DAYS_FIRST_DUE"::numeric) <> 365243 THEN p."DAYS_FIRST_DUE"::numeric END AS days_first_due,
       CASE WHEN abs(p."DAYS_LAST_DUE_1ST_VERSION"::numeric) <> 365243 THEN p."DAYS_LAST_DUE_1ST_VERSION"::numeric END AS days_last_due_first_version,
       CASE WHEN abs(p."DAYS_LAST_DUE"::numeric) <> 365243 THEN p."DAYS_LAST_DUE"::numeric END AS days_last_due,
       CASE WHEN abs(p."DAYS_TERMINATION"::numeric) <> 365243 THEN p."DAYS_TERMINATION"::numeric END AS days_termination,
       (p."DAYS_FIRST_DRAWING"::numeric = 365243)::integer AS is_first_drawing_sentinel,
       (p."DAYS_FIRST_DUE"::numeric = 365243)::integer AS is_first_due_sentinel,
       (p."DAYS_LAST_DUE_1ST_VERSION"::numeric = 365243)::integer AS is_last_due_first_version_sentinel,
       (p."DAYS_LAST_DUE"::numeric = 365243)::integer AS is_last_due_sentinel,
       (p."DAYS_TERMINATION"::numeric = 365243)::integer AS is_termination_sentinel
FROM raw.previous_application p
WHERE EXISTS (SELECT 1 FROM raw.application_train a WHERE a."SK_ID_CURR"::numeric = p."SK_ID_CURR"::numeric);

CREATE OR REPLACE VIEW staging.installments_payments AS
SELECT i."SK_ID_CURR"::numeric::bigint AS applicant_id,
       i."SK_ID_PREV"::numeric::bigint AS previous_loan_id,
       i."NUM_INSTALMENT_VERSION"::numeric AS installment_version,
       i."NUM_INSTALMENT_NUMBER"::numeric::integer AS installment_number,
       CASE WHEN abs(i."DAYS_INSTALMENT"::numeric) <> 365243 THEN i."DAYS_INSTALMENT"::numeric END AS due_day,
       CASE WHEN abs(i."DAYS_ENTRY_PAYMENT"::numeric) <> 365243 THEN i."DAYS_ENTRY_PAYMENT"::numeric END AS payment_day,
       CASE WHEN i."AMT_INSTALMENT"::numeric >= 0 THEN i."AMT_INSTALMENT"::numeric END AS scheduled_amount,
       CASE WHEN i."AMT_PAYMENT"::numeric >= 0 THEN i."AMT_PAYMENT"::numeric END AS payment_amount,
       CASE WHEN abs(i."DAYS_ENTRY_PAYMENT"::numeric) <> 365243 AND abs(i."DAYS_INSTALMENT"::numeric) <> 365243
            THEN (i."DAYS_ENTRY_PAYMENT"::numeric - i."DAYS_INSTALMENT"::numeric)::numeric END AS days_late,
       (i."AMT_PAYMENT"::numeric IS NULL OR i."AMT_PAYMENT"::numeric < 0)::integer AS is_payment_amount_missing,
       (i."DAYS_ENTRY_PAYMENT"::numeric IS NULL OR abs(i."DAYS_ENTRY_PAYMENT"::numeric) = 365243)::integer AS is_payment_day_missing
FROM raw.installments_payments i
WHERE EXISTS (SELECT 1 FROM raw.application_train a WHERE a."SK_ID_CURR"::numeric = i."SK_ID_CURR"::numeric);

CREATE OR REPLACE VIEW staging.pos_cash_balance AS
SELECT p."SK_ID_CURR"::numeric::bigint AS applicant_id,
       p."SK_ID_PREV"::numeric::bigint AS previous_loan_id,
       p."MONTHS_BALANCE"::numeric::integer AS relative_month,
       staging.clean_category(p."NAME_CONTRACT_STATUS"::text) AS contract_status,
       CASE WHEN p."CNT_INSTALMENT"::numeric >= 0 THEN p."CNT_INSTALMENT"::numeric END AS installment_count,
       CASE WHEN p."CNT_INSTALMENT_FUTURE"::numeric >= 0 THEN p."CNT_INSTALMENT_FUTURE"::numeric END AS future_installment_count,
       CASE WHEN p."SK_DPD"::numeric >= 0 THEN p."SK_DPD"::numeric END AS dpd,
       CASE WHEN p."SK_DPD_DEF"::numeric >= 0 THEN p."SK_DPD_DEF"::numeric END AS dpd_def
FROM raw.pos_cash_balance p
WHERE EXISTS (SELECT 1 FROM raw.application_train a WHERE a."SK_ID_CURR"::numeric = p."SK_ID_CURR"::numeric);

CREATE OR REPLACE VIEW staging.credit_card_balance AS
SELECT c."SK_ID_CURR"::numeric::bigint AS applicant_id,
       c."SK_ID_PREV"::numeric::bigint AS previous_loan_id,
       c."MONTHS_BALANCE"::numeric::integer AS relative_month,
       staging.clean_category(c."NAME_CONTRACT_STATUS"::text) AS contract_status,
       c."AMT_BALANCE"::numeric AS balance_amount,
       -- A negative balance can be an overpayment/credit; preserve it and flag it.
       (c."AMT_BALANCE"::numeric < 0)::integer AS is_credit_balance,
       CASE WHEN c."AMT_CREDIT_LIMIT_ACTUAL"::numeric >= 0 THEN c."AMT_CREDIT_LIMIT_ACTUAL"::numeric END AS credit_limit_amount,
       CASE WHEN c."AMT_PAYMENT_TOTAL_CURRENT"::numeric >= 0 THEN c."AMT_PAYMENT_TOTAL_CURRENT"::numeric END AS payment_amount,
       CASE WHEN c."AMT_DRAWINGS_CURRENT"::numeric >= 0 THEN c."AMT_DRAWINGS_CURRENT"::numeric END AS drawings_amount,
       CASE WHEN c."SK_DPD"::numeric >= 0 THEN c."SK_DPD"::numeric END AS dpd,
       CASE WHEN c."SK_DPD_DEF"::numeric >= 0 THEN c."SK_DPD_DEF"::numeric END AS dpd_def,
       CASE WHEN c."AMT_CREDIT_LIMIT_ACTUAL"::numeric > 0 AND c."AMT_BALANCE"::numeric IS NOT NULL
            THEN greatest(c."AMT_BALANCE"::numeric, 0) / c."AMT_CREDIT_LIMIT_ACTUAL"::numeric END AS utilization
FROM raw.credit_card_balance c
WHERE EXISTS (SELECT 1 FROM raw.application_train a WHERE a."SK_ID_CURR"::numeric = c."SK_ID_CURR"::numeric);

COMMIT;
