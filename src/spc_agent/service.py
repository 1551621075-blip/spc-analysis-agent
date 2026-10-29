from __future__ import annotations

import json
import sqlite3
from copy import deepcopy

from .domain import Dataset
from .errors import DomainError
from .schemas import (
    BaselineRequest,
    EvaluateRequest,
    ReviewRequest,
    canonical,
    digest,
)
from .statistics import analyze, build_baseline, validate_baseline
from .storage import new_id, now


class SPCService:
    """Statistics create immutable facts; reviews authorize use, never change the facts."""

    def __init__(self, store):
        self.store = store

    def import_dataset(self, value, actor):
        dataset = Dataset.model_validate(value)
        return self.store.put_dataset(dataset.model_dump(mode="json"), actor)

    def _owned(self, table, key, identity, actor, db):
        if (table, key) not in {
            ("baselines", "baseline_id"), ("evaluations", "evaluation_id"), ("risks", "risk_id"),
        }:
            raise ValueError("Unknown internal object type")
        self.store.current_actor(actor, db)
        row = db.execute(f"SELECT * FROM {table} WHERE {key}=? AND owner=?",
                         (identity, actor.actor_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Object not found", 404)
        self.store.authorize(actor, row["plant_id"], db=db)
        return row

    def _payload(self, row):
        value = self.store.read_artifact(json.loads(row["payload_ref"]))
        if "content_hash" in row.keys() and digest(value) != row["content_hash"]:
            raise DomainError("integrity", "Stored object hash mismatch", 409)
        return value

    def get_baseline(self, baseline_id, actor, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.get_baseline(baseline_id, actor, connection)
        row = self._owned("baselines", "baseline_id", baseline_id, actor, db)
        payload = self._payload(row)
        try:
            validate_baseline(payload)
        except DomainError:
            raise DomainError("integrity", "Stored baseline validation failed", 409) from None
        review = json.loads(row["review_json"]) if row["review_json"] else None
        if payload.get("baseline_id") != baseline_id or payload.get("plant_id") != row["plant_id"]:
            raise DomainError("integrity", "Baseline identity binding changed", 409)
        if row["status"] == "draft":
            if review is not None:
                raise DomainError("integrity", "Draft baseline contains a review", 409)
        elif row["status"] not in {"approved", "rejected"} or not isinstance(review, dict) or (
            review.get("content_hash") != row["content_hash"] or review.get("decision") != row["status"]
        ):
            raise DomainError("integrity", "Baseline review binding changed", 409)
        return {"baseline_id": baseline_id, "owner": row["owner"], "plant_id": row["plant_id"],
                "payload": payload, "content_hash": row["content_hash"],
                "status": row["status"], "review": review}

    def list_baselines(self, actor):
        with self.store.connect() as db:
            self.store.current_actor(actor, db)
            rows = db.execute("SELECT baseline_id,plant_id FROM baselines WHERE owner=? ORDER BY created_at",
                              (actor.actor_id,)).fetchall()
            return [self.get_baseline(r["baseline_id"], actor, db)
                    for r in rows if r["plant_id"] in actor.plants]

    def create_baseline(self, request, actor):
        request = BaselineRequest.model_validate(request)
        command = {"operation": "baseline_build", **request.model_dump()}
        with self.store.transaction() as db:
            data = self.store.dataset(request.dataset_id, actor, db)
            old = self.store.receipt(db, actor, request.action_id, command)
            if old is not None:
                return old
            identity = new_id("baseline")
            payload = build_baseline(Dataset.model_validate(data), identity)
            reference = self.store.write_artifact("baseline", payload)
            content_hash = digest(payload)
            db.execute("INSERT INTO baselines VALUES(?,?,?,?,?,?,?,?)", (
                identity, actor.actor_id, data["plant_id"], canonical(reference), content_hash,
                "draft", None, now(),
            ))
            result = self.get_baseline(identity, actor, db)
            self.store.audit(db, actor.actor_id, "baseline_built", {
                "content_hash": content_hash, "dataset_id": request.dataset_id,
            }, identity)
            self.store.save_receipt(db, actor, request.action_id, command, result)
            return result

    def review_baseline(self, baseline_id, request, actor):
        request = ReviewRequest.model_validate(request)
        command = {"operation": "baseline_review", "baseline_id": baseline_id, **request.model_dump()}
        with self.store.transaction() as db:
            current = self.get_baseline(baseline_id, actor, db)
            self.store.authorize(actor, current["plant_id"], {"reviewer", "admin"}, db)
            old = self.store.receipt(db, actor, request.action_id, command)
            if old is not None:
                return old
            if current["content_hash"] != request.content_hash:
                raise DomainError("stale_hash", "Review must bind the exact baseline", 409)
            if current["status"] != "draft":
                raise DomainError("immutable_review", "Baseline was already reviewed", 409)
            review = {"decision": request.decision, "note": request.note, "actor_id": actor.actor_id,
                      "content_hash": request.content_hash, "reviewed_at": now(),
                      "scope": "Human review of original synthetic baseline; not production approval"}
            db.execute("UPDATE baselines SET status=?,review_json=? WHERE baseline_id=?", (
                request.decision, canonical(review), baseline_id,
            ))
            result = self.get_baseline(baseline_id, actor, db)
            self.store.audit(db, actor.actor_id, "baseline_reviewed", review, baseline_id)
            self.store.save_receipt(db, actor, request.action_id, command, result)
            return result

    def compute_followup(self, dataset_id, baseline_id, actor, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.compute_followup(dataset_id, baseline_id, actor, connection)
        baseline = self.get_baseline(baseline_id, actor, db)
        if baseline["status"] != "approved":
            raise DomainError("baseline_not_approved", "An explicitly approved baseline is required", 409)
        data = self.store.dataset(dataset_id, actor, db)
        result = analyze(Dataset.model_validate(data), baseline["payload"])
        return {"dataset": data, "analysis": result, "data_hash": digest(data),
                "baseline_hash": baseline["payload"]["baseline_hash"]}

    def get_evaluation(self, evaluation_id, actor, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.get_evaluation(evaluation_id, actor, connection)
        row = self._owned("evaluations", "evaluation_id", evaluation_id, actor, db)
        payload = self._payload(row)
        return {**payload, "content_hash": row["content_hash"]}

    def get_risk(self, risk_id, actor, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.get_risk(risk_id, actor, connection)
        row = self._owned("risks", "risk_id", risk_id, actor, db)
        result = self._payload(row)
        evaluation = self.get_evaluation(result["evaluation_id"], actor, db)
        if evaluation["content_hash"] != result["evaluation_hash"]:
            raise DomainError("integrity", "Risk evaluation binding changed", 409)
        current_data = self.store.dataset(result["dataset_id"], actor, db)
        baseline = self.get_baseline(result["baseline_id"], actor, db)
        if digest(current_data) != result["data_hash"] or baseline["payload"]["baseline_hash"] != result["baseline_hash"]:
            raise DomainError("snapshot_changed", "Risk source snapshot changed", 409)
        return {**result, "analysis": evaluation["analysis"], "dataset": evaluation["dataset"]}

    def list_evaluations(self, actor):
        with self.store.connect() as db:
            self.store.current_actor(actor, db)
            rows = db.execute("SELECT evaluation_id,plant_id FROM evaluations WHERE owner=? ORDER BY created_at",
                              (actor.actor_id,)).fetchall()
            return [self.get_evaluation(r["evaluation_id"], actor, db)
                    for r in rows if r["plant_id"] in actor.plants]

    def list_risks(self, actor):
        with self.store.connect() as db:
            self.store.current_actor(actor, db)
            rows = db.execute("SELECT risk_id,plant_id FROM risks WHERE owner=? ORDER BY created_at",
                              (actor.actor_id,)).fetchall()
            return [self.get_risk(r["risk_id"], actor, db)
                    for r in rows if r["plant_id"] in actor.plants]

    @staticmethod
    def _event_key(data, baseline_hash, actor):
        observations = deepcopy(data)
        observations.pop("dataset_id", None)
        for group in observations.get("groups", []):
            group.pop("group_id", None)
        return digest({"owner": actor.actor_id, "baseline_hash": baseline_hash, "observations": observations})

    def evaluate(self, request, actor):
        request = EvaluateRequest.model_validate(request)
        command = {"operation": "evaluate", **request.model_dump()}
        with self.store.transaction() as db:
            computed = self.compute_followup(request.dataset_id, request.baseline_id, actor, db)
            old = self.store.receipt(db, actor, request.action_id, command)
            if old is not None:
                return old
            row = db.execute("SELECT evaluation_id FROM evaluations WHERE owner=? AND dataset_id=? AND baseline_id=?", (
                actor.actor_id, request.dataset_id, request.baseline_id,
            )).fetchone()
            if row:
                result = self.get_evaluation(row["evaluation_id"], actor, db)
                self.store.save_receipt(db, actor, request.action_id, command, result)
                return result
            data, analysis = computed["dataset"], computed["analysis"]
            evaluation_id = new_id("evaluation")
            risk_id = None
            event_key = self._event_key(data, computed["baseline_hash"], actor)
            duplicate = None
            if analysis["status"] != "stable":
                duplicate = db.execute("SELECT risk_id FROM risks WHERE event_key=?", (event_key,)).fetchone()
                risk_id = duplicate["risk_id"] if duplicate else new_id("risk")
            payload = {"evaluation_id": evaluation_id, "owner": actor.actor_id, "plant_id": data["plant_id"],
                       "dataset_id": request.dataset_id, "baseline_id": request.baseline_id,
                       "baseline_hash": computed["baseline_hash"], "data_hash": computed["data_hash"],
                       "dataset": data, "analysis": analysis, "risk_id": risk_id,
                       "source": "original_synthetic", "created_at": now()}
            reference = self.store.write_artifact("evaluation", payload)
            content_hash = digest(payload)
            try:
                db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)", (
                    evaluation_id, actor.actor_id, data["plant_id"], request.dataset_id, request.baseline_id,
                    canonical(reference), content_hash, now(),
                ))
            except sqlite3.IntegrityError:
                raise DomainError("evaluation_conflict", "Evaluation already exists", 409) from None
            if risk_id and not duplicate:
                risk = {"risk_id": risk_id, "plant_id": data["plant_id"], "process_id": data["process_id"],
                        "characteristic": data["characteristic"], "unit": data["unit"],
                        "dataset_id": request.dataset_id, "baseline_id": request.baseline_id,
                        "data_hash": computed["data_hash"], "baseline_hash": computed["baseline_hash"],
                        "evaluation_id": evaluation_id, "evaluation_hash": content_hash,
                        "classification": "statistical_signal" if analysis["status"] == "signals" else "data_or_applicability",
                        "severity": "investigate", "event_key": event_key,
                        "not_a_root_cause": True, "source": "original_synthetic"}
                risk_ref = self.store.write_artifact("risk", risk)
                db.execute("INSERT INTO risks VALUES(?,?,?,?,?,?,?)", (
                    risk_id, actor.actor_id, data["plant_id"], evaluation_id, event_key, canonical(risk_ref), now(),
                ))
            result = {**payload, "content_hash": content_hash}
            self.store.audit(db, actor.actor_id, "statistics_evaluated", {
                "content_hash": content_hash, "risk_id": risk_id, "status": analysis["status"],
                "deduplicated_risk": bool(duplicate),
            }, evaluation_id)
            self.store.save_receipt(db, actor, request.action_id, command, result)
            return result

    def audit(self, object_id, actor):
        with self.store.connect() as db:
            self.store.current_actor(actor, db)
            # Authorization follows the referenced object, not just the audit actor label.
            found = False
            for table, key in (("baselines", "baseline_id"), ("evaluations", "evaluation_id"), ("risks", "risk_id"),
                               ("proposals", "proposal_id"), ("investigations", "investigation_id")):
                row = db.execute(f"SELECT owner,plant_id FROM {table} WHERE {key}=?", (object_id,)).fetchone()
                if row and row["owner"] == actor.actor_id and row["plant_id"] in actor.plants:
                    found = True
                    break
            if not found:
                raise DomainError("not_found", "Object not found", 404)
            return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in db.execute(
                "SELECT id,actor_id,event,detail_json,created_at FROM audit WHERE object_id=? ORDER BY id", (object_id,),
            )]
