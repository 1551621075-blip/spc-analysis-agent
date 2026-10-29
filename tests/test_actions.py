"""Lifecycle tests use real SQLite, content-addressed artifacts and SPC statistics."""
from __future__ import annotations

import json
import math
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from spc_agent.actions import FOLLOWUP_EXPLANATION, ActionService
from spc_agent.domain import Dataset
from spc_agent.errors import DomainError
from spc_agent.schemas import (
    CloseRequest,
    ExecuteRequest,
    FollowupRequest,
    ProposalRequest,
    ReviewRequest,
    canonical,
    digest,
)
from spc_agent.statistics import analyze, build_baseline, dataset_hash
from spc_agent.storage import Store

START = 1_700_000_000_000  # Historical synthetic observations, independent of wall time.
EXECUTED_AT = START + 15_000_000
NOTE = "Synthetic demonstration reviewed against the frozen evidence."


def synthetic(dataset_id, start, *, seed=1, center=10.0, shift=0.0):
    sigma_xbar = 4 / 2.326 / math.sqrt(5)
    return {
        "dataset_id": dataset_id, "plant_id": "plant-a", "process_id": "turning",
        "characteristic": "diameter", "unit": "mm", "source": "original_synthetic",
        "sampling_seconds": 60,
        "msa": {"status": "passed", "study_id": "msa-a", "valid_from_ms": START - 1_000,
                "valid_until_ms": START + 500_000_000},
        "calibrations": [{"gauge_id": "gauge-a", "status": "valid", "certificate_id": "cal-a",
                          "valid_from_ms": START - 1_000, "valid_until_ms": START + 500_000_000}],
        "specifications": {"lsl": 0.0, "usl": 20.0, "revision": "spec-a", "unit": "mm"},
        "groups": [{
            "group_id": f"{dataset_id}-g{i}", "timestamp_ms": start + i * 60_000,
            "batch_id": "batch-a", "gauge_id": "gauge-a", "process_version": "v1",
            "values": [center + shift + random.Random(seed * 100 + i).gauss(0, 1) * sigma_xbar + delta
                       for delta in (-2.0, -1.0, 0.0, 1.0, 2.0)],
        } for i in range(25)],
    }


class FakeService:
    """Only the parent Service facade is fake; its evidence and all storage are real."""

    def __init__(self, store):
        self.store = store
        self.compute_hook = None

    def _get(self, table, key, object_id, actor, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self._get(table, key, object_id, actor, connection)
        self.store.current_actor(actor, db)
        row = db.execute(f"SELECT * FROM {table} WHERE {key}=? AND owner=?",
                         (object_id, actor.actor_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Evidence not found", 404)
        self.store.authorize(actor, row["plant_id"], db=db)
        payload = self.store.read_artifact(json.loads(row["payload_ref"]))
        if table == "baselines":
            return {"payload": payload, "status": row["status"], "owner": row["owner"]}
        if table == "evaluations":
            return {**payload, "content_hash": row["content_hash"]}
        return payload

    def get_risk(self, risk_id, actor, db=None):
        return self._get("risks", "risk_id", risk_id, actor, db)

    def get_evaluation(self, evaluation_id, actor, db=None):
        return self._get("evaluations", "evaluation_id", evaluation_id, actor, db)

    def get_baseline(self, baseline_id, actor, db=None):
        return self._get("baselines", "baseline_id", baseline_id, actor, db)

    def compute_followup(self, dataset_id, baseline_id, actor, db=None):
        raw = self.store.dataset(dataset_id, actor, db=db)
        baseline = self.get_baseline(baseline_id, actor, db=db)["payload"]
        result = {"dataset": raw, "analysis": analyze(Dataset.model_validate(raw), baseline),
                  "data_hash": digest(raw),
                  "baseline_hash": baseline["baseline_hash"]}
        if self.compute_hook:
            self.compute_hook()
        return result


def seed_evidence(store, actor):
    baseline_raw = synthetic("phase-one", START)
    store.put_dataset(baseline_raw, actor)
    baseline = build_baseline(Dataset.model_validate(baseline_raw), "baseline-a")
    risk_raw = synthetic("risk-data", START + 5_000_000, seed=3, center=baseline["center"], shift=5.0)
    store.put_dataset(risk_raw, actor)
    analysis = analyze(Dataset.model_validate(risk_raw), baseline)
    evaluation = {
        "evaluation_id": "evaluation-a", "dataset_id": "risk-data", "baseline_id": "baseline-a",
        "dataset": risk_raw, "analysis": analysis,
    }
    risk = {
        "risk_id": "risk-a", "plant_id": "plant-a", "process_id": "turning",
        "evaluation_id": "evaluation-a", "baseline_id": "baseline-a", "dataset_id": "risk-data",
        "data_hash": digest(risk_raw),
        "baseline_hash": baseline["baseline_hash"], "analysis": analysis,
    }
    baseline_ref = store.write_artifact("baseline", baseline)
    evaluation_ref = store.write_artifact("evaluation", evaluation)
    risk_ref = store.write_artifact("risk", risk)
    with store.transaction() as db:
        db.execute("INSERT INTO baselines VALUES(?,?,?,?,?,?,?,?)",
                   ("baseline-a", actor.actor_id, "plant-a", canonical(baseline_ref), digest(baseline),
                    "approved", canonical({"note": NOTE}), 1.0))
        db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)",
                   ("evaluation-a", actor.actor_id, "plant-a", "risk-data", "baseline-a",
                    canonical(evaluation_ref), digest(evaluation), 1.0))
        db.execute("INSERT INTO risks VALUES(?,?,?,?,?,?,?)",
                   ("risk-a", actor.actor_id, "plant-a", "evaluation-a", "risk-event",
                    canonical(risk_ref), 1.0))
    return baseline, risk_raw


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "store")
    token = store.provision_user("owner", "admin", ["plant-a"])
    actor = store.authenticate(token)
    baseline, original = seed_evidence(store, actor)
    service = FakeService(store)
    result = SimpleNamespace(store=store, service=service, actions=ActionService(service),
                             actor=actor, token=token, baseline=baseline, original=original)
    for name, seed, shift in (("followup-good", 2, 0.0), ("followup-signals", 4, 5.0),
                              ("followup-other", 5, 0.0)):
        store.put_dataset(synthetic(name, START + 20_000_000, seed=seed,
                                    center=baseline["center"], shift=shift), actor)
    return result


