"""Real-store investigation boundaries; only inference and crash/clock edges are faked."""
import json
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from test_service import approve, error_code, evaluate
from test_service import system as system

import spc_agent.investigation as module
from spc_agent.investigation import InvestigationService
from spc_agent.provider import Action, LocalProvider, ModelReply, ProviderFailure, SyntheticProvider
from spc_agent.schemas import canonical, digest
from spc_agent.service import SPCService
from spc_agent.storage import Store


class FakeProvider:
    fingerprint = "fixed-test-model"

    def __init__(self, decide=None):
        self.contexts = []
        self.decide = decide or SyntheticProvider().decision

    def decision(self, context):
        self.contexts.append(deepcopy(context))
        return self.decide(context)


def reply(tool="get_measurement_context", tokens=37, arguments=None):
    return ModelReply(action=Action(tool=tool, arguments=arguments or {}, reason="Test issued tool"),
                      tokens=tokens)


def draft(context):
    return {"summary": "Unverified association; statistics remain authoritative.",
            "hypotheses": ["Measurement context requires verification."],
            "citations": [e["evidence_id"] for e in context["evidence"]],
            "recommended_action": "Human review only; no measure executed."}


@pytest.fixture
def investigation(system):
    service, actor, baseline = system
    approve(service, actor, baseline)
    from spc_agent.fixtures import demo_datasets
    evaluation = evaluate(service, actor, baseline, demo_datasets()["mean_shift"])
    provider = FakeProvider()
    inv = InvestigationService(service, provider)
    state = inv.create({"action_id": "investigate", "risk_id": evaluation["risk_id"]}, actor)
    return inv, actor, state["investigation_id"]


def checkpoint(inv, identity, **changes):
    """Seed already-accounted budgets or crash checkpoints, never replace the real store."""
    with inv.store.transaction() as db:
        state = json.loads(db.execute("SELECT state_json FROM investigations WHERE investigation_id=?",
                                      (identity,)).fetchone()[0])
        state.update(changes)
        db.execute("UPDATE investigations SET state_json=? WHERE investigation_id=?",
                   (canonical(state), identity))
    return state


def restart(inv, provider=None):
    return InvestigationService(SPCService(Store(inv.store.root)), provider or inv.provider)


@pytest.mark.parametrize("order", ["measurement-first", "process-first"])
def test_actual_statistics_to_report_reordered_reads_exact_citations(investigation, order):
    inv, actor, identity = investigation
    inv.provider.decide = SyntheticProvider(order).decision
    before = inv.service.get_risk(inv.get(identity, actor)["risk_id"], actor)
    result = inv.run(identity, actor)
    assert result["status"] == "completed"
    tools = [e["source"] for e in result["evidence"]]
    assert tools[:2] == (["get_measurement_context", "get_process_context"] if
                         order == "measurement-first" else
                         ["get_process_context", "get_measurement_context"])
    assert all("submit_investigation" not in c["available_actions"] for c in inv.provider.contexts[:2])
    assert "submit_investigation" in inv.provider.contexts[2]["available_actions"]
    report = result["report"]
    assert report["authoritative"]["risk"] == before
    analysis = before["analysis"]
    hits = analysis["rule_hits"] + analysis.get("range_hits", [])
    counts = Counter((h["chart"], h["rule"], h["side"]) for h in hits)
    for context in inv.provider.contexts:
        stats = context["risk"]["statistics"]
        assert stats["analysis_hash"] == digest(analysis)
        assert stats["rule_hit_count"] == len(hits)
        assert {(s["chart"], s["rule"], s["side"]): s["count"]
                for s in stats["rule_summaries"]} == counts
        assert {k: stats[k] for k in ("status", "issues", "capability")} == {
            k: analysis[k] for k in ("status", "issues", "capability")}
        assert "rule_hits" not in stats and "range_hits" not in stats
    assert report["authoritative"]["evidence"] == result["evidence"]
    assert report["review_status"] == "unreviewed"
    assert report["action_created"] is report["equipment_operation"] is False
    assert set(report["untrusted_model_draft"]["citations"]) == {e["evidence_id"] for e in result["evidence"]}
    assert result["calls"] == result["tools"] == len(tools) + 1
    assert result["tokens"] == result["calls"] * 128
    assert all(c["status"] == "completed" for c in result["model_calls"])
    assert restart(inv).run(identity, actor) == result
    assert len(inv.provider.contexts) == result["calls"]
    assert inv.service.get_risk(before["risk_id"], actor) == before
    with inv.store.connect() as db:
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0


