"""Schema tests independent of statistical computation."""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from spc_agent.domain import MSA, Baseline, Calibration, Dataset, Specifications, Subgroup
from spc_agent.fixtures import baseline_dataset
from spc_agent.statistics import build_baseline, convert_values


def test_strict_dataset_and_unknown_fields():
    raw = baseline_dataset().model_dump(mode="python")
    for field, value in (("sampling_seconds", "60"), ("sampling_seconds", True),
                         ("sampling_seconds", 0), ("sampling_seconds", -1),
                         ("source", "production"), ("unit", "in"), ("plant_id", "")):
        changed = dict(raw, **{field: value})
        with pytest.raises(ValidationError):
            Dataset.model_validate(changed)
    with pytest.raises(ValidationError):
        Dataset.model_validate(dict(raw, scenario="normal"))


@pytest.mark.parametrize("value", ["a", "A0._-", "0" * 80])
def test_identifier_contract_accepts_schema_store_boundaries(value):
    from pydantic import TypeAdapter

    from spc_agent.domain import Identifier

    assert TypeAdapter(Identifier).validate_python(value, strict=True) == value


@pytest.mark.parametrize("value", [
    "", "a" * 81, "plant/line", "plant:line", "_leading", ".leading", "-leading",
    "white space", "with\\nnewline", "工厂", True, 123,
])
def test_identifier_contract_rejects_non_schema_store_ids(value):
    from pydantic import TypeAdapter

    from spc_agent.domain import Identifier

    with pytest.raises(ValidationError):
        TypeAdapter(Identifier).validate_python(value, strict=True)


@pytest.mark.parametrize("value", ["10.0", True, math.nan, math.inf, -math.inf, 1e13, -1e13])
def test_measurements_are_strict_finite_and_bounded(value):
    raw = baseline_dataset().groups[0].model_dump(mode="python")
    raw["values"][0] = value
    with pytest.raises(ValidationError):
        Subgroup.model_validate(raw)


def test_null_retained_and_original_order_preserved():
    raw = baseline_dataset().model_dump(mode="python")
    raw["groups"][0]["values"][1] = None
    raw["groups"][0], raw["groups"][1] = raw["groups"][1], raw["groups"][0]
    model = Dataset.model_validate(raw)
    assert model.groups[1].values[1] is None
    assert model.groups[0].timestamp_ms > model.groups[1].timestamp_ms


@pytest.mark.parametrize("values", [[], [1.0] * 51])
def test_subgroup_input_size_bounded(values):
    raw = baseline_dataset().groups[0].model_dump(mode="python")
    raw["values"] = values
    with pytest.raises(ValidationError):
        Subgroup.model_validate(raw)


def test_per_sample_provenance_exposes_mixed_batch_but_checks_length():
    raw = baseline_dataset().groups[0].model_dump(mode="python")
    raw["sample_batch_ids"] = [raw["batch_id"]] * 4 + ["other-batch"]
    assert Subgroup.model_validate(raw).sample_batch_ids[-1] == "other-batch"
    raw["sample_batch_ids"] = ["short"]
    with pytest.raises(ValidationError):
        Subgroup.model_validate(raw)


@pytest.mark.parametrize("lsl,usl", [(10.0, 10.0), (11.0, 10.0)])
def test_specification_order(lsl, usl):
    with pytest.raises(ValidationError):
        Specifications(lsl=lsl, usl=usl, unit="mm", revision="r1")


def test_study_and_calibration_are_independent_validated_intervals():
    with pytest.raises(ValidationError):
        MSA(status="passed", valid_from_ms=2, valid_until_ms=1)
    with pytest.raises(ValidationError):
        Calibration(gauge_id="g1", status="valid", valid_from_ms=2, valid_until_ms=1)
    dataset = baseline_dataset()
    assert dataset.msa.status == "passed"
    raw = dataset.model_dump(mode="python")
    raw["calibrations"][0]["status"] = "invalid"
    changed = Dataset.model_validate(raw)
    assert changed.msa.status == "passed"
    assert changed.calibrations[0].status == "invalid"


def test_baseline_is_deeply_immutable_and_json_roundtrips():
    import json

    raw = build_baseline(baseline_dataset(), "frozen-test")
    model = Baseline.model_validate_json(json.dumps(raw), strict=True)
    assert isinstance(model.group_ids, tuple)
    assert isinstance(model.calibrations, tuple)
    assert model.model_dump(mode="json") == raw
    with pytest.raises(ValidationError):
        model.center = 0.0
    with pytest.raises(ValidationError):
        model.msa.status = "failed"
    with pytest.raises(ValidationError):
        model.constants.d2 = 2.0


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("r_lcl", False), ("subgroup_size", 5.0), ("group_count", 25.0),
])
def test_baseline_numeric_constants_do_not_coerce_booleans_or_float_counts(field, value):
    import json

    raw = build_baseline(baseline_dataset(), "strict-constants")
    raw[field] = value
    with pytest.raises(ValidationError):
        Baseline.model_validate_json(json.dumps(raw), strict=True)


def test_constant_zero_does_not_accept_false():
    from spc_agent.domain import ControlConstants

    with pytest.raises(ValidationError):
        ControlConstants(d3=False)


def test_explicit_same_dimension_unit_conversion():
    original = [1.2, None, -0.004, 0.0]
    micrometres = convert_values(original, "mm", "um")
    assert micrometres == [1200.0, None, -4.0, 0.0]
    assert convert_values(micrometres, "um", "mm") == original
    assert convert_values(original, "mm", "mm") == original
    assert original == [1.2, None, -0.004, 0.0]


@pytest.mark.parametrize("values,source,target", [
    ([1.0], "mm", "s"), ([True], "mm", "um"), (["1"], "mm", "um"),
    ([math.nan], "mm", "um"), ([math.inf], "um", "mm"),
    ([1e12], "mm", "um"), ([10**1000], "mm", "um"),
])
def test_conversion_rejects_unknown_dimension_or_invalid_numbers(values, source, target):
    from spc_agent.errors import DomainError

    with pytest.raises(DomainError):
        convert_values(values, source, target)
