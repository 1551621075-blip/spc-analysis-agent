from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Note = Annotated[str, Field(min_length=10, max_length=1500)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Actor(StrictModel):
    actor_id: Identifier
    role: Literal["operator", "reviewer", "admin"]
    plants: list[Identifier] = Field(min_length=1, max_length=20)
    acl_version: int = Field(ge=1)

    @model_validator(mode="after")
    def unique_plants(self):
        if len(set(self.plants)) != len(self.plants):
            raise ValueError("Plants must be unique")
        return self


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class BaselineRequest(StrictModel):
    action_id: Identifier
    dataset_id: Identifier


class ReviewRequest(StrictModel):
    action_id: Identifier
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "rejected"]
    note: Note


class EvaluateRequest(StrictModel):
    action_id: Identifier
    dataset_id: Identifier
    baseline_id: Identifier


class ProposalRequest(StrictModel):
    action_id: Identifier
    risk_id: Identifier
    description: str = Field(min_length=10, max_length=1000)
    expected_result: str = Field(min_length=10, max_length=500)


class ExecuteRequest(StrictModel):
    action_id: Identifier
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at_ms: int = Field(ge=0, le=9007199254740991)
    note: Note
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Execution requires an explicit boolean confirmation")
        return value


class FollowupRequest(StrictModel):
    action_id: Identifier
    dataset_id: Identifier


class CloseRequest(StrictModel):
    action_id: Identifier
    followup_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: Note


class InvestigationRequest(StrictModel):
    action_id: Identifier
    risk_id: Identifier


class ReconcileRequest(StrictModel):
    action_id: Identifier
    call_id: Identifier
    no_result: Literal[True]
    tokens: int | None = Field(default=None, ge=0, le=100000)
    note: Note

    @field_validator("no_result", mode="before")
    @classmethod
    def explicit_attestation(cls, value):
        if value is not True:
            raise ValueError("Reconciliation requires an explicit boolean attestation")
        return value