@pytest.mark.parametrize("ordering", ["forward", "reverse", "empty"])
def test_projection_preserves_every_rule_side_count_bounds_and_source(investigation, ordering):
    inv, actor, identity = investigation
    state = inv.get(identity, actor)
    risk = inv.service.get_risk(state["risk_id"], actor)
    categories = [("xbar", rule, side) for rule, sides in (
        ("R1", ("upper", "lower")), ("R2", ("upper", "lower")),
        ("R3", ("ascending", "descending")), ("R4", ("alternating",)),
        ("R5", ("upper", "lower")), ("R6", ("upper", "lower")),
        ("R7", ("inside",)), ("R8", ("both",)),
    ) for side in sides] + [("r", "R_LIMIT", side) for side in ("upper", "lower")]
    all_hits, expected = [], {}
    for i, (chart, rule, side) in enumerate(categories):
        common = {"chart": chart, "rule": rule, "side": side,
                  "window": [3.14] * 15, "group_ids": ["RAW-WINDOW-ID"],
                  "start_timestamp_ms": 1767225600000}
        entries = [dict(common, start_index=i + 10, end_index=i + 20),
                   dict(common, start_index=i + 2, end_index=i + 6),
                   dict(common, index=i + 30)]
        if i % 2:
            entries.append(dict(common, index=i + 15))
        all_hits.extend(entries)
        expected[(chart, rule, side)] = (len(entries), i + 2, i + 30)
    if ordering == "empty":
        all_hits, expected = [], {}
    elif ordering == "reverse":
        all_hits.reverse()
    analysis = risk["analysis"]
    # Split a category across both inputs to check aggregation is not limited to rule_hits.
    analysis["rule_hits"], analysis["range_hits"] = all_hits[:-2], all_hits[-2:]
    before = deepcopy(risk)
    for completed, menu in (
        ([], ["get_action_history", "get_measurement_context", "get_process_context"]),
        (["get_measurement_context"], ["get_action_history", "get_process_context"]),
        (["get_process_context", "get_measurement_context"],
         ["get_action_history", "submit_investigation"]),
    ):
        state["completed_tools"] = completed
        before_state = deepcopy(state)
        context = inv._context(state, risk)
        stats = context["risk"]["statistics"]
        summaries = stats["rule_summaries"]
        assert all(set(s) == {"chart", "rule", "side", "count", "first_index", "last_index"}
                   for s in summaries)
        keys = [(s["chart"], s["rule"], s["side"]) for s in summaries]
        assert keys == list(dict.fromkeys((h["chart"], h["rule"], h["side"]) for h in all_hits))
        assert dict(zip(keys, [(s["count"], s["first_index"], s["last_index"])
                               for s in summaries], strict=True)) == expected
        assert stats["rule_hit_count"] == len(all_hits) == sum(s["count"] for s in summaries)
        assert stats["analysis_hash"] == digest(before["analysis"])
        assert all(stats[k] == before["analysis"][k] for k in ("status", "issues", "capability"))
        assert "All hits aggregated" in stats["projection_notice"]
        assert "Complete immutable analysis remains authoritative" in stats["projection_notice"]
        assert set(stats) == {"status", "issues", "capability", "rule_hit_count",
                              "rule_summaries", "analysis_hash", "projection_notice"}
        assert "RAW-WINDOW-ID" not in canonical(context)
        assert context["available_actions"] == menu
        assert context["evidence"] == []
        assert context["remaining"] == {"calls": 10, "tools": 8, "tokens": 32000}
        assert risk == before and state == before_state


