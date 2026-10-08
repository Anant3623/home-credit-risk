"""Read-only acceptance checks against the finished real or synthetic warehouse."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from .database import connect,local_dsn
from .generate_ddl import FILES

def verify(dsn: str,output_dir: Path,data_status: str="real") -> list[dict]:
    checks=[]
    with connect(dsn) as db:
        def check(name: str,query: str,expected=0):
            actual=db.execute(query).fetchone()[0]
            checks.append({"check":name,"actual":actual,"expected":expected,"passed":actual==expected})
        n=db.execute("SELECT count(*) FROM raw.application_train").fetchone()[0]
        for table,(_,count) in FILES.items():
            if data_status=="real":
                check(f"raw_count_{table}",f"SELECT count(*) FROM raw.{table}",count)
        for table in ["int_applicant_repay","int_applicant_bureau","int_applicant_prev","int_applicant_pos","int_applicant_cc"]:
            check(f"unique_{table}",f"SELECT count(*)-count(DISTINCT applicant_id) FROM intermediate.{table}")
        check("unique_fact_application","SELECT count(*)-count(DISTINCT applicant_id) FROM marts.fact_application")
        check("application_coverage","SELECT count(*) FROM marts.fact_application",n)
        check("critical_dq_failures","SELECT count(*) FROM audit.dq_results WHERE status='FAIL'")
        check("model_target_alignment","SELECT count(*) FROM marts.fact_application f JOIN raw.application_train a ON a.\"SK_ID_CURR\"=f.applicant_id WHERE f.target IS DISTINCT FROM a.\"TARGET\"::int")
        check("valid_score_probability","SELECT count(*) FROM marts.fact_application WHERE pd IS NULL OR pd<=0 OR pd>=1 OR risk_band IS NULL OR risk_band NOT BETWEEN 1 AND 10")
        check("complete_dimension_keys","SELECT count(*) FROM marts.fact_application WHERE profile_id IS NULL OR contract_id IS NULL OR region_rating_id IS NULL")
        check("monthly_grain_unique","SELECT count(*)-count(DISTINCT (bureau_loan_id,relative_month)) FROM marts.fact_repayment_monthly")
        detail=db.execute("SELECT count(*) FROM marts.fact_repayment_monthly").fetchone()[0]
        check("monthly_summary_reconciles","SELECT sum(observed_loan_month_count) FROM marts.agg_repayment_monthly",detail)
        rolls=db.execute("""SELECT count(*) FROM (SELECT relative_month-lag(relative_month) OVER(PARTITION BY bureau_loan_id ORDER BY relative_month) gap
              FROM intermediate.int_bureau_month) t WHERE gap=1""").fetchone()[0]
        check("roll_counts_consecutive_months","SELECT coalesce(sum(transition_count),0) FROM marts.fact_roll_rate",rolls)
        check("roll_denominators_sum_to_one","""SELECT count(*) FROM (SELECT relative_month,from_bucket,risk_band,contract_id,region_rating_id,
            abs(sum(roll_rate)-1) deviation FROM marts.fact_roll_rate GROUP BY 1,2,3,4,5) t WHERE deviation>0.000000001""")
        check("score_direction","""SELECT count(*) FROM (SELECT pd,risk_score,lag(risk_score) OVER(ORDER BY pd) previous_score
            FROM marts.fact_application) t WHERE risk_score>previous_score+0.000001""")
        check("predicted_band_order","""SELECT count(*) FROM (SELECT risk_band,min(pd) low,lag(max(pd)) OVER(ORDER BY risk_band) previous_high
            FROM marts.fact_application GROUP BY risk_band) t WHERE low<previous_high-0.000000001""")
    output_dir.mkdir(parents=True,exist_ok=True)
    with (output_dir / "acceptance_checks.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["check","actual","expected","passed"])
        writer.writeheader();writer.writerows(checks)
    (output_dir / "acceptance_checks.json").write_text(json.dumps(checks,indent=2,default=str))
    if any(not c["passed"] for c in checks):
        raise AssertionError(f"Acceptance check failures: {[c for c in checks if not c['passed']]}")
    return checks

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dsn")
    p.add_argument("--output-dir",type=Path,default=Path("runs/real"))
    p.add_argument("--synthetic",action="store_true")
    a=p.parse_args(); status="synthetic" if a.synthetic else "real"
    result=verify(a.dsn or local_dsn("homecredit_"+status),a.output_dir,status)
    print(f"{len(result)} read-only warehouse acceptance checks passed")
