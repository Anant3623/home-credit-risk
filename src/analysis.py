"""Reproducible accepted-cohort credit-risk analysis with a held-out test set.

This module is shared by the command-line pipeline and all four notebooks.
TARGET is a repayment-difficulty label. Expected loss is a scenario, not a
measured accounting loss; rejected applicants' outcomes are unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import joblib

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "home-credit-matplotlib"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score, roc_curve
from sklearn.model_selection import train_test_split


MODEL_VERSION = "woe-logistic-v1"
ANALYSIS_ARTIFACTS = [
    "eda_overall.csv", "eda_segments.csv", "feature_nulls.csv", "woe_bins.csv",
    "iv_summary.csv", "chi_square_tests.csv", "scored_applications.csv",
    "odds_ratios.csv", "model_metrics.csv", "model_metrics.json", "risk_bands.csv",
    "calibration.csv", "cutoff_table.csv", "strategy_comparisons.csv",
    "model_manifest.json", "scorecard.joblib", "findings.md",
    "figures/test_roc.png", "figures/risk_bands.png", "figures/test_calibration.png",
    "figures/strategy_tradeoff.png",
]
NUMERIC_FEATURES = [
    "age_years", "employed_years", "is_employed_missing", "income_amount",
    "credit_amount", "annuity_amount", "goods_amount", "credit_to_income",
    "annuity_to_income", "credit_to_goods", "ext_source_1", "ext_source_2",
    "ext_source_3", "region_rating", "region_rating_city",
    "prev_loans_with_installments", "installment_count", "settled_installment_count",
    "unpaid_installment_count", "late_installment_count", "max_days_late",
    "mean_days_late", "scheduled_installment_amount", "installment_paid_amount",
    "installment_paid_ratio", "missing_payment_installment_count", "installment_late_rate",
    "bureau_loan_count", "bureau_active_loan_count", "bureau_closed_loan_count",
    "bureau_credit_sum", "bureau_debt_sum", "bureau_overdue_sum", "bureau_max_dpd",
    "bureau_months_31plus", "bureau_credit_to_debt_ratio", "previous_application_count",
    "previous_approved_count", "previous_refused_count", "previous_refusal_rate",
    "previous_credit_mean", "previous_credit_sum", "previous_annuity_mean",
    "pos_loan_count", "pos_month_count", "pos_max_dpd", "pos_max_dpd_def",
    "pos_delinquent_month_rate", "cc_loan_count", "cc_month_count", "cc_max_dpd",
    "cc_max_dpd_def", "cc_utilization_mean", "cc_utilization_max",
    "cc_balance_sum_latest", "cc_limit_sum_latest",
]
CATEGORICAL_FEATURES = [
    "income_type", "education_type", "family_status", "gender", "housing_type",
    "occupation_type", "contract_type",
]
MISSING = "__MISSING__"
RARE = "__RARE__"
UNSEEN = "__UNSEEN__"


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=_json_default, allow_nan=False) + "\n")


def load_features(input_path: str | Path) -> pd.DataFrame:
    """Read a one-row-per-applicant export and validate the analysis boundary."""
    frame = pd.read_csv(input_path, low_memory=False)
    frame.columns = frame.columns.str.lower()
    required = {"applicant_id", "target", "credit_amount"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Applicant feature export is missing columns: {sorted(missing)}")
    if frame.applicant_id.isna().any() or frame.applicant_id.duplicated().any():
        raise ValueError("applicant_id must be present and unique before model fitting")
    frame["target"] = pd.to_numeric(frame.target, errors="raise")
    if not frame.target.isin([0, 1]).all():
        raise ValueError("TARGET must contain only observed binary 0/1 labels")
    frame["target"] = frame.target.astype("int8")
    for column in NUMERIC_FEATURES:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            )
    return frame.sort_values("applicant_id", kind="stable").reset_index(drop=True)


def assign_splits(frame: pd.DataFrame, seed: int = 42) -> pd.Series:
    """Stratified 60/20/20 applicant split; no applicant can enter two splits."""
    counts = frame.target.value_counts()
    if len(counts) != 2 or counts.min() < 5 or len(frame) < 25:
        raise ValueError("Analysis needs at least 25 applicants and 5 labels in each class")
    train_idx, remaining_idx = train_test_split(
        frame.index, test_size=0.4, random_state=seed, stratify=frame.target
    )
    validation_idx, test_idx = train_test_split(
        remaining_idx, test_size=0.5, random_state=seed + 1,
        stratify=frame.loc[remaining_idx, "target"],
    )
    split = pd.Series("train", index=frame.index, name="split")
    split.loc[validation_idx] = "validation"
    split.loc[test_idx] = "test"
    return split


def wilson_interval(bad_count: int, total: int, z: float = 1.95996398454) -> tuple[float, float]:
    if total <= 0:
        return float("nan"), float("nan")
    p = bad_count / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _clean_category(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.strip()
    values = values.mask(values.str.upper().isin(["XNA", "XAP", "NAN", "NONE", ""]))
    return values.fillna(MISSING).astype(str)


def eda_tables(frame: pd.DataFrame, output_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Descriptive rates and Wilson intervals; these are not predictive selection."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    low, high = wilson_interval(int(frame.target.sum()), len(frame))
    overall = pd.DataFrame([{
        "applicants": len(frame), "bad_count": int(frame.target.sum()),
        "bad_rate": float(frame.target.mean()), "bad_rate_ci_low": low,
        "bad_rate_ci_high": high, "label_definition": "TARGET repayment difficulty",
    }])
    rows = []
    categorical = [c for c in CATEGORICAL_FEATURES + ["region_rating", "region_rating_city"] if c in frame]
    for column in categorical:
        groups = frame.groupby(_clean_category(frame[column]), dropna=False).target.agg(["size", "sum"])
        for level, row in groups.iterrows():
            lower, upper = wilson_interval(int(row["sum"]), int(row["size"]))
            rows.append({"feature": column, "level": level, "applicants": int(row["size"]),
                         "bad_count": int(row["sum"]), "bad_rate": row["sum"] / row["size"],
                         "bad_rate_ci_low": lower, "bad_rate_ci_high": upper})
    if "age_years" in frame:
        age_group = pd.cut(frame.age_years, [-np.inf, 25, 35, 45, 55, 65, np.inf],
                           labels=["<=25", "25-35", "35-45", "45-55", "55-65", ">65"])
        for level, group in frame.groupby(age_group.astype("string").fillna(MISSING), dropna=False):
            lower, upper = wilson_interval(int(group.target.sum()), len(group))
            rows.append({"feature": "age_band", "level": level, "applicants": len(group),
                         "bad_count": int(group.target.sum()), "bad_rate": group.target.mean(),
                         "bad_rate_ci_low": lower, "bad_rate_ci_high": upper})
    segments = pd.DataFrame(rows, columns=["feature", "level", "applicants", "bad_count", "bad_rate",
                                          "bad_rate_ci_low", "bad_rate_ci_high"])
    nulls = pd.DataFrame({"feature": frame.columns, "null_count": frame.isna().sum().values,
                          "null_pct": frame.isna().mean().values * 100})
    overall.to_csv(output / "eda_overall.csv", index=False)
    segments.to_csv(output / "eda_segments.csv", index=False)
    nulls.to_csv(output / "feature_nulls.csv", index=False)
    return {"overall": overall, "segments": segments, "nulls": nulls}


class WoeEncoder:
    """Train-only clipped quantile/rare-category bins with smoothed good:bad WoE."""

    def __init__(self, bins: int = 5, smoothing: float = 0.5):
        self.bins = bins
        self.smoothing = smoothing
        self.specs: dict[str, dict[str, Any]] = {}
        self.tables = pd.DataFrame()
        self.iv = pd.DataFrame()

    def _bin(self, series: pd.Series, spec: dict[str, Any]) -> pd.Series:
        if spec["kind"] == "numeric":
            values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
            if spec["clip_low"] is not None:
                values = values.clip(spec["clip_low"], spec["clip_high"])
            bin_ids = np.searchsorted(spec["edges"], values.fillna(0).to_numpy(), side="right")
            result = pd.Series([f"bin_{i + 1}" for i in bin_ids], index=series.index)
            return result.mask(values.isna(), MISSING)
        values = _clean_category(series)
        result = values.where(values.isin(spec["common_categories"]), RARE)
        result = result.mask(values == MISSING, MISSING)
        if not spec["has_rare"]:
            result = result.mask(result == RARE, UNSEEN)
        return result

    def fit(self, frame: pd.DataFrame, target: pd.Series) -> "WoeEncoder":
        all_rows = []
        iv_rows = []
        self.specs = {}
        columns = [c for c in NUMERIC_FEATURES + CATEGORICAL_FEATURES if c in frame]
        for column in columns:
            if column in NUMERIC_FEATURES:
                values = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
                valid = values.dropna()
                if valid.empty:
                    spec = {"kind": "numeric", "clip_low": None, "clip_high": None,
                            "edges": [], "labels": ["bin_1", MISSING]}
                else:
                    lower, upper = valid.quantile([0.01, 0.99]).to_numpy(dtype=float)
                    clipped = valid.clip(lower, upper)
                    # Outer bounds are implicit +/- infinity, covering every future value.
                    edges = sorted(set(float(x) for x in clipped.quantile(
                        np.linspace(0, 1, self.bins + 1)[1:-1]).to_numpy()))
                    if lower == upper:
                        edges = []
                    spec = {"kind": "numeric", "clip_low": float(lower), "clip_high": float(upper),
                            "edges": edges, "labels": [f"bin_{i+1}" for i in range(len(edges)+1)] + [MISSING]}
            else:
                values = _clean_category(frame[column])
                counts = values[values != MISSING].value_counts()
                minimum = max(5, math.ceil(len(frame) * 0.005))
                common = sorted(counts[counts >= minimum].index.tolist())
                has_rare = bool((~values.isin(common + [MISSING])).any())
                labels = common + ([RARE] if has_rare else []) + [MISSING]
                spec = {"kind": "categorical", "common_categories": common,
                        "has_rare": has_rare, "minimum_count": minimum, "labels": labels}
            bins = self._bin(frame[column], spec)
            grouped = pd.DataFrame({"bin": bins, "target": target}).groupby("bin").target.agg(["size", "sum"])
            grouped = grouped.reindex(spec["labels"], fill_value=0)
            bad = grouped["sum"].astype(float)
            good = grouped["size"].astype(float) - bad
            count_bins = len(grouped)
            good_dist = (good + self.smoothing) / (good.sum() + self.smoothing * count_bins)
            bad_dist = (bad + self.smoothing) / (bad.sum() + self.smoothing * count_bins)
            woe = np.log(good_dist / bad_dist).where(grouped["size"] > 0, 0.0)
            iv_components = ((good_dist - bad_dist) * woe).where(grouped["size"] > 0, 0.0)
            spec["woe"] = {str(level): float(value) for level, value in woe.items()}
            spec["woe"][UNSEEN] = 0.0
            self.specs[column] = spec
            for level, row in grouped.iterrows():
                lower, upper = wilson_interval(int(row["sum"]), int(row["size"]))
                if spec["kind"] == "numeric" and level != MISSING:
                    number = int(level.split("_")[1]) - 1
                    edges = spec["edges"]
                    description = f"[{edges[number-1] if number > 0 else '-infinity'}, {edges[number] if number < len(edges) else 'infinity'})"
                else:
                    description = str(level)
                all_rows.append({"feature": column, "kind": spec["kind"], "bin": level,
                                 "bin_description": description, "applicants": int(row["size"]),
                                 "bad_count": int(row["sum"]), "good_count": int(row["size"] - row["sum"]),
                                 "bad_rate": row["sum"] / row["size"] if row["size"] else np.nan,
                                 "bad_rate_ci_low": lower, "bad_rate_ci_high": upper,
                                 "good_distribution": good_dist[level], "bad_distribution": bad_dist[level],
                                 "woe": woe[level], "iv_component": iv_components[level],
                                 "fit_split": "train"})
            iv_rows.append({"feature": column, "iv": float(iv_components.sum()),
                            "kind": spec["kind"], "fit_split": "train"})
        self.tables = pd.DataFrame(all_rows)
        self.iv = pd.DataFrame(iv_rows).sort_values(["iv", "feature"], ascending=[False, True]).reset_index(drop=True)
        return self

    def transform(self, frame: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
        columns = columns or list(self.specs)
        return pd.DataFrame({column: self._bin(frame[column], self.specs[column]).map(
            self.specs[column]["woe"]).fillna(0.0) for column in columns}, index=frame.index, dtype=float)


def _benjamini_hochberg(pvalues: np.ndarray) -> np.ndarray:
    if len(pvalues) == 0:
        return pvalues
    order = np.argsort(pvalues)
    ranked = pvalues[order] * len(pvalues) / np.arange(1, len(pvalues) + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1].clip(0, 1)
    result = np.empty_like(adjusted)
    result[order] = adjusted
    return result


def chi_square_tests(train: pd.DataFrame, encoder: WoeEncoder) -> pd.DataFrame:
    """Exploratory binned associations on train, BH FDR and Cramer's V."""
    rows = []
    for column, spec in encoder.specs.items():
        contingency = pd.crosstab(encoder._bin(train[column], spec), train.target)
        contingency = contingency.loc[contingency.sum(axis=1) > 0]
        if contingency.shape[0] < 2 or contingency.shape[1] < 2:
            continue
        chi2, pvalue, dof, expected = chi2_contingency(contingency, correction=False)
        n = int(contingency.to_numpy().sum())
        cramers_v = math.sqrt(chi2 / (n * min(contingency.shape[0] - 1, contingency.shape[1] - 1)))
        rows.append({"feature": column, "chi_square": chi2, "degrees_of_freedom": dof,
                     "p_value": pvalue, "cramers_v": cramers_v, "train_applicants": n,
                     "min_expected_count": float(expected.min()),
                     "expected_cells_below_5_pct": float((expected < 5).mean() * 100),
                     "asymptotic_check": "sparse: interpret cautiously" if (expected < 5).any() else "adequate",
                     "fit_split": "train"})
    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame(columns=["feature", "p_value", "p_value_bh", "cramers_v"])
    result["p_value_bh"] = _benjamini_hochberg(result.p_value.to_numpy())
    result["significant_bh_0_05"] = result.p_value_bh < 0.05
    return result.sort_values(["p_value_bh", "feature"]).reset_index(drop=True)


