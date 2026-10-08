"""PostgreSQL integration tests against the explicit synthetic fixture.

Run the synthetic pipeline first, then set TEST_DATABASE_DSN to its dedicated DB.
Mutating edge tests use force_rollback transactions and execute the actual layer SQL.
No production-like claims are based on these synthetic observations.
"""
from __future__ import annotations

import json
import os
import re
import unittest
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


PROJECT = Path(__file__).resolve().parents[1]


def layer_sql(filename: str) -> str:
    """Use the actual layer inside a test-owned rollback transaction."""
    content = (PROJECT / "sql" / filename).read_text(encoding="utf-8")
    return re.sub(r"^(?:BEGIN|COMMIT);\s*$", "", content, flags=re.MULTILINE)


class PipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ.get("TEST_DATABASE_DSN")
        if not dsn:
            raise unittest.SkipTest("Set TEST_DATABASE_DSN after running the synthetic pipeline.")
        cls.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        database = cls.connection.execute("SELECT current_database() AS database").fetchone()["database"]
        if "synthetic" not in database:
            cls.connection.close()
            raise RuntimeError("Mutating integration tests require a dedicated database with 'synthetic' in its name.")
        meta_path = Path(os.environ.get("TEST_FIXTURE_META", PROJECT / "runs/synthetic/data/meta_fixture.json"))
        cls.metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        cls.edges = cls.metadata["edge_cases"]
        cls.connection.execute("SET statement_timeout='120s'")
        cls.connection.execute("SET work_mem='64MB'")
        count = cls.connection.execute("SELECT count(*) AS count FROM raw.application_train").fetchone()["count"]
        if count != cls.metadata["counts"]["application_train"]:
            cls.connection.close()
            raise RuntimeError("Loaded synthetic applicant count does not match fixture metadata.")

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def one(self, query, params=()):
        return self.connection.execute(query, params).fetchone()

    def emi(self, loan, number, version=1):
        return self.one("""SELECT * FROM intermediate.int_installment
            WHERE previous_loan_id=%s AND installment_number=%s AND installment_version=%s""", (loan, number, version))

    def test_raw_counts_match_fixture_and_all_columns_profiled(self):
        for table, expected in self.metadata["counts"].items():
            with self.subTest(table=table):
                actual = self.one(sql.SQL("SELECT count(*) AS n FROM raw.{}").format(sql.Identifier(table)))["n"]
                self.assertEqual(actual, expected)
                columns = self.one("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema='raw' AND table_name=%s", (table,))["n"]
                profiled = self.one("SELECT count(*) AS n FROM audit.dq_results WHERE table_name=%s AND check_name='null_pct'", (table,))["n"]
                self.assertEqual(profiled, columns)
        self.assertEqual(self.one("SELECT count(*) AS n FROM audit.dq_results WHERE status='FAIL'")["n"], 0)

    def test_one_row_per_applicant_no_join_explosion(self):
        for table in ("int_applicant_repay", "int_applicant_bureau", "int_applicant_prev", "int_applicant_pos", "int_applicant_cc"):
            with self.subTest(table=table):
                row = self.one(sql.SQL("SELECT count(*) AS n,count(DISTINCT applicant_id) AS distinct_n FROM intermediate.{}").format(sql.Identifier(table)))
                self.assertEqual(row["n"], row["distinct_n"])
        feature_count = self.one("SELECT count(*) AS n,count(DISTINCT applicant_id) AS distinct_n FROM marts.applicant_features")
        self.assertEqual(feature_count["n"], self.metadata["n_applicants"])
        self.assertEqual(feature_count["n"], feature_count["distinct_n"])
        self.assertGreater(self.one("SELECT count(*) AS n FROM marts.applicant_features WHERE previous_application_count=0 AND bureau_loan_count=0")["n"], 0)

    def test_staging_sentinels_invalid_ranges_and_raw_preservation(self):
        row = self.one("SELECT employed_years,is_employed_missing FROM staging.application WHERE applicant_id=%s", (self.edges["sentinel_employed_applicant_id"],))
        self.assertIsNone(row["employed_years"])
        self.assertEqual(row["is_employed_missing"], 1)
        self.assertEqual(self.one('SELECT "DAYS_EMPLOYED" AS v FROM raw.application_train WHERE "SK_ID_CURR"=%s', (100001,))["v"], 365243)
        self.assertIsNone(self.one("SELECT age_years FROM staging.application WHERE applicant_id=%s", (self.edges["invalid_age_applicant_id"],))["age_years"])
        invalid_amount = self.one("SELECT credit_amount,credit_to_income FROM staging.application WHERE applicant_id=%s", (self.edges["negative_credit_applicant_id"],))
        self.assertIsNone(invalid_amount["credit_amount"])
        self.assertIsNone(invalid_amount["credit_to_income"])
        self.assertEqual(self.one("SELECT income_amount FROM staging.application WHERE applicant_id=%s", (self.edges["high_income_applicant_id"],))["income_amount"], 15000000)
        previous = self.one("SELECT days_first_drawing,is_first_drawing_sentinel FROM staging.previous_application WHERE previous_loan_id=20000001")
        self.assertIsNone(previous["days_first_drawing"])
        self.assertEqual(previous["is_first_drawing_sentinel"], 1)
        self.assertIsNone(self.one("SELECT reject_reason FROM staging.previous_application WHERE previous_loan_id=20000001")["reject_reason"])

    def test_partial_payments_use_full_settlement_not_first_payment(self):
        row = self.emi(self.edges["partial_payment_loan_id"], 1)
        self.assertEqual(row["source_row_count"], 2)
        self.assertEqual(row["scheduled_amount"], 100)
        self.assertEqual(row["paid_amount_asof"], 100)
        self.assertEqual(row["paid_by_due_amount"], 50)
        self.assertEqual(row["settlement_day"], -20)
        self.assertEqual(row["days_late"], 10)
        self.assertEqual(row["paid_ratio"], 1)
        self.assertEqual(row["is_settled"], 1)

    def test_same_day_payments_and_distinct_installment_versions(self):
        row = self.emi(20000001, 2)
        self.assertEqual(row["paid_amount_asof"], 100)
        self.assertEqual(row["settlement_day"], -59)
        self.assertEqual(row["days_late"], 1)
        second_version = self.emi(20000001, 1, version=2)
        self.assertEqual(second_version["days_late"], 0)
        self.assertEqual(second_version["scheduled_amount"], 100)
        self.assertEqual(self.one("SELECT count(*) AS n FROM intermediate.int_installment WHERE previous_loan_id=20000001 AND installment_number=1")["n"], 2)

    def test_missing_payments_future_dates_and_overpayments(self):
        unpaid = self.emi(20000001, 3)
        self.assertEqual(unpaid["is_settled"], 0)
        self.assertEqual(unpaid["has_missing_payment"], 1)
        self.assertEqual(unpaid["paid_amount_asof"], 0)
        self.assertEqual(unpaid["days_late"], 10)
        self.assertIsNone(self.emi(20000001, 4))
        future_payment = self.emi(20000001, 5)
        self.assertEqual(future_payment["paid_amount_asof"], 0)
        self.assertEqual(future_payment["is_settled"], 0)
        self.assertEqual(future_payment["future_payment_row_count"], 1)
        self.assertEqual(future_payment["days_late"], 5)
        overpaid = self.emi(20000001, 6)
        self.assertEqual(overpaid["paid_amount_asof"], 125)
        self.assertEqual(overpaid["paid_ratio"], Decimal("1.25"))
        self.assertEqual(overpaid["outstanding_amount_asof"], 0)

    def test_schedule_conflicts_audited_without_splitting_emi(self):
        row = self.emi(20000002, 1)
        self.assertEqual(row["scheduled_amount"], 100)
        self.assertEqual(row["due_day"], -50)
        self.assertEqual(row["paid_amount_asof"], 80)
        self.assertEqual(row["paid_ratio"], Decimal("0.8"))
        self.assertEqual(row["is_due_day_conflict"], 1)
        self.assertEqual(row["is_scheduled_amount_conflict"], 1)
        self.assertEqual(row["days_late"], 50)
        for check in ("installment_scheduled_amount_conflict", "installment_due_day_conflict", "installment_exact_duplicate_source_rows"):
            self.assertGreater(self.one("SELECT issue_count FROM audit.dq_results WHERE check_name=%s", (check,))["issue_count"], 0)

    def test_applicant_aggregation_uses_sums_not_mean_of_loan_ratios(self):
        row = self.one("SELECT * FROM intermediate.int_applicant_repay WHERE applicant_id=100001")
        self.assertEqual(row["installment_count"], 7)
        self.assertEqual(row["scheduled_installment_amount"], 700)
        self.assertEqual(row["installment_paid_amount"], 505)
        self.assertAlmostEqual(float(row["installment_paid_ratio"]), 505 / 700)
        self.assertEqual(row["late_installment_count"], 5)
        self.assertEqual(row["max_days_late"], 50)

    def test_other_cohort_and_absent_parent_are_distinct(self):
        outside = self.edges["outside_training_applicant_id"]
        for view in ("bureau", "previous_application", "installments_payments", "pos_cash_balance", "credit_card_balance"):
            with self.subTest(view=view):
                self.assertEqual(self.one(sql.SQL("SELECT count(*) AS n FROM staging.{} WHERE applicant_id=%s").format(sql.Identifier(view)), (outside,))["n"], 0)
        self.assertEqual(self.one("SELECT count(*) AS n FROM staging.bureau_balance WHERE bureau_loan_id=%s", (self.edges["absent_bureau_parent_loan_id"],))["n"], 0)
        # Applicant-known installments survive missing previous-application extract.
        self.assertEqual(self.one("SELECT count(*) AS n FROM intermediate.int_prev_loan_repay WHERE previous_loan_id=%s", (self.edges["absent_previous_parent_loan_id"],))["n"], 1)
        for check in ("outside_training_cohort", "absent_previous_loan_parent", "absent_bureau_loan_parent"):
            self.assertGreater(self.one("SELECT sum(issue_count) AS n FROM audit.dq_results WHERE check_name=%s", (check,))["n"], 0)

    def test_bureau_unknown_closed_and_future_are_not_current(self):
        closed = self.one("SELECT dpd_bucket,dpd_lower_bound_days FROM intermediate.int_bureau_month WHERE bureau_loan_id=30000001 AND relative_month=0")
        self.assertEqual(closed["dpd_bucket"], "Closed")
        self.assertIsNone(closed["dpd_lower_bound_days"])
        unknown = self.one("SELECT dpd_bucket,dpd_lower_bound_days FROM intermediate.int_bureau_month WHERE bureau_loan_id=30000002 AND relative_month=0")
        self.assertEqual(unknown["dpd_bucket"], "Unknown")
        self.assertIsNone(unknown["dpd_lower_bound_days"])
        self.assertEqual(self.one("SELECT count(*) AS n FROM intermediate.int_bureau_month WHERE relative_month>0")["n"], 0)
        bureau = self.one("SELECT bureau_months_31plus,bureau_max_dpd FROM intermediate.int_applicant_bureau WHERE applicant_id=100001")
        self.assertEqual(bureau["bureau_months_31plus"], 4)
        self.assertEqual(bureau["bureau_max_dpd"], 121)

    def test_pos_card_and_future_month_exclusion(self):
        self.assertEqual(self.one("SELECT pos_max_dpd FROM intermediate.int_applicant_pos WHERE applicant_id=100001")["pos_max_dpd"], 5)
        card = self.one("SELECT * FROM intermediate.int_applicant_cc WHERE applicant_id=100001")
        self.assertEqual(card["cc_max_dpd"], 0)
        self.assertEqual(card["cc_utilization_max"], Decimal("0.5"))
        self.assertEqual(card["cc_utilization_mean"], Decimal("0.25"))
        self.assertEqual(card["cc_balance_sum_latest"], 25000)
        credit_balance = self.one("SELECT balance_amount,utilization,has_credit_balance FROM intermediate.int_cc_month WHERE applicant_id=100001 AND relative_month=-1")
        self.assertEqual(credit_balance["balance_amount"], -50)
        self.assertEqual(credit_balance["utilization"], 0)
        self.assertEqual(credit_balance["has_credit_balance"], 1)

    def test_duplicate_entity_keys_fail_dq_and_rollback(self):
        for table, column, key in (("application_train", "SK_ID_CURR", 100001), ("previous_application", "SK_ID_PREV", 20000001), ("bureau", "SK_ID_BUREAU", 30000001)):
            with self.subTest(table=table), self.connection.transaction(force_rollback=True):
                self.connection.execute(sql.SQL("INSERT INTO raw.{} SELECT * FROM raw.{} WHERE {}=%s").format(sql.Identifier(table), sql.Identifier(table), sql.Identifier(column)), (key,))
                self.connection.execute(layer_sql("03_dq_checks.sql"))
                check = self.one("SELECT status,issue_count FROM audit.dq_results WHERE check_name='primary_key_duplicates_or_null' AND table_name=%s", (table,))
                self.assertEqual(check["status"], "FAIL")
                self.assertEqual(check["issue_count"], 1)
        self.assertEqual(self.one("SELECT count(*) AS n FROM audit.dq_results WHERE status='FAIL'")["n"], 0)

    def test_repeated_month_snapshots_conservatively_aggregate_before_windows(self):
        with self.connection.transaction(force_rollback=True):
            self.connection.execute('INSERT INTO raw.bureau_balance ("SK_ID_BUREAU","MONTHS_BALANCE","STATUS") VALUES (30000001,-4,\'5\')')
            self.connection.execute('INSERT INTO raw.pos_cash_balance SELECT "SK_ID_PREV","SK_ID_CURR","MONTHS_BALANCE","CNT_INSTALMENT","CNT_INSTALMENT_FUTURE","NAME_CONTRACT_STATUS",40,40 FROM raw.pos_cash_balance WHERE "SK_ID_PREV"=20000001 AND "MONTHS_BALANCE"=0')
            columns = self.connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='raw' AND table_name='credit_card_balance' ORDER BY ordinal_position").fetchall()
            expressions = [sql.SQL("40000") if item["column_name"] == "AMT_BALANCE" else sql.Identifier(item["column_name"]) for item in columns]
            self.connection.execute(sql.SQL('INSERT INTO raw.credit_card_balance SELECT {} FROM raw.credit_card_balance WHERE "SK_ID_PREV"=20000002 AND "MONTHS_BALANCE"=0').format(sql.SQL(",").join(expressions)))
            self.connection.execute(layer_sql("03_dq_checks.sql"))
            self.assertEqual(self.one("SELECT count(*) AS n FROM audit.dq_results WHERE check_name='duplicate_loan_month_source_rows' AND status='WARN'")["n"], 3)
            self.connection.execute(layer_sql("05_intermediate.sql"))
            bureau = self.one("SELECT * FROM intermediate.int_bureau_month WHERE bureau_loan_id=30000001 AND relative_month=-4")
            self.assertEqual(bureau["source_row_count"], 2)
            self.assertEqual(bureau["dpd_lower_bound_days"], 121)
            self.assertEqual(bureau["is_status_conflict"], 1)
            self.assertEqual(self.one("SELECT pos_max_dpd FROM intermediate.int_applicant_pos WHERE applicant_id=100001")["pos_max_dpd"], 40)
            card = self.one("SELECT * FROM intermediate.int_cc_month WHERE previous_loan_id=20000002 AND relative_month=0")
            self.assertEqual(card["source_row_count"], 2)
            self.assertEqual(card["balance_amount"], 40000)
            self.assertEqual(card["utilization"], Decimal("0.8"))
            self.assertEqual(card["is_amount_conflict"], 1)
        self.assertEqual(self.one("SELECT pos_max_dpd FROM intermediate.int_applicant_pos WHERE applicant_id=100001")["pos_max_dpd"], 5)

    def test_star_schema_coverage_and_model_band_order(self):
        count = self.one("SELECT count(*) AS n,count(DISTINCT applicant_id) AS distinct_n FROM marts.fact_application")
        self.assertEqual(count["n"], self.metadata["n_applicants"])
        self.assertEqual(count["n"], count["distinct_n"])
        self.assertEqual(self.one("SELECT count(*) AS n FROM marts.dim_risk_band")["n"], 10)
        invalid = self.one("""SELECT count(*) AS n FROM marts.fact_application
            WHERE profile_id IS NULL OR contract_id IS NULL OR region_rating_id IS NULL
              OR risk_band NOT BETWEEN 1 AND 10 OR pd<=0 OR pd>=1 OR expected_loss<0""")["n"]
        self.assertEqual(invalid, 0)
        # Prediction-defined bands must retain their direction; observed bad rates
        # are allowed to be nonmonotonic on a noisy independent test sample.
        inversions = self.one("""SELECT count(*) AS n FROM (
            SELECT min_scored_pd,lag(max_scored_pd) OVER(ORDER BY risk_band) AS previous_max
            FROM marts.dim_risk_band) bands WHERE min_scored_pd<previous_max""")["n"]
        self.assertEqual(inversions, 0)
        joins = self.one("""SELECT count(*) AS n FROM marts.fact_application a
            JOIN marts.dim_profile p USING(profile_id)
            JOIN marts.dim_contract c USING(contract_id)
            JOIN marts.dim_region_rating r USING(region_rating_id)
            JOIN marts.dim_risk_band b USING(risk_band)""")["n"]
        self.assertEqual(joins, count["n"])

    def test_roll_rates_exclude_missing_month_gap_and_future_history(self):
        # Restrict the actual input table inside a rollback transaction so the
        # aggregate mart can be checked against this loan's known event sequence.
        with self.connection.transaction(force_rollback=True):
            self.connection.execute("DELETE FROM intermediate.int_bureau_month WHERE bureau_loan_id<>30000001")
            self.connection.execute(layer_sql("06_marts.sql"))
            rows = self.connection.execute("SELECT relative_month,from_bucket,to_bucket,transition_count,from_bucket_count,roll_rate FROM marts.fact_roll_rate ORDER BY relative_month").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[0]["relative_month"], rows[0]["from_bucket"], rows[0]["to_bucket"]), (-3, "1-30", "31-60"))
            self.assertEqual((rows[1]["relative_month"], rows[1]["from_bucket"], rows[1]["to_bucket"]), (0, "Current", "Closed"))
            for row in rows:
                self.assertEqual(row["transition_count"], 1)
                self.assertEqual(row["from_bucket_count"], 1)
                self.assertEqual(row["roll_rate"], 1)
            self.assertEqual(self.one("SELECT count(*) AS n FROM marts.fact_repayment_monthly")["n"], 4)
        self.assertGreater(self.one("SELECT count(*) AS n FROM marts.fact_repayment_monthly")["n"], 4)

    def test_roll_denominators_and_compact_monthly_export_counts(self):
        # Every from-bucket denominator is reconstructed from transition counts;
        # each distribution sums to one at its complete segment grain.
        invalid = self.one("""SELECT count(*) AS n FROM (
            SELECT relative_month,from_bucket,risk_band,contract_id,region_rating_id,
                   sum(transition_count) AS reconstructed_denominator,
                   min(from_bucket_count) AS denominator_min,
                   max(from_bucket_count) AS denominator_max,
                   sum(roll_rate) AS total_rate
            FROM marts.fact_roll_rate
            GROUP BY relative_month,from_bucket,risk_band,contract_id,region_rating_id
        ) checks WHERE reconstructed_denominator<>denominator_min OR denominator_min<>denominator_max
                   OR abs(total_rate-1)>0.000000001""")["n"]
        self.assertEqual(invalid, 0)
        detail = self.one("SELECT count(*) AS n FROM marts.fact_repayment_monthly")["n"]
        aggregate = self.one("SELECT sum(observed_loan_month_count) AS n FROM marts.agg_repayment_monthly")["n"]
        self.assertEqual(detail, aggregate)
        self.assertEqual(self.one("SELECT count(*) AS n FROM marts.fact_repayment_monthly WHERE relative_month>0")["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
