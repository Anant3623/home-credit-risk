"""Create and execute the four notebooks against a completed, verified analysis."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMON = """from pathlib import Path
import os
import sys
import json
import pandas as pd
from IPython.display import display, Image, Markdown

# The executor supplies these paths. Interactive runs default to local exports.
ROOT = Path(os.environ.get('HOME_CREDIT_PROJECT_ROOT', str(Path.cwd()))).resolve()
if not (ROOT / 'src' / 'analysis.py').exists():
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
from src.analysis import ensure_analysis, load_features, eda_tables

INPUT = Path(os.environ.get('HOME_CREDIT_FEATURES', str(ROOT / 'exports' / 'applicant_features.csv')))
OUTPUT = Path(os.environ.get('HOME_CREDIT_ANALYSIS', str(ROOT / 'outputs' / 'analysis')))
STATUS = os.environ.get('HOME_CREDIT_DATA_STATUS', 'real')
LGD = float(os.environ.get('HOME_CREDIT_LGD', '0.45'))
SEED = int(os.environ.get('HOME_CREDIT_SEED', '42'))
manifest = ensure_analysis(INPUT, OUTPUT, data_status=STATUS, lgd=LGD, seed=SEED)
get_ipython().run_line_magic('matplotlib', 'inline')
display(Markdown(f'**Data status: {manifest["data_status"].upper()}**. '
    + ('Synthetic pipeline validation; no Home Credit conclusions.' if STATUS != 'real'
       else 'Computed from the supplied accepted-cohort export.')))
"""

NOTEBOOKS = {
    "01_eda.ipynb": [
        ("markdown", "# 1 · Applicant EDA\n\nExplore the observed cohort, segment repayment-difficulty rates and missingness. TARGET is a repayment-difficulty proxy, not a measured loss. These tables describe all applicants; predictive selection uses training rows only in the next notebook."),
        ("code", COMMON),
        ("code", "frame = load_features(INPUT)\neda = eda_tables(frame, OUTPUT)\ndisplay(eda['overall'])\ndisplay(eda['nulls'].sort_values('null_pct', ascending=False).head(20))"),
        ("markdown", "## Segment rates\n\nWilson 95% intervals show rate uncertainty. Small segments can have noisy rates; compare sample sizes before interpreting differences. This is descriptive exploration, not a decision rule fitted to the test outcomes."),
        ("code", "segments = eda['segments']\ndisplay(segments.sort_values(['feature', 'bad_rate'], ascending=[True, False]).head(40))"),
        ("code", "import matplotlib.pyplot as plt\nage = segments[segments.feature == 'age_band']\nif not age.empty:\n    ax = age.plot.bar(x='level', y='bad_rate', legend=False, figsize=(7, 4), title='Observed TARGET rate by age band')\n    ax.set_ylabel('Repayment-difficulty rate')\n    ax.set_xlabel('Age band')\n    plt.tight_layout()\n    plt.show()"),
        ("markdown", "## Scope\n\nOnly application_train outcomes are analyzed. Raw data retain test-population historical records; SQL filters intermediate features to the observed training applicants. Missing numeric history measurements remain missing, while genuinely absent history counts are zero. Full raw null/sentinel/orphan checks are in the separate DQ exports."),
    ],
    "02_risk_drivers.ipynb": [
        ("markdown", "# 2 · Risk drivers: WoE, IV and chi-square\n\nEvery bin edge, rare-category mapping, clipping bound and feature-selection statistic is fitted on the training 60%. Validation and test labels do not enter these calculations. WoE is log(good distribution / bad distribution), with 0.5 additive smoothing; missing values have a separate bucket."),
        ("code", COMMON),
        ("code", "iv = pd.read_csv(OUTPUT / 'iv_summary.csv')\nwoe = pd.read_csv(OUTPUT / 'woe_bins.csv')\nchi = pd.read_csv(OUTPUT / 'chi_square_tests.csv')\ndisplay(iv.head(25))\ndisplay(chi.head(25))"),
        ("markdown", "## Read a driver\n\nHigh IV indicates separation of good/bad distributions in the training sample. IV is an association screen, not a guarantee of future performance. Chi-square tests use Benjamini–Hochberg false-discovery correction and report Cramér's V. Sparse expected counts are flagged; significance in a large dataset can coexist with a small practical effect."),
        ("code", "top_feature = iv.iloc[0].feature\ndisplay(Markdown(f'### Train-only bins: {top_feature}'))\ndisplay(woe[woe.feature == top_feature])\nassert set(woe.fit_split.unique()) == {'train'}\nassert set(iv.fit_split.unique()) == {'train'}"),
        ("code", "display(pd.DataFrame([{'feature': name, 'kind': spec['kind'],\n    'clip_low': spec.get('clip_low'), 'clip_high': spec.get('clip_high'),\n    'numeric_edges': spec.get('edges'), 'rare_minimum': spec.get('minimum_count')}\n    for name, spec in manifest['woe_specs'].items()]).head(30))"),
        ("markdown", "## Missing, rare and unseen values\n\nMissing is explicitly included even when absent in train. Rare observed levels share a train-defined bucket. Unseen levels receive neutral WoE 0 when no rare bucket exists; otherwise they use the train rare-level bucket. Outlying future values are clipped using train 1st/99th percentiles before applying the persisted bins. The statistical tests are exploratory because continuous bins are data derived."),
    ],
    "03_scorecard.ipynb": [
        ("markdown", "# 3 · Interpretable scorecard and untouched test evaluation\n\nUnweighted logistic regression estimates the cohort's probability of TARGET=1 from train-selected WoE features. Fixed L2 regularization controls correlated features. Native logistic probabilities are retained; no calibration or model tuning uses test outcomes. The exported score represents score 600 at good:bad odds 50:1, with 20 points doubling those odds."),
        ("code", COMMON),
        ("code", """metrics = pd.read_csv(OUTPUT / 'model_metrics.csv')