def score_from_pd(pd_values: np.ndarray | pd.Series, base_score: float = 600.0,
                  base_good_bad_odds: float = 50.0, pdo: float = 20.0) -> np.ndarray:
    probability = np.clip(np.asarray(pd_values, dtype=float), 1e-9, 1 - 1e-9)
    factor = pdo / math.log(2)
    return base_score + factor * (np.log((1 - probability) / probability) - math.log(base_good_bad_odds))


def assign_risk_bands(pd_values: np.ndarray | pd.Series, boundaries: np.ndarray) -> np.ndarray:
    return np.searchsorted(boundaries, np.asarray(pd_values), side="right") + 1


def performance_metrics(target: pd.Series | np.ndarray, pd_values: np.ndarray) -> dict[str, float | int]:
    y = np.asarray(target)
    probability = np.clip(np.asarray(pd_values), 1e-9, 1 - 1e-9)
    auc = float(roc_auc_score(y, probability))
    fpr, tpr, _ = roc_curve(y, probability)
    return {"applicants": len(y), "bad_count": int(y.sum()), "bad_rate": float(y.mean()),
            "mean_predicted_pd": float(probability.mean()), "roc_auc": auc,
            "gini": 2 * auc - 1, "ks": float(np.max(tpr - fpr)),
            "brier_score": float(brier_score_loss(y, probability)),
            "log_loss": float(log_loss(y, probability, labels=[0, 1]))}


