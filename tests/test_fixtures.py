"""Original fixture reproducibility and independent numerical outcome checks."""
from __future__ import annotations

import math
import statistics as oracle_stats

from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.statistics import analyze, build_baseline


def _independent_stability(means, ranges, center, sigma_xbar, r_ucl):
    z = [(mean - center) / sigma_xbar for mean in means]
    assert all(abs(value) <= 3 for value in z)
    assert all(0 <= value <= r_ucl for value in ranges)
    for size in (3, 5, 6, 8, 9, 14, 15):
        for start in range(len(z) - size + 1):
            w = z[start : start + size]
            if size == 3:
                assert sum(value > 2 for value in w) < 2
                assert sum(value < -2 for value in w) < 2
            elif size == 5:
                assert sum(value > 1 for value in w) < 4
                assert sum(value < -1 for value in w) < 4
            elif size == 6:
                assert not all(w[i] < w[i + 1] for i in range(5))
                assert not all(w[i] > w[i + 1] for i in range(5))
            elif size == 8:
                assert not (all(abs(value) > 1 for value in w) and min(w) < 0 < max(w))
            elif size == 9:
                assert not (min(w) > 0 or max(w) < 0)
            elif size == 14:
                assert not all((w[i + 1] - w[i]) * (w[i + 2] - w[i + 1]) < 0 for i in range(12))
            elif size == 15:
                assert max(abs(value) for value in w) >= 1


def test_baseline_and_normal_really_pass_all_eight_rules_and_range_oracle():
    phase_one = baseline_dataset()
    means = [oracle_stats.mean(g.values) for g in phase_one.groups]
    ranges = [max(g.values) - min(g.values) for g in phase_one.groups]
    center = oracle_stats.mean(means)
    rbar = oracle_stats.mean(ranges)
    sigma_xbar = rbar / 2.326 / math.sqrt(5)
    _independent_stability(means, ranges, center, sigma_xbar, 2.114 * rbar)
    for key in ("normal", "followup"):
        monitoring = demo_datasets()[key]
        _independent_stability(
            [oracle_stats.mean(g.values) for g in monitoring.groups],
            [max(g.values) - min(g.values) for g in monitoring.groups],
            center, sigma_xbar, 2.114 * rbar,
        )
    # A symmetric constant-mean fixture would trip R7; ours has genuine mean variation.
    assert len(set(means)) == 25
    assert build_baseline(phase_one, "fixture-baseline")["group_count"] == 25


def test_fixture_functions_are_reproducible_fresh_and_original_only():
    first = demo_datasets()
    second = demo_datasets()
    assert first == second
    assert baseline_dataset() == baseline_dataset()
    for dataset in [baseline_dataset(), *first.values()]:
        assert dataset.source == "original_synthetic"
        assert len(dataset.groups) == 25
        assert all(len(group.values) == 5 for group in dataset.groups)
        assert "scenario" not in type(dataset).model_fields
        assert "case" not in type(dataset).model_fields
    before = second["normal"].groups[0].values[0]
    first["normal"].groups[0].values[0] = -12.0
    assert second["normal"].groups[0].values[0] == before
    assert first["variance"].groups[0].values[0] == before


def test_monitoring_after_baseline_and_valid_study_and_calibration():
    phase_one = baseline_dataset()
    last = phase_one.groups[-1].timestamp_ms
    for key, dataset in demo_datasets().items():
        expected_offset = 26 if key == "followup" else 1
        assert dataset.groups[0].timestamp_ms == last + expected_offset * 60_000
        assert set(g.group_id for g in dataset.groups).isdisjoint(g.group_id for g in phase_one.groups)
        assert all(g.timestamp_ms < phase_one.msa.valid_until_ms for g in dataset.groups)
        assert all(g.timestamp_ms < phase_one.calibrations[0].valid_until_ms for g in dataset.groups)
        assert dataset.msa.study_id == phase_one.msa.study_id


def test_scenarios_derive_expected_signals_or_quality_blocks_not_precomputed_answers():
    baseline = build_baseline(baseline_dataset(), "fixture-status")
    scenarios = demo_datasets()
    expected = {
        "normal": ("stable", None),
        "followup": ("stable", None),
        "mean_shift": ("signals", None),
        "variance": ("signals", None),
        "gauge_shift": ("signals", None),
        "trend": ("signals", None),
        "mixed_batch": ("not_evaluable", "MIXED_BATCH"),
        "missing": ("not_evaluable", "MISSING_MEASUREMENT"),
        "msa_failed": ("not_evaluable", "MSA_FAILED"),
        "calibration_expired": ("not_evaluable", "CALIBRATION_EXPIRED"),
    }
    assert set(scenarios) == set(expected)
    for key, (status, issue) in expected.items():
        result = analyze(scenarios[key], baseline)
        assert result["status"] == status
        if issue:
            assert issue in {item["code"] for item in result["issues"]}
        if status != "stable":
            assert result["capability"]["cpk"] is None
            assert result["capability"]["ppk"] is None
        else:
            assert result["capability"]["status"] == "available"
    assert any(hit["chart"] == "r" for hit in analyze(scenarios["variance"], baseline)["rule_hits"])
    assert any(hit["rule"] == "R3" for hit in analyze(scenarios["trend"], baseline)["rule_hits"])
    gauge = analyze(scenarios["gauge_shift"], baseline)
    assert any(hit["chart"] == "xbar" for hit in gauge["rule_hits"])
    assert "root_cause" not in gauge
    assert any("does not identify" in item for item in gauge["assumptions"])


def test_followup_is_genuinely_later_independent_measurements_and_capable():
    import numpy as np

    phase_one = baseline_dataset()
    scenarios = demo_datasets()
    followup = scenarios["followup"]
    prior = [phase_one, *(dataset for key, dataset in scenarios.items() if key != "followup")]
    assert followup.groups[0].timestamp_ms == max(g.timestamp_ms for d in prior for g in d.groups) + 60_000
    prior_ids = {g.group_id for dataset in prior for g in dataset.groups}
    prior_values = {value for dataset in prior for g in dataset.groups for value in g.values if value is not None}
    assert prior_ids.isdisjoint(g.group_id for g in followup.groups)
    assert prior_values.isdisjoint(value for g in followup.groups for value in g.values)
    # Explicit generator oracle protects against copying old values and only changing provenance.
    expected = np.random.default_rng(161803).normal(10.0, 0.02, size=(25, 5)).tolist()
    assert [g.values for g in followup.groups] == expected
    assert all(b.timestamp_ms - a.timestamp_ms == 60_000 for a, b in zip(followup.groups, followup.groups[1:], strict=False))
    baseline = build_baseline(phase_one, "acceptance-baseline")
    result = analyze(followup, baseline)
    assert result["status"] == "stable"
    assert result["rule_hits"] == result["issues"] == []
    assert result["capability"]["status"] == "available"
    assert result["capability"]["n_groups"] == 25
    assert result["capability"]["n_values"] == 125
    assert result["baseline_hash"] == baseline["baseline_hash"]


def test_fixture_labels_are_not_engine_inputs():
    from spc_agent.domain import Dataset

    baseline = build_baseline(baseline_dataset(), "label-free")
    raw = demo_datasets()["mean_shift"].model_dump(mode="python")
    raw["dataset_id"] = "normal"
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["status"] == "signals"
    raw = demo_datasets()["normal"].model_dump(mode="python")
    raw["dataset_id"] = "mean_shift"
    assert analyze(Dataset.model_validate(raw), baseline)["status"] == "stable"
