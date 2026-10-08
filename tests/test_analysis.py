"""Behavior checks for leakage boundaries, score interpretation and artifacts."""

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.analysis import (
    WoeEncoder, _strategy_comparisons, assign_splits, cutoff_strategy, load_features,
    risk_band_table, run_analysis, score_from_pd, wilson_interval,
    ensure_analysis, MODEL_VERSION,
)
from src.notebooks import execute_all


def synthetic_features(size=600):
    generator = np.random.default_rng(44)
    age = generator.uniform(20, 75, size)
    external = generator.uniform(0.02, 0.95, size)
    income = generator.lognormal(11.8, 0.45, size)
    credit = generator.lognormal(12.7, 0.5, size)
    probability = 1 / (1 + np.exp(-(-2.0 + 3 * (0.5 - external) + (40 - age) * 0.025)))
    target = generator.binomial(1, probability)
    return pd.DataFrame({
        "applicant_id": np.arange(100000, 100000 + size), "target": target,
        "age_years": age, "employed_years": generator.uniform(0, 30, size),
        "income_amount": income, "credit_amount": credit,
        "credit_to_income": credit / income, "ext_source_2": external,
        "income_type": generator.choice(["Working", "Pensioner", "XNA"], size),
        "education_type": generator.choice(["Secondary", "Higher"], size),
        "bureau_debt_sum": generator.choice([0.0, 50000.0, np.nan], size),
    })


