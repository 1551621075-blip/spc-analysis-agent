"""Original, reproducible synthetic measurements; no stored statistical answers.

Scenario names are presentation metadata only. Neither Dataset nor the statistical engine
receives a scenario label. A synthetic gauge offset is not evidence of a real gauge cause.
"""
from __future__ import annotations

from copy import deepcopy

import numpy as np

from spc_agent.domain import Dataset

_START_MS = 1_767_225_600_000  # 2026-01-01 UTC
_PERIOD_MS = 60_000


def _dataset(seed: int, prefix: str, start_index: int) -> Dataset:
    measurements = np.random.default_rng(seed).normal(10.0, 0.02, size=(25, 5))
    return Dataset.model_validate({
        "dataset_id": prefix,
        "plant_id": "synthetic-plant",
        "process_id": "synthetic-bore-finish",
        "characteristic": "bore diameter",
        "unit": "mm",
        "source": "original_synthetic",
        "sampling_seconds": 60,
        "msa": {
            "status": "passed",
            "study_id": "synthetic-msa-01",
            "valid_from_ms": _START_MS - 86_400_000,
            "valid_until_ms": _START_MS + 365 * 86_400_000,
        },
        "calibrations": [{
            "gauge_id": "synthetic-gauge-01",
            "status": "valid",
            "certificate_id": "synthetic-cal-01",
            "valid_from_ms": _START_MS - 86_400_000,
            "valid_until_ms": _START_MS + 365 * 86_400_000,
        }],
        "specifications": {
            "lsl": 9.90, "usl": 10.10, "revision": "synthetic-spec-1", "unit": "mm",
        },
        "groups": [{
            "group_id": f"{prefix}-g{index + 1:03d}",
            "timestamp_ms": _START_MS + (start_index + index) * _PERIOD_MS,
            "batch_id": f"synthetic-batch-{1 + index // 10:02d}",
            "gauge_id": "synthetic-gauge-01",
            "process_version": "synthetic-process-v1",
            "values": row.tolist(),
        } for index, row in enumerate(measurements)],
    })


def baseline_dataset() -> Dataset:
    """Exactly 25 original Phase-I subgroups; seed fixed independently of monitoring."""
    return _dataset(271829, "synthetic-phase-one", 0)


def demo_datasets() -> dict[str, Dataset]:
    """Return independent monitoring datasets after the frozen Phase-I window."""
    normal = _dataset(314159, "synthetic-monitor", 25)
    raw = normal.model_dump(mode="python")
    scenarios = {"normal": normal}

    def changed(key: str, data: dict) -> None:
        # IDs intentionally do not encode the scenario label.
        serial = len(scenarios) + 1
        data["dataset_id"] = f"synthetic-monitor-{serial:02d}"
        for index, group in enumerate(data["groups"]):
            group["group_id"] = f"monitor-{serial:02d}-g{index + 1:03d}"
        scenarios[key] = Dataset.model_validate(data)

    data = deepcopy(raw)
    for group in data["groups"][8:]:
        group["values"] = [value + 0.065 for value in group["values"]]
    changed("mean_shift", data)

    data = deepcopy(raw)
    for group in data["groups"][8:]:
        mean = sum(group["values"]) / 5
        group["values"] = [mean + 3.5 * (value - mean) for value in group["values"]]
    changed("variance", data)

    data = deepcopy(raw)
    data["groups"][9]["sample_batch_ids"] = [data["groups"][9]["batch_id"]] * 4 + ["synthetic-other-batch"]
    data["groups"][9]["values"][-1] += 0.05
    changed("mixed_batch", data)

    data = deepcopy(raw)
    for group in data["groups"][10:]:
        group["values"] = [value + 0.055 for value in group["values"]]
    changed("gauge_shift", data)

    data = deepcopy(raw)
    data["groups"][6]["values"][2] = None
    data["groups"][16]["values"][0] = None
    changed("missing", data)

    data = deepcopy(raw)
    for index, group in enumerate(data["groups"]):
        previous_mean = sum(group["values"]) / 5
        target_mean = 9.977 + index * 0.002
        group["values"] = [value - previous_mean + target_mean for value in group["values"]]
    changed("trend", data)

    data = deepcopy(raw)
    data["msa"]["status"] = "failed"
    changed("msa_failed", data)

    data = deepcopy(raw)
    # Independent from MSA validity: expired before the first monitoring observation.
    data["calibrations"][0]["valid_until_ms"] = _START_MS + 24 * _PERIOD_MS
    changed("calibration_expired", data)

    # New random measurements, not a relabeled/re-timestamped monitoring sample. All 25
    # groups follow every earlier scenario and remain within the original MSA/calibration.
    scenarios["followup"] = _dataset(161803, "synthetic-monitor-10", 50)
    return scenarios