@pytest.mark.parametrize("invalid", ["early", "duplicate-tool", "read-args", "missing", "duplicate", "foreign", "stats", "approval", "measure"])
def test_backend_rejects_unissued_tools_citations_and_model_writes(investigation, invalid):
    inv, actor, identity = investigation

    def decide(context):
        if invalid == "early":
            return reply("submit_investigation", arguments=draft(context))
        if invalid == "read-args":
            return reply(arguments={"risk_id": "another-risk"})
        if invalid == "duplicate-tool":
            return reply()
        if "submit_investigation" not in context["available_actions"]:
            return SyntheticProvider().decision(context)
        args = draft(context)
        if invalid == "missing":
            args["citations"].pop()
        elif invalid == "duplicate":
            args["citations"].append(args["citations"][0])
        elif invalid == "foreign":
            args["citations"][0] = "evidence-issued-by-another-task"
        else:
            args[{"stats": "cpk", "approval": "approved", "measure": "execute"}[invalid]] = True
        return reply("submit_investigation", arguments=args)

    inv.provider.decide = decide
    result = inv.run(identity, actor)
    assert result["status"] == "escalated"
    assert result["report"] is None
    expected = ("tool_forbidden" if invalid in {"early", "duplicate-tool"} else
                "invalid_arguments" if invalid == "read-args" else
                "invalid_citations" if invalid in {"missing", "duplicate", "foreign"} else
                "invalid_submission")
    assert result["error"]["code"] == expected
    assert result["tokens"] == sum(c["tokens"] for c in result["model_calls"])
    restart(inv).run(identity, actor)
    assert len(inv.provider.contexts) == result["calls"]


@pytest.mark.parametrize("usage", [37, 0, None])
def test_invalid_reply_usage_is_charged_without_retry(investigation, usage):
    inv, actor, identity = investigation
    inv.provider.decide = lambda _: {"tokens": usage, "action": {"tool": "execute_equipment"}}
    result = inv.run(identity, actor)
    assert result["calls"] == 1
    assert result["tools"] == 0
    assert result["tokens"] == (usage or 0)
    assert result["status"] == ("blocked" if usage is None else "escalated")
    assert result["model_calls"][0]["status"] == ("unknown" if usage is None else "failed")
    assert bool(result["pending_call_id"]) == (usage is None)
    assert restart(inv).run(identity, actor) == result
    assert len(inv.provider.contexts) == 1


@pytest.mark.parametrize("tokens", [None, 0, 42])
def test_unknown_requires_explicit_reconcile_omitted_is_conservative(investigation, tokens):
    inv, actor, identity = investigation

    def unknown(_):
        raise ProviderFailure("provider_unknown", "Read timeout")

    inv.provider.decide = unknown
    blocked = inv.run(identity, actor)
    assert blocked["status"] == "blocked"
    assert blocked["error"]["code"] == "model_unknown"
    assert blocked["calls"] == len(inv.provider.contexts) == 1
    assert blocked["model_calls"][0]["status"] == "unknown"
    assert blocked["model_calls"][0]["tokens"] is None
    assert blocked["model_calls"][0]["error_code"] == "provider_unknown"
    assert blocked["tokens"] == blocked["tools"] == 0
    assert blocked["pending_call_id"] == blocked["model_calls"][0]["call_id"]
    inv = restart(inv)
    assert inv.run(identity, actor) == blocked
    request = {"action_id": "reconcile", "call_id": blocked["pending_call_id"],
               "no_result": True, "note": "Human attests no result received"}
    if tokens is not None:
        request["tokens"] = tokens
    reconciled = inv.reconcile(identity, request, actor)
    assert reconciled["tokens"] == (32000 if tokens is None else tokens)
    assert reconciled["status"] == "ready"
    assert reconciled["pending_call_id"] is None
    assert inv.reconcile(identity, request, actor) == reconciled
    error_code("idempotency_conflict", lambda: inv.reconcile(identity, dict(request, tokens=99), actor))
    inv.provider.decide = SyntheticProvider().decision
    result = inv.run(identity, actor)
    assert result["status"] == ("escalated" if tokens is None else "completed")
    assert len(inv.provider.contexts) == (1 if tokens is None else 4)
    assert result["model_calls"][0]["status"] == "reconciled_no_result"
    events = inv.service.audit(identity, actor)
    event = next(e["detail"] for e in events if e["event"] == "model_reconciled")
    assert event["conservative_usage"] is (tokens is None)
    assert event["tokens"] == reconciled["tokens"]


