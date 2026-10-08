"""Run raw -> staging -> intermediate -> marts -> analysis -> CSV exports."""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import os
import time
from pathlib import Path
from psycopg import sql
from .database import PROJECT, connect, export_query, local_dsn
from .generate_ddl import FILES, generate
from .load_raw import load_raw

def execute_file(connection, name: str) -> None:
    print(f"Running {name}", flush=True)
    start = time.monotonic()
    try:
        connection.execute((PROJECT / "sql" / name).read_text())
    except Exception:
        connection.execute("ROLLBACK")
        raise
    print(f"Finished {name} in {time.monotonic()-start:.1f}s", flush=True)

def build_features(connection, generated_dir: Path) -> None:
    select = ["a.*"]
    joins = []
    for alias, table in [("r","int_applicant_repay"),("b","int_applicant_bureau"),("p","int_applicant_prev"),("o","int_applicant_pos"),("c","int_applicant_cc")]:
        columns = connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='intermediate' AND table_name=%s ORDER BY ordinal_position", (table,)).fetchall()
        if not columns:
            raise ValueError(f"Missing intermediate table {table}")
        for (column,) in columns:
            if column == "applicant_id":
                continue
            # Known absent histories can have zero counts; monetary measurements
            # and rates stay NULL so an absent record cannot invent a balance.
            is_count = column.endswith("_count") or column in {"prev_loans_with_installments","bureau_months_31plus"}
            value = f'COALESCE({alias}."{column}", 0)' if is_count else f'{alias}."{column}"'
            select.append(f'{value} AS "{column}"')
        joins.append(f"LEFT JOIN intermediate.{table} {alias} USING (applicant_id)")
    query = """BEGIN;
CREATE SCHEMA IF NOT EXISTS marts;
DROP TABLE IF EXISTS marts.fact_application;
DROP TABLE IF EXISTS marts.applicant_features;
CREATE TABLE marts.applicant_features AS
SELECT """ + ",\n       ".join(select) + "\nFROM staging.application a\n" + "\n".join(joins) + """;
CREATE UNIQUE INDEX applicant_features_pk ON marts.applicant_features(applicant_id);
DO $$ BEGIN
IF (SELECT count(*) FROM marts.applicant_features) <> (SELECT count(*) FROM raw.application_train) THEN
RAISE EXCEPTION 'Applicant join multiplied or lost rows'; END IF;
END $$;
ANALYZE marts.applicant_features;
COMMIT;
"""
    (generated_dir / "06_applicant_features.sql").write_text(query)
    connection.execute(query)

