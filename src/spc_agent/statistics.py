"""Auditable n=5 Xbar/R statistics. Specifications never determine chart limits."""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

import numpy as np
from pydantic import ValidationError

from spc_agent.domain import Baseline, Dataset, Unit
from spc_agent.errors import DomainError

N = 5
BASELINE_GROUPS = 25
D2 = 2.326
D4 = 2.114
MAX_GROUPS = 10_000
ASSUMPTIONS = [
    "Normality, independent sampling, and rational subgroups require human confirmation; "
    "absence of chart signals does not establish them.",
    "Capability indices are conditional estimates, not a conformance guarantee.",
    "A statistical signal does not identify a physical or gauge root cause.",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _checked_dataset(dataset: Dataset) -> Dataset:
    if not isinstance(dataset, Dataset):
        raise DomainError("INVALID_DATASET", "Expected a strict Dataset input")
    # Revalidate nested lists as well: frozen models prohibit assignment, not list mutation.
    try:
        return Dataset.model_validate(dataset.model_dump(mode="python"))
    except (ValidationError, ValueError, TypeError, OverflowError) as exc:
        raise DomainError("INVALID_DATASET", "Dataset violates the bounded input schema") from exc


def dataset_hash(dataset: Dataset) -> str:
    return _digest(_checked_dataset(dataset).model_dump(mode="json"))


def convert_values(
    values: list[float | None], from_unit: Unit, to_unit: Unit
) -> list[float | None]:
    """Explicit same-dimension conversion; neither charting API silently converts units."""
    if from_unit not in ("mm", "um") or to_unit not in ("mm", "um"):
        raise DomainError("UNSUPPORTED_UNIT", "Only length units mm and um are supported")
    if not isinstance(values, list) or len(values) > MAX_GROUPS * N:
        raise DomainError("INVALID_VALUES", "Expected a bounded list of measurements")
    factor = 1.0 if from_unit == to_unit else (1000.0 if from_unit == "mm" else 0.001)
    result: list[float | None] = []
    for value in values:
        if value is None:
            result.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DomainError("INVALID_VALUES", "Measurements must be finite numbers or null")
        if not -1e12 <= value <= 1e12 or not math.isfinite(value):
            raise DomainError("INVALID_VALUES", "Input measurement is outside the finite bounds")
        converted = float(value) * factor
        if not math.isfinite(converted) or not -1e12 <= converted <= 1e12:
            raise DomainError("CONVERSION_OUT_OF_RANGE", "Converted measurement exceeds input bounds")
        result.append(converted)
    return result


def nelson(z: list[float | None]) -> list[dict[str, Any]]:
    """All eight Nelson rules, with strict boundaries and null-separated windows.

    Every qualifying window is returned, including overlapping windows and simultaneous rules.
    Indices are zero-based positions in the supplied sequence, never in a sorted/filtered copy.
    """
    if not isinstance(z, list) or len(z) > MAX_GROUPS:
        raise DomainError("INVALID_Z", "Expected at most 10000 standardized mean points")
    for value in z:
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not -1e308 <= value <= 1e308
            or not math.isfinite(value)
        ):
            raise DomainError("INVALID_Z", "Standardized points must be finite numbers or null")
    hits: list[dict[str, Any]] = []
    run_start = 0
    sizes = (1, 9, 6, 14, 3, 5, 15, 8)
    for end, point in enumerate(z):
        if point is None:
            run_start = end + 1
            continue
        for rule, size in enumerate(sizes, 1):
            start = end - size + 1
            if start < run_start:
                continue
            window = z[start : end + 1]
            # run_start guarantees no null exists in this window.
            w = [float(value) for value in window if value is not None]
            side: str | None = None
            if rule == 1:
                if abs(w[0]) > 3:
                    side = "upper" if w[0] > 0 else "lower"
            elif rule == 2:
                if all(value > 0 for value in w):
                    side = "upper"
                elif all(value < 0 for value in w):
                    side = "lower"
            elif rule == 3:
                if all(b > a for a, b in zip(w, w[1:], strict=False)):
                    side = "ascending"
                elif all(b < a for a, b in zip(w, w[1:], strict=False)):
                    side = "descending"
            elif rule == 4:
                steps = [b > a for a, b in zip(w, w[1:], strict=False)]
                if all(b != a for a, b in zip(w, w[1:], strict=False)) and all(
                    a != b for a, b in zip(steps, steps[1:], strict=False)
                ):
                    side = "alternating"
            elif rule in (5, 6):
                threshold, required = (2, 2) if rule == 5 else (1, 4)
                if sum(value > threshold for value in w) >= required:
                    side = "upper"
                elif sum(value < -threshold for value in w) >= required:
                    side = "lower"
            elif rule == 7:
                if all(abs(value) < 1 for value in w):
                    side = "inside"
            elif rule == 8 and all(abs(value) > 1 for value in w):
                if any(value > 0 for value in w) and any(value < 0 for value in w):
                    side = "both"
            if side is not None:
                hit = {
                    "rule": f"R{rule}",
                    "start_index": start,
                    "end_index": end,
                    "index": end,
                    "window_size": size,
                    "window": w,
                    "side": side,
                }
                hit["id"] = _digest(hit)[:24]
                hits.append(hit)
    return hits