def test_reconciliation_is_owner_admin_scoped_and_cancellation_never_retries(investigation):
    inv, actor, identity = investigation
    inv.provider.decide = lambda _: {"tokens": None, "action": {}}
    blocked = inv.run(identity, actor)
    request = {"action_id": "reconcile-owner", "call_id": blocked["pending_call_id"],
               "no_result": True, "tokens": 0, "note": "Human attests no result received"}
    other = inv.store.authenticate(inv.store.provision_user("second-admin", "admin", actor.plants))
    error_code("not_found", lambda: inv.reconcile(identity, request, other))
    inv.store.set_user(actor.actor_id, other, enabled=True, plants=actor.plants, role="reviewer")
    reviewer = actor.model_copy(update={"role": "reviewer", "acl_version": 2})
    error_code("forbidden", lambda: inv.reconcile(identity, request, reviewer))
    cancelled = inv.cancel(identity, reviewer)
    assert cancelled["status"] == "cancelled"
    assert cancelled["model_calls"][0]["status"] == "unknown"
    assert cancelled["pending_call_id"] == blocked["pending_call_id"]
    assert restart(inv).run(identity, reviewer) == cancelled
    assert len(inv.provider.contexts) == 1


@pytest.mark.parametrize("counter,used", [("calls", 10), ("tools", 8), ("tokens", 32000), ("elapsed_s", 300)])
def test_each_budget_independently_prevents_dispatch(investigation, counter, used):
    inv, actor, identity = investigation
    assert inv.get(identity, actor)["limits"] == {"calls": 10, "tools": 8, "tokens": 32000, "seconds": 300.0}
    checkpoint(inv, identity, **{counter: used})
    result = inv.run(identity, actor)
    assert result["status"] == "escalated"
    assert result["error"]["code"] == "budget_exhausted"
    assert inv.provider.contexts == []
    assert result["model_calls"] == []


@pytest.mark.parametrize("last_tokens,status", [(31744, "completed"), (31745, "escalated")])
@pytest.mark.parametrize("near_limits", [False, True])
def test_last_submit_may_exactly_reach_token_limit(investigation, last_tokens, status, near_limits):
    inv, actor, identity = investigation
    if near_limits:
        checkpoint(inv, identity, calls=7, tools=5)

    def decide(context):
        if "submit_investigation" in context["available_actions"]:
            return reply("submit_investigation", last_tokens, draft(context))
        return SyntheticProvider().decision(context)

    inv.provider.decide = decide
    result = inv.run(identity, actor)
    assert result["status"] == status
    assert result["tokens"] == last_tokens + 256
    assert result["calls"] == (10 if near_limits else 3)
    assert result["tools"] == (5 if near_limits else 0) + (3 if status == "completed" else 2)
    assert len(inv.provider.contexts) == 3
    assert (result["report"] is not None) == (status == "completed")


def test_monotonic_deadline_beats_backward_wall_clock(investigation, monkeypatch):
    inv, actor, identity = investigation
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(module, "now", lambda: 1000 - clock.value)

    def slow(_):
        clock.value = 300.0
        return reply()

    inv.provider.decide = slow
    result = inv.run(identity, actor)
    assert result["status"] == "escalated"
    assert result["error"]["code"] == "budget_exhausted"
    assert result["elapsed_s"] == 300.0
    assert result["tokens"] == 37
    assert result["evidence"] == []
    assert len(inv.provider.contexts) == 1


@pytest.mark.parametrize("artifact", ["investigation_evidence", "investigation_report"])
def test_time_spent_publishing_tools_cannot_commit_past_deadline(investigation, monkeypatch, artifact):
    inv, actor, identity = investigation
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    write = inv.store.write_artifact

    def slow_write(kind, value):
        reference = write(kind, value)
        if kind == artifact:
            clock.value = 300.0
        return reference

    monkeypatch.setattr(inv.store, "write_artifact", slow_write)
    result = inv.run(identity, actor)
    assert result["status"] == "escalated"
    assert result["error"]["code"] == "budget_exhausted"
    assert result["report_ref"] is result["report"] is None
    assert result["tools"] == len(result["evidence"]) == (0 if artifact == "investigation_evidence" else 2)
    assert result["tokens"] == result["calls"] * 128


def test_deadline_before_dispatch_is_known_zero_not_unknown(investigation, monkeypatch):
    inv, actor, identity = investigation
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    write = inv.store.write_artifact

    def slow_write(kind, value):
        ref = write(kind, value)
        if kind == "model_context":
            clock.value = 300.0
        return ref

    monkeypatch.setattr(inv.store, "write_artifact", slow_write)
    result = inv.run(identity, actor)
    assert inv.provider.contexts == [], "Inference was never dispatched"
    assert result["status"] == "escalated", "Known unsent call must not demand human reconciliation"
    assert result["error"]["code"] == "budget_exhausted"
    assert result["pending_call_id"] is None
    assert result["calls"] == len(result["model_calls"]) == 1
    assert result["tokens"] == result["tools"] == 0
    assert result["model_calls"][0]["status"] == "failed"
    assert result["model_calls"][0]["tokens"] == 0
    assert result["model_calls"][0]["error_code"] == "budget_exhausted"
    assert restart(inv).run(identity, actor) == result
    assert inv.provider.contexts == []


