-- Run AFTER Python scoring and validation-defined cutoff tables are imported.
-- These star dimensions describe CURRENT applications; historical fact segments
-- are retrospective groupings by current application score, not historical scores.
BEGIN;
CREATE SCHEMA IF NOT EXISTS marts;
DROP TABLE IF EXISTS marts.fact_roll_rate;
DROP TABLE IF EXISTS marts.agg_repayment_monthly;
DROP TABLE IF EXISTS marts.fact_repayment_monthly;
DROP TABLE IF EXISTS marts.fact_application;
DROP TABLE IF EXISTS marts.dim_profile;
DROP TABLE IF EXISTS marts.dim_contract;
DROP TABLE IF EXISTS marts.dim_region_rating;
DROP TABLE IF EXISTS marts.dim_risk_band;

CREATE TABLE marts.dim_profile AS
WITH profiles AS (
    SELECT DISTINCT coalesce(income_type,'Unknown') AS income_type,
           coalesce(education_type,'Unknown') AS education_type,
           coalesce(family_status,'Unknown') AS family_status,
           CASE WHEN age_years IS NULL THEN 'Unknown'
                WHEN age_years<25 THEN '<25' WHEN age_years<35 THEN '25-34'
                WHEN age_years<45 THEN '35-44' WHEN age_years<55 THEN '45-54'
                WHEN age_years<65 THEN '55-64' ELSE '65+' END AS age_band
    FROM marts.applicant_features
)
SELECT row_number() OVER (ORDER BY income_type,education_type,family_status,age_band)::integer AS profile_id, profiles.*
FROM profiles;
ALTER TABLE marts.dim_profile ADD PRIMARY KEY (profile_id);
CREATE UNIQUE INDEX dim_profile_natural_key ON marts.dim_profile(income_type,education_type,family_status,age_band);

CREATE TABLE marts.dim_contract AS
SELECT row_number() OVER (ORDER BY contract_type)::integer AS contract_id,contract_type
FROM (SELECT DISTINCT coalesce(contract_type,'Unknown') contract_type FROM marts.applicant_features) types;
ALTER TABLE marts.dim_contract ADD PRIMARY KEY(contract_id);

CREATE TABLE marts.dim_region_rating AS
SELECT row_number() OVER(ORDER BY region_rating,region_rating_city)::integer AS region_rating_id,
       region_rating,region_rating_city
FROM (SELECT DISTINCT coalesce(region_rating,0) AS region_rating,coalesce(region_rating_city,0) AS region_rating_city
      FROM marts.applicant_features) ratings;
ALTER TABLE marts.dim_region_rating ADD PRIMARY KEY(region_rating_id);
COMMENT ON COLUMN marts.dim_region_rating.region_rating IS '0 = missing/invalid rating, not an observed fourth rating';

CREATE TABLE marts.dim_risk_band AS
SELECT bands.risk_band::integer,
       'Band '||bands.risk_band AS risk_band_name,
       min(s.pd) AS min_scored_pd,max(s.pd) AS max_scored_pd,
       count(s.applicant_id) AS applicants,
       count(s.applicant_id) FILTER(WHERE split='test') AS test_applicants,
       avg(s.target::numeric) FILTER(WHERE split='test') AS test_bad_rate,
       avg(s.pd) FILTER(WHERE split='test') AS test_mean_predicted_pd,
       count(s.applicant_id) FILTER(WHERE split='validation') AS validation_applicants,
       avg(s.target::numeric) FILTER(WHERE split='validation') AS validation_bad_rate,
       'Validation prediction deciles; 1 lowest risk, 10 highest risk'::text AS band_definition
FROM generate_series(1,10) bands(risk_band)
LEFT JOIN marts.model_scores s ON s.risk_band=bands.risk_band
GROUP BY bands.risk_band;
ALTER TABLE marts.dim_risk_band ADD PRIMARY KEY(risk_band);

CREATE TABLE marts.fact_application AS
SELECT f.*,p.profile_id,c.contract_id,r.region_rating_id,
       s.pd,s.risk_score,s.risk_band,s.expected_loss,s.split,s.model_version,
       f.credit_amount AS ead_proxy
FROM marts.applicant_features f
JOIN marts.model_scores s USING(applicant_id)
JOIN marts.dim_profile p ON
    p.income_type=coalesce(f.income_type,'Unknown') AND
    p.education_type=coalesce(f.education_type,'Unknown') AND
    p.family_status=coalesce(f.family_status,'Unknown') AND
    p.age_band=CASE WHEN f.age_years IS NULL THEN 'Unknown'
                WHEN f.age_years<25 THEN '<25' WHEN f.age_years<35 THEN '25-34'
                WHEN f.age_years<45 THEN '35-44' WHEN f.age_years<55 THEN '45-54'
                WHEN f.age_years<65 THEN '55-64' ELSE '65+' END