def _issue(code: str, message: str, index: int | None = None, group_id: str | None = None):
    result: dict[str, Any] = {"code": code, "message": message}
    if index is not None:
        result["index"] = index
    if group_id is not None:
        result["group_id"] = group_id
    return result


def _quality(dataset: Dataset):
    """Return all discoverable issues, invalid point indices, and continuity breaks."""
    issues: list[dict[str, Any]] = []
    invalid: set[int] = set()
    breaks: set[int] = set()
    groups = dataset.groups

    def add(code: str, message: str, index: int | None = None, block: bool = True):
        issues.append(_issue(code, message, index, groups[index].group_id if index is not None else None))
        if block:
            invalid.update(range(len(groups)) if index is None else [index])

    if dataset.specifications and dataset.specifications.unit != dataset.unit:
        add("SPECIFICATION_UNIT_MISMATCH", "Specification and measurement units must match")
    msa = dataset.msa
    if msa.status != "passed":
        add(f"MSA_{msa.status.upper()}", "A passed measurement-system study is required")
    if msa.study_id is None or msa.valid_from_ms is None or msa.valid_until_ms is None:
        add("MSA_INCOMPLETE", "MSA requires study ID and an explicit validity interval")
    calibrations = {}
    for calibration in dataset.calibrations:
        if calibration.gauge_id in calibrations:
            add("DUPLICATE_CALIBRATION", "Each gauge must have exactly one calibration record")
        calibrations[calibration.gauge_id] = calibration

    seen_ids: dict[str, int] = {}
    seen_times: set[int] = set()
    version = groups[0].process_version
    now_ms = time.time_ns() // 1_000_000
    for index, group in enumerate(groups):
        if group.group_id in seen_ids:
            add("DUPLICATE_GROUP_ID", "Subgroup identifiers must be unique", index)
            invalid.add(seen_ids[group.group_id])
        seen_ids[group.group_id] = index
        if group.timestamp_ms in seen_times:
            add("DUPLICATE_TIMESTAMP", "Subgroup timestamps must be unique", index)
        seen_times.add(group.timestamp_ms)
        if group.timestamp_ms > now_ms:
            add("FUTURE_TIMESTAMP", "Future observations cannot be used for SPC", index)
        if index:
            delta = group.timestamp_ms - groups[index - 1].timestamp_ms
            if delta != dataset.sampling_seconds * 1000:
                breaks.add(index)
                code = "OUT_OF_ORDER" if delta <= 0 else "SAMPLING_GAP"
                add(code, "Subgroups must remain in original, continuous sampling order", index, False)
        if len(group.values) != N:
            add("SUBGROUP_SIZE", "Xbar/R evaluation requires exactly five observations per subgroup", index)
        if any(value is None for value in group.values):
            add("MISSING_MEASUREMENT", "Missing observations are retained; no imputation is performed", index)
        if group.measurement_status != "valid":
            add("MEASUREMENT_QUALITY", "Subgroup measurement quality is not valid", index)
        if group.sample_batch_ids is not None and any(
            batch != group.batch_id for batch in group.sample_batch_ids
        ):
            add("MIXED_BATCH", "A rational subgroup must contain one declared batch", index)
        if group.sample_process_versions is not None and any(
            value != group.process_version for value in group.sample_process_versions
        ):
            add("MIXED_PROCESS_CONDITION", "A subgroup must contain one declared process condition", index)
        if group.process_version != version:
            add("PROCESS_VERSION_CHANGE", "A dataset must use one process version", index)
        if msa.valid_from_ms is not None and group.timestamp_ms < msa.valid_from_ms:
            add("MSA_NOT_YET_VALID", "The measurement-system study is not yet valid", index)
        if msa.valid_until_ms is not None and group.timestamp_ms > msa.valid_until_ms:
            add("MSA_EXPIRED", "The measurement-system study has expired", index)
        calibration = calibrations.get(group.gauge_id)
        if calibration is None:
            add("CALIBRATION_MISSING", "No calibration record exists for the subgroup gauge", index)
        else:
            if calibration.status != "valid":
                add("CALIBRATION_INVALID", "Gauge calibration is not valid", index)
            if (
                calibration.certificate_id is None
                or calibration.valid_from_ms is None
                or calibration.valid_until_ms is None
            ):
                add("CALIBRATION_INCOMPLETE", "Calibration requires certificate and validity interval", index)
            if calibration.valid_from_ms is not None and group.timestamp_ms < calibration.valid_from_ms:
                add("CALIBRATION_NOT_YET_VALID", "Gauge calibration is not yet valid", index)
            if calibration.valid_until_ms is not None and group.timestamp_ms > calibration.valid_until_ms:
                add("CALIBRATION_EXPIRED", "Gauge calibration has expired", index)
    return issues, invalid, breaks