class Crash(BaseException):
    pass


def inject_crash(monkeypatch, inv, stage, after_commit):
    original = inv.store.transaction
    armed = True

    @contextmanager
    def transaction():
        nonlocal armed
        crash = False
        with original() as db:
            yield db
            rows = db.execute("SELECT state_json FROM investigations").fetchall()
            if armed and rows:
                state = json.loads(rows[0][0])
                crash = bool(state["pending_action"] if stage == "reply" else state["evidence_refs"])
            if crash:
                armed = False
                if not after_commit:
                    raise Crash("before commit")
        if crash:
            raise Crash("after commit")

    monkeypatch.setattr(inv.store, "transaction", transaction)


@pytest.mark.parametrize("after_commit", [False, True])
def test_reply_commit_crash_resumes_pending_action_without_recall(investigation, monkeypatch, after_commit):
    inv, actor, identity = investigation
    inject_crash(monkeypatch, inv, "reply", after_commit)
    with pytest.raises(Crash):
        inv.run(identity, actor)
    inv = restart(inv)
    # A committed checkpoint may retain the crashed worker's lease until its expiry.
    with inv.store.transaction() as db:
        db.execute("UPDATE investigations SET lease_until=0")
    result = inv.run(identity, actor)
    assert result["status"] == ("completed" if after_commit else "blocked")
    assert len(inv.provider.contexts) == (3 if after_commit else 1)
    assert result["calls"] == (3 if after_commit else 1)
    assert len(result["evidence"]) == (2 if after_commit else 0)
    if not after_commit:
        assert result["pending_action"] is None
        assert result["pending_call_id"] is not None
        assert result["model_calls"][0]["status"] == "unknown", "Crash recovery must persist started-to-unknown transition"


@pytest.mark.parametrize("after_commit", [False, True])
def test_evidence_commit_crash_leaves_no_orphan_citation(investigation, monkeypatch, after_commit):
    inv, actor, identity = investigation
    inject_crash(monkeypatch, inv, "evidence", after_commit)
    with pytest.raises(Crash):
        inv.run(identity, actor)
    inv = restart(inv)
    with inv.store.transaction() as db:
        db.execute("UPDATE investigations SET lease_until=0")
    state = inv.get(identity, actor)
    assert len(state["evidence"]) == int(after_commit)
    assert state["tools"] == int(after_commit)
    result = inv.run(identity, actor)
    assert result["status"] == "completed"
    assert result["calls"] == 3
    assert len(inv.provider.contexts) == 3
    assert len(result["evidence"]) == len({e["evidence_id"] for e in result["evidence"]}) == 2
    assert all(len(c["evidence"]) == i for i, c in enumerate(inv.provider.contexts))
    with inv.store.connect() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='investigation_tool'").fetchone()[0] == 3


def test_db_lease_and_cas_fence_stale_worker(investigation):
    inv, actor, identity = investigation
    rival = restart(inv)

    def competing(_):
        error_code("active_worker", lambda: rival.run(identity, actor))
        error_code("active_worker", lambda: rival.cancel(identity, actor))
        with inv.store.transaction() as db:
            row, state = inv._load(identity, actor, db)
            inv._persist(state, db)
            stale = deepcopy(state)
            inv._persist(state, db)
            error_code("lease_lost", lambda: inv._persist(stale, db))
            state["worker"] = "new-worker"
            state["version"] += 1
            db.execute("UPDATE investigations SET state_json=?,version=?,lease_owner=? WHERE investigation_id=?",
                       (canonical(state), state["version"], "new-worker", identity))
        return reply()

    inv.provider.decide = competing
    error_code("lease_lost", lambda: inv.run(identity, actor))
    with inv.store.connect() as db:
        row = db.execute("SELECT * FROM investigations").fetchone()
        state = json.loads(row["state_json"])
        assert row["lease_owner"] == state["worker"] == "new-worker"
        assert row["version"] == state["version"]
        assert state["tokens"] == state["tools"] == 0
        assert state["evidence_refs"] == []
        assert db.execute("SELECT status FROM calls").fetchone()[0] == "started"