JOIN marts.dim_contract c ON c.contract_type=coalesce(f.contract_type,'Unknown')
JOIN marts.dim_region_rating r ON r.region_rating=coalesce(f.region_rating,0)
     AND r.region_rating_city=coalesce(f.region_rating_city,0);
ALTER TABLE marts.fact_application ADD PRIMARY KEY(applicant_id);
ALTER TABLE marts.fact_application ADD FOREIGN KEY(profile_id) REFERENCES marts.dim_profile(profile_id);
ALTER TABLE marts.fact_application ADD FOREIGN KEY(contract_id) REFERENCES marts.dim_contract(contract_id);
ALTER TABLE marts.fact_application ADD FOREIGN KEY(region_rating_id) REFERENCES marts.dim_region_rating(region_rating_id);
ALTER TABLE marts.fact_application ADD FOREIGN KEY(risk_band) REFERENCES marts.dim_risk_band(risk_band);
DO $$ BEGIN
IF (SELECT count(*) FROM marts.fact_application) <> (SELECT count(*) FROM raw.application_train) THEN
RAISE EXCEPTION 'Mart application coverage differs from labelled applicants'; END IF;
IF EXISTS(SELECT 1 FROM marts.fact_application WHERE pd IS NULL OR pd<=0 OR pd>=1 OR risk_band IS NULL OR risk_band NOT BETWEEN 1 AND 10
    OR risk_score IS NULL OR risk_score IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8)
    OR expected_loss<0 OR expected_loss IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8)
    OR (ead_proxy IS NOT NULL AND expected_loss IS NULL)) THEN
RAISE EXCEPTION 'Invalid model probability or risk band'; END IF;
END $$;

-- Detailed warehouse fact: exactly one bureau loan x observed relative month.
CREATE TABLE marts.fact_repayment_monthly AS
SELECT b.applicant_id,b.bureau_loan_id,b.relative_month,b.dpd_bucket,b.dpd_lower_bound_days,
       b.source_row_count,b.is_status_conflict,
       a.profile_id,a.contract_id,a.region_rating_id,a.risk_band
FROM intermediate.int_bureau_month b JOIN marts.fact_application a USING(applicant_id);
CREATE UNIQUE INDEX fact_repayment_monthly_key ON marts.fact_repayment_monthly(bureau_loan_id,relative_month);
COMMENT ON TABLE marts.fact_repayment_monthly IS 'Bureau loan/month detail remains in PostgreSQL; export uses the compact agg_repayment_monthly table';

-- Compact export: no loan-month detail, no summed percentage rates.
CREATE TABLE marts.agg_repayment_monthly AS
SELECT relative_month,dpd_bucket,risk_band,contract_id,region_rating_id,
       count(*)::bigint AS observed_loan_month_count,
       count(DISTINCT applicant_id)::bigint AS distinct_applicant_count,
       count(*) FILTER(WHERE dpd_lower_bound_days>=31)::bigint AS months_31plus_count,
       sum(is_status_conflict)::bigint AS conflicting_month_count
FROM marts.fact_repayment_monthly
GROUP BY relative_month,dpd_bucket,risk_band,contract_id,region_rating_id;
COMMENT ON COLUMN marts.agg_repayment_monthly.distinct_applicant_count IS 'Nonadditive across months/buckets/segments; use fact_application for applicant denominators';

-- LAG only represents a one-month roll when month difference = 1.
-- Closed and Unknown stay explicit states. A missing month is not a transition.
CREATE TABLE marts.fact_roll_rate AS
WITH ordered AS (
    SELECT bureau_loan_id,relative_month,dpd_bucket AS to_bucket,risk_band,contract_id,region_rating_id,
           lag(relative_month) OVER(PARTITION BY bureau_loan_id ORDER BY relative_month) AS previous_month,
           lag(dpd_bucket) OVER(PARTITION BY bureau_loan_id ORDER BY relative_month) AS from_bucket
    FROM marts.fact_repayment_monthly
), counts AS (
    SELECT relative_month,from_bucket,to_bucket,risk_band,contract_id,region_rating_id,
           count(*)::bigint AS transition_count
    FROM ordered WHERE relative_month-previous_month=1
    GROUP BY relative_month,from_bucket,to_bucket,risk_band,contract_id,region_rating_id
)
SELECT counts.*,sum(transition_count) OVER(PARTITION BY relative_month,from_bucket,risk_band,contract_id,region_rating_id)::bigint AS from_bucket_count,
       transition_count::numeric / NULLIF(sum(transition_count) OVER(PARTITION BY relative_month,from_bucket,risk_band,contract_id,region_rating_id),0) AS roll_rate
FROM counts;
COMMENT ON COLUMN marts.fact_roll_rate.roll_rate IS 'Nonadditive; after filters use SUM(transition_count) / total transitions leaving the selected from bucket';
ANALYZE marts.fact_application;
ANALYZE marts.fact_repayment_monthly;
ANALYZE marts.fact_roll_rate;
COMMIT;