def _summaries(dataset: Dataset) -> list[dict[str, Any]]:
    points = []
    for index, group in enumerate(dataset.groups):
        complete = len(group.values) == N and all(value is not None for value in group.values)
        values = [value for value in group.values if value is not None]
        points.append({
            "index": index,
            "group_id": group.group_id,
            "timestamp_ms": group.timestamp_ms,
            "batch_id": group.batch_id,
            "gauge_id": group.gauge_id,
            "mean": math.fsum(values) / N if complete else None,
            "range": max(values) - min(values) if complete else None,
            "z": None,
            "valid": complete,
        })
    return points


def _chart_hits(points: list[dict[str, Any]], breaks: set[int], r_lcl: float, r_ucl: float):
    hits = []
    boundaries = [0, *sorted(breaks), len(points)]
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        for hit in nelson([point["z"] for point in points[left:right]]):
            hit["start_index"] += left
            hit["end_index"] += left
            hit["index"] += left
            hit["chart"] = "xbar"
            hits.append(hit)
    for point in points:
        if point["valid"] and point["range"] is not None:
            if point["range"] < r_lcl or point["range"] > r_ucl:
                hits.append({
                    "chart": "r",
                    "rule": "R_LIMIT",
                    "start_index": point["index"],
                    "end_index": point["index"],
                    "index": point["index"],
                    "window_size": 1,
                    "window": [point["range"]],
                    "side": "upper" if point["range"] > r_ucl else "lower",
                })
    for hit in hits:
        selected = points[hit["start_index"] : hit["end_index"] + 1]
        hit["group_ids"] = [point["group_id"] for point in selected]
        hit["start_group_id"] = selected[0]["group_id"]
        hit["end_group_id"] = selected[-1]["group_id"]
        hit["start_timestamp_ms"] = selected[0]["timestamp_ms"]
        hit["end_timestamp_ms"] = selected[-1]["timestamp_ms"]
        hit.pop("id", None)
        hit["id"] = _digest(hit)[:24]
    return sorted(hits, key=lambda hit: (hit["end_index"], hit["chart"], hit["rule"]))


