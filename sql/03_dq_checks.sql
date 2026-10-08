-- Latest-run audit snapshot. The runner archives it before the next execution.
-- Profiling all nullable fields, sentinels, placeholders and negative amounts uses
-- one aggregate scan per table; to_jsonb wraps the SINGLE aggregate output record.
-- Raw CSV values remain unchanged. WARN records need interpretation, not deletion.
BEGIN;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE TABLE IF NOT EXISTS audit.dq_results (
    check_name text NOT NULL,
    table_name text NOT NULL,
    column_name text,
    metric_value numeric,
    row_count bigint,
    issue_count bigint,
    status text NOT NULL CHECK (status IN ('PASS', 'WARN', 'FAIL')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    checked_at timestamptz NOT NULL DEFAULT transaction_timestamp()
);
DELETE FROM audit.dq_results;

DO $$
DECLARE
    table_rec record;
    column_rec record;
    aggregates text;
    counts jsonb;
    total bigint;
    issues bigint;
    numeric_column boolean;
BEGIN
    FOR table_rec IN
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'raw' AND table_type = 'BASE TABLE'
          AND table_name IN ('application_train','bureau','bureau_balance',
              'previous_application','installments_payments','pos_cash_balance','credit_card_balance')
        ORDER BY table_name
    LOOP
        aggregates := 'count(*) AS "__rows"';
        FOR column_rec IN
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = 'raw' AND table_name = table_rec.table_name ORDER BY ordinal_position
        LOOP
            numeric_column := column_rec.data_type IN ('smallint','integer','bigint','numeric','decimal','real','double precision');
            aggregates := aggregates || format(', count(%I) AS %I', column_rec.column_name, 'nonnull__' || column_rec.column_name);
            IF numeric_column AND column_rec.column_name LIKE 'DAYS_%' THEN
                aggregates := aggregates || format(', count(*) FILTER (WHERE abs(%I) = 365243) AS %I', column_rec.column_name, 'sentinel__' || column_rec.column_name);
            END IF;
            IF column_rec.data_type IN ('text','character varying','character') THEN
                aggregates := aggregates || format(', count(*) FILTER (WHERE upper(btrim(%I::text)) IN (''XNA'',''XAP'')) AS %I', column_rec.column_name, 'placeholder__' || column_rec.column_name);
            END IF;
            IF numeric_column AND column_rec.column_name LIKE 'AMT_%' THEN
                aggregates := aggregates || format(', count(*) FILTER (WHERE %I < 0) AS %I', column_rec.column_name, 'negative__' || column_rec.column_name);
            END IF;
        END LOOP;
        EXECUTE format('SELECT to_jsonb(profile) FROM (SELECT %s FROM raw.%I) AS profile', aggregates, table_rec.table_name) INTO counts;
        total := (counts ->> '__rows')::bigint;
        INSERT INTO audit.dq_results(check_name,table_name,metric_value,row_count,issue_count,status,details)
        VALUES ('row_count',table_rec.table_name,total,total,0,'PASS',jsonb_build_object('scope','loaded raw CSV, count compared with source by runner'));
        FOR column_rec IN
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = 'raw' AND table_name = table_rec.table_name ORDER BY ordinal_position
        LOOP
            issues := total - (counts ->> ('nonnull__' || column_rec.column_name))::bigint;
            INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
            VALUES ('null_pct',table_rec.table_name,column_rec.column_name,100.0*issues/NULLIF(total,0),total,issues,
                    CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,jsonb_build_object('unit','percent','policy','missingness profiled, no raw mutation'));
            IF counts ? ('sentinel__' || column_rec.column_name) THEN
                issues := (counts ->> ('sentinel__' || column_rec.column_name))::bigint;
                INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
                VALUES ('sentinel_365243',table_rec.table_name,column_rec.column_name,issues,total,issues,
                        CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,jsonb_build_object('sentinel','absolute value 365243','policy','staging NULL plus available missing flags'));
            END IF;
            IF counts ? ('placeholder__' || column_rec.column_name) THEN
                issues := (counts ->> ('placeholder__' || column_rec.column_name))::bigint;
                INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
                VALUES ('placeholder_xna_xap',table_rec.table_name,column_rec.column_name,issues,total,issues,
                        CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,jsonb_build_object('values',jsonb_build_array('XNA','XAP'),'policy','staging normalized to NULL'));
            END IF;
            IF counts ? ('negative__' || column_rec.column_name) THEN
                issues := (counts ->> ('negative__' || column_rec.column_name))::bigint;
                INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
                VALUES ('negative_amount',table_rec.table_name,column_rec.column_name,issues,total,issues,
                        CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,
                        jsonb_build_object('policy',CASE WHEN table_rec.table_name='credit_card_balance' AND column_rec.column_name='AMT_BALANCE'
                            THEN 'negative card balance may be overpayment; retained and flagged, utilization floored at zero'
                            ELSE 'unexpected negatives audited; analyzed amount fields NULL in staging' END));
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- Only entity keys are required to be unique; applicant IDs repeat in histories.
DO $$
DECLARE r record; total bigint; issues bigint; nulls bigint;
BEGIN
    FOR r IN SELECT * FROM (VALUES ('application_train','SK_ID_CURR'),('previous_application','SK_ID_PREV'),('bureau','SK_ID_BUREAU')) AS t(table_name,column_name)
    LOOP
        EXECUTE format('SELECT count(*),count(*)-count(DISTINCT %I),count(*) FILTER (WHERE %I IS NULL) FROM raw.%I',r.column_name,r.column_name,r.table_name)
            INTO total,issues,nulls;
        INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
        VALUES ('primary_key_duplicates_or_null',r.table_name,r.column_name,issues,total,issues,
                CASE WHEN issues>0 THEN 'FAIL' ELSE 'PASS' END,
                jsonb_build_object('null_keys',nulls,'duplicate_excess_nonnull',issues-nulls,'policy','blocking: entity ownership must be unambiguous'));
    END LOOP;
END $$;

-- Histories contain train/test and possibly other extract cohorts. Exclusion is
-- based on presence in application_train; no assertion about absent test outcomes.
DO $$
DECLARE r record; total bigint; issues bigint;
BEGIN
    FOR r IN SELECT unnest(ARRAY['bureau','previous_application','installments_payments','pos_cash_balance','credit_card_balance']) AS table_name
    LOOP
        EXECUTE format('SELECT count(*),count(*) FILTER (WHERE a."SK_ID_CURR" IS NULL) FROM raw.%I h LEFT JOIN raw.application_train a ON a."SK_ID_CURR"=h."SK_ID_CURR"',r.table_name)
            INTO total,issues;
        INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
        VALUES ('outside_training_cohort',r.table_name,'SK_ID_CURR',issues,total,issues,
                CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,
                jsonb_build_object('policy','exclude from staging train analysis; these are not automatically corrupt foreign keys','other_cohort_outcomes','unknown'));
    END LOOP;
END $$;

-- Loan-parent absence is audited separately. Some legitimate history loans can
-- be absent in the prior-application extract; the applicant cohort is authoritative.
DO $$
DECLARE r record; total bigint; issues bigint; owner_conflicts bigint;
BEGIN
    FOR r IN SELECT unnest(ARRAY['installments_payments','pos_cash_balance','credit_card_balance']) AS table_name
    LOOP
        EXECUTE format('SELECT count(*),count(*) FILTER (WHERE p."SK_ID_PREV" IS NULL),count(*) FILTER (WHERE p."SK_ID_PREV" IS NOT NULL AND h."SK_ID_CURR" IS DISTINCT FROM p."SK_ID_CURR") FROM raw.%I h LEFT JOIN raw.previous_application p ON p."SK_ID_PREV"=h."SK_ID_PREV"',r.table_name)
            INTO total,issues,owner_conflicts;
        INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
        VALUES ('absent_previous_loan_parent',r.table_name,'SK_ID_PREV',issues,total,issues,
                CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,jsonb_build_object('policy','audit missing parent extracts separately from applicant cohort; do not invent outcomes'));
        INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
        VALUES ('previous_loan_owner_conflict',r.table_name,'SK_ID_CURR',owner_conflicts,total,owner_conflicts,
                CASE WHEN owner_conflicts>0 THEN 'FAIL' ELSE 'PASS' END,jsonb_build_object('policy','blocking if source history contradicts a known loan parent applicant'));
    END LOOP;
END $$;
INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
SELECT 'absent_bureau_loan_parent','bureau_balance','SK_ID_BUREAU',count(*) FILTER(WHERE b."SK_ID_BUREAU" IS NULL),count(*),count(*) FILTER(WHERE b."SK_ID_BUREAU" IS NULL),
       CASE WHEN count(*) FILTER(WHERE b."SK_ID_BUREAU" IS NULL)>0 THEN 'WARN' ELSE 'PASS' END,
       jsonb_build_object('policy','balance history without bureau loan parent excluded by staging existence filter')
FROM raw.bureau_balance bb LEFT JOIN raw.bureau b ON b."SK_ID_BUREAU"=bb."SK_ID_BUREAU";

WITH profile AS (
    SELECT count(*) total,
           count(*) FILTER (WHERE "DAYS_BIRTH" < -36525 OR "DAYS_BIRTH" >= 0) invalid_age,
           count(*) FILTER (WHERE "AMT_INCOME_TOTAL" > 10000000) high_income,
           count(*) FILTER (WHERE "AMT_INCOME_TOTAL" <= 0) invalid_income,
           count(*) FILTER (WHERE "TARGET" NOT IN (0,1) OR "TARGET" IS NULL) invalid_target
    FROM raw.application_train
)
INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
SELECT v.name,'application_train',v.column_name,v.issues,p.total,v.issues,
       CASE WHEN v.issues=0 THEN 'PASS' WHEN v.name='invalid_target' THEN 'FAIL' ELSE 'WARN' END,v.details
FROM profile p CROSS JOIN LATERAL (VALUES
    ('invalid_age','DAYS_BIRTH',p.invalid_age,jsonb_build_object('bounds','negative days, age <=100 years','policy','NULL invalid age in staging')),
    ('income_outlier_gt_10m','AMT_INCOME_TOTAL',p.high_income,jsonb_build_object('threshold',10000000,'policy','audit only; no automatic deletion')),
    ('nonpositive_income','AMT_INCOME_TOTAL',p.invalid_income,jsonb_build_object('policy','NULL invalid income and derived denominator ratios')),
    ('invalid_target','TARGET',p.invalid_target,jsonb_build_object('allowed',jsonb_build_array(0,1),'policy','blocking training outcome validation'))
) AS v(name,column_name,issues,details);

-- Four-column EMI grain is essential: an installment version is NOT interchangeable
-- with its sequence number. Count schedule conflicts before choosing conservative values.
WITH installment_groups AS MATERIALIZED (
    SELECT "SK_ID_CURR","SK_ID_PREV","NUM_INSTALMENT_VERSION","NUM_INSTALMENT_NUMBER",
           count(*) source_rows,
           count(DISTINCT ROW("DAYS_INSTALMENT","DAYS_ENTRY_PAYMENT","AMT_INSTALMENT","AMT_PAYMENT")) distinct_source_rows,
           count(DISTINCT "AMT_INSTALMENT") schedule_amount_count,
           count(DISTINCT "DAYS_INSTALMENT") due_day_count,
           bool_or("AMT_PAYMENT" IS NULL OR "DAYS_ENTRY_PAYMENT" IS NULL) missing_payment,
           bool_or("DAYS_ENTRY_PAYMENT">0) future_payment,
           bool_or("DAYS_INSTALMENT">0) future_due,
           bool_or("NUM_INSTALMENT_VERSION" IS NULL OR "NUM_INSTALMENT_NUMBER" IS NULL) invalid_grain
    FROM raw.installments_payments
    GROUP BY "SK_ID_CURR","SK_ID_PREV","NUM_INSTALMENT_VERSION","NUM_INSTALMENT_NUMBER"
), profile AS (
    SELECT coalesce(sum(source_rows),0)::bigint total,
           count(*) FILTER(WHERE source_rows>1) multiple_rows,
           coalesce(sum(source_rows-distinct_source_rows),0)::bigint exact_duplicate_excess,
           count(*) FILTER(WHERE schedule_amount_count>1) schedule_conflicts,
           count(*) FILTER(WHERE due_day_count>1) due_conflicts,
           count(*) FILTER(WHERE missing_payment) missing_payments,
           count(*) FILTER(WHERE future_payment) future_payments,
           count(*) FILTER(WHERE future_due) future_dues,
           count(*) FILTER(WHERE invalid_grain) invalid_grains
    FROM installment_groups
)
INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
SELECT v.name,'installments_payments',v.column_name,v.issues,p.total,v.issues,
       CASE WHEN v.issues=0 THEN 'PASS' WHEN v.name='installment_invalid_grain' THEN 'FAIL' ELSE 'WARN' END,
       v.details || jsonb_build_object('grain',jsonb_build_array('SK_ID_CURR','SK_ID_PREV','NUM_INSTALMENT_VERSION','NUM_INSTALMENT_NUMBER'))
FROM profile p CROSS JOIN LATERAL (VALUES
    ('installment_multiple_payment_rows',NULL::text,p.multiple_rows,jsonb_build_object('unit','EMIs with multiple source rows','policy','sum payments once, scheduled amount once per EMI')),
    ('installment_exact_duplicate_source_rows',NULL::text,p.exact_duplicate_excess,jsonb_build_object('unit','duplicate excess source rows','policy','audited, no silent deletion; payment sum may include repeat source events')),
    ('installment_scheduled_amount_conflict','AMT_INSTALMENT',p.schedule_conflicts,jsonb_build_object('unit','EMIs','policy','MAX scheduled amount, conflict flag')),
    ('installment_due_day_conflict','DAYS_INSTALMENT',p.due_conflicts,jsonb_build_object('unit','EMIs','policy','MIN due day, conflict flag')),
    ('installment_missing_payment',NULL::text,p.missing_payments,jsonb_build_object('unit','EMIs','policy','missing is not a proven zero amount; unsettled arrears lower bound at application date')),
    ('installment_future_payment','DAYS_ENTRY_PAYMENT',p.future_payments,jsonb_build_object('unit','EMIs','policy','positive days excluded from observed payment sums and settlement')),
    ('installment_future_due','DAYS_INSTALMENT',p.future_dues,jsonb_build_object('unit','EMIs','policy','positive due days excluded from repayment history')),
    ('installment_invalid_grain',NULL::text,p.invalid_grains,jsonb_build_object('unit','EMIs','policy','blocking: missing sequence/version cannot establish unique EMI grain'))
) AS v(name,column_name,issues,details);

-- Bureau/POS/card snapshots can have multiple source rows at a loan/month grain.
-- Audit the excess; intermediate uses one month with conservative MAX DPD, not a
-- nondeterministic LAG ordering between same-month rows.
DO $$
DECLARE r record; total bigint; issues bigint; conflicts bigint; future_rows bigint;
BEGIN
    FOR r IN SELECT * FROM (VALUES ('bureau_balance','SK_ID_BUREAU'),('pos_cash_balance','SK_ID_PREV'),('credit_card_balance','SK_ID_PREV')) AS t(table_name,key_column)
    LOOP
        EXECUTE format('SELECT sum(n)::bigint,coalesce(sum(n-1),0)::bigint FROM (SELECT count(*) n FROM raw.%I GROUP BY %I,"MONTHS_BALANCE") grouped',r.table_name,r.key_column)
            INTO total,issues;
        INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
        VALUES ('duplicate_loan_month_source_rows',r.table_name,'MONTHS_BALANCE',issues,total,issues,
                CASE WHEN issues>0 THEN 'WARN' ELSE 'PASS' END,jsonb_build_object('policy','aggregate one snapshot per loan/month before windows; MAX observed DPD with conflicts flagged'));
        EXECUTE format('SELECT count(*) FILTER(WHERE "MONTHS_BALANCE">0) FROM raw.%I',r.table_name) INTO future_rows;
        INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
        VALUES ('future_relative_month',r.table_name,'MONTHS_BALANCE',future_rows,total,future_rows,
                CASE WHEN future_rows>0 THEN 'WARN' ELSE 'PASS' END,jsonb_build_object('policy','positive relative months excluded from observed features and roll rates'));
        IF r.table_name<>'bureau_balance' THEN
            EXECUTE format('SELECT count(*) FROM (SELECT %I FROM raw.%I GROUP BY %I HAVING count(DISTINCT "SK_ID_CURR")>1) conflicted',r.key_column,r.table_name,r.key_column) INTO conflicts;
            INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
            VALUES ('history_loan_multiple_applicants',r.table_name,'SK_ID_CURR',conflicts,total,conflicts,
                    CASE WHEN conflicts>0 THEN 'FAIL' ELSE 'PASS' END,jsonb_build_object('policy','blocking: a loan cannot belong to multiple applicants'));
        END IF;
    END LOOP;
END $$;

INSERT INTO audit.dq_results(check_name,table_name,column_name,metric_value,row_count,issue_count,status,details)
SELECT 'unknown_bureau_status','bureau_balance','STATUS',count(*) FILTER(WHERE "STATUS"::text NOT IN ('C','X','0','1','2','3','4','5') OR "STATUS" IS NULL),count(*),
       count(*) FILTER(WHERE "STATUS"::text NOT IN ('C','X','0','1','2','3','4','5') OR "STATUS" IS NULL),
       CASE WHEN count(*) FILTER(WHERE "STATUS"::text NOT IN ('C','X','0','1','2','3','4','5') OR "STATUS" IS NULL)>0 THEN 'WARN' ELSE 'PASS' END,
       jsonb_build_object('policy','unknown category, NULL DPD; never reinterpret as Current')
FROM raw.bureau_balance;

COMMIT;
