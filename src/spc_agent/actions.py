"""Owner-scoped, evidence-bound SPC actions; execution is an attestation only."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError

from .domain import Dataset
from .errors import DomainError
from .schemas import (
    Actor,
    CloseRequest,
    ExecuteRequest,
    FollowupRequest,
    ProposalRequest,
    ReviewRequest,
    canonical,
    digest,
)
from .statistics import dataset_hash

FOLLOWUP_EXPLANATION = "本次合成观察窗满足已声明稳定性条件，不证明措施因果有效/持续稳定"
REVIEW_POLICY = "owner-only demo; owner reviewer/admin may self-review; no two-person approval claim"
_REVIEW_ROLES = ("reviewer", "admin")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _request(model, value):
    # Revalidate model instances too: callers can mutate a previously validated model.
    if isinstance(value, model):
        value = value.model_dump(mode="python")
    if type(value) is not dict:
        raise DomainError("invalid_request", "Expected a strict request object", 422)
    if model is ExecuteRequest and type(value.get("confirmed")) is not bool:
        raise DomainError("invalid_request", "Execution requires an explicit true attestation", 422)
    try:
        result = model.model_validate(value, strict=True)
    except ValidationError:
        raise DomainError("invalid_request", "Request violates the strict action schema", 422) from None
    for name in ("note", "description", "expected_result"):
        text = getattr(result, name, None)
        if text is not None and not text.strip():
            raise DomainError("invalid_request", "Action text must not be blank", 422)
    return result


def _dataset(raw) -> Dataset:
    try:
        return Dataset.model_validate(raw, strict=True)
    except (ValidationError, TypeError, ValueError):
        raise DomainError("invalid_dataset", "Expected a strict synthetic dataset", 422) from None


def _window(dataset: Dataset) -> dict:
    times = [group.timestamp_ms for group in dataset.groups]
    return {"start_ms": min(times), "end_ms": max(times)}


def _evidence_hash(dataset: Dataset) -> str:
    raw = dataset.model_dump(mode="json")
    raw.pop("dataset_id")
    return digest(raw)


def _measurements_hash(dataset: Dataset) -> str:
    # IDs and timestamps are not proof of new measurements. Reject an exact copied
    # observation sequence even when every label and timestamp has been changed.
    return digest([group.values for group in dataset.groups])


def _scope(dataset: Dataset) -> dict:
    return {
        **{key: getattr(dataset, key) for key in
           ("plant_id", "process_id", "characteristic", "unit", "sampling_seconds")},
        "process_versions": sorted({group.process_version for group in dataset.groups}),
        "gauge_ids": sorted({group.gauge_id for group in dataset.groups}),
    }


class ActionService:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    @staticmethod
    def _envelope(method, request, proposal_id=None):
        return {"method": "actions." + method, "proposal_id": proposal_id,
                "request": request.model_dump(mode="json")}

    def _artifact(self, reference, kind):
        if type(reference) is not dict or reference.get("kind") != kind:
            raise DomainError("artifact_integrity", "Unexpected action artifact kind", 409)
        return self.store.read_artifact(reference)

    def _verify(self, state):
        """Never trust mutable state_json in place of the frozen, hashed artifacts."""
        try:
            payload = self._artifact(state["payload_ref"], "action_proposal")
            if payload != state["payload"] or digest(payload) != state["content_hash"]:
                raise ValueError
            if any(state[key] != payload[key] for key in ("proposal_id", "owner", "plant_id", "risk_id")):
                raise ValueError
            risk = self._artifact(payload["risk_snapshot_ref"], "action_risk")
            evaluation = self._artifact(payload["evaluation_ref"], "action_evaluation")
            baseline = self._artifact(payload["baseline_ref"], "action_baseline")
            original_raw = self._artifact(payload["dataset_ref"], "action_dataset")
            original = _dataset(original_raw)
            ref = payload["risk_ref"]
            if (digest(evaluation) != payload["evaluation_snapshot_hash"]
                    or evaluation["content_hash"] != ref["evaluation_hash"]
                    or digest({key: value for key, value in evaluation.items() if key != "content_hash"}) != ref["evaluation_hash"]
                    or evaluation["dataset"] != original_raw
                    or any(evaluation[key] != ref[key] for key in ("evaluation_id", "dataset_id", "baseline_id"))
                    or digest(original_raw) != ref["data_hash"]
                    or _evidence_hash(original) != payload["evidence_hash"]
                    or _measurements_hash(original) != payload["measurements_hash"]
                    or _window(original) != payload["original_window"]
                    or _scope(original) != payload["scope"]
                    or original.dataset_id != ref["dataset_id"]
                    or baseline["baseline_hash"] != ref["baseline_hash"]
                    or baseline["baseline_id"] != ref["baseline_id"]
                    or any(risk[key] != ref[key] for key in
                           ("risk_id", "evaluation_id", "baseline_id", "dataset_id", "data_hash", "baseline_hash"))):
                raise ValueError
            review = state["review"]
            execution = state["execution"]
            if review is not None:
                if (self._artifact(review["artifact_ref"], "action_review") != review["payload"]
                        or digest(review["payload"]) != review["content_hash"]
                        or review["payload"]["proposal_hash"] != state["content_hash"]
                        or review["payload"]["actor_id"] != state["owner"]
                        or review["payload"]["role"] not in _REVIEW_ROLES):
                    raise ValueError
            if execution is not None:
                if (self._artifact(execution["artifact_ref"], "action_execution") != execution["payload"]
                        or digest(execution["payload"]) != execution["content_hash"]
                        or execution["payload"]["proposal_hash"] != state["content_hash"]
                        or execution["payload"]["actor_id"] != state["owner"]
                        or execution["payload"]["confirmed"] is not True
                        or execution["payload"]["observed_at_ms"] > _now_ms()):
                    raise ValueError
            followups = state["followups"]
            for followup in followups:
                evidence = self._artifact(followup["artifact_ref"], "action_followup")
                if (evidence != followup["payload"] or digest(evidence) != followup["followup_hash"]
                        or evidence["proposal_hash"] != state["content_hash"]
                        or execution is None
                        or evidence["execution_hash"] != execution["content_hash"]
                        or evidence["baseline_hash"] != ref["baseline_hash"]):
                    raise ValueError
                followup_raw = self._artifact(evidence["dataset_ref"], "action_dataset")
                raw = _dataset(followup_raw)
                analysis = self._artifact(evidence["analysis_ref"], "action_analysis")
                if (analysis != evidence["analysis"] or digest(analysis) != evidence["analysis_hash"]
                        or digest(followup_raw) != evidence["data_hash"]
                        or dataset_hash(raw) != analysis["dataset_hash"]
                        or raw.dataset_id != evidence["dataset_id"]
                        or evidence["window"] != _window(raw)):
                    raise ValueError
            closure = state["closure"]
            if closure is not None:
                if (self._artifact(closure["artifact_ref"], "action_closure") != closure["payload"]
                        or digest(closure["payload"]) != closure["content_hash"]
                        or closure["payload"]["proposal_hash"] != state["content_hash"]
                        or closure["payload"]["actor_id"] != state["owner"]
                        or closure["payload"]["role"] not in _REVIEW_ROLES
                        or not followups
                        or closure["payload"]["followup_hash"] != followups[-1]["followup_hash"]):
                    raise ValueError
            status = state["status"]
            if status == "proposed":
                valid = review is None and execution is None and not followups and closure is None
            elif status == "rejected":
                valid = (review is not None and review["payload"]["decision"] == "rejected"
                         and execution is None and not followups and closure is None)
            elif status in ("approved", "executed", "followup_recorded", "closed"):
                valid = review is not None and review["payload"]["decision"] == "approved"
                if status == "approved":
                    valid = valid and execution is None and not followups and closure is None
                else:
                    valid = valid and execution is not None
                    eligible = bool(followups and followups[-1]["payload"]["eligible"])
                    valid = valid and (eligible == (status in ("followup_recorded", "closed")))
                    valid = valid and ((closure is not None) == (status == "closed"))
            else:
                valid = False
            expected_version = 1 + int(review is not None) + int(execution is not None) + len(followups) + int(closure is not None)
            if not valid or type(state["version"]) is not int or state["version"] != expected_version:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise DomainError("artifact_integrity", "Action evidence or state was modified", 409) from None
        return state

    def _load(self, db, proposal_id, actor, roles=None):
        actor = self.store.current_actor(actor, db)
        if not isinstance(proposal_id, str):
            raise DomainError("not_found", "Proposal not found", 404)
        row = db.execute("SELECT * FROM proposals WHERE proposal_id=? AND owner=?",
                         (proposal_id, actor.actor_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Proposal not found", 404)
        self.store.authorize(actor, row["plant_id"], roles=roles, db=db)
        try:
            state = json.loads(row["state_json"])
            if (type(state) is not dict or state["version"] != row["version"]
                    or any(state[key] != row[key] for key in ("proposal_id", "owner", "plant_id", "risk_id"))):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DomainError("artifact_integrity", "Invalid proposal state", 409) from None
        return self._verify(state)

    def _receipt(self, db, actor, request, envelope):
        result = self.store.receipt(db, actor, request.action_id, envelope)
        if result is not None:
            self._verify(result)
            if (result["owner"] != actor.actor_id
                    or (envelope["proposal_id"] is not None
                        and result["proposal_id"] != envelope["proposal_id"])
                    or (isinstance(request, ProposalRequest) and result["risk_id"] != request.risk_id)):
                raise DomainError("receipt_integrity", "Receipt belongs to different action evidence", 409)
        return result

    def _record(self, kind, payload):
        return {"payload": payload, "content_hash": digest(payload),
                "artifact_ref": self.store.write_artifact(kind, payload)}

    def _save(self, db, state, actor, request, envelope, event):
        previous = state["version"]
        state["version"] += 1
        updated = db.execute(
            "UPDATE proposals SET state_json=?,version=? WHERE proposal_id=? AND owner=? AND version=?",
            (canonical(state), state["version"], state["proposal_id"], actor.actor_id, previous))
        if updated.rowcount != 1:
            raise DomainError("version_conflict", "Proposal changed; reload before acting", 409)
        self.store.audit(db, actor.actor_id, event,
                         {"action_id": request.action_id, "proposal_hash": state["content_hash"],
                          "version": state["version"], "status": state["status"]}, state["proposal_id"])
        self.store.save_receipt(db, actor, request.action_id, envelope, state)
        return deepcopy(state)

    @staticmethod
    def _require(state, status):
        if state["status"] != status:
            raise DomainError("invalid_transition", "Action is not in the required lifecycle state", 409)

    def _baseline(self, payload, actor, db=None):
        baseline = self.service.get_baseline(payload["risk_ref"]["baseline_id"], actor, db=db)
        frozen = self._artifact(payload["baseline_ref"], "action_baseline")
        if baseline["status"] != "approved" or baseline["payload"] != frozen:
            raise DomainError("baseline_changed", "The originally approved baseline is required", 409)
        return baseline

    def propose(self, request, actor: Actor):
        request = _request(ProposalRequest, request)
        envelope = self._envelope("propose", request)
        with self.store.connect() as db:
            actor = self.store.current_actor(actor, db)
            risk = self.service.get_risk(request.risk_id, actor, db=db)
            self.store.authorize(actor, risk["plant_id"], db=db)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                self._load(db, replay["proposal_id"], actor)
                return replay
            evaluation = self.service.get_evaluation(risk["evaluation_id"], actor, db=db)
            baseline = self.service.get_baseline(risk["baseline_id"], actor, db=db)
            raw = self.store.dataset(risk["dataset_id"], actor, db=db)
        dataset = _dataset(raw)
        if (baseline["status"] != "approved" or dataset.plant_id != risk["plant_id"]
                or dataset.process_id != risk["process_id"]
                or evaluation["dataset"] != raw
                or any(evaluation[key] != risk[key] for key in ("evaluation_id", "dataset_id", "baseline_id"))
                or digest({key: value for key, value in evaluation.items() if key != "content_hash"}) != evaluation["content_hash"]
                or digest(raw) != risk["data_hash"]
                or baseline["payload"]["baseline_hash"] != risk["baseline_hash"]):
            raise DomainError("evidence_mismatch", "Risk evidence or approved baseline does not match", 409)
        if _window(dataset)["end_ms"] > _now_ms():
            raise DomainError("future_observation", "Risk observations must not be in the future", 422)
        proposal_id = "proposal-" + uuid4().hex
        payload = {
            "proposal_id": proposal_id, "owner": actor.actor_id, "plant_id": dataset.plant_id,
            "risk_id": request.risk_id, "description": request.description,
            "expected_result": request.expected_result, "source": "original_synthetic",
            "created_at_ms": _now_ms(), "review_policy": REVIEW_POLICY,
            "risk_ref": {**{key: risk[key] for key in
                              ("risk_id", "evaluation_id", "baseline_id", "dataset_id", "data_hash", "baseline_hash")},
                         "evaluation_hash": evaluation["content_hash"]},
            "evaluation_snapshot_hash": digest(evaluation),
            "risk_snapshot_ref": self.store.write_artifact("action_risk", risk),
            "evaluation_ref": self.store.write_artifact("action_evaluation", evaluation),
            "baseline_ref": self.store.write_artifact("action_baseline", baseline["payload"]),
            "dataset_ref": self.store.write_artifact("action_dataset", raw),
            "original_window": _window(dataset), "scope": _scope(dataset),
            "evidence_hash": _evidence_hash(dataset), "measurements_hash": _measurements_hash(dataset),
        }
        state = {
            **{key: payload[key] for key in ("proposal_id", "owner", "plant_id", "risk_id")},
            "payload": payload, "payload_ref": self.store.write_artifact("action_proposal", payload),
            "content_hash": digest(payload), "status": "proposed", "version": 1,
            "review": None, "execution": None, "followups": [], "closure": None,
        }
        with self.store.transaction() as db:
            self.store.authorize(actor, dataset.plant_id, db=db)
            current_risk = self.service.get_risk(request.risk_id, actor, db=db)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                self._load(db, replay["proposal_id"], actor)
                return replay
            if current_risk != risk or self.service.get_evaluation(risk["evaluation_id"], actor, db=db) != evaluation:
                raise DomainError("evidence_mismatch", "Risk evidence changed while proposing", 409)
            self._baseline(payload, actor, db)
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                       (proposal_id, actor.actor_id, dataset.plant_id, request.risk_id, canonical(state), 1))
            self.store.audit(db, actor.actor_id, "action_proposed",
                             {"action_id": request.action_id, "proposal_hash": state["content_hash"]}, proposal_id)
            self.store.save_receipt(db, actor, request.action_id, envelope, state)
            return deepcopy(state)

    def get(self, proposal_id, actor: Actor):
        with self.store.connect() as db:
            state = self._load(db, proposal_id, actor)
            self.store.authorize(actor, state["plant_id"], db=db)
            return state

    def list(self, actor: Actor):
        with self.store.connect() as db:
            actor = self.store.current_actor(actor, db)
            placeholders = ",".join("?" for _ in actor.plants)
            rows = db.execute(
                f"SELECT proposal_id FROM proposals WHERE owner=? AND plant_id IN ({placeholders}) ORDER BY proposal_id",
                (actor.actor_id, *actor.plants)).fetchall()
            result = [self._load(db, row["proposal_id"], actor) for row in rows]
            self.store.current_actor(actor, db)
            return result

    def review(self, proposal_id, request, actor: Actor):
        request = _request(ReviewRequest, request)
        envelope = self._envelope("review", request, proposal_id)
        with self.store.transaction() as db:
            state = self._load(db, proposal_id, actor, _REVIEW_ROLES)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                return replay
            self._require(state, "proposed")
            if request.content_hash != state["content_hash"]:
                raise DomainError("hash_mismatch", "Review must bind the exact frozen proposal", 409)
            self._baseline(state["payload"], actor, db)
            state["review"] = self._record("action_review", {
                "proposal_hash": state["content_hash"], "decision": request.decision,
                "note": request.note, "actor_id": actor.actor_id, "role": actor.role,
                "recorded_at_ms": _now_ms(), "policy": REVIEW_POLICY,
            })
            state["status"] = request.decision
            return self._save(db, state, actor, request, envelope, "action_reviewed")

    def confirm_execution(self, proposal_id, request, actor: Actor):
        request = _request(ExecuteRequest, request)
        envelope = self._envelope("confirm_execution", request, proposal_id)
        with self.store.transaction() as db:
            state = self._load(db, proposal_id, actor)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                return replay
            self._require(state, "approved")
            if request.proposal_hash != state["content_hash"]:
                raise DomainError("hash_mismatch", "Execution must bind the approved proposal", 409)
            if request.observed_at_ms > _now_ms():
                raise DomainError("future_observation", "Execution observation must not be in the future", 422)
            self._baseline(state["payload"], actor, db)
            state["execution"] = self._record("action_execution", {
                "proposal_hash": state["content_hash"], "confirmed": True,
                "observed_at_ms": request.observed_at_ms, "note": request.note,
                "actor_id": actor.actor_id, "recorded_at_ms": _now_ms(),
                "kind": "manual_attestation", "physical_execution_performed": False,
            })
            state["status"] = "executed"
            return self._save(db, state, actor, request, envelope, "action_execution_attested")

    def _followup_evidence(self, state, computed, dataset_id):
        raw = computed["dataset"]
        dataset = _dataset(raw)
        payload = state["payload"]
        window = _window(dataset)
        if dataset.dataset_id != dataset_id or dataset.plant_id != state["plant_id"]:
            raise DomainError("scope_mismatch", "Follow-up dataset identity or plant differs", 409)
        if (window["start_ms"] <= state["execution"]["payload"]["observed_at_ms"]
                or window["start_ms"] <= payload["original_window"]["end_ms"]):
            raise DomainError("followup_window", "Follow-up must be strictly after execution and the original risk window", 422)
        if window["end_ms"] > _now_ms():
            raise DomainError("future_observation", "Follow-up observations must not be in the future", 422)
        data_hash = digest(raw)
        if (dataset.dataset_id == payload["risk_ref"]["dataset_id"]
                or data_hash == payload["risk_ref"]["data_hash"]
                or _evidence_hash(dataset) == payload["evidence_hash"]
                or _measurements_hash(dataset) == payload["measurements_hash"]):
            raise DomainError("reused_evidence", "Original risk measurements cannot be relabeled as follow-up", 409)
        if any(dataset.dataset_id == old["payload"]["dataset_id"]
               or _evidence_hash(dataset) == old["payload"]["evidence_hash"]
               or _measurements_hash(dataset) == old["payload"]["measurements_hash"]
               for old in state["followups"]):
            raise DomainError("reused_evidence", "A new follow-up requires different observations", 409)
        analysis = computed["analysis"]
        baseline_hash = payload["risk_ref"]["baseline_hash"]
        if (computed["data_hash"] != data_hash or computed["baseline_hash"] != baseline_hash
                or analysis.get("dataset_hash") != dataset_hash(dataset)
                or analysis.get("dataset_id") != dataset_id
                or analysis.get("baseline_hash") != baseline_hash
                or analysis.get("baseline_id") != payload["risk_ref"]["baseline_id"]):
            raise DomainError("evidence_mismatch", "Computed follow-up does not bind the original baseline and dataset", 409)
        blockers = []
        scope = _scope(dataset)
        frozen_baseline = self._artifact(payload["baseline_ref"], "action_baseline")
        if (scope != payload["scope"]
                or any(scope[key] != frozen_baseline[key] for key in
                       ("plant_id", "process_id", "characteristic", "unit", "sampling_seconds"))
                or scope["process_versions"] != [frozen_baseline["process_version"]]
                or scope["gauge_ids"] != sorted({item["gauge_id"] for item in frozen_baseline["calibrations"]})):
            blockers.append("scope_mismatch")
        if len(dataset.groups) < 25 or any(len(group.values) != 5 for group in dataset.groups):
            blockers.append("requires_25_subgroups_of_5")
        if (analysis.get("status") != "stable" or analysis.get("issues") != []
                or analysis.get("rule_hits") != []
                or analysis.get("counts", {}).get("valid_groups") != len(dataset.groups)
                or analysis.get("capability", {}).get("status") == "not_evaluable"):
            blockers.append("analysis_not_qualified")
        # The engine handles all quality issues, but retain a fail-closed check at
        # this lifecycle boundary for MSA/calibration and missing observations.
        msa = dataset.msa
        if (msa.status != "passed" or not msa.study_id or msa.valid_from_ms is None
                or msa.valid_until_ms is None or msa.valid_from_ms > window["start_ms"]
                or msa.valid_until_ms < window["end_ms"]):
            blockers.append("msa_unresolved")
        calibrations = {item.gauge_id: item for item in dataset.calibrations}
        for group in dataset.groups:
            calibration = calibrations.get(group.gauge_id)
            if (calibration is None or calibration.status != "valid" or not calibration.certificate_id
                    or calibration.valid_from_ms is None or calibration.valid_until_ms is None
                    or not calibration.valid_from_ms <= group.timestamp_ms <= calibration.valid_until_ms):
                blockers.append("calibration_unresolved")
                break
        if any(group.measurement_status != "valid" or any(value is None for value in group.values)
               for group in dataset.groups):
            blockers.append("invalid_measurements")
        eligible = not blockers
        evidence = {
            "proposal_hash": state["content_hash"], "execution_hash": state["execution"]["content_hash"],
            "dataset_id": dataset_id, "baseline_id": payload["risk_ref"]["baseline_id"],
            "data_hash": data_hash, "baseline_hash": baseline_hash,
            "evidence_hash": _evidence_hash(dataset), "measurements_hash": _measurements_hash(dataset),
            "window": window, "source": "original_synthetic", "recorded_at_ms": _now_ms(),
            "analysis": analysis, "analysis_hash": digest(analysis),
            "analysis_ref": self.store.write_artifact("action_analysis", analysis),
            "dataset_ref": self.store.write_artifact("action_dataset", raw),
            "eligible": eligible, "blockers": blockers,
            "explanation": FOLLOWUP_EXPLANATION if eligible else None,
        }
        return {"payload": evidence, "followup_hash": digest(evidence),
                "artifact_ref": self.store.write_artifact("action_followup", evidence)}

    def followup(self, proposal_id, request, actor: Actor):
        request = _request(FollowupRequest, request)
        envelope = self._envelope("followup", request, proposal_id)
        with self.store.connect() as db:
            snapshot = self._load(db, proposal_id, actor)
            # Authorization for *both* objects precedes replay, even if a receipt exists.
            raw = self.store.dataset(request.dataset_id, actor, db=db)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                return replay
            self._require(snapshot, "executed")
            self._baseline(snapshot["payload"], actor, db)
        # Statistics and artifact publication do not hold the SQLite write lock.
        computed = self.service.compute_followup(request.dataset_id,
                                                 snapshot["payload"]["risk_ref"]["baseline_id"], actor)
        if computed["dataset"] != raw:
            raise DomainError("evidence_mismatch", "Follow-up dataset changed while computing", 409)
        followup = self._followup_evidence(snapshot, computed, request.dataset_id)
        with self.store.transaction() as db:
            state = self._load(db, proposal_id, actor)
            current_raw = self.store.dataset(request.dataset_id, actor, db=db)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                return replay
            if state["version"] != snapshot["version"]:
                raise DomainError("version_conflict", "Proposal changed during follow-up; reload", 409)
            self._require(state, "executed")
            self._baseline(state["payload"], actor, db)
            if current_raw != raw:
                raise DomainError("evidence_mismatch", "Follow-up observations changed while computing", 409)
            state["followups"].append(followup)
            if followup["payload"]["eligible"]:
                state["status"] = "followup_recorded"
            return self._save(db, state, actor, request, envelope, "action_followup_recorded")

    def close(self, proposal_id, request, actor: Actor):
        request = _request(CloseRequest, request)
        envelope = self._envelope("close", request, proposal_id)
        with self.store.transaction() as db:
            state = self._load(db, proposal_id, actor, _REVIEW_ROLES)
            replay = self._receipt(db, actor, request, envelope)
            if replay is not None:
                return replay
            self._require(state, "followup_recorded")
            latest = state["followups"][-1]
            if not latest["payload"]["eligible"] or request.followup_hash != latest["followup_hash"]:
                raise DomainError("hash_mismatch", "Closure requires the exact latest qualified follow-up", 409)
            self._baseline(state["payload"], actor, db)
            state["closure"] = self._record("action_closure", {
                "proposal_hash": state["content_hash"], "followup_hash": request.followup_hash,
                "note": request.note, "actor_id": actor.actor_id, "role": actor.role,
                "recorded_at_ms": _now_ms(), "policy": REVIEW_POLICY,
                "explanation": FOLLOWUP_EXPLANATION,
            })
            state["status"] = "closed"
            return self._save(db, state, actor, request, envelope, "action_closed")