def build_baseline(dataset: Dataset, baseline_id: str) -> dict[str, Any]:
    """Freeze exactly 25 preselected Phase-I groups; never slice or fit monitoring data."""
    dataset = _checked_dataset(dataset)
    if len(dataset.groups) != BASELINE_GROUPS:
        raise DomainError("BASELINE_GROUP_COUNT", "A baseline requires exactly 25 subgroups of five")
    issues, _, breaks = _quality(dataset)
    if issues:
        codes = ", ".join(dict.fromkeys(issue["code"] for issue in issues))
        raise DomainError("BASELINE_NOT_EVALUABLE", f"Baseline data quality failed: {codes}")
    points = _summaries(dataset)
    center = math.fsum(point["mean"] for point in points) / BASELINE_GROUPS
    rbar = math.fsum(point["range"] for point in points) / BASELINE_GROUPS
    sigma_within = rbar / D2
    sigma_xbar = sigma_within / math.sqrt(N)
    if sigma_within <= 0 or sigma_xbar <= 0:
        raise DomainError("ZERO_VARIATION", "A nonzero, numerically representable within sigma is required")
    xbar_lcl, xbar_ucl = center - 3 * sigma_xbar, center + 3 * sigma_xbar
    if not xbar_lcl < center < xbar_ucl:
        raise DomainError("NUMERICAL_RANGE", "Control limits are below floating-point resolution")
    for point in points:
        point["z"] = (point["mean"] - center) / sigma_xbar
        if not math.isfinite(point["z"]) or abs(point["z"]) > 1e308:
            raise DomainError("NUMERICAL_RANGE", "Standardized baseline mean is outside numeric range")
    hits = _chart_hits(points, breaks, 0.0, D4 * rbar)
    if hits:
        rules = ", ".join(dict.fromkeys(f"{hit['chart']}:{hit['rule']}" for hit in hits))
        raise DomainError("BASELINE_UNSTABLE", f"Phase-I baseline has statistical signals: {rules}")
    payload = {
        "schema_version": 1,
        "baseline_id": baseline_id,
        "baseline_hash": "0" * 64,
        "source_dataset_hash": dataset_hash(dataset),
        "plant_id": dataset.plant_id,
        "process_id": dataset.process_id,
        "characteristic": dataset.characteristic,
        "unit": dataset.unit,
        "process_version": dataset.groups[0].process_version,
        "subgroup_size": N,
        "group_count": BASELINE_GROUPS,
        "sampling_seconds": dataset.sampling_seconds,
        "window_start_ms": dataset.groups[0].timestamp_ms,
        "window_end_ms": dataset.groups[-1].timestamp_ms,
        "group_ids": tuple(group.group_id for group in dataset.groups),
        "center": center,
        "rbar": rbar,
        "sigma_within": sigma_within,
        "sigma_xbar": sigma_xbar,
        "xbar_lcl": xbar_lcl,
        "xbar_ucl": xbar_ucl,
        "r_lcl": 0.0,
        "r_ucl": D4 * rbar,
        "constants": {"d2": D2, "d3": 0.0, "d4": D4},
        "specifications": dataset.specifications,
        "msa": dataset.msa,
        "calibrations": tuple(
            calibration for calibration in dataset.calibrations
            if calibration.gauge_id in {group.gauge_id for group in dataset.groups}
        ),
    }
    try:
        result = Baseline.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise DomainError("INVALID_BASELINE", "Baseline ID or derived numbers violate the schema") from exc
    result["baseline_hash"] = _digest({key: value for key, value in result.items() if key != "baseline_hash"})
    return result


