"""Independent formula and rule oracles, provenance, and fail-closed quality tests."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics as oracle_stats
import time

import pytest

from spc_agent.domain import Dataset
from spc_agent.errors import DomainError
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.statistics import analyze, build_baseline, dataset_hash, nelson, validate_baseline


def _replace(dataset: Dataset, change) -> Dataset:
    raw = dataset.model_dump(mode="python")
    change(raw)
    return Dataset.model_validate(raw)


@pytest.fixture
def baseline():
    return build_baseline(baseline_dataset(), "independent-oracle")


@pytest.fixture
def normal():
    return demo_datasets()["normal"]


def _oracle_hits(z):
    """Enumerate start-first using set/sign and pair products, independent of engine."""
    result = set()
    lengths = {1: 1, 2: 9, 3: 6, 4: 14, 5: 3, 6: 5, 7: 15, 8: 8}
    for start in range(len(z)):
        for rule, length in lengths.items():
            w = z[start : start + length]
            if len(w) != length or None in w:
                continue
            differences = [w[i + 1] - w[i] for i in range(length - 1)]
            side_set = {math.copysign(1, v) if v else 0 for v in w}
            match = {
                1: min(w) < -3 or max(w) > 3,
                2: side_set in ({-1}, {1}),
                3: bool(differences) and (min(differences) > 0 or max(differences) < 0),
                4: len(differences) >= 2 and all(
                    differences[i] * differences[i + 1] < 0
                    for i in range(len(differences) - 1)
                ),
                5: len([v for v in w if v > 2]) >= 2 or len([v for v in w if v < -2]) >= 2,
                6: len([v for v in w if v > 1]) >= 4 or len([v for v in w if v < -1]) >= 4,
                7: max(abs(v) for v in w) < 1,
                8: min(abs(v) for v in w) > 1 and side_set == {-1, 1},
            }[rule]
            if match:
                result.add((f"R{rule}", start, start + length - 1))
    return result


# Each example reaches the target rule for the first time at its final point.
RULE_CASES = [
    ("R1", [3.01]), ("R1", [-3.01]),
    ("R2", [0.4] * 9), ("R2", [-0.4] * 9),
    ("R3", [-1.0, -0.8, -0.2, 0.3, 0.9, 1.2]),
    ("R3", [1.0, 0.8, 0.2, -0.3, -0.9, -1.2]),
    ("R4", [-0.5, 0.5] * 7),
    ("R5", [2.1, -0.2, 2.2]), ("R5", [-2.1, 0.2, -2.2]),
    ("R6", [1.1, 1.2, -0.2, 1.3, 1.4]), ("R6", [-1.1, -1.2, 0.2, -1.3, -1.4]),
    ("R7", [0.2, -0.1, 0.3, -0.4, -0.2] * 3),
    ("R8", [1.1, 1.2, -1.3, 1.4, -1.5, -1.2, 1.6, -1.7]),
]


@pytest.mark.parametrize("rule,values", RULE_CASES)
def test_all_nelson_rules_first_trigger_and_traceability(rule, values):
    assert not any(hit["rule"] == rule for hit in nelson(values[:-1]))
    hits = [hit for hit in nelson(values) if hit["rule"] == rule]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["start_index"] == 0
    assert hit["end_index"] == hit["index"] == len(values) - 1
    assert hit["window"] == values
    assert hit["window_size"] == len(values)
    assert len(hit["id"]) == 24
    assert nelson(values) == nelson(values)


@pytest.mark.parametrize("rule,values", [
    ("R1", [3.0, -3.0]),
    ("R2", [0.1] * 4 + [0.0] + [0.1] * 4),
    ("R3", [0.0, 0.1, 0.2, 0.2, 0.3, 0.4]),
    ("R4", [-0.5, 0.5] * 6 + [0.5, -0.5]),
    ("R5", [2.0, 2.0, 2.1]),
    ("R5", [2.1, -2.1, 0.0]),
    ("R6", [1.0, 1.2, 1.3, 1.4, -0.1]),
    ("R6", [1.2, 1.3, -1.2, -1.3, 0.0]),
    ("R7", [0.0] * 7 + [1.0] + [0.0] * 7),
    ("R7", [0.0] * 7 + [-1.0] + [0.0] * 7),
    ("R8", [1.0, -1.2, 1.2, -1.2, 1.2, -1.2, 1.2, -1.2]),
    ("R8", [1.2] * 8),
])
def test_exact_boundary_and_equality_break_rule(rule, values):
    assert not any(hit["rule"] == rule for hit in nelson(values))


@pytest.mark.parametrize("rule,values", RULE_CASES)
def test_missing_breaks_every_rule_window(rule, values):
    interrupted = values[: len(values) // 2] + [None] + values[len(values) // 2 :]
    if rule != "R1":
        assert not any(hit["rule"] == rule for hit in nelson(interrupted))
    resumed = nelson([None] + values)
    hit = next(hit for hit in resumed if hit["rule"] == rule)
    assert hit["start_index"] == 1
    assert hit["end_index"] == len(values)


def test_multiple_rules_same_point_and_overlapping_windows():
    hits = nelson([3.1] * 10)
    at_eight = {hit["rule"] for hit in hits if hit["index"] == 8}
    assert {"R1", "R2", "R5", "R6"} <= at_eight
    assert [(h["start_index"], h["end_index"]) for h in hits if h["rule"] == "R2"] == [(0, 8), (1, 9)]
    assert len({h["id"] for h in hits}) == len(hits)


def test_all_rules_against_independent_random_and_boundary_oracle():
    import random

    rng = random.Random(9173)
    examples = [values for _, values in RULE_CASES]
    examples += [[rng.choice([None, -3.1, -3, -2, -1, 0, 1, 2, 3, 3.1]) for _ in range(150)] for _ in range(20)]
    examples += [[rng.uniform(-3.5, 3.5) for _ in range(150)] for _ in range(20)]
    for values in examples:
        actual = {(h["rule"], h["start_index"], h["end_index"]) for h in nelson(values)}
        assert actual == _oracle_hits(values)


@pytest.mark.parametrize("values", [[math.nan], [math.inf], [-math.inf], [True], ["1"], [10**1000], [0.0] * 10_001])
def test_nelson_invalid_numbers_and_bound_fail_closed(values):
    with pytest.raises(DomainError) as error:
        nelson(values)
    assert error.value.code == "INVALID_Z"


def test_baseline_n5_25_formulas_against_standard_library_oracle(baseline):
    raw = [g.values for g in baseline_dataset().groups]
    ranges = [max(row) - min(row) for row in raw]
    center = oracle_stats.mean(oracle_stats.mean(row) for row in raw)
    rbar = oracle_stats.mean(ranges)
    within = rbar / 2.326
    se = within / math.sqrt(5)
    assert baseline["center"] == pytest.approx(center, rel=1e-14)
    assert baseline["rbar"] == pytest.approx(rbar, rel=1e-14)
    assert baseline["sigma_within"] == pytest.approx(within, rel=1e-14)
    assert baseline["sigma_xbar"] == pytest.approx(se, rel=1e-14)
    assert baseline["xbar_lcl"] == pytest.approx(center - 3 * se)
    assert baseline["xbar_ucl"] == pytest.approx(center + 3 * se)
    assert baseline["r_lcl"] == 0.0
    assert baseline["r_ucl"] == pytest.approx(2.114 * rbar)
    assert baseline["group_count"] == 25
    assert baseline["subgroup_size"] == 5
    assert not _oracle_hits([(oracle_stats.mean(row) - center) / se for row in raw])
    assert all(0 <= r <= 2.114 * rbar for r in ranges)


def test_baseline_rejects_24_or_26_without_slicing_future():
    raw = baseline_dataset().model_dump(mode="python")
    for groups in (raw["groups"][:-1], raw["groups"] + [raw["groups"][-1]]):
        with pytest.raises(DomainError) as error:
            build_baseline(Dataset.model_validate(dict(raw, groups=groups)), "count-test")
        assert error.value.code == "BASELINE_GROUP_COUNT"


@pytest.mark.parametrize("defect", ["missing", "n4", "duplicate", "order", "gap", "mixed", "quality", "msa", "calibration", "unit"])
def test_baseline_rejects_quality_defects(defect):
    raw = baseline_dataset().model_dump(mode="python")
    group = raw["groups"][10]
    if defect == "missing":
        group["values"][0] = None
    elif defect == "n4":
        group["values"].pop()
    elif defect == "duplicate":
        group["group_id"] = raw["groups"][0]["group_id"]
    elif defect == "order":
        raw["groups"][0], raw["groups"][1] = raw["groups"][1], raw["groups"][0]
    elif defect == "gap":
        group["timestamp_ms"] += 1
    elif defect == "mixed":
        group["sample_batch_ids"] = [group["batch_id"]] * 4 + ["other"]
    elif defect == "quality":
        group["measurement_status"] = "suspect"
    elif defect == "msa":
        raw["msa"]["status"] = "failed"
    elif defect == "calibration":
        raw["calibrations"][0]["valid_until_ms"] = raw["groups"][0]["timestamp_ms"]
    elif defect == "unit":
        raw["specifications"]["unit"] = "um"
    with pytest.raises(DomainError) as error:
        build_baseline(Dataset.model_validate(raw), "quality-test")
    assert error.value.code == "BASELINE_NOT_EVALUABLE"


def test_future_baseline_not_admitted_even_with_valid_msa():
    raw = baseline_dataset().model_dump(mode="python")
    start = time.time_ns() // 1_000_000 + 86_400_000
    for index, group in enumerate(raw["groups"]):
        group["timestamp_ms"] = start + index * 60_000
    raw["msa"]["valid_until_ms"] = start + 86_400_000
    raw["calibrations"][0]["valid_until_ms"] = start + 86_400_000
    with pytest.raises(DomainError) as error:
        build_baseline(Dataset.model_validate(raw), "future")
    assert "FUTURE_TIMESTAMP" in str(error.value)


@pytest.mark.parametrize("kind", ["mean", "r", "constant_mean", "trend"])
def test_baseline_phase_one_rejects_actual_chart_instability(kind):
    raw = baseline_dataset().model_dump(mode="python")
    if kind == "mean":
        raw["groups"][12]["values"] = [v + 0.5 for v in raw["groups"][12]["values"]]
    elif kind == "r":
        raw["groups"][12]["values"] = [9.0, 10.0, 10.0, 10.0, 11.0]
    elif kind == "constant_mean":
        for g in raw["groups"]:
            g["values"] = [9.98, 9.99, 10.0, 10.01, 10.02]
    else:
        for index, g in enumerate(raw["groups"]):
            center = 10.0 + index * 0.01
            g["values"] = [center - 0.04, center - 0.02, center, center + 0.02, center + 0.04]
    with pytest.raises(DomainError) as error:
        build_baseline(Dataset.model_validate(raw), "unstable")
    assert error.value.code == "BASELINE_UNSTABLE"
    if kind == "constant_mean":
        assert "R7" in str(error.value)
    if kind == "r":
        assert "r:R_LIMIT" in str(error.value)


def test_zero_within_variance_baseline_is_explicit():
    raw = baseline_dataset().model_dump(mode="python")
    for group in raw["groups"]:
        group["values"] = [10.0] * 5
    with pytest.raises(DomainError) as error:
        build_baseline(Dataset.model_validate(raw), "zero")
    assert error.value.code == "ZERO_VARIATION"


def test_monitoring_points_and_capability_against_independent_oracle(normal, baseline):
    analysis = analyze(normal, baseline)
    assert analysis["status"] == "stable"
    raw = [v for group in normal.groups for v in group.values]
    averages = [oracle_stats.mean(group.values) for group in normal.groups]
    ranges = [max(group.values) - min(group.values) for group in normal.groups]
    mean = oracle_stats.mean(raw)
    within = oracle_stats.mean(ranges) / 2.326
    overall = oracle_stats.stdev(raw)
    for index, point in enumerate(analysis["points"]):
        assert point["mean"] == pytest.approx(averages[index], rel=1e-14)
        assert point["range"] == pytest.approx(ranges[index], rel=1e-14)
        assert point["z"] == pytest.approx((averages[index] - baseline["center"]) / baseline["sigma_xbar"], abs=1e-12)
    capability = analysis["capability"]
    assert capability["status"] == "available"
    assert capability["n_values"] == 125
    assert capability["n_groups"] == 25
    assert capability["cpk"] == pytest.approx(min(10.1 - mean, mean - 9.9) / (3 * within), rel=1e-12)
    assert capability["ppk"] == pytest.approx(min(10.1 - mean, mean - 9.9) / (3 * overall), rel=1e-12)
    assert capability["cp"] == pytest.approx(0.2 / (6 * within))
    assert capability["pp"] == pytest.approx(0.2 / (6 * overall))
    assert capability["std_overall"] == pytest.approx(overall, rel=1e-12)
    assert capability["sigma_within"] != baseline["sigma_within"]
    assert capability["assumptions"]


def test_missing_spec_and_small_stable_window_disable_only_capability(normal, baseline):
    no_specs = _replace(normal, lambda raw: raw.update(specifications=None))
    result = analyze(no_specs, baseline)
    assert result["status"] == "stable"
    assert result["capability"]["cpk"] is None
    assert "SPECIFICATIONS_MISSING" in result["capability"]["reasons"]
    short = _replace(normal, lambda raw: raw.update(groups=raw["groups"][:24]))
    result = analyze(short, baseline)
    assert result["status"] == "stable"
    assert result["capability"]["status"] == "not_evaluable"
    assert "INSUFFICIENT_DATA" in result["capability"]["reasons"]


def test_specs_are_not_control_limits_and_revision_is_current(normal, baseline):
    raw = normal.model_dump(mode="python")
    raw["specifications"].update(lsl=9.999, usl=10.001, revision="narrow-2")
    changed = analyze(Dataset.model_validate(raw), baseline)
    original = analyze(normal, baseline)
    assert changed["status"] == "stable"
    assert changed["control_limits"] == original["control_limits"]
    assert changed["points"] == original["points"]
    assert changed["rule_hits"] == []
    assert changed["capability"]["cpk"] < original["capability"]["cpk"]
    assert changed["capability"]["specifications"]["revision"] == "narrow-2"
    raw = baseline_dataset().model_dump(mode="python")
    raw["specifications"] = None
    no_specs = build_baseline(Dataset.model_validate(raw), "no-specs")
    assert no_specs["xbar_lcl"] == baseline["xbar_lcl"]
    assert no_specs["xbar_ucl"] == baseline["xbar_ucl"]


def test_baseline_unchanged_by_extreme_monitoring_and_hash_tampering(normal, baseline):
    original = copy.deepcopy(baseline)
    raw = normal.model_dump(mode="python")
    raw["groups"][24]["values"] = [1e12] * 5
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["status"] == "signals"
    assert baseline == original
    assert result["control_limits"]["center"] == baseline["center"]
    assert result["baseline_hash"] == baseline["baseline_hash"]
    for key in ("center", "xbar_lcl", "r_ucl"):
        altered = copy.deepcopy(baseline)
        altered[key] += 0.1
        with pytest.raises(DomainError) as error:
            analyze(normal, altered)
        assert error.value.code == "BASELINE_HASH_MISMATCH"


def test_baseline_hash_covers_window_group_ids_specs_and_study(normal, baseline):
    changes = [
        lambda b: b["group_ids"].__setitem__(0, "replaced"),
        lambda b: b.update(window_start_ms=b["window_start_ms"] + 1),
        lambda b: b["msa"].update(study_id="changed"),
        lambda b: b["calibrations"][0].update(certificate_id="changed"),
        lambda b: b["specifications"].update(revision="changed"),
    ]
    for change in changes:
        altered = copy.deepcopy(baseline)
        change(altered)
        with pytest.raises(DomainError) as error:
            analyze(normal, altered)
        assert error.value.code == "BASELINE_HASH_MISMATCH"


@pytest.mark.parametrize("field", ["schema_version", "constants", "r_lcl", "subgroup_size", "group_count"])
def test_deleting_baseline_default_is_not_silently_normalized(normal, baseline, field):
    altered = copy.deepcopy(baseline)
    del altered[field]
    with pytest.raises(DomainError) as error:
        analyze(normal, altered)
    assert error.value.code == "INVALID_BASELINE"


def test_nested_baseline_defaults_are_also_protected_by_original_hash(normal, baseline):
    altered = copy.deepcopy(baseline)
    del altered["constants"]["d3"]
    with pytest.raises(DomainError) as error:
        analyze(normal, altered)
    assert error.value.code == "BASELINE_HASH_MISMATCH"


def test_hashes_use_canonical_json_and_all_raw_values(normal, baseline):
    raw = normal.model_dump(mode="json")
    expected = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert dataset_hash(normal) == expected
    assert dataset_hash(Dataset.model_validate(dict(reversed(list(raw.items()))))) == expected
    edited = _replace(normal, lambda data: data["groups"][0]["values"].__setitem__(0, 10.123))
    assert dataset_hash(edited) != expected
    payload = {k: v for k, v in baseline.items() if k != "baseline_hash"}
    expected_baseline = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert baseline["baseline_hash"] == expected_baseline


def test_unicode_canonical_hashes_match_utf8_payload_not_ascii_escapes(normal):
    characteristic = "孔径 μm"
    phase_one = _replace(baseline_dataset(), lambda raw: raw.update(characteristic=characteristic))
    monitoring = _replace(normal, lambda raw: raw.update(characteristic=characteristic))
    baseline = build_baseline(phase_one, "utf8-baseline")
    dataset_payload = monitoring.model_dump(mode="json")
    baseline_payload = {k: v for k, v in baseline.items() if k != "baseline_hash"}
    for payload, actual in ((dataset_payload, dataset_hash(monitoring)), (baseline_payload, baseline["baseline_hash"])):
        utf8_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        ascii_json = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert characteristic in utf8_json
        assert hashlib.sha256(utf8_json.encode("utf-8")).hexdigest() == actual
        assert hashlib.sha256(ascii_json.encode("utf-8")).hexdigest() != actual
    result = analyze(monitoring, baseline)
    assert result["status"] == "stable"
    assert result["baseline_hash"] == baseline["baseline_hash"]
    assert result["dataset_hash"] == dataset_hash(monitoring)


@pytest.mark.parametrize("defect,code", [
    ("missing", "MISSING_MEASUREMENT"), ("n4", "SUBGROUP_SIZE"),
    ("duplicate", "DUPLICATE_GROUP_ID"), ("order", "OUT_OF_ORDER"),
    ("time", "DUPLICATE_TIMESTAMP"), ("gap", "SAMPLING_GAP"),
    ("mixed", "MIXED_BATCH"), ("condition", "MIXED_PROCESS_CONDITION"),
    ("quality", "MEASUREMENT_QUALITY"), ("version", "PROCESS_VERSION_MISMATCH"),
    ("unit", "UNIT_MISMATCH"), ("plant", "BASELINE_SCOPE_MISMATCH"),
    ("overlap", "BASELINE_OVERLAP"), ("reuse", "BASELINE_GROUP_REUSED"),
])
def test_invalid_monitoring_is_never_stable_or_capable(normal, baseline, defect, code):
    raw = normal.model_dump(mode="python")
    group = raw["groups"][10]
    if defect == "missing":
        group["values"][0] = None
    elif defect == "n4":
        group["values"].pop()
    elif defect == "duplicate":
        group["group_id"] = raw["groups"][0]["group_id"]
    elif defect == "order":
        raw["groups"][0], raw["groups"][1] = raw["groups"][1], raw["groups"][0]
    elif defect == "time":
        group["timestamp_ms"] = raw["groups"][9]["timestamp_ms"]
    elif defect == "gap":
        group["timestamp_ms"] += 60_000
    elif defect == "mixed":
        group["sample_batch_ids"] = [group["batch_id"]] * 4 + ["different"]
    elif defect == "condition":
        group["sample_process_versions"] = [group["process_version"]] * 4 + ["different"]
    elif defect == "quality":
        group["measurement_status"] = "invalid"
    elif defect == "version":
        group["process_version"] = "new-version"
    elif defect == "unit":
        raw["unit"] = "um"
    elif defect == "plant":
        raw["plant_id"] = "other-plant"
    elif defect == "overlap":
        group["timestamp_ms"] = baseline["window_end_ms"]
    elif defect == "reuse":
        group["group_id"] = baseline["group_ids"][0]
    dataset = Dataset.model_validate(raw)
    result = analyze(dataset, baseline)
    assert result["status"] == "not_evaluable"
    assert code in {i["code"] for i in result["issues"]}
    assert result["capability"]["cpk"] is None
    assert result["capability"]["ppk"] is None
    assert [p["group_id"] for p in result["points"]] == [g.group_id for g in dataset.groups]


@pytest.mark.parametrize("defect,code", [
    ("failed", "MSA_FAILED"), ("unknown", "MSA_UNKNOWN"),
    ("study_id", "MSA_INCOMPLETE"), ("study_expired", "MSA_EXPIRED"),
    ("study_future", "MSA_NOT_YET_VALID"), ("cal_missing", "CALIBRATION_MISSING"),
    ("cal_invalid", "CALIBRATION_INVALID"), ("cal_expired", "CALIBRATION_EXPIRED"),
    ("cal_future", "CALIBRATION_NOT_YET_VALID"), ("cal_id", "CALIBRATION_INCOMPLETE"),
    ("cal_duplicate", "DUPLICATE_CALIBRATION"),
])
def test_measurement_study_and_calibration_gate_independently(normal, baseline, defect, code):
    raw = normal.model_dump(mode="python")
    first = raw["groups"][0]["timestamp_ms"]
    last = raw["groups"][-1]["timestamp_ms"]
    if defect in ("failed", "unknown"):
        raw["msa"]["status"] = defect
    elif defect == "study_id":
        raw["msa"]["study_id"] = None
    elif defect == "study_expired":
        raw["msa"]["valid_until_ms"] = first - 1
    elif defect == "study_future":
        raw["msa"]["valid_from_ms"] = last + 1
    elif defect == "cal_missing":
        raw["calibrations"] = []
    elif defect == "cal_invalid":
        raw["calibrations"][0]["status"] = "invalid"
    elif defect == "cal_expired":
        raw["calibrations"][0]["valid_until_ms"] = first - 1
    elif defect == "cal_future":
        raw["calibrations"][0]["valid_from_ms"] = last + 1
    elif defect == "cal_id":
        raw["calibrations"][0]["certificate_id"] = None
    else:
        raw["calibrations"].append(copy.deepcopy(raw["calibrations"][0]))
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["status"] == "not_evaluable"
    assert code in {i["code"] for i in result["issues"]}
    assert result["capability"]["status"] == "not_evaluable"
    assert result["capability"]["cpk"] is result["capability"]["ppk"] is None


def test_validity_interval_boundaries_are_inclusive(normal, baseline):
    raw = normal.model_dump(mode="python")
    for record in (raw["msa"], raw["calibrations"][0]):
        record["valid_from_ms"] = raw["groups"][0]["timestamp_ms"]
        record["valid_until_ms"] = raw["groups"][-1]["timestamp_ms"]
    assert analyze(Dataset.model_validate(raw), baseline)["status"] == "stable"


def test_missing_and_sampling_gaps_break_analysis_rule_windows(normal, baseline):
    raw = normal.model_dump(mode="python")
    for group in raw["groups"][:9]:
        target = baseline["center"] + 0.5 * baseline["sigma_xbar"]
        group["values"] = [target - 0.02, target - 0.01, target, target + 0.01, target + 0.02]
    raw["groups"][4]["values"][0] = None
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["points"][4]["z"] is None
    assert result["points"][4]["mean"] is None
    assert not any(h["start_index"] <= 4 <= h["end_index"] for h in result["rule_hits"])
    raw["groups"][4]["values"][0] = target - 0.02
    for group in raw["groups"][4:]:
        group["timestamp_ms"] += 60_000
    result = analyze(Dataset.model_validate(raw), baseline)
    assert not any(h["start_index"] < 4 <= h["end_index"] for h in result["rule_hits"])
    assert result["status"] == "not_evaluable"


def test_range_zero_boundary_is_not_a_range_signal(normal, baseline):
    raw = normal.model_dump(mode="python")
    raw["groups"][3]["values"] = [10.0] * 5
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["points"][3]["range"] == 0.0
    assert not any(hit["chart"] == "r" and hit["index"] == 3 for hit in result["rule_hits"])


def test_range_upper_boundary_is_strict_and_independent(normal, baseline):
    raw = normal.model_dump(mode="python")
    limit = baseline["r_ucl"]
    # Range exactly equals limit in binary; center may independently trigger Xbar.
    raw["groups"][3]["values"] = [0.0, 0.0, 0.0, 0.0, limit]
    result = analyze(Dataset.model_validate(raw), baseline)
    assert not any(h["chart"] == "r" and h["index"] == 3 for h in result["rule_hits"])
    raw["groups"][3]["values"][-1] = math.nextafter(limit, math.inf)
    result = analyze(Dataset.model_validate(raw), baseline)
    assert any(h["chart"] == "r" and h["index"] == 3 for h in result["rule_hits"])
    assert any(h["chart"] == "xbar" and h["index"] == 3 for h in result["rule_hits"])


def test_zero_variance_monitoring_keeps_descriptive_stats_and_no_capability(normal, baseline):
    raw = normal.model_dump(mode="python")
    for group in raw["groups"]:
        group["values"] = [10.0] * 5
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["status"] == "signals"
    assert any(hit["rule"] == "R7" for hit in result["rule_hits"])
    assert "ZERO_VARIATION" in result["capability"]["reasons"]
    assert result["capability"]["mean"] == 10.0
    assert result["capability"]["std_overall"] == 0.0
    assert result["capability"]["cpk"] is None


def test_analysis_hit_ids_windows_and_json_are_deterministic(baseline):
    dataset = demo_datasets()["mean_shift"]
    result = analyze(dataset, baseline)
    assert result == analyze(dataset, baseline)
    assert result["status"] == "signals"
    assert len({hit["id"] for hit in result["rule_hits"]}) == len(result["rule_hits"])
    for hit in result["rule_hits"]:
        selected = dataset.groups[hit["start_index"] : hit["end_index"] + 1]
        assert hit["group_ids"] == [group.group_id for group in selected]
        assert hit["start_group_id"] == selected[0].group_id
        assert hit["end_group_id"] == selected[-1].group_id
        assert hit["start_timestamp_ms"] == selected[0].timestamp_ms
        assert hit["end_timestamp_ms"] == selected[-1].timestamp_ms
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_nested_list_mutation_revalidated_and_invalid_baselines_are_domain_errors(normal, baseline):
    normal.groups[0].values[0] = math.nan
    with pytest.raises(DomainError) as error:
        analyze(normal, baseline)
    assert error.value.code == "INVALID_DATASET"
    for raw in ({}, dict(baseline, center=math.nan), dict(baseline, unexpected=True), dict(baseline, center="10.0")):
        with pytest.raises(DomainError):
            analyze(demo_datasets()["normal"], raw)


def test_public_baseline_validator_returns_frozen_model_without_mutating_input(baseline):
    from pydantic import ValidationError

    from spc_agent.domain import Baseline

    before = copy.deepcopy(baseline)
    model = validate_baseline(baseline)
    assert isinstance(model, Baseline)
    assert model.model_dump(mode="json") == before
    assert baseline == before
    with pytest.raises(ValidationError):
        model.center = 0.0


@pytest.mark.parametrize("payload", [None, [], "snapshot", {}, {"baseline_hash": "0" * 64}])
def test_public_baseline_validator_checks_schema_without_monitoring(payload):
    with pytest.raises(DomainError) as error:
        validate_baseline(payload)
    assert error.value.code == "INVALID_BASELINE"


def test_public_baseline_validator_rejects_stale_inner_hash_despite_rehashed_outer(baseline):
    altered = copy.deepcopy(baseline)
    altered["center"] += 0.1
    artifact = {"payload": altered}
    artifact["content_hash"] = hashlib.sha256(json.dumps(
        altered, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    assert artifact["content_hash"] != altered["baseline_hash"]
    with pytest.raises(DomainError) as error:
        validate_baseline(artifact["payload"])
    assert error.value.code == "BASELINE_HASH_MISMATCH"


def test_analyze_calls_shared_public_baseline_validator(normal, baseline, monkeypatch):
    import spc_agent.statistics as engine

    calls = []
    original = engine.validate_baseline

    def checked(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(engine, "validate_baseline", checked)
    assert engine.analyze(normal, baseline)["status"] == "stable"
    assert calls == [baseline]


@pytest.mark.parametrize("defect", ["sigma", "window", "study", "calibration", "unit"])
def test_even_rehashed_inconsistent_baseline_is_rejected(normal, baseline, defect):
    altered = copy.deepcopy(baseline)
    if defect == "sigma":
        altered["sigma_within"] *= 2
    elif defect == "window":
        altered["window_start_ms"] += 1
    elif defect == "study":
        altered["msa"]["valid_until_ms"] = altered["window_start_ms"]
    elif defect == "calibration":
        altered["calibrations"][0]["status"] = "invalid"
    else:
        altered["specifications"]["unit"] = "um"
    payload = {k: v for k, v in altered.items() if k != "baseline_hash"}
    altered["baseline_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(DomainError) as error:
        analyze(normal, altered)
    assert error.value.code == "INVALID_BASELINE"
    with pytest.raises(DomainError) as error:
        validate_baseline(altered)
    assert error.value.code == "INVALID_BASELINE"


def test_build_snapshot_includes_only_used_gauge_calibrations(normal):
    raw = baseline_dataset().model_dump(mode="python")
    raw["calibrations"].append({"gauge_id": "unused", "status": "unknown"})
    built = build_baseline(Dataset.model_validate(raw), "used-calibrations")
    assert len(built["calibrations"]) == 1
    assert analyze(normal, built)["status"] == "stable"


def test_unit_conversion_scales_chart_not_capability(normal, baseline):
    from spc_agent.statistics import convert_values

    def converted(dataset):
        raw = dataset.model_dump(mode="python")
        raw["unit"] = "um"
        for group in raw["groups"]:
            group["values"] = convert_values(group["values"], "mm", "um")
        raw["specifications"]["unit"] = "um"
        raw["specifications"]["lsl"] *= 1000.0
        raw["specifications"]["usl"] *= 1000.0
        return Dataset.model_validate(raw)

    converted_baseline = build_baseline(converted(baseline_dataset()), "um-baseline")
    converted_result = analyze(converted(normal), converted_baseline)
    result = analyze(normal, baseline)
    assert converted_result["status"] == result["status"] == "stable"
    for field in ("center", "rbar", "sigma_within", "sigma_xbar", "xbar_lcl", "xbar_ucl", "r_ucl"):
        assert converted_baseline[field] == pytest.approx(1000.0 * baseline[field], rel=1e-12)
    for field in ("cp", "cpk", "pp", "ppk"):
        assert converted_result["capability"][field] == pytest.approx(result["capability"][field], rel=1e-11)


def test_multiple_quality_issues_and_later_valid_signals_are_all_retained(normal, baseline):
    raw = normal.model_dump(mode="python")
    raw["groups"][0]["values"][0] = None
    raw["groups"][1]["measurement_status"] = "suspect"
    raw["groups"][2]["sample_batch_ids"] = ["wrong"] * 5
    raw["groups"][20]["values"] = [10.3] * 5
    result = analyze(Dataset.model_validate(raw), baseline)
    assert result["status"] == "not_evaluable"
    assert {"MISSING_MEASUREMENT", "MEASUREMENT_QUALITY", "MIXED_BATCH"} <= {item["code"] for item in result["issues"]}
    assert any(h["rule"] == "R1" and h["index"] == 20 for h in result["rule_hits"])
    assert result["capability"]["cpk"] is None


def test_tiny_and_extreme_values_fail_without_unhandled_numeric_errors(normal, baseline):
    for scale in (0.0, 1e-323, 1e-300, 1e-100, 1e10):
        raw = baseline_dataset().model_dump(mode="python")
        for group in raw["groups"]:
            group["values"] = [(v - 10.0) * scale for v in group["values"]]
        raw["specifications"] = None
        try:
            built = build_baseline(Dataset.model_validate(raw), "numeric-boundary")
        except DomainError as error:
            assert error.code in {"ZERO_VARIATION", "NUMERICAL_RANGE", "BASELINE_UNSTABLE"}
        else:
            result = analyze(normal, built)
            json.dumps(result, allow_nan=False)
            assert result["status"] in ("signals", "not_evaluable")