@pytest.mark.parametrize("change", ["risk", "baseline-status", "provider", "actor"])
def test_scope_changes_stop_before_model_dispatch(investigation, change):
    inv, actor, identity = investigation
    original = inv.get(identity, actor)
    if change == "provider":
        inv.provider.fingerprint = "other-model"
    elif change == "actor":
        other = inv.store.authenticate(inv.store.provision_user("stranger", "admin", actor.plants))
        error_code("not_found", lambda: inv.run(identity, other))
        error_code("not_found", lambda: inv.get(identity, other))
        assert inv.provider.contexts == []
        return
    else:
        with inv.store.transaction() as db:
            if change == "baseline-status":
                db.execute("UPDATE baselines SET status='rejected'")
            else:
                ref = json.loads(db.execute("SELECT payload_ref FROM risks").fetchone()[0])
                risk = inv.store.read_artifact(ref)
                risk["classification"] = "changed-risk"
                ref = inv.store.write_artifact("risk", risk)
                db.execute("UPDATE risks SET payload_ref=?", (canonical(ref),))
    result = inv.run(identity, actor)
    assert result["status"] == "escalated"
    expected = {"provider": "provider_changed", "baseline-status": "integrity",
                "risk": "snapshot_changed"}
    assert result["error"]["code"] == expected[change]
    assert result["risk_hash"] == original["risk_hash"]
    assert inv.provider.contexts == []


def test_live_actor_plant_loss_blocks_cached_reads_and_run(investigation):
    inv, actor, identity = investigation
    token = inv.store.provision_user("acl-admin", "admin", [*actor.plants, "new-plant"])
    admin = inv.store.authenticate(token)
    inv.store.set_user(actor.actor_id, admin, enabled=True, plants=["new-plant"], role="admin")
    error_code("identity_changed", lambda: inv.run(identity, actor))
    error_code("identity_changed", lambda: inv.get(identity, actor))
    refreshed = actor.model_copy(update={"plants": ["new-plant"], "acl_version": 2})
    error_code("forbidden", lambda: inv.run(identity, refreshed))
    assert inv.provider.contexts == []


def test_real_issued_citation_cannot_cross_investigations(investigation):
    inv, actor, identity = investigation
    first = inv.run(identity, actor)
    request = {"action_id": "other-investigate", "risk_id": first["risk_id"]}
    second = inv.create(request, actor)
    assert inv.create(request, actor) == second
    alien_id = first["evidence"][0]["evidence_id"]

    def decide(context):
        if "submit_investigation" not in context["available_actions"]:
            return SyntheticProvider().decision(context)
        args = draft(context)
        assert alien_id not in args["citations"]
        args["citations"][0] = alien_id
        return reply("submit_investigation", arguments=args)

    inv.provider.decide = decide
    result = inv.run(second["investigation_id"], actor)
    assert result["status"] == "escalated"
    assert result["error"]["code"] == "invalid_citations"
    assert all(e["investigation_id"] == second["investigation_id"] for e in result["evidence"])
    assert all(e["actor_id"] == actor.actor_id and e["risk_hash"] == second["risk_hash"]
               for e in result["evidence"])
    assert inv.get(identity, actor) == first


def test_runtime_error_after_tool_mutation_rolls_back_python_checkpoint(investigation, monkeypatch):
    inv, actor, identity = investigation
    audit = inv.store.audit
    failed = False

    def fail_audit(db, actor_id, event, detail, object_id=None):
        nonlocal failed
        if event == "investigation_tool" and not failed:
            failed = True
            raise RuntimeError("After artifact publish, before checkpoint commit")
        return audit(db, actor_id, event, detail, object_id)

    monkeypatch.setattr(inv.store, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="After artifact publish"):
        inv.run(identity, actor)
    inv = restart(inv)
    state = inv.get(identity, actor)
    assert state["tools"] == 0
    assert state["evidence_refs"] == state["completed_tools"] == []
    assert state["pending_action"]["tool"] == "get_measurement_context"
    assert state["calls"] == 1 and state["tokens"] == 128
    result = inv.run(identity, actor)
    assert result["status"] == "completed"
    assert result["calls"] == len(inv.provider.contexts) == 3
    assert len(result["evidence"]) == 2
    assert [len(c["evidence"]) for c in inv.provider.contexts] == [0, 1, 2]