def validate_baseline(baseline: dict[str, Any]) -> Baseline:
    """Validate a complete inner snapshot and return its immutable typed representation.

    Checks strict schema, original payload hash, fixed n=5 formulas, observation window,
    and measurement-system validity without needing monitoring data or mutating input.
    Raise DomainError on any failure. Service callers must separately verify the outer
    artifact content hash and its exact review binding; neither is part of this snapshot.
    """
    if not isinstance(baseline, dict):
        raise DomainError("INVALID_BASELINE", "Expected a JSON baseline snapshot")
    try:
        # JSON validation permits JSON arrays for immutable tuple fields, while remaining strict.
        model = Baseline.model_validate_json(_canonical(baseline), strict=True)
    except (ValidationError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise DomainError("INVALID_BASELINE", "Baseline violates the strict snapshot schema") from exc
    payload = model.model_dump(mode="json")
    if set(baseline) != set(payload):
        raise DomainError("INVALID_BASELINE", "A frozen snapshot must contain every serialized field")
    # Hash the supplied snapshot, not schema-normalized defaults/coercions: deleting a default
    # or changing a JSON number representation must not silently preserve its original digest.
    expected_hash = _digest({key: value for key, value in baseline.items() if key != "baseline_hash"})
    if expected_hash != model.baseline_hash:
        raise DomainError("BASELINE_HASH_MISMATCH", "Baseline snapshot changed after it was frozen")
    formulas = (
        (model.sigma_within, model.rbar / D2),
        (model.sigma_xbar, model.sigma_within / math.sqrt(N)),
        (model.xbar_lcl, model.center - 3 * model.sigma_xbar),
        (model.xbar_ucl, model.center + 3 * model.sigma_xbar),
        (model.r_ucl, D4 * model.rbar),
    )
    msa = model.msa
    baseline_measurement_valid = (
        msa.status == "passed"
        and msa.study_id is not None
        and msa.valid_from_ms is not None
        and msa.valid_until_ms is not None
        and msa.valid_from_ms <= model.window_start_ms <= model.window_end_ms <= msa.valid_until_ms
    )
    calibration_ids = [cal.gauge_id for cal in model.calibrations]
    baseline_calibrations_valid = (
        len(set(calibration_ids)) == len(calibration_ids)
        and all(
            cal.status == "valid"
            and cal.certificate_id is not None
            and cal.valid_from_ms is not None
            and cal.valid_until_ms is not None
            # Individual gauge-use times are covered by source_dataset_hash, not duplicated here.
            and cal.valid_from_ms <= model.window_end_ms
            and cal.valid_until_ms >= model.window_start_ms
            for cal in model.calibrations
        )
    )
    if (
        any(not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0.0) for actual, expected in formulas)
        or not model.xbar_lcl < model.center < model.xbar_ucl
        or model.window_end_ms - model.window_start_ms != 24 * model.sampling_seconds * 1000
        or model.window_end_ms > time.time_ns() // 1_000_000
        or len(set(model.group_ids)) != 25
        or not baseline_measurement_valid
        or not baseline_calibrations_valid
        or (model.specifications is not None and model.specifications.unit != model.unit)
    ):
        raise DomainError("INVALID_BASELINE", "Baseline constants, window, or measurement status are inconsistent")
    return model


def _capability(dataset: Dataset, points: list[dict[str, Any]], status: str):
    raw = [value for group in dataset.groups for value in group.values if value is not None]
    n_values = len(raw)
    n_groups = sum(point["valid"] for point in points)
    mean = math.fsum(raw) / n_values if n_values else None
    # Ppk uses all available individual measurements and the sample (ddof=1) standard deviation.
    std_overall = float(np.std(np.asarray(raw, dtype=np.float64), ddof=1)) if n_values > 1 else None
    ranges = [point["range"] for point in points if point["valid"]]
    sigma_within = math.fsum(ranges) / len(ranges) / D2 if ranges else None
    reasons = []
    if status != "stable":
        reasons.append("PROCESS_UNSTABLE" if status == "signals" else "DATA_NOT_EVALUABLE")
    if n_groups < 25 or n_values < 125:
        reasons.append("INSUFFICIENT_DATA")
    specs = dataset.specifications
    if specs is None:
        reasons.append("SPECIFICATIONS_MISSING")
    elif specs.unit != dataset.unit:
        reasons.append("SPECIFICATION_UNIT_MISMATCH")
    if std_overall is None or std_overall <= 0 or sigma_within is None or sigma_within <= 0:
        reasons.append("ZERO_VARIATION")
    result = {
        "status": "not_evaluable",
        "reasons": reasons,
        "cp": None,
        "cpk": None,
        "pp": None,
        "ppk": None,
        "mean": mean,
        "std_overall": std_overall,
        "sigma_within": sigma_within,
        "n_values": n_values,
        "n_groups": n_groups,
        "sigma_within_method": "monitoring_mean_range / 2.326",
        "specifications": specs.model_dump(mode="json") if specs else None,
        "assumptions": list(ASSUMPTIONS),
    }
    if not reasons:
        width = specs.usl - specs.lsl
        margin = min(specs.usl - mean, mean - specs.lsl)
        values = {
            "cp": width / (6 * sigma_within),
            "cpk": margin / (3 * sigma_within),
            "pp": width / (6 * std_overall),
            "ppk": margin / (3 * std_overall),
        }
        if all(math.isfinite(value) for value in values.values()):
            result.update(values)
            result["status"] = "available"
        else:
            reasons.append("NUMERICAL_RANGE")
    return result