def risk_band_table(scored: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for split_name in ["train", "validation", "test", "all"]:
        data = scored if split_name == "all" else scored[scored.split == split_name]
        prior_rate = None
        prior_band = None
        for band in range(1, 11):
            group = data[data.risk_band == band]
            n = len(group)
            count = int(group.target.sum())
            lower, upper = wilson_interval(count, n)
            rate = count / n if n else np.nan
            violation = bool(n and prior_rate is not None and rate < prior_rate)
            rows.append({"split": split_name, "risk_band": band, "applicants": n,
                         "bad_count": count, "bad_rate": rate, "bad_rate_ci_low": lower,
                         "bad_rate_ci_high": upper, "mean_predicted_pd": group.pd.mean(),
                         "min_pd": group.pd.min(), "max_pd": group.pd.max(),
                         "mean_risk_score": group.risk_score.mean(),
                         "expected_loss": group.expected_loss.sum(min_count=1) if n else 0.0,
                         "known_ead_count": int(group.ead.notna().sum()),
                         "missing_ead_count": int(group.ead.isna().sum()),
                         "expected_loss_coverage_rate": group.ead.notna().mean() if n else np.nan,
                         "observed_rate_monotonicity_violation": violation,
                         "compared_previous_nonempty_band": prior_band})
            if n:
                prior_rate, prior_band = rate, band
    return pd.DataFrame(rows)


def calibration_table(scored: pd.DataFrame, bins: int = 10) -> pd.DataFrame:
    """Fixed probability bins, so held-out outcomes never define bin boundaries."""
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for split_name in ["train", "validation", "test"]:
        data = scored[scored.split == split_name]
        bin_numbers = np.minimum(np.searchsorted(edges, data.pd.to_numpy(), side="right") - 1, bins - 1)
        for number in range(bins):
            group = data[bin_numbers == number]
            low, high = wilson_interval(int(group.target.sum()), len(group))
            rows.append({"split": split_name, "bin": number + 1,
                         "pd_lower": edges[number], "pd_upper": edges[number + 1],
                         "applicants": len(group), "mean_predicted_pd": group.pd.mean(),
                         "observed_bad_rate": group.target.mean(),
                         "bad_rate_ci_low": low, "bad_rate_ci_high": high})
    return pd.DataFrame(rows)


def cutoff_strategy(scored: pd.DataFrame) -> pd.DataFrame:
    """Set each PD threshold on validation predictions, then hold it fixed on test."""
    validation = scored[scored.split == "validation"]
    sorted_pd = np.sort(validation.pd.to_numpy())
    rows = []
    requested_rates = sorted(set([0.0, 1.0, 0.5, 0.55, 0.6] + [round(q, 2) for q in np.arange(0.05, 1, 0.05)]))
    for requested in requested_rates:
        threshold = 0.0 if requested == 0 else float(sorted_pd[min(len(sorted_pd) - 1, math.ceil(requested * len(sorted_pd)) - 1)])
        row = {"requested_validation_approval_rate": requested, "pd_cutoff": threshold,
               "score_cutoff": float(score_from_pd(np.array([threshold]))[0]),
               "threshold_source": "validation_predictions", "approval_rule": "pd <= pd_cutoff"}
        for split_name in ["validation", "test"]:
            data = scored[scored.split == split_name]
            approved = data[data.pd <= threshold]
            n = len(approved)
            count = int(approved.target.sum())
            low, high = wilson_interval(count, n)
            known_ead = int(approved.ead.notna().sum())
            portfolio_loss = approved.expected_loss.sum(min_count=1) if n else 0.0
            values = {"applicants": len(data), "approved_count": n,
                      "approval_rate": n / len(data), "bad_count": count,
                      "bad_rate": count / n if n else np.nan,
                      "bad_rate_ci_low": low, "bad_rate_ci_high": high,
                      "expected_bad_rate": approved.pd.mean(),
                      "expected_loss": portfolio_loss,
                      "approved_known_ead_count": known_ead,
                      "approved_missing_ead_count": n - known_ead,
                      "expected_loss_coverage_rate": known_ead / n if n else np.nan,
                      "expected_loss_per_approved": portfolio_loss / n if n else np.nan,
                      "expected_loss_per_approved_denominator": "all approved; numerator is known-EAD EL subtotal",
                      "expected_loss_per_approved_known_ead": approved.expected_loss.mean(),
                      "approved_ead": approved.ead.sum(min_count=1) if n else 0.0}
            row.update({f"{split_name}_{key}": value for key, value in values.items()})
        rows.append(row)
    return pd.DataFrame(rows)


def _strategy_comparisons(cutoffs: pd.DataFrame) -> pd.DataFrame:
    baseline = cutoffs[np.isclose(cutoffs.requested_validation_approval_rate, 0.5)].iloc[0]
    rows = []
    for requested, name in [(0.55, "10_percent_more_approvals_relative"), (0.6, "10_percentage_points_more_approval")]:
        scenario = cutoffs[np.isclose(cutoffs.requested_validation_approval_rate, requested)].iloc[0]
        for split_name in ["validation", "test"]:
            rows.append({"scenario": name, "split": split_name,
                         "baseline_requested_validation_approval_rate": 0.5,
                         "scenario_requested_validation_approval_rate": requested,
                         "baseline_pd_cutoff": baseline.pd_cutoff, "scenario_pd_cutoff": scenario.pd_cutoff,
                         "baseline_actual_approval_rate": baseline[f"{split_name}_approval_rate"],
                         "scenario_actual_approval_rate": scenario[f"{split_name}_approval_rate"],
                         "approval_change_percentage_points": 100 * (scenario[f"{split_name}_approval_rate"] - baseline[f"{split_name}_approval_rate"]),
                         "baseline_bad_rate": baseline[f"{split_name}_bad_rate"],
                         "scenario_bad_rate": scenario[f"{split_name}_bad_rate"],
                         "bad_rate_change_percentage_points": 100 * (scenario[f"{split_name}_bad_rate"] - baseline[f"{split_name}_bad_rate"]),
                         "baseline_expected_loss": baseline[f"{split_name}_expected_loss"],
                         "scenario_expected_loss": scenario[f"{split_name}_expected_loss"],
                         "expected_loss_change": scenario[f"{split_name}_expected_loss"] - baseline[f"{split_name}_expected_loss"],
                         "baseline_expected_loss_coverage_rate": baseline[f"{split_name}_expected_loss_coverage_rate"],
                         "scenario_expected_loss_coverage_rate": scenario[f"{split_name}_expected_loss_coverage_rate"],
                         "interpretation": "retrospective accepted-cohort scenario; rejected outcomes unknown"})
    return pd.DataFrame(rows)


def _write_plots(scored: pd.DataFrame, bands: pd.DataFrame, calibration: pd.DataFrame,
                 cutoffs: pd.DataFrame, output: Path) -> None:
    figures = output / "figures"
    figures.mkdir(exist_ok=True)
    test = scored[scored.split == "test"]
    fpr, tpr, _ = roc_curve(test.target, test.pd)
    fig, axis = plt.subplots(figsize=(6.5, 4.5), constrained_layout=True)
    axis.plot(fpr, tpr, label=f"Held-out test AUC {roc_auc_score(test.target, test.pd):.3f}")
    axis.plot([0, 1], [0, 1], "--", color="gray")
    axis.set(xlabel="False positive rate", ylabel="True positive rate", title="Scorecard discrimination")
    axis.legend()
    fig.savefig(figures / "test_roc.png", dpi=160)
    plt.close(fig)
    fig, axis = plt.subplots(figsize=(7.5, 4.8), constrained_layout=True)
    for name in ["validation", "test"]:
        group = bands[(bands.split == name) & (bands.applicants > 0)]
        axis.plot(group.risk_band, group.bad_rate, marker="o", label=f"{name}: observed")
    group = bands[(bands.split == "test") & (bands.applicants > 0)]
    axis.plot(group.risk_band, group.mean_predicted_pd, "--", color="black", label="test: predicted")
    axis.set(xlabel="Risk band (1 safer, 10 riskier)", ylabel="Repayment-difficulty rate",
             title="Validation-defined risk bands; observed rates may reverse")
    axis.legend()
    fig.savefig(figures / "risk_bands.png", dpi=160)
    plt.close(fig)
    fig, axis = plt.subplots(figsize=(6.5, 4.5), constrained_layout=True)
    group = calibration[(calibration.split == "test") & (calibration.applicants > 0)]
    errors = np.array([group.observed_bad_rate - group.bad_rate_ci_low,
                       group.bad_rate_ci_high - group.observed_bad_rate])
    axis.errorbar(group.mean_predicted_pd, group.observed_bad_rate, yerr=errors, fmt="o-",
                  label="Test observed rate + 95% Wilson CI")
    axis.plot([0, 1], [0, 1], "--", color="gray", label="Perfect calibration")
    axis.set(xlabel="Mean predicted PD", ylabel="Observed repayment-difficulty rate", title="Untouched test calibration")
    axis.legend()
    fig.savefig(figures / "test_calibration.png", dpi=160)
    plt.close(fig)
    fig, axis = plt.subplots(figsize=(7.5, 4.5), constrained_layout=True)
    for name in ["validation", "test"]:
        axis.plot(cutoffs[f"{name}_approval_rate"], cutoffs[f"{name}_bad_rate"], marker=".", label=name)
    axis.set(xlabel="Approval rate within observed accepted cohort", ylabel="Observed repayment-difficulty rate",
             title="Fixed validation thresholds: approval / risk trade-off")
    axis.legend()
    fig.savefig(figures / "strategy_tradeoff.png", dpi=160)
    plt.close(fig)


def _write_findings(output: Path, manifest: dict[str, Any], eda: dict[str, pd.DataFrame],
                    metrics: dict[str, dict[str, Any]], bands: pd.DataFrame,
                    comparisons: pd.DataFrame, iv: pd.DataFrame) -> None:
    status = manifest["data_status"]
    label = "SYNTHETIC VALIDATION DATA: these numbers demonstrate the pipeline, not Home Credit findings." if status != "real" else "Computed from the supplied applicant feature export."
    overall = eda["overall"].iloc[0]
    test = metrics["test"]
    relative = comparisons[(comparisons.scenario == "10_percent_more_approvals_relative") & (comparisons.split == "test")].iloc[0]
    points = []
    for split_name in ["validation", "test"]:
        violations = bands[(bands.split == split_name) & bands.observed_rate_monotonicity_violation]
        points.append(f"{split_name}: {len(violations)} observed adjacent nonempty-band reversals")
    top = ", ".join(f"{row.feature} (IV {row.iv:.3f})" for row in iv.head(5).itertuples())
    text = f"""# Credit-risk analysis findings

**Data status: {status.upper()}.** {label}

The export contains {int(overall.applicants):,} applicants; {int(overall.bad_count):,} have TARGET=1. The observed repayment-difficulty rate is {overall.bad_rate:.2%} (95% Wilson interval {overall.bad_rate_ci_low:.2%}–{overall.bad_rate_ci_high:.2%}). TARGET is a repayment-difficulty proxy, not a verified loss or an independently established default event.

## Model and risk drivers

The scorecard uses transparent train-only Weight of Evidence bins and unweighted regularized logistic regression. Strongest univariate train associations: {top}. Associations and odds ratios are descriptive and conditional, not causal. All feature selection, clipping, rare-level pooling and WoE mappings use only the training 60%; validation (20%) defines decile boundaries and approval thresholds. Test (20%) is reserved for evaluation.

On the untouched test split, ROC AUC is {test['roc_auc']:.4f}, KS is {test['ks']:.4f}, Gini is {test['gini']:.4f} and Brier score is {test['brier_score']:.4f}. Mean predicted PD is {test['mean_predicted_pd']:.2%}, compared with observed TARGET rate {test['bad_rate']:.2%}. Inspect the calibration curve and bin counts; rare events and small bands have wide intervals. These are point estimates on one random split, with no temporal validation. The test set was not used to calibrate, select features, tune the model, define bands or choose cutoffs.

Risk bands ascend in predicted risk by construction. Observed rates are audited rather than forced into monotonicity: {'; '.join(points)}. Repeated prediction thresholds can leave empty bands; all ten band numbers remain in the export. A reversal is reported transparently and does not trigger a refit using test outcomes.

## Stakeholder question: 10% more approvals

The illustrative baseline requests 50% approval on validation predictions. Ten percent more approvals **relative** requests 55%; ten **percentage points** requests 60%, and both scenarios are exported. On test, the fixed 50%/55% validation thresholds approve {relative.baseline_actual_approval_rate:.2%}/{relative.scenario_actual_approval_rate:.2%} of the accepted cohort. The observed bad rate changes from {relative.baseline_bad_rate:.2%} to {relative.scenario_bad_rate:.2%}, a change of {relative.bad_rate_change_percentage_points:+.2f} percentage points. Scenario expected loss changes by {relative.expected_loss_change:,.2f} dataset currency units. Actual approvals can differ from requested approvals because threshold ties and population variation are retained; no test outcomes determine the thresholds. See cutoff_table.csv for rate confidence intervals and strategy_comparisons.csv for both interpretations.

## Interpretation limits

- **Reject inference:** outcomes for rejected current applicants are unavailable. These retrospective policies re-rank the observed accepted cohort; they do not estimate the outcome of accepting previously rejected customers. Historical refused applications supply behavior features, not outcomes of rejected current applications.
- **Expected loss:** PD × assumed LGD {manifest['lgd']:.0%} × EAD. EAD uses nonnegative current credit amount as a proxy; missing/invalid EAD produces missing applicant EL and is excluded from known-EAD portfolio subtotals. The export has {manifest['ead_missing_count']:,} missing EAD values. Cutoff and band tables report known/missing EAD counts and EL coverage. A nonempty portfolio with entirely missing EAD has unavailable EL, and its strategy comparison remains unavailable; an empty approved portfolio has zero EL. `expected_loss_per_approved` divides the known-EAD EL subtotal by all approved applicants, while `expected_loss_per_approved_known_ead` divides it by applicants with known EAD. Neither is a complete-portfolio estimate when coverage is below 100%. LGD is a configurable scenario assumption, not a value learned from recoveries. These figures are not calibrated accounting loss estimates or a claim about currency conversion.
- **Time and population:** the data do not provide a usable current-application calendar for this pipeline, so random stratified validation cannot establish future performance. Relative-day/month history must already be filtered at the application observation point by SQL.
- **Confidence intervals and tests:** Wilson intervals describe observed binomial TARGET rates; they do not include model, LGD or EAD uncertainty. Chi-square p-values use asymptotic assumptions; sparse expected counts are flagged and all exploratory feature tests use Benjamini–Hochberg correction. Train-selected continuous bins make these tests exploratory. Large samples can make weak effects significant; inspect Cramér's V.
- **Score units:** score 600 represents good:bad odds 50:1 and 20 points double those odds. Higher scores are safer. Odds ratios are per +1 WoE (log good-distribution/bad-distribution), not per year or currency unit. Positive/negative coefficients can be affected by correlated features.
- **Selection and fairness:** historical lending policy shapes this cohort. The model is an educational demonstration and has not been reviewed for fairness, credit-policy compliance or live lending use.

## Reproducibility

Input SHA-256: `{manifest['input_sha256']}`. Random seed: {manifest['seed']}. Model: {MODEL_VERSION}. Training-selected features: {len(manifest['selected_features'])}. Preprocessor and model are saved together; model_manifest.json records split sizes, clipping rules, WoE interpretation and nine validation boundaries. No manual outcome-based reordering or rejection-outcome imputation was performed.
"""
    (output / "findings.md").write_text(text)


def run_analysis(input_path: str | Path, output_dir: str | Path, *, lgd: float = 0.45,
                 seed: int = 42, data_status: str = "real", max_features: int = 25,
                 min_iv: float = 0.01) -> dict[str, Any]:
    """Execute EDA, train-only driver analysis, scorecard and fixed-threshold strategy."""
    if not 0 <= lgd <= 1:
        raise ValueError("LGD must be a scenario assumption between 0 and 1")
    if data_status not in {"real", "synthetic", "sample"}:
        raise ValueError("data_status must be real, synthetic or sample")
    if max_features < 1:
        raise ValueError("max_features must be positive")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    completion_path = output / "analysis_complete.json"
    completion_path.unlink(missing_ok=True)
    input_path = Path(input_path)
    frame = load_features(input_path)
    split = assign_splits(frame, seed)
    frame["split"] = split
    train = frame[split == "train"]
    eda = eda_tables(frame, output)
    encoder = WoeEncoder().fit(train, train.target)
    if encoder.iv.empty:
        raise ValueError("No allowed model features are present in the export")
    usable = encoder.iv[encoder.iv.iv >= min_iv].head(max_features)
    if usable.empty:
        usable = encoder.iv.head(min(5, max_features))
    selected = usable.feature.tolist()
    encoder.iv["selected"] = encoder.iv.feature.isin(selected)
    encoder.tables.to_csv(output / "woe_bins.csv", index=False)
    encoder.iv.to_csv(output / "iv_summary.csv", index=False)
    chi_square_tests(train, encoder).to_csv(output / "chi_square_tests.csv", index=False)
    transformed = encoder.transform(frame, selected)
    model = LogisticRegression(C=0.5, class_weight=None, solver="lbfgs", max_iter=1000, random_state=seed)
    model.fit(transformed.loc[train.index], train.target)
    probability = np.clip(model.predict_proba(transformed)[:, 1], 1e-9, 1 - 1e-9)
    validation_probability = probability[split == "validation"]
    boundaries = np.quantile(validation_probability, np.arange(0.1, 1.0, 0.1))
    ead = frame.credit_amount.where(frame.credit_amount >= 0)
    scored = pd.DataFrame({"applicant_id": frame.applicant_id, "target": frame.target,
                           "pd": probability, "risk_score": score_from_pd(probability),
                           "risk_band": assign_risk_bands(probability, boundaries),
                           "ead": ead, "lgd_assumption": lgd,
                           "expected_loss": probability * lgd * ead,
                           "split": split, "model_version": MODEL_VERSION})
    scored.to_csv(output / "scored_applications.csv", index=False)
    odds = pd.DataFrame({"feature": selected, "coefficient": model.coef_[0],
                         "odds_ratio": np.exp(model.coef_[0]),
                         "input_unit": "+1 WoE = +1 log(good_distribution/bad_distribution)",
                         "coefficient_method": "L2 regularized unweighted logistic; no Wald CI reported"})
    odds.to_csv(output / "odds_ratios.csv", index=False)
    metrics = {name: performance_metrics(scored.loc[split == name, "target"], probability[split == name])
               for name in ["train", "validation", "test"]}
    metric_table = pd.DataFrame([{"split": name, **values} for name, values in metrics.items()])
    metric_table.to_csv(output / "model_metrics.csv", index=False)
    _write_json(output / "model_metrics.json", metrics)
    bands = risk_band_table(scored)
    bands.to_csv(output / "risk_bands.csv", index=False)
    calibration = calibration_table(scored)
    calibration.to_csv(output / "calibration.csv", index=False)
    cutoffs = cutoff_strategy(scored)
    cutoffs.to_csv(output / "cutoff_table.csv", index=False)
    comparisons = _strategy_comparisons(cutoffs)
    comparisons.to_csv(output / "strategy_comparisons.csv", index=False)
    with input_path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    manifest = {"model_version": MODEL_VERSION, "data_status": data_status,
                "input_path": str(input_path.resolve()), "input_sha256": digest,
                "seed": seed, "lgd": lgd, "ead_proxy": "nonnegative current credit_amount",
                "ead_missing_count": int(ead.isna().sum()),
                "target_definition": "TARGET repayment difficulty; not verified loss",
                "split_counts": split.value_counts().to_dict(), "split_method": "stratified random 60/20/20",
                "selected_features": selected, "iv_threshold": min_iv, "max_features": max_features,
                "intercept": float(model.intercept_[0]), "logistic_C": 0.5, "class_weight": None,
                "calibration_method": "native logistic probabilities; no post-hoc calibration fitted",
                "band_boundaries": boundaries.tolist(), "band_boundary_source": "validation predictions only",
                "score_base": 600, "score_base_good_bad_odds": 50, "score_pdo": 20,
                "woe_convention": "ln(smoothed good distribution / smoothed bad distribution)",
                "woe_smoothing": encoder.smoothing, "woe_specs": encoder.specs,
                "strategy_baseline_requested_validation_approval": 0.5,
                "test_used_for": "reporting metrics, observed band rates and fixed-threshold strategy only",
                "artifacts": [p.name for p in sorted(output.glob("*.csv"))]}
    _write_json(output / "model_manifest.json", manifest)
    # Persist plain bin specifications rather than a custom class pickle; the
    # artifact remains loadable when training was started through ``python -m``.
    joblib.dump({"encoder_specs": encoder.specs, "model": model, "features": selected,
                 "band_boundaries": boundaries, "manifest": manifest}, output / "scorecard.joblib")
    _write_plots(scored, bands, calibration, cutoffs, output)
    _write_findings(output, manifest, eda, metrics, bands, comparisons, encoder.iv)
    # Written last: a matching model manifest alone cannot prove that plots,
    # notebook tables and the business report all finished successfully.
    _write_json(completion_path, {"complete": True, "model_version": MODEL_VERSION,
                                 "input_sha256": digest, "lgd": lgd, "seed": seed,
                                 "data_status": data_status, "artifacts": ANALYSIS_ARTIFACTS})
    return manifest


def ensure_analysis(input_path: str | Path, output_dir: str | Path, *, data_status: str = "real",
                    lgd: float = 0.45, seed: int = 42) -> dict[str, Any]:
    """Reuse only a matching completed analysis; notebooks never invent outputs."""
    output = Path(output_dir)
    manifest_path = output / "model_manifest.json"
    completion_path = output / "analysis_complete.json"
    if manifest_path.exists() and completion_path.exists():
        manifest = json.loads(manifest_path.read_text())
        completion = json.loads(completion_path.read_text())
        with Path(input_path).open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if (manifest.get("model_version") == MODEL_VERSION
                and completion.get("complete") is True and completion.get("model_version") == MODEL_VERSION
                and completion.get("input_sha256") == digest
                and completion.get("data_status") == data_status and completion.get("lgd") == lgd
                and completion.get("seed") == seed
                and manifest.get("input_sha256") == digest and manifest.get("data_status") == data_status
                and manifest.get("seed") == seed and manifest.get("lgd") == lgd
                and all((output / item).is_file() for item in ANALYSIS_ARTIFACTS)):
            return manifest
    return run_analysis(input_path, output_dir, data_status=data_status, lgd=lgd, seed=seed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lgd", type=float, default=0.45)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--data-status", choices=["real", "synthetic", "sample"], default="real")
    args = parser.parse_args()
    manifest = run_analysis(args.input, args.output, lgd=args.lgd, seed=args.seed, data_status=args.data_status)
    print(json.dumps({"output": str(args.output.resolve()), "data_status": manifest["data_status"],
                      "split_counts": manifest["split_counts"], "selected_features": manifest["selected_features"]}, indent=2))


if __name__ == "__main__":
    main()
