"""Actual SPC lifecycle acceptance on newly generated original synthetic data.

The deterministic provider is explicitly TEST ONLY; --runtime-config performs real
local model calls. Expected scenario outcomes are never supplied to the model.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from spc_agent.actions import ActionService
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.investigation import InvestigationService
from spc_agent.managed_provider import ManagedLocalProvider
from spc_agent.provider import SyntheticProvider
from spc_agent.schemas import digest
from spc_agent.service import SPCService
from spc_agent.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test-provider", action="store_true")
    mode.add_argument("--runtime-config", type=Path)
    args = parser.parse_args()
    root = args.data_root
    if root.exists() or root.is_symlink():
        parser.error("Acceptance requires a new, nonexistent data directory")
    root.mkdir(parents=True, mode=0o700)
    start = time.monotonic()
    result = {"status": "running", "synthetic_data": True, "model_mode": "TEST ONLY" if args.test_provider else "real-local",
              "cases": [], "investigation": None, "lifecycle": None}

    def save():
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        (root / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

    try:
        provider = SyntheticProvider() if args.test_provider else ManagedLocalProvider(args.runtime_config)
        result["provider_fingerprint"] = provider.fingerprint
        store = Store(root / "data")
        actor = store.authenticate(store.provision_user("local-reviewer", "admin", ["synthetic-plant"]))
        service, fixtures = SPCService(store), demo_datasets()
        baseline_data = baseline_dataset()
        service.import_dataset(baseline_data.model_dump(mode="json"), actor)
        for dataset in fixtures.values():
            service.import_dataset(dataset.model_dump(mode="json"), actor)
        baseline = service.create_baseline({"action_id": "build", "dataset_id": baseline_data.dataset_id}, actor)
        assert baseline["status"] == "draft"
        baseline = service.review_baseline(baseline["baseline_id"], {
            "action_id": "review", "content_hash": baseline["content_hash"], "decision": "approved",
            "note": "Explicit review of original synthetic baseline only; no physical approval.",
        }, actor)
        baseline_hash = digest(baseline["payload"])
        expectations = {"normal": "stable", "mean_shift": "signals", "variance": "signals", "trend": "signals",
                        "mixed_batch": "not_evaluable", "gauge_shift": "signals", "missing": "not_evaluable",
                        "msa_failed": "not_evaluable", "calibration_expired": "not_evaluable"}
        shift = None
        for name, expected in expectations.items():
            evaluated = service.evaluate({"action_id": "evaluate-" + name, "dataset_id": fixtures[name].dataset_id,
                                          "baseline_id": baseline["baseline_id"]}, actor)
            assert evaluated["analysis"]["status"] == expected, name
            assert bool(evaluated["risk_id"]) == (expected != "stable"), name
            result["cases"].append({"fixture": name, "status": expected,
                                    "capability": evaluated["analysis"]["capability"]["status"],
                                    "rules": len(evaluated["analysis"]["rule_hits"]), "checks_passed": True})
            if name == "mean_shift":
                shift = evaluated
            save()
        assert shift is not None
        before = digest(service.get_evaluation(shift["evaluation_id"], actor))
        investigations = InvestigationService(service, provider)
        task = investigations.create({"action_id": "investigate", "risk_id": shift["risk_id"]}, actor)
        task = investigations.run(task["investigation_id"], actor)
        result["investigation"] = {key: task[key] for key in (
            "investigation_id", "status", "calls", "tools", "tokens", "elapsed_s", "error", "model_calls",
        )}
        save()
        assert task["status"] == "completed", task["error"]
        assert all(call["status"] == "completed" and call["tokens"] > 0 for call in task["model_calls"])
        assert task["report"]["authoritative"]["risk"]["analysis"] == shift["analysis"]
        assert digest(service.get_evaluation(shift["evaluation_id"], actor)) == before
        assert not task["report"]["equipment_operation"] and not task["report"]["action_created"]
        assert investigations.run(task["investigation_id"], actor) == task
        result["investigation"]["checks_passed"] = True
        actions = ActionService(service)
        proposal = actions.propose({"action_id": "propose", "risk_id": shift["risk_id"],
                                    "description": "Original synthetic observation: review setup and measurement records safely.",
                                    "expected_result": "A separate later observation window may meet the declared SPC criteria."}, actor)
        proposal = actions.review(proposal["proposal_id"], {"action_id": "approve-action", "content_hash": proposal["content_hash"],
                                  "decision": "approved", "note": "Explicit original synthetic demonstration approval; not an equipment command."}, actor)
        end = max(g.timestamp_ms for g in fixtures["mean_shift"].groups)
        proposal = actions.confirm_execution(proposal["proposal_id"], {"action_id": "confirm-execution",
                    "proposal_hash": proposal["content_hash"], "observed_at_ms": end + 1000,
                    "confirmed": True, "note": "Explicit ORIGINAL SYNTHETIC execution observation, not physical work."}, actor)
        followup = fixtures["followup"]
        proposal = actions.followup(proposal["proposal_id"], {"action_id": "observe-followup", "dataset_id": followup.dataset_id}, actor)
        assert proposal["status"] == "followup_recorded", proposal["followups"][-1]["payload"]["blockers"]
        proposal = actions.close(proposal["proposal_id"], {"action_id": "close", "followup_hash": proposal["followups"][-1]["followup_hash"],
                                 "note": "Qualified original synthetic follow-up explicitly reviewed; no causal claim."}, actor)
        assert proposal["status"] == "closed"
        assert digest(service.get_baseline(baseline["baseline_id"], actor)["payload"]) == baseline_hash
        result["lifecycle"] = {"status": proposal["status"], "followup_eligible": True,
                               "source": "explicit original synthetic human attestations", "causal_effect_proven": False,
                               "checks_passed": True}
        result["status"] = "passed"
    except BaseException as exc:
        result["status"] = "failed"
        result["failure"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        raise
    finally:
        save()
    print(json.dumps({"status": result["status"], "cases": len(result["cases"]),
                      "model_mode": result["model_mode"], "elapsed_seconds": result["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