def analyze(dataset: Dataset, baseline: dict[str, Any]) -> dict[str, Any]:
    """Evaluate monitoring groups against an unchanged, independently fitted baseline."""
    dataset = _checked_dataset(dataset)
    frozen = validate_baseline(baseline)
    issues, invalid, breaks = _quality(dataset)
    for field in ("plant_id", "process_id", "characteristic", "unit", "sampling_seconds"):
        if getattr(dataset, field) != getattr(frozen, field):
            code = "UNIT_MISMATCH" if field == "unit" else "BASELINE_SCOPE_MISMATCH"
            issues.append(_issue(code, f"Monitoring {field} must match the frozen baseline"))
            invalid.update(range(len(dataset.groups)))
    baseline_ids = set(frozen.group_ids)
    for index, group in enumerate(dataset.groups):
        if group.process_version != frozen.process_version:
            issues.append(_issue("PROCESS_VERSION_MISMATCH", "Process version differs from baseline", index, group.group_id))
            invalid.add(index)
        if group.timestamp_ms <= frozen.window_end_ms:
            issues.append(_issue("BASELINE_OVERLAP", "Monitoring observations must follow the baseline window", index, group.group_id))
            invalid.add(index)
        if group.group_id in baseline_ids:
            issues.append(_issue("BASELINE_GROUP_REUSED", "A baseline subgroup cannot be reused for monitoring", index, group.group_id))
            invalid.add(index)
    points = _summaries(dataset)
    for index, point in enumerate(points):
        point["valid"] = point["valid"] and index not in invalid
        if point["valid"]:
            z = (point["mean"] - frozen.center) / frozen.sigma_xbar
            if not math.isfinite(z) or abs(z) > 1e308:
                issues.append(_issue("NUMERICAL_RANGE", "Standardized mean is outside numeric range", index, point["group_id"]))
                point["valid"] = False
            else:
                point["z"] = z
    hits = _chart_hits(points, breaks, frozen.r_lcl, frozen.r_ucl)
    input_hash = dataset_hash(dataset)
    for hit in hits:
        hit["id"] = _digest({
            "baseline_hash": frozen.baseline_hash,
            "dataset_hash": input_hash,
            "hit": hit,
        })[:24]
    status = "not_evaluable" if issues else ("signals" if hits else "stable")
    capability = _capability(dataset, points, status)
    if "NUMERICAL_RANGE" in capability["reasons"]:
        issues.append(_issue("NUMERICAL_RANGE", "Capability calculation is outside finite numeric range"))
        status = "not_evaluable"
    return {
        "status": status,
        "issues": issues,
        "capability": capability,
        "rule_hits": hits,
        "points": points,
        "baseline_id": frozen.baseline_id,
        "baseline_hash": frozen.baseline_hash,
        "dataset_hash": input_hash,
        "dataset_id": dataset.dataset_id,
        "control_limits": {
            "center": frozen.center,
            "xbar_lcl": frozen.xbar_lcl,
            "xbar_ucl": frozen.xbar_ucl,
            "rbar": frozen.rbar,
            "r_lcl": frozen.r_lcl,
            "r_ucl": frozen.r_ucl,
            "sigma_within": frozen.sigma_within,
            "sigma_xbar": frozen.sigma_xbar,
            "unit": frozen.unit,
        },
        "counts": {"groups": len(points), "valid_groups": sum(point["valid"] for point in points)},
        "assumptions": list(ASSUMPTIONS),
    }