odds = pd.read_csv(OUTPUT / 'odds_ratios.csv')
bands = pd.read_csv(OUTPUT / 'risk_bands.csv')
calibration = pd.read_csv(OUTPUT / 'calibration.csv')
display(metrics)
display(odds.sort_values('coefficient'))
display(Markdown(f'Intercept: **{manifest["intercept"]:.4f}**. Selected features: **{len(manifest["selected_features"])}**.'))"""),
        ("markdown", "## Interpret odds ratios correctly\n\nEach odds ratio is exp(coefficient) for a **+1 WoE** change, conditional on the other included features. It is not a one-year age change or a one-currency-unit income change. The model uses regularized estimates, so the table does not present naive Wald confidence intervals. Coefficients can change sign with correlated predictors; they are not causal effects."),
        ("code", "display(Image(filename=str(OUTPUT / 'figures' / 'test_roc.png')))\ndisplay(Image(filename=str(OUTPUT / 'figures' / 'test_calibration.png')))\ndisplay(calibration[calibration.split == 'test'])"),
        ("markdown", "## Risk bands\n\nNine validation-prediction quantiles define ten increasing predicted-risk bands. Held-out observed rates are shown with Wilson intervals and may reverse. Test outcomes never trigger bin reordering, merging, recalibration or a refit. Tied predictions can produce empty bands. Higher risk_score means safer; higher risk_band means riskier."),
        ("code", "display(bands[bands.split.isin(['validation', 'test'])])\ndisplay(Image(filename=str(OUTPUT / 'figures' / 'risk_bands.png')))\nviolations = bands[bands.split.isin(['validation', 'test']) & bands.observed_rate_monotonicity_violation]\ndisplay(Markdown(f'Observed nonempty-band monotonicity reversals: **{len(violations)}**.'))\nprint('Validation-defined PD boundaries:', manifest['band_boundaries'])"),
        ("markdown", "## Validation limits\n\nThe 60/20/20 split is stratified and at applicant level, with no duplicate applicants. It is random because no usable calendar date of the current application is provided; it does not establish temporal performance. AUC, KS, Gini and Brier scores are point estimates for this cohort. Inspect sample sizes and calibration before considering any deployment."),
    ],
    "04_strategy.ipynb": [
        ("markdown", "# 4 · Approval and risk strategy\n\nIllustrative approval policies use **validation prediction thresholds only**, then report the same fixed thresholds on the untouched test split. The baseline requests 50% validation-cohort approval. Ten percent more approvals relative requests 55%; ten percentage points more requests 60%. Actual approval rates can differ due to threshold ties and cohort differences."),
        ("code", COMMON),
        ("code", "cutoffs = pd.read_csv(OUTPUT / 'cutoff_table.csv')\ncomparisons = pd.read_csv(OUTPUT / 'strategy_comparisons.csv')\ndisplay(cutoffs)\ndisplay(comparisons)\ndisplay(Image(filename=str(OUTPUT / 'figures' / 'strategy_tradeoff.png')))"),
        ("markdown", "## Answer: 10% more approvals\n\nCompare the 50% and 55% requested policies with the fixed test thresholds. Approval/bad-rate intervals come from observed accepted-cohort outcomes; expected loss is a model-and-assumption scenario. No test-derived optimization or outcome-based acceptance decision is made."),
        ("code", """row = comparisons[(comparisons.scenario == '10_percent_more_approvals_relative') & (comparisons.split == 'test')].iloc[0]
