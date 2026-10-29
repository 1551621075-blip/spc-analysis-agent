"""Real SQLite/artifact/fixture/statistics regression tests; no production inputs."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from spc_agent.errors import DomainError
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.schemas import canonical, digest
from spc_agent.service import SPCService
from spc_agent.storage import Store


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path / "store")
    actor = store.authenticate(store.provision_user("owner", "admin", ["synthetic-plant"]))
    service = SPCService(store)
    data = service.import_dataset(baseline_dataset(), actor)
    baseline = service.create_baseline({"action_id": "freeze", "dataset_id": data["dataset_id"]}, actor)
    return service, actor, baseline


def approve(service, actor, baseline, **changes):
    request = {"action_id": "approve", "content_hash": baseline["content_hash"],
               "decision": "approved", "note": "Explicit human fixture review", **changes}
    return service.review_baseline(baseline["baseline_id"], request, actor)


def evaluate(service, actor, baseline, data, action="evaluate"):
    imported = service.import_dataset(data, actor)
    return service.evaluate({"action_id": action, "dataset_id": imported["dataset_id"],
                             "baseline_id": baseline["baseline_id"]}, actor)


def error_code(code, call):
    with pytest.raises(DomainError) as caught:
        call()
    assert caught.value.code == code


def test_freeze_requires_human_review_and_cannot_reestimate(system):
    service, actor, baseline = system
    assert baseline["status"] == "draft"
    assert baseline["review"] is None
    assert baseline["payload"]["group_count"] == 25
    assert baseline["payload"]["subgroup_size"] == 5
    normal = demo_datasets()["normal"]
    error_code("baseline_not_approved", lambda: evaluate(service, actor, baseline, normal))
    assert service.list_evaluations(actor) == service.list_risks(actor) == []
    approved = approve(service, actor, baseline)
    assert approved["review"]["actor_id"] == actor.actor_id
    assert approved["review"]["content_hash"] == baseline["content_hash"]
    assert approve(service, actor, baseline) == approved
    error_code("immutable_review", lambda: approve(service, actor, baseline, action_id="again"))
    for name in ("normal", "mean_shift", "variance"):
        result = evaluate(service, actor, baseline, demo_datasets()[name], name)
        assert result["analysis"]["control_limits"]["center"] == baseline["payload"]["center"]
    assert service.get_baseline(baseline["baseline_id"], actor) == approved


@pytest.mark.parametrize("scenario,status", [
    ("normal", "stable"), ("mean_shift", "signals"), ("variance", "signals"),
    ("missing", "not_evaluable"), ("msa_failed", "not_evaluable"),
    ("calibration_expired", "not_evaluable"),
])
def test_actual_engine_risk_and_capability_boundaries(system, scenario, status):
    service, actor, baseline = system
    approve(service, actor, baseline)
    result = evaluate(service, actor, baseline, demo_datasets()[scenario])
    assert result["analysis"]["status"] == status
    if status == "stable":
        assert result["risk_id"] is None
        assert service.list_risks(actor) == []
        assert result["analysis"]["capability"]["cpk"] > 0
    else:
        risk = service.get_risk(result["risk_id"], actor)
        assert risk["classification"] == (
            "statistical_signal" if status == "signals" else "data_or_applicability")
        assert risk["analysis"] == result["analysis"]
        assert risk["evaluation_hash"] == result["content_hash"]
        assert risk["baseline_hash"] == baseline["payload"]["baseline_hash"]
        assert risk["not_a_root_cause"] is True
        assert result["analysis"]["capability"]["cpk"] is None
        assert result["analysis"]["capability"]["ppk"] is None


def test_missing_msa_never_imports_as_success_and_specs_only_gate_capability(system):
    service, actor, baseline = system
    approve(service, actor, baseline)
    raw = demo_datasets()["normal"].model_dump(mode="json")
    raw.pop("msa")
    with pytest.raises(ValidationError):
        service.import_dataset(raw, actor)
    assert service.list_evaluations(actor) == []
    raw["msa"] = {"status": "unknown"}
    result = evaluate(service, actor, baseline, raw)
    assert result["analysis"]["status"] == "not_evaluable"
    assert "MSA_UNKNOWN" in {i["code"] for i in result["analysis"]["issues"]}
    raw = demo_datasets()["normal"].model_dump(mode="json")
    raw.update(dataset_id="no-specifications", specifications=None)
    result = evaluate(service, actor, baseline, raw, "no-specifications")
    assert result["analysis"]["status"] == "stable"
    assert result["risk_id"] is None
    assert result["analysis"]["capability"]["cpk"] is None
    assert "SPECIFICATIONS_MISSING" in result["analysis"]["capability"]["reasons"]


def test_review_hash_and_action_content_conflicts_are_atomic(system):
    service, actor, baseline = system
    error_code("stale_hash", lambda: approve(service, actor, baseline, content_hash="0" * 64))
    assert service.get_baseline(baseline["baseline_id"], actor) == baseline
    approved = approve(service, actor, baseline)
    error_code("idempotency_conflict", lambda: approve(service, actor, baseline, note="Changed human review note"))
    assert service.get_baseline(baseline["baseline_id"], actor) == approved
    evaluate(service, actor, baseline, demo_datasets()["normal"])
    error_code("idempotency_conflict", lambda: evaluate(service, actor, baseline, demo_datasets()["mean_shift"]))
    assert len(service.list_evaluations(actor)) == 1
    assert service.list_risks(actor) == []


def test_owner_plant_role_and_revocation_checked_before_receipt(system):
    service, actor, baseline = system
    store = service.store
    other = store.authenticate(store.provision_user("other", "admin", actor.plants))
    error_code("not_found", lambda: service.get_baseline(baseline["baseline_id"], other))
    error_code("not_found", lambda: approve(service, other, baseline))
    foreign = baseline_dataset().model_dump(mode="json")
    foreign.update(dataset_id="foreign", plant_id="foreign-plant")
    error_code("forbidden", lambda: service.import_dataset(foreign, actor))
    operator = store.authenticate(store.provision_user("operator", "operator", actor.plants))
    foreign.update(dataset_id="operator-data", plant_id=actor.plants[0])
    data = service.import_dataset(foreign, operator)
    draft = service.create_baseline({"dataset_id": data["dataset_id"], "action_id": "freeze"}, operator)
    error_code("forbidden", lambda: approve(service, operator, draft))
    approve(service, actor, baseline)
    store.set_user(actor.actor_id, other, enabled=False, plants=actor.plants, role="admin")
    error_code("identity_changed", lambda: approve(service, actor, baseline))
    error_code("identity_changed", lambda: service.list_baselines(actor))


def test_rename_deduplicates_risk_and_restart_keeps_immutable_evaluation(system):
    service, actor, baseline = system
    approve(service, actor, baseline)
    data = demo_datasets()["mean_shift"].model_dump(mode="json")
    first = evaluate(service, actor, baseline, data)
    expected = deepcopy(first)
    first["analysis"]["status"] = "model-invented-success"
    first["dataset"]["groups"][0]["values"][0] = 900
    restarted = SPCService(Store(service.store.root))
    assert restarted.get_evaluation(first["evaluation_id"], actor) == expected
    assert evaluate(restarted, actor, baseline, data) == expected
    assert evaluate(restarted, actor, baseline, data, "another-action") == expected
    renamed = deepcopy(data)
    renamed["dataset_id"] = "renamed"
    for index, group in enumerate(renamed["groups"]):
        group["group_id"] = f"renamed-{index}"
    second = evaluate(restarted, actor, baseline, renamed, "renamed")
    assert second["evaluation_id"] != expected["evaluation_id"]
    assert second["risk_id"] == expected["risk_id"]
    assert len(restarted.list_risks(actor)) == 1
    assert len(restarted.list_evaluations(actor)) == 2
    renamed["groups"][0]["values"][0] += 0.01
    error_code("dataset_conflict", lambda: service.import_dataset(renamed, actor))


@pytest.mark.parametrize("tamper", ["bytes", "reference"])
def test_baseline_artifact_cannot_change_after_approval(system, tamper):
    service, actor, baseline = system
    approve(service, actor, baseline)
    with service.store.transaction() as db:
        import json
        ref = json.loads(db.execute("SELECT payload_ref FROM baselines").fetchone()[0])
        if tamper == "bytes":
            (service.store.artifacts / (ref["sha256"] + ".json")).write_text("{}")
        else:
            changed = deepcopy(baseline["payload"])
            changed["center"] += 1
            ref = service.store.write_artifact("baseline", changed)
            db.execute("UPDATE baselines SET payload_ref=?", (canonical(ref),))
    error_code("artifact_integrity" if tamper == "bytes" else "integrity",
               lambda: evaluate(service, actor, baseline, demo_datasets()["normal"]))
    assert service.list_evaluations(actor) == []
    assert digest(baseline["payload"]) == baseline["content_hash"]