def test_restart_preserves_elapsed_budget_after_crash(investigation, monkeypatch):
    inv, actor, identity = investigation
    checkpoint(inv, identity, elapsed_s=290.0, run_started_at=1000.0, status="running")
    with inv.store.transaction() as db:
        db.execute("UPDATE investigations SET lease_owner='dead-worker',lease_until=1001")
    monkeypatch.setattr(module, "now", lambda: 1010.0)
    result = restart(inv).run(identity, actor)
    assert result["status"] == "escalated"
    assert result["error"]["code"] == "budget_exhausted"
    assert result["elapsed_s"] >= 300
    assert inv.provider.contexts == []


@pytest.mark.parametrize("rehash", [True, False])
def test_approved_baseline_replacement_must_not_cross_risk_snapshot(investigation, rehash):
    inv, actor, identity = investigation
    with inv.store.transaction() as db:
        baseline = db.execute("SELECT * FROM baselines").fetchone()
        payload = inv.store.read_artifact(json.loads(baseline["payload_ref"]))
        payload["msa"]["study_id"] = "different-but-valid-study"
        if rehash:
            payload["baseline_hash"] = digest({k: v for k, v in payload.items() if k != "baseline_hash"})
        ref = inv.store.write_artifact("baseline", payload)
        db.execute("UPDATE baselines SET payload_ref=?,content_hash=?",
                   (canonical(ref), digest(payload)))
    result = inv.run(identity, actor)
    assert result["status"] == "escalated", "Changed approved baseline must not produce a completed report"
    assert result["error"]["code"] == "integrity"
    assert result["report"] is None
    assert inv.provider.contexts == []


def test_revoke_during_reply_still_accounts_known_usage(investigation):
    inv, actor, identity = investigation
    admin = inv.store.authenticate(inv.store.provision_user("revoker", "admin", actor.plants))

    def revoke(_):
        inv.store.set_user(actor.actor_id, admin, enabled=False, plants=actor.plants, role="admin")
        return reply(tokens=37)

    inv.provider.decide = revoke
    error_code("identity_changed", lambda: inv.run(identity, actor))
    with inv.store.connect() as db:
        state = json.loads(db.execute("SELECT state_json FROM investigations").fetchone()[0])
        call = db.execute("SELECT * FROM calls").fetchone()
    assert state["tools"] == 0
    assert state["evidence_refs"] == []
    assert state["tokens"] == call["tokens"] == 37, "Known response usage is not erased by ACL revocation"
    assert len(inv.provider.contexts) == 1


@pytest.mark.parametrize("failure,status,tokens", [
    ("invalid-action", "escalated", 41), ("bad-envelope", "escalated", 41),
    ("connect", "escalated", 0), ("read", "blocked", 0),
    ("body", "blocked", 0), ("oversize", "blocked", 0),
])
def test_actual_http_provider_failures_account_without_retry(investigation, monkeypatch, failure, status, tokens):
    inv, actor, identity = investigation
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "connect":
            raise httpx.ConnectError("Mock connection failure", request=request)
        if failure == "read":
            raise httpx.ReadTimeout("Mock unknown outcome", request=request)
        if failure == "body":
            return httpx.Response(200, content=b"not-json")
        if failure == "oversize":
            return httpx.Response(200, content=b" " * 128001)
        body = {"usage": {"total_tokens": 41}, "choices": []}
        if failure == "invalid-action":
            body["choices"] = [{"finish_reason": "stop", "message": {"content": '{"tool":"execute"}'}}]
        return httpx.Response(200, json=body)

    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    provider = LocalProvider("http://127.0.0.1:9999", "mock-model")
    # Fingerprint is bound at creation, not silently swapped for the existing task.
    inv = InvestigationService(inv.service, provider)
    state = inv.create({"action_id": "http-investigate", "risk_id": inv.get(identity, actor)["risk_id"]}, actor)
    result = inv.run(state["investigation_id"], actor)
    assert result["status"] == status
    assert result["tokens"] == tokens
    assert result["calls"] == 1
    assert result["tools"] == 0
    assert result["report"] is None
    restart(inv).run(state["investigation_id"], actor)
    assert len(calls) == 1