class AnalysisTests(unittest.TestCase):
    def test_score_direction_and_pdo(self):
        base_pd = 1 / 51
        double_good_odds_pd = 1 / 101
        scores = score_from_pd([base_pd, double_good_odds_pd, 0.9])
        self.assertAlmostEqual(scores[0], 600)
        self.assertAlmostEqual(scores[1], 620)
        self.assertLess(scores[2], scores[0])

    def test_wilson_handles_no_events_and_empty(self):
        low, high = wilson_interval(0, 100)
        self.assertAlmostEqual(low, 0)
        self.assertGreater(high, 0)
        self.assertTrue(np.isnan(wilson_interval(0, 0)[0]))

    def test_missing_and_unseen_are_finite(self):
        frame = synthetic_features()
        frame.loc[0:10, "age_years"] = np.nan
        encoder = WoeEncoder().fit(frame, frame.target)
        new = frame.head(3).copy()
        new["income_type"] = ["new-category", None, "XAP"]
        new["age_years"] = [999, -100, np.nan]
        values = encoder.transform(new)
        self.assertTrue(np.isfinite(values.to_numpy()).all())
        missing = encoder.tables[(encoder.tables.feature == "age_years") &
                                 (encoder.tables.bin == "__MISSING__")]
        self.assertEqual(int(missing.applicants.iloc[0]), 11)
        self.assertEqual(set(encoder.tables.fit_split), {"train"})

    def test_validation_threshold_is_held_fixed_on_test(self):
        scored = pd.DataFrame({
            "pd": [0.1, 0.2, 0.3, 0.4, 0.11, 0.12, 0.13, 0.9],
            "target": [0, 1, 0, 1, 1, 1, 0, 0],
            "split": ["validation"] * 4 + ["test"] * 4,
            "expected_loss": [1.0] * 8, "ead": [100.0] * 8,
        })
        cutoffs = cutoff_strategy(scored)
        baseline = cutoffs[np.isclose(cutoffs.requested_validation_approval_rate, 0.5)].iloc[0]
        self.assertEqual(baseline.pd_cutoff, 0.2)
        self.assertEqual(baseline.validation_approved_count, 2)
        self.assertEqual(baseline.test_approved_count, 3)
        scored.loc[scored.split == "test", "target"] = 1 - scored.loc[scored.split == "test", "target"]
        changed = cutoff_strategy(scored)
        np.testing.assert_equal(cutoffs.pd_cutoff.to_numpy(), changed.pd_cutoff.to_numpy())

    def test_duplicate_applicant_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "features.csv"
            data = synthetic_features()
            data.loc[1, "applicant_id"] = data.loc[0, "applicant_id"]
            data.to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "unique"):
                load_features(path)

    def test_missing_ead_denominators_and_unavailable_portfolio(self):
        scored = pd.DataFrame({
            "pd": [0.1, 0.2, 0.3, 0.4, 0.11, 0.12, 0.13, 0.9],
            "target": [0, 1, 0, 1, 1, 1, 0, 0],
            "split": ["validation"] * 4 + ["test"] * 4,
            "risk_band": [1, 2, 3, 4, 1, 2, 3, 10],
            "risk_score": score_from_pd([0.1, 0.2, 0.3, 0.4, 0.11, 0.12, 0.13, 0.9]),
            "expected_loss": [4.5, np.nan, 13.5, 18, 4.95, np.nan, 5.85, 40.5],
            "ead": [100, np.nan, 100, 100, 100, np.nan, 100, 100],
        })
        baseline = cutoff_strategy(scored).query("requested_validation_approval_rate == 0.5").iloc[0]
        self.assertEqual(baseline.validation_approved_count, 2)
        self.assertEqual(baseline.validation_approved_known_ead_count, 1)
        self.assertEqual(baseline.validation_approved_missing_ead_count, 1)
        self.assertEqual(baseline.validation_expected_loss_coverage_rate, 0.5)
        self.assertEqual(baseline.validation_expected_loss_per_approved, 2.25)
        self.assertEqual(baseline.validation_expected_loss_per_approved_known_ead, 4.5)
        scored["ead"] = np.nan
        scored["expected_loss"] = np.nan
        cutoffs = cutoff_strategy(scored)
        nonempty = cutoffs[cutoffs.validation_approved_count > 0]
        self.assertTrue(nonempty.validation_expected_loss.isna().all())
        self.assertTrue(nonempty.validation_approved_ead.isna().all())
        self.assertTrue((nonempty.validation_expected_loss_coverage_rate == 0).all())
        empty = cutoffs[cutoffs.validation_approved_count == 0]
        self.assertTrue((empty.validation_expected_loss == 0).all())
        bands = risk_band_table(scored)
        self.assertTrue(bands.loc[bands.applicants > 0, "expected_loss"].isna().all())
        self.assertTrue((bands.loc[bands.applicants == 0, "expected_loss"] == 0).all())
        comparisons = _strategy_comparisons(cutoffs)
        self.assertTrue(comparisons.expected_loss_change.isna().all())

    def test_notebooks_reuse_completed_custom_lgd_and_seed(self):
        class FakeNotebookClient:
            def __init__(self, notebook, **kwargs):
                self.notebook = notebook

            def execute(self, **kwargs):
                # Run the real shared notebook cell, avoiding only the local
                # kernel socket transport. Wrong LGD would cause a real refit.
                ipython = type("NotebookIPython", (), {"run_line_magic": lambda *args: None})()
                exec(self.notebook.cells[1].source, {"get_ipython": lambda: ipython})

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            feature_path = folder / "features.csv"
            analysis = folder / "analysis"
            synthetic_features().to_csv(feature_path, index=False)
            run_analysis(feature_path, analysis, data_status="synthetic", lgd=0.6, seed=19)
            original_scores = (analysis / "scored_applications.csv").read_bytes()
            with patch("src.notebooks.NotebookClient", FakeNotebookClient), contextlib.redirect_stdout(io.StringIO()):
                paths = execute_all(feature_path, analysis, folder / "notebooks", "synthetic")
            self.assertEqual(len(paths), 4)
            self.assertEqual((analysis / "scored_applications.csv").read_bytes(), original_scores)
            manifest = json.loads((analysis / "model_manifest.json").read_text())
            self.assertEqual(manifest["lgd"], 0.6)
            self.assertEqual(manifest["seed"], 19)
            execution = json.loads((folder / "notebooks" / "execution_manifest.json").read_text())
            self.assertEqual(execution["lgd"], 0.6)
            self.assertEqual(execution["seed"], 19)

    def test_analysis_cache_requires_model_version_and_complete_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            feature_path = folder / "features.csv"
            output = folder / "analysis"
            synthetic_features().to_csv(feature_path, index=False)
            run_analysis(feature_path, output, data_status="synthetic")
            with patch("src.analysis.run_analysis", wraps=run_analysis) as rerun:
                ensure_analysis(feature_path, output, data_status="synthetic")
                rerun.assert_not_called()
                (output / "chi_square_tests.csv").unlink()
                ensure_analysis(feature_path, output, data_status="synthetic")
                self.assertEqual(rerun.call_count, 1)
                self.assertTrue((output / "chi_square_tests.csv").exists())
                manifest = json.loads((output / "model_manifest.json").read_text())
                manifest["model_version"] = "obsolete-model"
                (output / "model_manifest.json").write_text(json.dumps(manifest))
                ensure_analysis(feature_path, output, data_status="synthetic")
                self.assertEqual(rerun.call_count, 2)
                repaired = json.loads((output / "model_manifest.json").read_text())
                self.assertEqual(repaired["model_version"], MODEL_VERSION)
            completed = json.loads((output / "analysis_complete.json").read_text())
            self.assertTrue(completed["complete"])
            self.assertTrue(all((output / artifact).exists() for artifact in completed["artifacts"]))

    def test_artifacts_and_test_features_do_not_enter_model_fit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            features = synthetic_features()
            original_input = folder / "features.csv"
            features.to_csv(original_input, index=False)
            first = run_analysis(original_input, folder / "first", data_status="synthetic")
            scored = pd.read_csv(folder / "first" / "scored_applications.csv")
            self.assertEqual(len(scored), len(features))
            self.assertTrue(scored.applicant_id.is_unique)
            self.assertTrue(scored.pd.between(0, 1).all())
            self.assertTrue(scored.risk_band.between(1, 10).all())
            self.assertEqual(first["split_counts"], {"train": 360, "validation": 120, "test": 120})
            metrics = pd.read_csv(folder / "first" / "model_metrics.csv")
            self.assertEqual(set(metrics.split), {"train", "validation", "test"})
            bands = pd.read_csv(folder / "first" / "risk_bands.csv")
            test_bands = bands[(bands.split == "test") & (bands.applicants > 0)]
            self.assertTrue((test_bands.mean_predicted_pd.diff().dropna() >= 0).all())
            self.assertIn("SYNTHETIC", (folder / "first" / "findings.md").read_text())
            # Changing held-out features must change predictions but cannot alter
            # train WoE/clipping/selection/model or validation threshold choices.
            split = assign_splits(features)
            changed = features.copy()
            changed.loc[split == "test", "ext_source_2"] = -99999
            changed.loc[split == "test", "income_type"] = "never-in-training"
            changed_input = folder / "changed.csv"
            changed.to_csv(changed_input, index=False)
            second = run_analysis(changed_input, folder / "second", data_status="synthetic")
            self.assertEqual(first["woe_specs"], second["woe_specs"])
            self.assertEqual(first["selected_features"], second["selected_features"])
            self.assertEqual(first["intercept"], second["intercept"])
            self.assertEqual(first["band_boundaries"], second["band_boundaries"])
            odds1 = pd.read_csv(folder / "first" / "odds_ratios.csv")
            odds2 = pd.read_csv(folder / "second" / "odds_ratios.csv")
            pd.testing.assert_frame_equal(odds1, odds2)


if __name__ == "__main__":
    unittest.main()