display(Markdown(f'Fixed thresholds approve **{row.baseline_actual_approval_rate:.2%} → {row.scenario_actual_approval_rate:.2%}** on test. '
    f'Observed TARGET rate among approved applicants is **{row.baseline_bad_rate:.2%} → {row.scenario_bad_rate:.2%}** '
    f'(**{row.bad_rate_change_percentage_points:+.2f} percentage points**). '
    f'Scenario expected-loss change: **{row.expected_loss_change:,.2f} dataset currency units**.'))"""),
        ("markdown", "## Reject inference and expected-loss assumptions\n\nOnly current accepted-applicant outcomes are observed. Previously rejected current applicants have no outcome, so these curves cannot say what happens when lending to them. Refusal history is a feature, not a substitute for missing rejected outcomes. Expected loss is PD × assumed LGD × EAD, using current credit as the exposure proxy. LGD is configurable (default 45%) and is not estimated from recoveries; execution preserves the completed analysis's LGD. TARGET records repayment difficulty, not confirmed loss. Known/missing EAD counts and EL coverage accompany each policy. Loss is a known-EAD subtotal when coverage is incomplete and is unavailable for a nonempty all-missing-EAD portfolio. The all-approved denominator and known-EAD-only denominator are labeled separately. Neither scenario EL nor its differences are accounting loss forecasts."),
        ("code", "display(Markdown((OUTPUT / 'findings.md').read_text()))"),
    ],
}


def create_notebooks(directory: str | Path | None = None) -> list[Path]:
    output = Path(directory) if directory else PROJECT_ROOT / "notebooks"
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, cells in NOTEBOOKS.items():
        notebook = nbformat.v4.new_notebook()
        notebook.cells = [nbformat.v4.new_code_cell(content) if kind == "code"
                          else nbformat.v4.new_markdown_cell(content) for kind, content in cells]
        notebook.metadata.kernelspec = {"display_name": "Python 3", "language": "python", "name": "python3"}
        notebook.metadata.language_info = {"name": "python", "version": "3"}
        path = output / name
        nbformat.write(notebook, path)
        paths.append(path)
    return paths


def execute_notebooks(input_path: str | Path, analysis_dir: str | Path,
                      output_dir: str | Path, *, data_status: str = "real",
                      lgd: float | None = None, seed: int | None = None,
                      timeout: int = 1800) -> list[str]:
    """Execute each source notebook and save actual outputs to a separate folder."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    saved_config = Path(analysis_dir) / "model_manifest.json"
    completed = json.loads(saved_config.read_text()) if saved_config.exists() else {}
    lgd = float(completed.get("lgd", 0.45)) if lgd is None else lgd
    seed = int(completed.get("seed", 42)) if seed is None else seed
    keys = {"HOME_CREDIT_PROJECT_ROOT": str(PROJECT_ROOT),
            "HOME_CREDIT_FEATURES": str(Path(input_path).resolve()),
            "HOME_CREDIT_ANALYSIS": str(Path(analysis_dir).resolve()),
            "HOME_CREDIT_DATA_STATUS": data_status, "HOME_CREDIT_LGD": str(lgd),
            "HOME_CREDIT_SEED": str(seed)}
    previous = {key: os.environ.get(key) for key in keys}
    paths = []
    kernel_root = output / ".kernels"
    kernel_folder = kernel_root / "home-credit"
    kernel_folder.mkdir(parents=True, exist_ok=True)
    (kernel_folder / "kernel.json").write_text(json.dumps({
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "Home Credit project runtime", "language": "python",
    }))
    try:
        os.environ.update(keys)
        for source in sorted((PROJECT_ROOT / "notebooks").glob("*.ipynb")):
            notebook = nbformat.read(source, as_version=4)
            manager = KernelManager(kernel_name="home-credit", kernel_spec_manager=
                                    KernelSpecManager(kernel_dirs=[str(kernel_root.resolve())]))
            client = NotebookClient(notebook, km=manager, timeout=timeout, kernel_name="home-credit",
                                    resources={"metadata": {"path": str(PROJECT_ROOT)}})
            client.execute(cleanup_kc=True)
            path = output / source.name
            nbformat.write(notebook, path)
            paths.append(str(path.resolve()))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    (output / "execution_manifest.json").write_text(json.dumps(
        {"data_status": data_status, "lgd": lgd, "seed": seed,
         "input": keys["HOME_CREDIT_FEATURES"],
         "analysis": keys["HOME_CREDIT_ANALYSIS"], "notebooks": paths}, indent=2) + "\n")
    return paths


def execute_all(input_path: str | Path, analysis_dir: str | Path,
                output_dir: str | Path, data_status: str = "real", *,
                lgd: float | None = None, seed: int | None = None) -> list[str]:
    """Small positional API for the end-to-end pipeline runner."""
    return execute_notebooks(input_path, analysis_dir, output_dir, data_status=data_status,
                             lgd=lgd, seed=seed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--analysis", type=Path, default=PROJECT_ROOT / "outputs" / "analysis")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs" / "notebooks")
    parser.add_argument("--data-status", choices=["real", "synthetic", "sample"], default="real")
    parser.add_argument("--lgd", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    if args.create:
        for path in create_notebooks():
            print(path)
    if args.input:
        for path in execute_notebooks(args.input, args.analysis, args.output,
                                      data_status=args.data_status, lgd=args.lgd, seed=args.seed):
            print(path)
    if not args.create and not args.input:
        parser.error("Specify --create and/or --input")


if __name__ == "__main__":
    main()
