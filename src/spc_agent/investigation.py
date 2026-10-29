from __future__ import annotations

import json
import time
from contextlib import contextmanager, nullcontext
from copy import deepcopy

from pydantic import ValidationError

from .errors import DomainError
from .provider import Action, ModelReply
from .schemas import InvestigationRequest, ReconcileRequest, canonical, digest
from .storage import new_id, now

MAX_CALLS = 10
MAX_TOOLS = 8
MAX_TOKENS = 32000
MAX_SECONDS = 300.0
LEASE_SECONDS = 600.0
READ_TOOLS = {"get_process_context", "get_measurement_context", "get_action_history"}
TERMINAL = {"completed", "escalated", "cancelled"}


class InvestigationService:
    """Bounded evidence investigation after deterministic SPC; no model-written process state."""

    def __init__(self, service, provider):
        self.service, self.store, self.provider = service, service.store, provider

    @contextmanager
    def _transaction(self, state):
        before = deepcopy(state)
        try:
            with self.store.transaction() as db:
                yield db
        except BaseException:
            state.clear()
            state.update(before)
            raise

    def _row(self, identity, actor, db):
        self.store.current_actor(actor, db)
        row = db.execute("SELECT * FROM investigations WHERE investigation_id=? AND owner=?",
                         (identity, actor.actor_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Investigation not found", 404)
        self.store.authorize(actor, row["plant_id"], db=db)
        return row

    def _load(self, identity, actor, db):
        row = self._row(identity, actor, db)
        state = json.loads(row["state_json"])
        if state["version"] != row["version"]:
            raise DomainError("integrity", "Investigation checkpoint version mismatch", 409)
        return row, state

    def get(self, identity, actor, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.get(identity, actor, connection)
        _, state = self._load(identity, actor, db)
        result = {k: v for k, v in state.items() if k not in {"worker", "run_started_at"}}
        result["evidence"] = [self.store.read_artifact(ref) for ref in state["evidence_refs"]]
        result["report"] = self.store.read_artifact(state["report_ref"]) if state["report_ref"] else None
        result["model_calls"] = [dict(row) for row in db.execute(
            "SELECT call_id,status,tokens,error_code FROM calls WHERE investigation_id=? ORDER BY created_at",
            (identity,),
        )]
        return result

    def create(self, request, actor):
        request = InvestigationRequest.model_validate(request)
        command = {"operation": "investigation_create", **request.model_dump()}
        with self.store.transaction() as db:
            risk = self.service.get_risk(request.risk_id, actor, db)
            old = self.store.receipt(db, actor, request.action_id, command)
            if old is not None:
                return old
            identity = new_id("investigation")
            state = {"investigation_id": identity, "owner": actor.actor_id, "plant_id": risk["plant_id"],
                     "risk_id": request.risk_id, "risk_hash": digest(risk), "version": 1,
                     "provider_fingerprint": self.provider.fingerprint,
                     "status": "ready", "calls": 0, "tools": 0, "tokens": 0,
                     "elapsed_s": 0.0, "run_started_at": None, "worker": None,
                     "evidence_refs": [], "completed_tools": [], "pending_call_id": None,
                     "pending_action": None, "report_ref": None, "error": None,
                     "limits": {"calls": MAX_CALLS, "tools": MAX_TOOLS, "tokens": MAX_TOKENS,
                                "seconds": MAX_SECONDS}}
            db.execute("INSERT INTO investigations VALUES(?,?,?,?,?,?,?,?)", (
                identity, actor.actor_id, risk["plant_id"], request.risk_id, canonical(state), 1, None, None,
            ))
            self.store.audit(db, actor.actor_id, "investigation_created", {
                "risk_id": request.risk_id, "risk_hash": state["risk_hash"],
            }, identity)
            result = self.get(identity, actor, db)
            self.store.save_receipt(db, actor, request.action_id, command, result)
            return result

    def _persist(self, state, db):
        version = state["version"]
        stored = {**state, "version": version + 1}
        cursor = db.execute(
            "UPDATE investigations SET state_json=?,version=?,lease_owner=?,lease_until=? "
            "WHERE investigation_id=? AND version=? AND lease_owner IS ?",
            (canonical(stored), version + 1, state["worker"],
             now() + LEASE_SECONDS if state["worker"] else None,
             state["investigation_id"], version, state["worker"]),
        )
        if cursor.rowcount != 1:
            raise DomainError("lease_lost", "A newer worker owns this investigation", 409)
        state["version"] = version + 1

    def _snapshot(self, state, actor, db):
        risk = self.service.get_risk(state["risk_id"], actor, db)
        baseline = self.service.get_baseline(risk["baseline_id"], actor, db)
        if digest(risk) != state["risk_hash"] or baseline["status"] != "approved":
            raise DomainError("snapshot_changed", "Investigation source or baseline changed", 409)
        if self.provider.fingerprint != state["provider_fingerprint"]:
            raise DomainError("provider_changed", "Investigation model changed", 409)
        return risk

    @staticmethod
    def _menu(state):
        result = sorted(READ_TOOLS - set(state["completed_tools"]))
        if {"get_process_context", "get_measurement_context"} <= set(state["completed_tools"]):
            result.append("submit_investigation")
        return result

    def _context(self, state, risk):
        analysis = risk["analysis"]
        stats = {key: analysis[key] for key in ("status", "issues", "capability") if key in analysis}
        hits = [*analysis.get("rule_hits", []), *analysis.get("range_hits", [])]
        summaries = {}
        for hit in hits:
            key = (hit.get("chart"), hit.get("rule"), hit.get("side"))
            if key not in summaries:
                summaries[key] = {"chart": key[0], "rule": key[1], "side": key[2], "count": 0,
                                  "first_index": hit.get("start_index", hit.get("index")),
                                  "last_index": hit.get("end_index", hit.get("index"))}
            summary = summaries[key]
            summary["count"] += 1
            start = hit.get("start_index", hit.get("index"))
            end = hit.get("end_index", hit.get("index"))
            if isinstance(start, int) and (summary["first_index"] is None or start < summary["first_index"]):
                summary["first_index"] = start
            if isinstance(end, int) and (summary["last_index"] is None or end > summary["last_index"]):
                summary["last_index"] = end
        stats["rule_hit_count"] = len(hits)
        stats["rule_summaries"] = list(summaries.values())
        stats["analysis_hash"] = digest(analysis)
        stats["projection_notice"] = (
            "All hits aggregated by chart/rule/side, not raw windows. "
            "Complete immutable analysis remains authoritative; signals are not causes."
        )
        return {"risk": {"classification": risk["classification"], "not_a_root_cause": True,
                         "statistics": stats, "unit": risk["unit"]},
                "available_actions": self._menu(state),
                "evidence": [self.store.read_artifact(ref) for ref in state["evidence_refs"]],
                "remaining": {"calls": MAX_CALLS - state["calls"], "tools": MAX_TOOLS - state["tools"],
                              "tokens": MAX_TOKENS - state["tokens"]}}

    def _content(self, tool, risk, actor, db):
        data = risk["dataset"]
        if tool == "get_process_context":
            return {"plant_id": data["plant_id"], "process_id": data["process_id"],
                    "characteristic": data["characteristic"], "unit": data["unit"],
                    "sampling_seconds": data.get("sampling_seconds"),
                    "group_count": len(data["groups"]),
                    "batches": sorted({g["batch_id"] for g in data["groups"]}),
                    "process_versions": sorted({g.get("process_version", "undeclared") for g in data["groups"]}),
                    "limitation": "Process metadata does not prove a causal mechanism."}
        if tool == "get_measurement_context":
            return {"declared_conditions": {k: v for k, v in data.items() if k not in {
                "dataset_id", "groups", "plant_id", "process_id", "characteristic",
            }}, "gauges": sorted({g["gauge_id"] for g in data["groups"]}),
                    "applicability_issues": risk["analysis"].get("issues", []),
                    "limitation": "A statistical signal is not proof of a gauge fault; MSA and calibration are separate."}
        if tool == "get_action_history":
            rows = db.execute("SELECT proposal_id,state_json FROM proposals WHERE owner=? AND plant_id=? ORDER BY rowid DESC LIMIT 30",
                              (actor.actor_id, risk["plant_id"])).fetchall()
            entries = []
            for row in rows:
                state = json.loads(row["state_json"])
                # Only this exact risk's action history is authorized as current evidence.
                if state.get("risk_id") == risk["risk_id"]:
                    entries.append({"proposal_id": row["proposal_id"], "status": state.get("status"),
                                    "note": "Recorded local action state; not causal evidence."})
            return {"entries": entries, "query": "same-owner-plant-risk-last-30-proposals",
                    "absence_not_proof": True}
        raise DomainError("tool_forbidden", "Tool is not an authorized read operation", 409)

    def _execute(self, state, risk, actor, db):
        action = Action.model_validate(state["pending_action"])
        if action.tool not in self._menu(state):
            raise DomainError("tool_forbidden", "Action is not currently available", 409)
        if state["tools"] >= MAX_TOOLS or state["tokens"] > MAX_TOKENS:
            raise DomainError("budget_exhausted", "Investigation budget exhausted", 409)
        if action.tool in READ_TOOLS:
            if action.arguments:
                raise DomainError("invalid_arguments", "Read tools accept no model arguments", 409)
            evidence = {"evidence_id": new_id("evidence"), "source": action.tool,
                        "investigation_id": state["investigation_id"], "risk_id": state["risk_id"],
                        "risk_hash": state["risk_hash"], "actor_id": actor.actor_id,
                        "content": self._content(action.tool, risk, actor, db)}
            reference = self.store.write_artifact("investigation_evidence", evidence)
            state["evidence_refs"] = [*state["evidence_refs"], reference]
            state["completed_tools"] = [*state["completed_tools"], action.tool]
        else:
            args = action.arguments
            if set(args) != {"summary", "hypotheses", "citations", "recommended_action"}:
                raise DomainError("invalid_submission", "Unexpected investigation fields", 409)
            if not isinstance(args["summary"], str) or not 1 <= len(args["summary"]) <= 500:
                raise DomainError("invalid_submission", "A bounded draft summary is required", 409)
            if not isinstance(args["recommended_action"], str) or not 1 <= len(args["recommended_action"]) <= 240:
                raise DomainError("invalid_submission", "A bounded draft recommendation is required", 409)
            hypotheses = args["hypotheses"]
            if not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= 3 or any(
                not isinstance(h, str) or not 1 <= len(h) <= 160 for h in hypotheses
            ):
                raise DomainError("invalid_submission", "One to three unverified hypotheses required", 409)
            evidence = [self.store.read_artifact(ref) for ref in state["evidence_refs"]]
            issued = {e["evidence_id"] for e in evidence}
            citations = args["citations"]
            if not isinstance(citations, list) or any(not isinstance(c, str) for c in citations) or set(citations) != issued or len(citations) != len(issued):
                raise DomainError("invalid_citations", "Citations must cover exactly the issued evidence", 409)
            report = {"authoritative": {"risk": risk, "evidence": evidence,
                        "conclusion": "Statistical signals and applicability only; no verified root cause."},
                      "untrusted_model_draft": args, "review_status": "unreviewed",
                      "action_created": False, "equipment_operation": False,
                      "limitations": ["Hypotheses and recommendations are not verified facts.",
                                      "Measure review, execution confirmation and follow-up are separate."]}
            state["report_ref"] = self.store.write_artifact("investigation_report", report)
            state["status"] = "completed"
        state["tools"] += 1
        state["pending_action"] = None
        state["pending_call_id"] = None
        self.store.audit(db, actor.actor_id, "investigation_tool", {
            "tool": action.tool, "model_reason_untrusted": action.reason,
        }, state["investigation_id"])
        self._persist(state, db)

    def run(self, identity, actor):
        worker = new_id("worker")
        with self.store.transaction() as db:
            row, state = self._load(identity, actor, db)
            if row["lease_owner"] and row["lease_until"] and row["lease_until"] > now():
                raise DomainError("active_worker", "Investigation is currently running", 409)
            if state["status"] in TERMINAL or state["status"] == "blocked":
                return self.get(identity, actor, db)
            if state["run_started_at"] is not None:
                state["elapsed_s"] += max(0.0, now() - state["run_started_at"])
            state["run_started_at"] = now()
            state["worker"] = worker
            state["status"] = "running"
            db.execute("UPDATE investigations SET lease_owner=? WHERE investigation_id=?", (worker, identity))
            self._persist(state, db)
        start, base = time.monotonic(), state["elapsed_s"]

        def clock():
            state["elapsed_s"] = base + time.monotonic() - start
            # Persisted start is advanced with accumulated monotonic time, so crash accounting is not doubled.
            state["run_started_at"] = now()
            if state["elapsed_s"] >= MAX_SECONDS:
                raise DomainError("budget_exhausted", "Investigation time budget exhausted", 409)

        try:
            with self.provider.session() if hasattr(self.provider, "session") else nullcontext():
                while state["status"] == "running":
                    with self._transaction(state) as db:
                        risk = self._snapshot(state, actor, db)
                        clock()
                        if state["pending_call_id"] and state["pending_action"] is None:
                            raise DomainError("model_unknown", "Pending model result requires reconciliation", 409)
                        if state["pending_action"] is not None:
                            self._execute(state, risk, actor, db)
                            clock()
                            self._persist(state, db)
                            continue
                        if state["calls"] >= MAX_CALLS or state["tokens"] >= MAX_TOKENS or state["tools"] >= MAX_TOOLS:
                            raise DomainError("budget_exhausted", "Investigation budget exhausted", 409)
                        context = self._context(state, risk)
                        reference = self.store.write_artifact("model_context", context)
                        call_id = new_id("call")
                        state["pending_call_id"] = call_id
                        state["calls"] += 1
                        db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)", (
                            call_id, identity, canonical(reference), None, "started", None, None, now(),
                        ))
                        self._persist(state, db)
                    usage = None
                    dispatched = False
                    try:
                        clock()
                        dispatched = True
                        reply = self.provider.decision(context)
                        usage = getattr(reply, "tokens", None)
                        if isinstance(reply, dict):
                            usage = reply.get("tokens")
                        reply = ModelReply.model_validate(reply)
                    except Exception as exc:
                        usage = getattr(exc, "reported_tokens", usage) if dispatched else 0
                        valid_usage = type(usage) is int and 0 <= usage <= 100000
                        with self._transaction(state) as db:
                            if valid_usage:
                                state["tokens"] += usage
                            state["elapsed_s"] = base + time.monotonic() - start
                            state["run_started_at"] = now()
                            db.execute("UPDATE calls SET status=?,tokens=?,error_code=? WHERE call_id=?", (
                                "failed" if valid_usage else "unknown", usage if valid_usage else None,
                                exc.code if isinstance(exc, DomainError) else "provider_invalid", call_id,
                            ))
                            if valid_usage:
                                state["pending_call_id"] = None
                            self._persist(state, db)
                        failure_code = "provider_invalid" if valid_usage else "model_unknown"
                        if not dispatched and isinstance(exc, DomainError):
                            failure_code = exc.code
                        raise DomainError(failure_code, "Local model failed; no automatic retry", 502) from None
                    with self._transaction(state) as db:
                        # A completed call is accounted under the worker fence even if its actor was
                        # revoked while the model ran. The next tool boundary rechecks authorization.
                        state["tokens"] += reply.tokens
                        state["pending_action"] = reply.action.model_dump()
                        response_ref = self.store.write_artifact("model_reply", reply.model_dump())
                        db.execute("UPDATE calls SET status='completed',tokens=?,response_ref=? WHERE call_id=?",
                                   (reply.tokens, canonical(response_ref), call_id))
                        state["elapsed_s"] = base + time.monotonic() - start
                        state["run_started_at"] = now()
                        self._persist(state, db)
        except (DomainError, ValidationError) as exc:
            with self._transaction(state) as db:
                code = exc.code if isinstance(exc, DomainError) else "invalid_action"
                state["status"] = "blocked" if state["pending_call_id"] and state["pending_action"] is None else "escalated"
                if state["status"] == "blocked":
                    db.execute("UPDATE calls SET status='unknown' WHERE call_id=? AND investigation_id=? AND status='started'",
                               (state["pending_call_id"], identity))
                state["error"] = {"code": code, "message": "Investigation stopped at an explicit boundary."}
                self.store.audit(db, actor.actor_id, "investigation_stopped", state["error"], identity)
                self._persist(state, db)
        finally:
            with self.store.transaction() as db:
                row = db.execute("SELECT lease_owner,version FROM investigations WHERE investigation_id=?", (identity,)).fetchone()
                if row and row["lease_owner"] == worker and row["version"] == state["version"]:
                    state["elapsed_s"] = base + time.monotonic() - start
                    state["run_started_at"] = None
                    state["worker"] = None
                    state["version"] += 1
                    db.execute("UPDATE investigations SET state_json=?,version=?,lease_owner=NULL,lease_until=NULL WHERE investigation_id=? AND lease_owner=?", (
                        canonical(state), state["version"], identity, worker,
                    ))
        return self.get(identity, actor)

    def cancel(self, identity, actor):
        with self.store.transaction() as db:
            row, state = self._load(identity, actor, db)
            if row["lease_owner"] and row["lease_until"] and row["lease_until"] > now():
                raise DomainError("active_worker", "Cannot cancel an active worker", 409)
            if state["status"] in TERMINAL:
                return self.get(identity, actor, db)
            db.execute("UPDATE investigations SET lease_owner=NULL,lease_until=NULL WHERE investigation_id=?", (identity,))
            state["worker"] = None
            state["status"] = "cancelled"
            self._persist(state, db)
            self.store.audit(db, actor.actor_id, "investigation_cancelled", {"unknown_call_not_retried": True}, identity)
            return self.get(identity, actor, db)

    def reconcile(self, identity, request, actor):
        request = ReconcileRequest.model_validate(request)
        command = {"operation": "reconcile", "investigation_id": identity, **request.model_dump()}
        with self.store.transaction() as db:
            row, state = self._load(identity, actor, db)
            self.store.authorize(actor, state["plant_id"], {"admin"}, db)
            old = self.store.receipt(db, actor, request.action_id, command)
            if old is not None:
                return old
            if row["lease_owner"] and row["lease_until"] and row["lease_until"] > now():
                raise DomainError("active_worker", "Cannot reconcile an active worker", 409)
            if state["status"] != "blocked" or state["pending_call_id"] != request.call_id:
                raise DomainError("not_pending", "Call is not awaiting reconciliation", 409)
            call = db.execute("SELECT * FROM calls WHERE call_id=? AND investigation_id=?", (request.call_id, identity)).fetchone()
            if call is None or call["status"] not in {"started", "unknown"}:
                raise DomainError("not_pending", "Call is not unknown", 409)
            known = call["tokens"] or 0
            total = request.tokens
            conservative = total is None
            if total is None:
                total = max(known, known + MAX_TOKENS - state["tokens"])
            if total < known:
                raise DomainError("usage_reduction", "Recorded token usage cannot be reduced", 409)
            state["tokens"] += total - known
            state["pending_call_id"] = None
            state["pending_action"] = None
            state["status"] = "ready"
            state["error"] = None
            state["worker"] = None
            state["run_started_at"] = None
            db.execute("UPDATE investigations SET lease_owner=NULL,lease_until=NULL WHERE investigation_id=?", (identity,))
            db.execute("UPDATE calls SET status='reconciled_no_result',tokens=? WHERE call_id=?", (total, request.call_id))
            self._persist(state, db)
            self.store.audit(db, actor.actor_id, "model_reconciled", {
                "call_id": request.call_id, "tokens": total, "conservative_usage": conservative,
                "note": request.note, "human_attestation_not_external_proof": True,
            }, identity)
            result = self.get(identity, actor, db)
            self.store.save_receipt(db, actor, request.action_id, command, result)
            return result
