"""Strict, bounded input records and immutable Phase-I baseline snapshots."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Unit = Literal["mm", "um"]
Timestamp = Annotated[int, Field(ge=0, le=32_503_680_000_000)]
Measurement = Annotated[float, Field(ge=-1e12, le=1e12, allow_inf_nan=False)]
PositiveNumber = Annotated[float, Field(gt=0, le=1e15, allow_inf_nan=False)]
FiniteNumber = Annotated[float, Field(ge=-1e15, le=1e15, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class MSA(StrictModel):
    status: Literal["passed", "failed", "unknown"] = "unknown"
    study_id: Identifier | None = None
    valid_from_ms: Timestamp | None = None
    valid_until_ms: Timestamp | None = None

    @model_validator(mode="after")
    def ordered_validity(self) -> MSA:
        if self.valid_from_ms is not None and self.valid_until_ms is not None:
            if self.valid_from_ms > self.valid_until_ms:
                raise ValueError("MSA validity start must not exceed its end")
        return self


class Calibration(StrictModel):
    gauge_id: Identifier
    status: Literal["valid", "invalid", "unknown"] = "unknown"
    certificate_id: Identifier | None = None
    valid_from_ms: Timestamp | None = None
    valid_until_ms: Timestamp | None = None

    @model_validator(mode="after")
    def ordered_validity(self) -> Calibration:
        if self.valid_from_ms is not None and self.valid_until_ms is not None:
            if self.valid_from_ms > self.valid_until_ms:
                raise ValueError("Calibration validity start must not exceed its end")
        return self


class Specifications(StrictModel):
    lsl: Measurement
    usl: Measurement
    revision: Identifier
    unit: Unit

    @model_validator(mode="after")
    def ordered_limits(self) -> Specifications:
        if self.lsl >= self.usl:
            raise ValueError("Specification lsl must be strictly smaller than usl")
        return self


class Subgroup(StrictModel):
    group_id: Identifier
    timestamp_ms: Timestamp
    batch_id: Identifier
    gauge_id: Identifier
    process_version: Identifier
    values: list[Measurement | None] = Field(min_length=1, max_length=50)
    measurement_status: Literal["valid", "suspect", "invalid"] = "valid"
    # Optional per-observation provenance exposes, rather than hides, mixed subgroups.
    sample_batch_ids: list[Identifier] | None = Field(default=None, max_length=50)
    sample_process_versions: list[Identifier] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def matching_provenance(self) -> Subgroup:
        for values in (self.sample_batch_ids, self.sample_process_versions):
            if values is not None and len(values) != len(self.values):
                raise ValueError("Per-observation provenance must match values length")
        return self


class Dataset(StrictModel):
    dataset_id: Identifier
    plant_id: Identifier
    process_id: Identifier
    characteristic: Annotated[str, Field(min_length=1, max_length=160)]
    unit: Unit
    source: Literal["original_synthetic"] = "original_synthetic"
    sampling_seconds: Annotated[int, Field(gt=0, le=86_400)]
    msa: MSA
    calibrations: list[Calibration] = Field(max_length=100)
    specifications: Specifications | None = None
    groups: list[Subgroup] = Field(min_length=1, max_length=10_000)


class ControlConstants(StrictModel):
    d2: Literal[2.326] = 2.326
    d3: Literal[0.0] = 0.0
    d4: Literal[2.114] = 2.114

    @field_validator("d2", "d3", "d4", mode="before")
    @classmethod
    def reject_boolean_constants(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean values are not control constants")
        return value


class Baseline(StrictModel):
    schema_version: Literal[1] = 1
    baseline_id: Identifier
    baseline_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_dataset_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    plant_id: Identifier
    process_id: Identifier
    characteristic: Annotated[str, Field(min_length=1, max_length=160)]
    unit: Unit
    process_version: Identifier
    subgroup_size: Literal[5] = 5
    group_count: Literal[25] = 25
    sampling_seconds: Annotated[int, Field(gt=0, le=86_400)]
    window_start_ms: Timestamp
    window_end_ms: Timestamp
    group_ids: tuple[Identifier, ...] = Field(min_length=25, max_length=25)
    center: FiniteNumber
    rbar: PositiveNumber
    sigma_within: PositiveNumber
    sigma_xbar: PositiveNumber
    xbar_lcl: FiniteNumber
    xbar_ucl: FiniteNumber
    r_lcl: Literal[0.0] = 0.0
    r_ucl: PositiveNumber
    constants: ControlConstants = Field(default_factory=ControlConstants)
    specifications: Specifications | None = None
    msa: MSA
    calibrations: tuple[Calibration, ...] = Field(min_length=1, max_length=100)

    @field_validator("schema_version", "subgroup_size", "group_count", mode="before")
    @classmethod
    def strict_integer_constants(cls, value):
        if type(value) is not int:
            raise ValueError("Baseline counts and version must be strict integers")
        return value

    @field_validator("r_lcl", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean values are not baseline limits")
        return value