def propose(env, action_id="propose-a"):
    return env.actions.propose(ProposalRequest(
        action_id=action_id, risk_id="risk-a", description="Check the declared synthetic setup.",
        expected_result="A new synthetic observation window has no chart signals."), env.actor)


def review_request(state, action_id="review-a", decision="approved"):
    return {"action_id": action_id, "content_hash": state["content_hash"], "decision": decision, "note": NOTE}


def execution_request(state, action_id="execute-a"):
    return {"action_id": action_id, "proposal_hash": state["content_hash"],
            "observed_at_ms": EXECUTED_AT, "confirmed": True, "note": NOTE}


def approved(env):
    state = propose(env)
    return env.actions.review(state["proposal_id"], ReviewRequest(**review_request(state)), env.actor)


def executed(env):
    state = approved(env)
    return env.actions.confirm_execution(state["proposal_id"], ExecuteRequest(**execution_request(state)), env.actor)


def followed(env):
    state = executed(env)
    return env.actions.followup(state["proposal_id"], FollowupRequest(
        action_id="followup-a", dataset_id="followup-good"), env.actor)


def counts(env):
    with env.store.connect() as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("proposals", "receipts", "audit"))


def fails(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


@pytest.mark.parametrize("characteristic", ["diameter", "轴径"])
def test_actual_service_end_to_end_without_fake_or_provider(tmp_path, characteristic):
    from spc_agent.service import SPCService

    store = Store(tmp_path / "actual-service")
    actor = store.authenticate(store.provision_user("demo-owner", "admin", ["plant-a"]))
    service = SPCService(store)
    actions = ActionService(service)
    raw_baseline = synthetic("phase-one", START)
    raw_baseline["characteristic"] = characteristic
    service.import_dataset(raw_baseline, actor)
    baseline = service.create_baseline({"action_id": "baseline-build", "dataset_id": "phase-one"}, actor)
    baseline = service.review_baseline(baseline["baseline_id"], {
        "action_id": "baseline-review", "content_hash": baseline["content_hash"],
        "decision": "approved", "note": NOTE}, actor)
    for identity, seed, start, shift in (
        ("risk-data", 3, START + 5_000_000, 5.0),
        ("followup-signals", 4, START + 20_000_000, 5.0),
        ("followup-good", 2, START + 30_000_000, 0.0),
    ):
        raw = synthetic(identity, start, seed=seed, center=baseline["payload"]["center"], shift=shift)
        raw["characteristic"] = characteristic
        service.import_dataset(raw, actor)
    evaluation = service.evaluate({"action_id": "evaluate-risk", "dataset_id": "risk-data",
                                   "baseline_id": baseline["baseline_id"]}, actor)
    assert evaluation["analysis"]["status"] == "signals" and evaluation["risk_id"]
    proposal = actions.propose({"action_id": "proposal-a", "risk_id": evaluation["risk_id"],
                                "description": "Review the declared synthetic machine setup.",
                                "expected_result": "A fresh observation window has no chart signals."}, actor)
    assert proposal["payload"]["risk_ref"]["evaluation_hash"] == evaluation["content_hash"]
    assert proposal["payload"]["risk_ref"]["data_hash"] == evaluation["data_hash"]
    pid = proposal["proposal_id"]
    actions.review(pid, review_request(proposal), actor)
    actions.confirm_execution(pid, execution_request(proposal), actor)
    failed = actions.followup(pid, {"action_id": "followup-failed", "dataset_id": "followup-signals"}, actor)
    assert failed["status"] == "executed" and not failed["followups"][-1]["payload"]["eligible"]
    qualified = actions.followup(pid, {"action_id": "followup-good", "dataset_id": "followup-good"}, actor)
    assert qualified["status"] == "followup_recorded" and len(qualified["followups"]) == 2
    evidence = qualified["followups"][-1]["payload"]
    assert evidence["data_hash"] == digest(store.dataset("followup-good", actor))
    assert evidence["analysis"]["dataset_hash"] == dataset_hash(Dataset.model_validate(store.dataset("followup-good", actor)))
    closed = actions.close(pid, {"action_id": "close-a", "followup_hash": qualified["followups"][-1]["followup_hash"],
                                 "note": NOTE}, actor)
    assert closed["status"] == "closed" and closed["version"] == 6
    assert closed["closure"]["payload"]["explanation"] == FOLLOWUP_EXPLANATION
    assert service.get_baseline(baseline["baseline_id"], actor) == baseline
    assert len(service.audit(pid, actor)) == 6
    assert ActionService(SPCService(Store(store.root))).get(pid, actor) == closed


@pytest.mark.parametrize("characteristic", ["bore diameter", "镗孔直径"])
def test_actual_service_official_independent_followup_fixtures(tmp_path, characteristic):
    from spc_agent.fixtures import baseline_dataset, demo_datasets
    from spc_agent.service import SPCService

    baseline_raw = baseline_dataset().model_dump(mode="json")
    fixtures = demo_datasets()
    original = fixtures["mean_shift"].model_dump(mode="json")
    normal = fixtures["normal"].model_dump(mode="json")
    followup_raw = fixtures["followup"].model_dump(mode="json")
    for raw in (baseline_raw, original, normal, followup_raw):
        raw["characteristic"] = characteristic
    original_end = max(group["timestamp_ms"] for group in original["groups"])
    followup_start = min(group["timestamp_ms"] for group in followup_raw["groups"])
    observed_at_ms = original_end + 1
    assert followup_start > observed_at_ms
    assert [group["values"] for group in followup_raw["groups"]] != [group["values"] for group in normal["groups"]]
    assert [group["values"] for group in followup_raw["groups"]] != [group["values"] for group in original["groups"]]

    store = Store(tmp_path / "official-fixtures")
    actor = store.authenticate(store.provision_user("fixture-owner", "admin", [baseline_raw["plant_id"]]))
    service = SPCService(store)
    actions = ActionService(service)
    for raw in (baseline_raw, original, normal, followup_raw):
        service.import_dataset(raw, actor)
    baseline = service.create_baseline({"action_id": "baseline-build", "dataset_id": baseline_raw["dataset_id"]}, actor)
    baseline = service.review_baseline(baseline["baseline_id"], {
        "action_id": "baseline-review", "content_hash": baseline["content_hash"],
        "decision": "approved", "note": NOTE}, actor)
    semantic_hash = baseline["payload"]["baseline_hash"]
    assert semantic_hash == digest({key: value for key, value in baseline["payload"].items() if key != "baseline_hash"})
    assert semantic_hash != baseline["content_hash"]
    evaluation = service.evaluate({"action_id": "evaluate-risk", "dataset_id": original["dataset_id"],
                                   "baseline_id": baseline["baseline_id"]}, actor)
    assert evaluation["analysis"]["status"] == "signals" and evaluation["risk_id"]
    assert evaluation["baseline_hash"] == semantic_hash
    proposal = actions.propose({"action_id": "proposal-a", "risk_id": evaluation["risk_id"],
                                "description": "Review this original synthetic process setup.",
                                "expected_result": "A genuinely later independent window has no signals."}, actor)
    pid = proposal["proposal_id"]
    assert proposal["payload"]["risk_ref"]["baseline_hash"] == semantic_hash
    assert proposal["payload"]["original_window"]["end_ms"] == original_end
    actions.review(pid, review_request(proposal), actor)
    execution = execution_request(proposal)
    execution["observed_at_ms"] = observed_at_ms
    actions.confirm_execution(pid, execution, actor)
    fails("followup_window", actions.followup, pid,
          {"action_id": "same-window", "dataset_id": normal["dataset_id"]}, actor)
    qualified = actions.followup(pid, {"action_id": "independent-followup", "dataset_id": followup_raw["dataset_id"]}, actor)
    assert qualified["status"] == "followup_recorded"
    evidence = qualified["followups"][-1]
    assert evidence["payload"]["eligible"] is True and evidence["payload"]["blockers"] == []
    assert evidence["payload"]["analysis"]["status"] == "stable"
    assert evidence["payload"]["baseline_hash"] == evidence["payload"]["analysis"]["baseline_hash"] == semantic_hash
    assert evidence["payload"]["data_hash"] == evidence["payload"]["analysis"]["dataset_hash"] == digest(followup_raw)
    closed = actions.close(pid, {"action_id": "close-a", "followup_hash": evidence["followup_hash"], "note": NOTE}, actor)
    assert closed["status"] == "closed" and closed["version"] == 5
    assert closed["closure"]["payload"]["explanation"] == FOLLOWUP_EXPLANATION
    assert service.get_baseline(baseline["baseline_id"], actor) == baseline
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts WHERE action_id='same-window'").fetchone()[0] == 0
    assert len(service.audit(pid, actor)) == 5
    assert ActionService(SPCService(Store(store.root))).get(pid, actor) == closed


def test_full_lifecycle_exact_replay_frozen_hash_and_persistence(env):
    before = counts(env)
    proposal = propose(env)
    pid, frozen = proposal["proposal_id"], proposal["content_hash"]
    assert proposal["status"] == "proposed" and proposal["version"] == 1
    assert proposal["payload"]["source"] == "original_synthetic"
    assert "no two-person" in proposal["payload"]["review_policy"]
    assert propose(env) == proposal
    assert counts(env) == tuple(a + b for a, b in zip(before, (1, 1, 1), strict=True))
    accepted = env.actions.review(pid, review_request(proposal), env.actor)
    assert accepted["status"] == "approved" and accepted["execution"] is None
    attested = env.actions.confirm_execution(pid, execution_request(accepted), env.actor)
    assert attested["status"] == "executed" and not attested["followups"]
    assert attested["execution"]["payload"]["kind"] == "manual_attestation"
    assert attested["execution"]["payload"]["physical_execution_performed"] is False
    observed = env.actions.followup(pid, {"action_id": "followup-a", "dataset_id": "followup-good"}, env.actor)
    followup = observed["followups"][-1]
    assert observed["status"] == "followup_recorded"
    assert followup["payload"]["analysis"]["status"] == "stable"
    assert followup["payload"]["eligible"] is True
    assert followup["payload"]["explanation"] == FOLLOWUP_EXPLANATION
    assert followup["payload"]["baseline_hash"] == env.baseline["baseline_hash"]
    assert len(followup["payload"]["analysis"]["points"]) == 25
    closure_request = CloseRequest(action_id="close-a", followup_hash=followup["followup_hash"], note=NOTE)
    closed = env.actions.close(pid, closure_request, env.actor)
    assert closed["status"] == "closed" and closed["version"] == 5
    assert all(s["content_hash"] == frozen for s in (accepted, attested, observed, closed))
    assert counts(env) == tuple(a + b for a, b in zip(before, (1, 5, 5), strict=True))
    # Old retries return the exact original receipt, not a second transition or latest state.
    assert env.actions.review(pid, review_request(proposal), env.actor) == accepted
    assert env.actions.confirm_execution(pid, execution_request(accepted), env.actor) == attested
    assert env.actions.followup(pid, {"action_id": "followup-a", "dataset_id": "followup-good"}, env.actor) == observed
    assert env.actions.close(pid, closure_request, env.actor) == closed
    assert propose(env) == proposal
    assert counts(env) == tuple(a + b for a, b in zip(before, (1, 5, 5), strict=True))
    restarted = ActionService(FakeService(Store(env.store.root)))
    assert restarted.get(pid, env.actor) == closed
    assert restarted.list(env.actor) == [closed]
    assert env.service.get_baseline("baseline-a", env.actor)["payload"] == env.baseline
    with env.store.connect() as db:
        events = db.execute("SELECT * FROM audit WHERE object_id=?", (pid,)).fetchall()
    assert len(events) == 5 and all(row["actor_id"] == env.actor.actor_id for row in events)


def test_action_id_conflicts_across_methods_objects_and_payload(env):
    proposal = propose(env)
    pid = proposal["proposal_id"]
    before = counts(env)
    fails("idempotency_conflict", env.actions.review, pid, review_request(proposal, "propose-a"), env.actor)
    changed = {"action_id": "propose-a", "risk_id": "risk-a", "description": "A different frozen proposal.",
               "expected_result": "A sufficient fresh observation window."}
    fails("idempotency_conflict", env.actions.propose, changed, env.actor)
    assert counts(env) == before
    accepted = env.actions.review(pid, review_request(proposal), env.actor)
    fails("idempotency_conflict", env.actions.confirm_execution, pid,
          execution_request(accepted, "review-a"), env.actor)
    other = propose(env, "propose-b")
    fails("idempotency_conflict", env.actions.review, other["proposal_id"], review_request(other), env.actor)


def test_rejected_is_terminal_and_wrong_hash_has_no_side_effect(env):
    proposal = propose(env)
    pid = proposal["proposal_id"]
    before = counts(env)
    bad = review_request(proposal)
    bad["content_hash"] = "0" * 64
    fails("hash_mismatch", env.actions.review, pid, bad, env.actor)
    assert counts(env) == before
    rejected = env.actions.review(pid, review_request(proposal, decision="rejected"), env.actor)
    assert rejected["status"] == "rejected" and rejected["version"] == 2
    before = counts(env)
    fails("invalid_transition", env.actions.review, pid, review_request(proposal, "new-review"), env.actor)
    fails("invalid_transition", env.actions.confirm_execution, pid, execution_request(proposal), env.actor)
    assert counts(env) == before


@pytest.mark.parametrize("stage", ["proposed", "approved", "executed"])
def test_cannot_close_without_qualified_followup_or_execute_on_approval(env, stage):
    state = {"proposed": propose, "approved": approved, "executed": executed}[stage](env)
    pid = state["proposal_id"]
    before = counts(env)
    fails("invalid_transition", env.actions.close, pid,
          {"action_id": "close-a", "followup_hash": "0" * 64, "note": NOTE}, env.actor)
    if stage != "executed":
        fails("invalid_transition", env.actions.followup, pid,
              {"action_id": "followup-a", "dataset_id": "followup-good"}, env.actor)
    if stage == "proposed":
        fails("invalid_transition", env.actions.confirm_execution, pid, execution_request(state), env.actor)
    assert counts(env) == before


@pytest.mark.parametrize("field,value", [
    ("confirmed", None), ("confirmed", False), ("confirmed", 1), ("confirmed", "true"),
    ("observed_at_ms", None), ("observed_at_ms", True), ("observed_at_ms", "1700015000000"),
    ("note", "          "), ("computed", True),
])
def test_strict_execution_missing_fields_and_no_client_computation(env, field, value):
    state = approved(env)
    request = execution_request(state)
    if value is None:
        request.pop(field)
    else:
        request[field] = value
    before = counts(env)
    fails("invalid_request", env.actions.confirm_execution, state["proposal_id"], request, env.actor)
    assert counts(env) == before
    assert env.actions.get(state["proposal_id"], env.actor)["status"] == "approved"


def test_future_execution_and_wrong_execution_hash(env, monkeypatch):
    state = approved(env)
    before = counts(env)
    request = execution_request(state)
    request["proposal_hash"] = "0" * 64
    fails("hash_mismatch", env.actions.confirm_execution, state["proposal_id"], request, env.actor)
    request = execution_request(state)
    monkeypatch.setattr("spc_agent.actions._now_ms", lambda: EXECUTED_AT - 1)
    fails("future_observation", env.actions.confirm_execution, state["proposal_id"], request, env.actor)
    assert counts(env) == before


def change_identity(env, *, role="admin", plants=None, enabled=True):
    with env.store.transaction() as db:
        db.execute("UPDATE users SET role=?,plants=?,enabled=?,acl_version=acl_version+1 WHERE actor_id=?",
                   (role, canonical(plants if plants is not None else ["plant-a"]), int(enabled), "owner"))
    if enabled:
        return env.store.authenticate(env.token)


def test_owner_only_even_other_admin_and_no_metadata_leak(env):
    state = followed(env)
    other = env.store.authenticate(env.store.provision_user("another-admin", "admin", ["plant-a"]))
    pid = state["proposal_id"]
    before = counts(env)
    fails("not_found", env.actions.get, pid, other)
    assert env.actions.list(other) == []
    fails("not_found", env.actions.review, pid, review_request(state), other)
    fails("not_found", env.actions.confirm_execution, pid, execution_request(state), other)
    fails("not_found", env.actions.followup, pid, {"action_id": "followup-a", "dataset_id": "followup-good"}, other)
    fails("not_found", env.actions.close, pid,
          {"action_id": "close-a", "followup_hash": state["followups"][-1]["followup_hash"], "note": NOTE}, other)
    fails("not_found", env.actions.propose,
          {"action_id": "propose-a", "risk_id": "risk-a", "description": "Not the risk owner proposal.",
           "expected_result": "Cannot access another owner risk."}, other)
    assert counts(env) == before


@pytest.mark.parametrize("kind", ["role", "plant", "disabled"])
def test_replay_rechecks_current_identity_and_authorization(env, kind):
    state = followed(env)
    pid = state["proposal_id"]
    close_request = {"action_id": "close-a", "followup_hash": state["followups"][-1]["followup_hash"], "note": NOTE}
    env.actions.close(pid, close_request, env.actor)
    current = change_identity(env, role="operator" if kind == "role" else "admin",
                              plants=["plant-b"] if kind == "plant" else None, enabled=kind != "disabled")
    before = counts(env)
    fails("identity_changed", env.actions.review, pid, review_request(state), env.actor)
    fails("identity_changed", env.actions.close, pid, close_request, env.actor)
    fails("identity_changed", env.actions.get, pid, env.actor)
    fails("identity_changed", propose, env)
    if current:
        fails("forbidden", env.actions.review, pid, review_request(state), current)
        fails("forbidden", env.actions.close, pid, close_request, current)
        if kind == "plant":
            fails("forbidden", env.actions.get, pid, current)
            assert env.actions.list(current) == []
    assert counts(env) == before


def test_operator_can_propose_but_cannot_review(env):
    env.actor = change_identity(env, role="operator")
    state = propose(env)
    before = counts(env)
    fails("forbidden", env.actions.review, state["proposal_id"], review_request(state), env.actor)
    assert counts(env) == before


@pytest.mark.parametrize("mutation", ["description", "proposal_hash", "execution", "status", "followup"])
def test_artifact_and_state_tampering_rejected_before_replay(env, mutation):
    state = followed(env)
    pid = state["proposal_id"]
    if mutation == "description":
        state["payload"]["description"] = "Substituted description after approval."
    elif mutation == "proposal_hash":
        path = env.store.artifacts / (state["payload_ref"]["sha256"] + ".json")
        path.write_text("tampered", encoding="utf-8")
    elif mutation == "execution":
        state["execution"]["payload"]["observed_at_ms"] -= 1
    elif mutation == "status":
        state["status"] = "closed"
    else:
        state["followups"][-1]["payload"]["analysis"]["status"] = "signals"
    if mutation != "proposal_hash":
        with env.store.transaction() as db:
            db.execute("UPDATE proposals SET state_json=? WHERE proposal_id=?", (canonical(state), pid))
    before = counts(env)
    fails("artifact_integrity", env.actions.get, pid, env.actor)
    fails("artifact_integrity", env.actions.review, pid, review_request(state), env.actor)
    fails("artifact_integrity", env.actions.close, pid,
          {"action_id": "close-a", "followup_hash": state["followups"][-1]["followup_hash"], "note": NOTE}, env.actor)
    assert counts(env) == before


@pytest.mark.parametrize("kind", ["original", "renamed", "same-values-new-window", "execution-boundary", "risk-boundary", "future"])
def test_followup_requires_fresh_historical_nonoverlapping_observations(env, kind):
    state = approved(env)
    execution = execution_request(state)
    if kind == "risk-boundary":
        execution["observed_at_ms"] = START + 2_000_000
    state = env.actions.confirm_execution(state["proposal_id"], execution, env.actor)
    pid = state["proposal_id"]
    dataset_id, expected = "bad-data", "followup_window"
    raw = synthetic(dataset_id, EXECUTED_AT, seed=2, center=env.baseline["center"])
    if kind == "original":
        dataset_id = "risk-data"
    elif kind in ("renamed", "same-values-new-window"):
        raw = deepcopy(env.original)
        raw["dataset_id"] = dataset_id
        if kind == "same-values-new-window":
            for i, group in enumerate(raw["groups"]):
                group["group_id"] = f"renamed-{i}"
                group["timestamp_ms"] = START + 20_000_000 + i * 60_000
            expected = "reused_evidence"
    elif kind == "risk-boundary":
        raw = synthetic(dataset_id, env.original["groups"][-1]["timestamp_ms"], seed=2)
    elif kind == "future":
        raw = synthetic(dataset_id, 30_000_000_000_000, seed=2)
        expected = "future_observation"
    if kind != "original":
        env.store.put_dataset(raw, env.actor)
    before = counts(env)
    fails(expected, env.actions.followup, pid, {"action_id": "followup-a", "dataset_id": dataset_id}, env.actor)
    assert counts(env) == before
    assert env.actions.get(pid, env.actor)["status"] == "executed"


@pytest.mark.parametrize("kind", ["signals", "missing", "msa", "calibration", "short", "n4", "scope", "gauge", "unit", "sampling"])
def test_failed_followup_keeps_complete_evidence_but_cannot_close(env, kind):
    state = executed(env)
    raw = synthetic("not-qualified", START + 20_000_000, seed=4, center=env.baseline["center"])
    if kind == "signals":
        raw = synthetic("not-qualified", START + 20_000_000, seed=4, center=env.baseline["center"], shift=5)
    elif kind == "missing":
        raw["groups"][4]["values"][0] = None
    elif kind == "msa":
        raw["msa"]["status"] = "unknown"
    elif kind == "calibration":
        raw["calibrations"][0]["status"] = "invalid"
    elif kind == "short":
        raw["groups"] = raw["groups"][:24]
    elif kind == "n4":
        raw["groups"][0]["values"].pop()
    elif kind == "scope":
        raw["characteristic"] = "length"
    elif kind == "gauge":
        raw["calibrations"][0]["gauge_id"] = "gauge-b"
        for group in raw["groups"]:
            group["gauge_id"] = "gauge-b"
    elif kind == "unit":
        raw["unit"] = "um"
    else:
        raw["sampling_seconds"] = 30
    env.store.put_dataset(raw, env.actor)
    before = counts(env)
    request = {"action_id": "failed-a", "dataset_id": "not-qualified"}
    failed = env.actions.followup(state["proposal_id"], request, env.actor)
    assert failed["status"] == "executed" and failed["version"] == 4
    evidence = failed["followups"][-1]
    assert evidence["payload"]["eligible"] is False
    assert evidence["payload"]["blockers"]
    assert evidence["payload"]["explanation"] is None
    assert env.store.read_artifact(evidence["payload"]["analysis_ref"]) == evidence["payload"]["analysis"]
    assert env.actions.followup(state["proposal_id"], request, env.actor) == failed
    assert counts(env) == tuple(a + b for a, b in zip(before, (0, 1, 1), strict=True))
    fails("invalid_transition", env.actions.close, state["proposal_id"],
          {"action_id": "close-failed", "followup_hash": evidence["followup_hash"], "note": NOTE}, env.actor)
    fails("reused_evidence", env.actions.followup, state["proposal_id"],
          {"action_id": "retry-without-new-data", "dataset_id": "not-qualified"}, env.actor)
    succeeded = env.actions.followup(state["proposal_id"],
                                     {"action_id": "followup-new", "dataset_id": "followup-good"}, env.actor)
    assert succeeded["status"] == "followup_recorded" and len(succeeded["followups"]) == 2
    fails("hash_mismatch", env.actions.close, state["proposal_id"],
          {"action_id": "close-a", "followup_hash": evidence["followup_hash"], "note": NOTE}, env.actor)
    closed = env.actions.close(state["proposal_id"], {
        "action_id": "close-a", "followup_hash": succeeded["followups"][-1]["followup_hash"], "note": NOTE}, env.actor)
    assert closed["status"] == "closed"


def test_followup_rejects_client_computed_outcome(env):
    state = executed(env)
    before = counts(env)
    fails("invalid_request", env.actions.followup, state["proposal_id"],
          {"action_id": "followup-a", "dataset_id": "followup-good", "eligible": True, "analysis": {}}, env.actor)
    assert counts(env) == before


def test_original_window_must_match_frozen_evaluation_dataset(env):
    evaluation = env.service.get_evaluation("evaluation-a", env.actor)
    evaluation.pop("content_hash")
    evaluation["dataset"]["groups"][-1]["timestamp_ms"] += 60_000
    reference = env.store.write_artifact("evaluation", evaluation)
    with env.store.transaction() as db:
        db.execute("UPDATE evaluations SET payload_ref=?,content_hash=? WHERE evaluation_id='evaluation-a'",
                   (canonical(reference), digest(evaluation)))
    before = counts(env)
    fails("evidence_mismatch", propose, env)
    assert counts(env) == before


def test_baseline_cannot_change_after_approval(env):
    state = executed(env)
    with env.store.transaction() as db:
        db.execute("UPDATE baselines SET status='rejected' WHERE baseline_id='baseline-a'")
    before = counts(env)
    fails("baseline_changed", env.actions.followup, state["proposal_id"],
          {"action_id": "followup-a", "dataset_id": "followup-good"}, env.actor)
    assert counts(env) == before


def test_baseline_scope_includes_gauge_even_when_original_risk_uses_new_gauge(env):
    # A risk may itself concern a gauge outside the approved baseline. Matching
    # that risk's scope alone must not make a follow-up eligible.
    baseline = deepcopy(env.baseline)
    baseline["calibrations"][0]["gauge_id"] = "baseline-only-gauge"
    baseline["baseline_hash"] = digest({key: value for key, value in baseline.items() if key != "baseline_hash"})
    baseline_ref = env.store.write_artifact("baseline", baseline)
    risk = env.service.get_risk("risk-a", env.actor)
    risk["baseline_hash"] = baseline["baseline_hash"]
    risk_ref = env.store.write_artifact("risk", risk)
    with env.store.transaction() as db:
        db.execute("UPDATE baselines SET payload_ref=?,content_hash=? WHERE baseline_id='baseline-a'",
                   (canonical(baseline_ref), digest(baseline)))
        db.execute("UPDATE risks SET payload_ref=? WHERE risk_id='risk-a'", (canonical(risk_ref),))
    state = executed(env)
    result = env.actions.followup(state["proposal_id"], {
        "action_id": "followup-a", "dataset_id": "followup-good"}, env.actor)
    assert result["status"] == "executed"
    assert "scope_mismatch" in result["followups"][-1]["payload"]["blockers"]
    assert result["followups"][-1]["payload"]["analysis"]["status"] == "stable"


def test_atomic_rollback_when_receipt_write_fails(env, monkeypatch):
    state = propose(env)
    before = counts(env)
    def fail_receipt(*args, **kwargs):
        raise RuntimeError("simulated receipt failure")
    monkeypatch.setattr(env.store, "save_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt failure"):
        env.actions.review(state["proposal_id"], review_request(state), env.actor)
    assert counts(env) == before
    assert env.actions.get(state["proposal_id"], env.actor) == state


def test_reviewer_owner_can_review_and_close_without_admin_role(env):
    env.actor = change_identity(env, role="reviewer")
    state = followed(env)
    closed = env.actions.close(state["proposal_id"], {
        "action_id": "close-a", "followup_hash": state["followups"][-1]["followup_hash"], "note": NOTE}, env.actor)
    assert closed["status"] == "closed"
    assert closed["review"]["payload"]["role"] == "reviewer"
    assert closed["closure"]["payload"]["role"] == "reviewer"


def test_proposal_rollback_when_receipt_write_fails(env, monkeypatch):
    before = counts(env)
    def fail_receipt(*args, **kwargs):
        raise RuntimeError("simulated receipt failure")
    monkeypatch.setattr(env.store, "save_receipt", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt failure"):
        propose(env)
    assert counts(env) == before
    assert env.actions.list(env.actor) == []


def test_compare_and_swap_rejects_changed_version_inside_commit(env, monkeypatch):
    state = propose(env)
    before = counts(env)
    original_save = env.actions._save
    def interfere(db, changed, actor, request, envelope, event):
        db.execute("UPDATE proposals SET version=version+1 WHERE proposal_id=?", (state["proposal_id"],))
        return original_save(db, changed, actor, request, envelope, event)
    monkeypatch.setattr(env.actions, "_save", interfere)
    fails("version_conflict", env.actions.review, state["proposal_id"], review_request(state), env.actor)
    assert counts(env) == before
    assert env.actions.get(state["proposal_id"], env.actor) == state


def test_concurrent_duplicate_proposal_has_one_row_and_receipt(env):
    barrier = threading.Barrier(2)
    before = counts(env)
    def run():
        barrier.wait(timeout=5)
        return propose(env)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert counts(env) == tuple(a + b for a, b in zip(before, (1, 1, 1), strict=True))


def test_concurrent_duplicate_review_has_one_transition_and_receipt(env):
    state = propose(env)
    barrier = threading.Barrier(2)
    before = counts(env)
    def run():
        barrier.wait(timeout=5)
        return env.actions.review(state["proposal_id"], review_request(state), env.actor)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert results[0] == results[1] and results[0]["version"] == 2
    assert counts(env) == tuple(a + b for a, b in zip(before, (0, 1, 1), strict=True))


def test_concurrent_different_reviews_cannot_both_commit(env):
    state = propose(env)
    barrier = threading.Barrier(2)
    before = counts(env)
    def run(index):
        barrier.wait(timeout=5)
        try:
            return env.actions.review(state["proposal_id"], review_request(
                state, f"review-{index}", "approved" if index else "rejected"), env.actor)
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert "invalid_transition" in results
    assert counts(env) == tuple(a + b for a, b in zip(before, (0, 1, 1), strict=True))


def test_followup_optimistic_version_and_no_compute_under_write_lock(env):
    state = executed(env)
    barrier = threading.Barrier(2)
    env.service.compute_hook = lambda: barrier.wait(timeout=10)
    before = counts(env)
    def run(index):
        try:
            return env.actions.followup(state["proposal_id"], {
                "action_id": f"followup-{index}", "dataset_id": "followup-good" if index else "followup-other"}, env.actor)
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert "version_conflict" in results
    assert counts(env) == tuple(a + b for a, b in zip(before, (0, 1, 1), strict=True))
    assert len(env.actions.get(state["proposal_id"], env.actor)["followups"]) == 1


def test_duplicate_followup_computation_commits_one_exact_receipt(env):
    state = executed(env)
    barrier = threading.Barrier(2)
    env.service.compute_hook = lambda: barrier.wait(timeout=10)
    before = counts(env)
    def run():
        return env.actions.followup(state["proposal_id"], {
            "action_id": "followup-a", "dataset_id": "followup-good"}, env.actor)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert results[0] == results[1] and results[0]["version"] == 4
    assert counts(env) == tuple(a + b for a, b in zip(before, (0, 1, 1), strict=True))


def test_existing_request_models_are_revalidated(env):
    state = approved(env)
    request = ExecuteRequest(**execution_request(state))
    request.observed_at_ms = True
    before = counts(env)
    fails("invalid_request", env.actions.confirm_execution, state["proposal_id"], request, env.actor)
    assert counts(env) == before


def test_receipt_cannot_substitute_a_different_owner_proposal(env):
    state = propose(env)
    second = propose(env, "propose-b")
    accepted = env.actions.review(state["proposal_id"], review_request(state), env.actor)
    other = env.actions.review(second["proposal_id"], review_request(second, "review-b"), env.actor)
    with env.store.transaction() as db:
        db.execute("UPDATE receipts SET result_json=? WHERE owner=? AND action_id=?",
                   (canonical(other), env.actor.actor_id, "review-a"))
    before = counts(env)
    fails("receipt_integrity", env.actions.review, state["proposal_id"], review_request(state), env.actor)
    assert env.actions.get(state["proposal_id"], env.actor) == accepted
    assert counts(env) == before


def test_followup_permission_revoked_during_computation_cannot_commit(env):
    state = executed(env)
    env.service.compute_hook = lambda: change_identity(env, plants=["plant-b"])
    before = counts(env)
    fails("identity_changed", env.actions.followup, state["proposal_id"],
          {"action_id": "followup-a", "dataset_id": "followup-good"}, env.actor)
    assert counts(env) == before
    with env.store.connect() as db:
        row = db.execute("SELECT version,state_json FROM proposals WHERE proposal_id=?", (state["proposal_id"],)).fetchone()
    assert row["version"] == 3 and json.loads(row["state_json"])["status"] == "executed"
