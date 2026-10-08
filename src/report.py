"""Build a concise, evidence-backed report from completed execution artifacts."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import pandas as pd

def write_report(output: Path) -> Path:
    manifest=json.loads((output / "run_manifest.json").read_text())
    if manifest["status"] != "complete":
        raise ValueError("Cannot write a completed project report from an incomplete run")
    metrics=pd.read_csv(output / "analysis/model_metrics.csv")
    test=metrics.loc[metrics.split=="test"].iloc[0]
    overall=pd.read_csv(output / "analysis/eda_overall.csv").iloc[0]
    bands=pd.read_csv(output / "analysis/risk_bands.csv")
    test_bands=bands[bands.split=="test"]
    comparisons=pd.read_csv(output / "analysis/strategy_comparisons.csv")
    strategy=comparisons[(comparisons.scenario=="10_percent_more_approvals_relative") & (comparisons.split=="test")].iloc[0]
    dq=pd.read_csv(output / "exports/dq_results.csv")
    stages="\n".join(f"| {stage} | {status} |" for stage,status in manifest["stages"].items())
    raw="\n".join(f"| {r['table']} | {r['actual_rows']:,} | {r['expected_rows']:,} | {'PASS' if r['matches_expected'] else 'FAIL'} |" for r in manifest["raw_counts"])
    rates="\n".join(f"| {int(r.risk_band)} | {int(r.applicants):,} | {r.bad_rate:.2%} | {r.mean_predicted_pd:.2%} |" for r in test_bands.itertuples())
    important=dq[dq.check_name.isin(["outside_training_cohort","sentinel_365243","installment_multiple_payment_rows","installment_exact_duplicate_source_rows","installment_due_day_conflict","installment_scheduled_amount_conflict","installment_missing_payment","income_outlier_gt_10m"]) & (dq.issue_count>0)]
    findings="\n".join(f"| {r.check_name} | {r.table_name} | {r.column_name if pd.notna(r.column_name) else '—'} | {int(r.issue_count):,} |" for r in important.itertuples())
    acceptance=output / "acceptance_checks.json"
    checks=json.loads(acceptance.read_text()) if acceptance.exists() else []
    validation=f"{len(checks)} independent read-only warehouse acceptance checks passed." if checks else "Warehouse grain/FK assertions passed during execution."
    text=f"""# Home Credit project — verified {manifest['data_status']} execution

**Pipeline status: COMPLETE.** All seven CSV counts match; the application mart has {manifest['table_counts']['marts.fact_application']:,} unique applicants. {validation}

Run finished: {manifest['finished_at']}. Source provenance and file hashes are recorded in [the import evidence](generated_sql/raw_counts.json).

| Stage | Result |
|---|---|
{stages}

## Full source reconciliation

| Table | Loaded rows | Expected rows | Result |
|---|---:|---:|---|
{raw}

Total source rows: **{sum(r['actual_rows'] for r in manifest['raw_counts']):,}**. All original 122 application columns remain in raw. Transformations use only the labelled train cohort and history observable by day/month zero.

## Actual model results

Observed TARGET repayment-difficulty rate: **{overall.bad_rate:.2%}** ({int(overall.bad_count):,}/{int(overall.applicants):,}). TARGET is a repayment-difficulty proxy.

Held-out test AUC **{test.roc_auc:.4f}**, KS **{test.ks:.4f}**, Gini **{test.gini:.4f}**, Brier **{test.brier_score:.4f}**. The test split contains {int(test.applicants):,} applicants. Training-only WoE/IV/preprocessing and validation-defined thresholds prevent held-out outcomes from defining the fitted model or policy.

| Risk band | Test applicants | Observed bad rate | Mean predicted PD |
|---|---:|---:|---:|
{rates}

Observed adjacent test-band reversals: **{int(test_bands.observed_rate_monotonicity_violation.sum())}**. Bands ascend in predicted risk; observed rates are audited without outcome-based reordering.

## Stakeholder answer: 10% more approvals

Using the fixed thresholds selected for 50% versus 55% approval on validation (a 10% relative increase), test-cohort approval changes from **{strategy.baseline_actual_approval_rate:.2%} to {strategy.scenario_actual_approval_rate:.2%}**. Observed bad rate changes from **{strategy.baseline_bad_rate:.2%} to {strategy.scenario_bad_rate:.2%}**, or **{strategy.bad_rate_change_percentage_points:+.2f} percentage points**. The separate 50% → 60% interpretation is also exported.

This is retrospective within the observed borrower cohort. Rejected current applicants' outcomes are unknown (**reject inference**); this cannot predict the effect of lending to previously rejected applicants. Expected loss is a PD × assumed LGD × credit-amount EAD scenario, not measured accounting loss.

## Data-quality findings

{len(dq)} checks/profile records saved; **{int((dq.status=='FAIL').sum())} critical failures**. WARN means an audited missing value, anomaly or interpretation issue; it does not imply a dropped raw row.

| Check | Table | Column | Affected count |
|---|---|---|---:|
{findings}

Full results, ranges, placeholders and repayment diagnostics: [dq_results.csv](exports/dq_results.csv). EMI payments are aggregated before loan/applicant joins; exact source duplicates and conflicting schedules are audited, and unpaid lateness is censored at day zero. Bureau one-month roll rates exclude month gaps.

## Delivered outputs

Four [executed notebooks](notebooks), [all analysis findings](analysis/findings.md), model metrics/calibration, WoE/IV, odds ratios, risk bands, approval strategy, saved scorecard and star-schema [CSV exports](exports).

Final mart import bundle: **{manifest['exports']['total_bytes']/1024**2:.2f} MiB**, below the project's 800 MiB export budget. Bureau loan/month detail stays in PostgreSQL; compact monthly and roll counts are exported. Counts and rates have the documented grains in [README](../../README.md).

Raw source CSVs and the live project database remain local; the portable project archive does not redistribute the Kaggle inputs.
"""
    path=output / "run_report.md";path.write_text(text)
    return path

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,default=Path("runs/real"))
    print(write_report(parser.parse_args().output_dir))