def import_scores(connection, analysis_dir: Path) -> None:
    connection.execute("DROP TABLE IF EXISTS marts.model_scores")
    connection.execute("""CREATE TABLE marts.model_scores(applicant_id bigint PRIMARY KEY,target integer,pd double precision,
        risk_score double precision,risk_band integer,expected_loss double precision,split text,model_version text,
        ead double precision,lgd_assumption double precision)""")
    with (analysis_dir / "scored_applications.csv").open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        required = ["applicant_id","target","pd","risk_score","risk_band","expected_loss","split","model_version","ead","lgd_assumption"]
        with connection.cursor().copy("COPY marts.model_scores (" + ",".join(required) + ") FROM STDIN") as copy:
            for row in reader:
                values = [row[c] if row.get(c) not in {None,""} else None for c in required]
                copy.write_row(values)
    count = connection.execute("SELECT count(*) FROM marts.model_scores").fetchone()[0]
    if count != connection.execute("SELECT count(*) FROM marts.applicant_features").fetchone()[0]:
        raise ValueError("Scoring coverage does not match applicant count")
    bad_alignment=connection.execute("""SELECT count(*) FROM marts.model_scores s
        LEFT JOIN marts.applicant_features f USING(applicant_id)
        WHERE f.applicant_id IS NULL OR s.target IS DISTINCT FROM f.target""").fetchone()[0]
    if bad_alignment:
        raise ValueError("Scoring applicant IDs or observed targets do not match the feature export")
    # Strategy and band tables preserve their exact analyst-facing output schema.
    for table,file in [("cutoff_table","cutoff_table.csv"),("risk_band_metrics","risk_bands.csv")]:
        with (analysis_dir / file).open(newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            rows = list(reader)
        connection.execute(sql.SQL("DROP TABLE IF EXISTS marts.{}").format(sql.Identifier(table)))
        numeric = []
        for i,c in enumerate(header):
            try:
                for row in rows:
                    if row[i]:
                        float(row[i])
                numeric.append(True)
            except ValueError:
                numeric.append(False)
        columns = [sql.SQL("{} {}").format(sql.Identifier(c),sql.SQL("double precision" if numeric[i] else "text")) for i,c in enumerate(header)]
        connection.execute(sql.SQL("CREATE TABLE marts.{} ({})").format(sql.Identifier(table),sql.SQL(",").join(columns)))
        with connection.cursor().copy(sql.SQL("COPY marts.{} FROM STDIN").format(sql.Identifier(table))) as copy:
            for row in rows:
                copy.write_row([v if v else None for v in row])

def export_final(connection, output_dir: Path) -> dict:
    files = {}
    names = ["dim_profile","dim_contract","dim_region_rating","dim_risk_band","fact_application","fact_roll_rate","agg_repayment_monthly","cutoff_table","risk_band_metrics"]
    for table in names:
        files[f"{table}.csv"] = export_query(connection,f"SELECT * FROM marts.{table}",output_dir / f"{table}.csv")
    # CSV adapter preserves requested import name, with the exported grain
    # explicitly identified as a monthly segment aggregate in the README.
    total = sum(files.values())
    if total >= 800 * 1024 * 1024:
        raise ValueError(f"Export bundle {total/1024**2:.1f} MiB is too large for the project budget")
    return {"files":files,"total_bytes":total,"monthly_detail_exported":False,"limit_bytes":800*1024*1024}

def run_pipeline(data_dir: Path, output_dir: Path, dsn: str, *, data_status: str="real",reload: bool=False, stop_after: str="all", lgd: float=0.45, execute_notebooks: bool=True) -> dict:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True,exist_ok=True)
    generated = output_dir / "generated_sql"
    generated.mkdir(exist_ok=True)
    exports = output_dir / "exports"
    result = {"data_status":data_status,"started_at":dt.datetime.now(dt.timezone.utc).isoformat(),"stages":{},"status":"running","source":"Home Credit Default Risk"}
    status_path = output_dir / "run_manifest.json"
    status_path.write_text(json.dumps(result,indent=2))
    try:
        manifest = generate(data_dir.resolve(),generated)
        with connect(dsn) as connection:
            result["raw_counts"] = load_raw(connection,dsn,manifest,generated,data_status,reload)
            result["stages"]["raw"] = "passed"
            if stop_after == "raw":
                result["status"]="raw_complete"
            else:
                execute_file(connection,"03_dq_checks.sql")
                export_query(connection,"SELECT * FROM audit.dq_results ORDER BY table_name,check_name,column_name",exports / "dq_results.csv")
                failures = connection.execute("SELECT check_name,table_name,issue_count FROM audit.dq_results WHERE status='FAIL'").fetchall()
                if failures:
                    raise ValueError(f"Critical DQ failures: {failures}; see dq_results.csv")
                result["stages"]["dq"]="passed"
                execute_file(connection,"04_staging.sql")
                result["stages"]["staging"]="passed"
                execute_file(connection,"05_intermediate.sql")
                result["stages"]["intermediate"]="passed"
                build_features(connection,generated)
                export_query(connection,"SELECT * FROM marts.applicant_features ORDER BY applicant_id",exports / "applicant_features.csv")
                export_query(connection,"SELECT * FROM intermediate.int_history_exclusion_audit",exports / "history_exclusions.csv")
                result["stages"]["applicant_features"]="passed"
                if stop_after == "features":
                    result["status"]="features_complete"
                else:
                    from .analysis import run_analysis
                    result["analysis"] = run_analysis(exports / "applicant_features.csv",output_dir / "analysis",lgd=lgd,data_status=data_status)
                    result["stages"]["analysis"]="passed"
                    import_scores(connection,output_dir / "analysis")
                    execute_file(connection,"06_marts.sql")
                    result["stages"]["marts"]="passed"
                    result["exports"] = export_final(connection,exports)
                    result["stages"]["exports"]="passed"
                    export_query(connection,"SELECT table_schema,table_name,column_name,data_type FROM information_schema.columns WHERE table_schema IN ('raw','staging','intermediate','marts') ORDER BY table_schema,table_name,ordinal_position",exports / "data_dictionary.csv")
                    if execute_notebooks:
                        from .notebooks import execute_all
                        result["notebooks"] = execute_all(exports / "applicant_features.csv",output_dir / "analysis",output_dir / "notebooks",data_status)
                        result["stages"]["notebooks"]="passed"
                    result["status"]="complete"
            counts = {}
            for schema in ["raw","intermediate","marts"]:
                tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",(schema,)).fetchall()
                for (table,) in tables:
                    counts[f"{schema}.{table}"] = connection.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema),sql.Identifier(table))).fetchone()[0]
            result["table_counts"]=counts
            if result["status"]=="complete":
                from .verify import verify
                checks=verify(dsn,output_dir,data_status)
                result["acceptance_checks_passed"]=len(checks)
    except Exception as exc:
        result["status"]="failed"
        result["error"]=str(exc)
        raise
    finally:
        result["finished_at"]=dt.datetime.now(dt.timezone.utc).isoformat()
        status_path.write_text(json.dumps(result,indent=2,default=str))
    if result["status"]=="complete":
        from .report import write_report
        write_report(output_dir)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",type=Path,default=PROJECT / "data/raw")
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--dsn",default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--synthetic",action="store_true")
    parser.add_argument("--reload",action="store_true",help="Replace only project raw tables and derived schemas in a dedicated database")
    parser.add_argument("--stop-after",choices=["raw","features","all"],default="all")
    parser.add_argument("--lgd",type=float,default=0.45)
    parser.add_argument("--skip-notebooks",action="store_true")
    args=parser.parse_args()
    status="synthetic" if args.synthetic else "real"
    if args.synthetic:
        from .make_fixture import make_fixture
        data = PROJECT / "runs/synthetic/data"
        make_fixture(data)
    else:
        data=args.data_dir
    dsn=args.dsn or local_dsn("homecredit_" + status)
    output=args.output_dir or PROJECT / "runs" / status
    result=run_pipeline(data,output,dsn,data_status=status,reload=args.reload,stop_after=args.stop_after,lgd=args.lgd,execute_notebooks=not args.skip_notebooks)
    print(f"Run {result['status']}: {output}",flush=True)

if __name__ == "__main__":
    main()
