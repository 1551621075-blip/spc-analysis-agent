from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from spc_agent.errors import DomainError
from spc_agent.schemas import Actor, canonical, digest
from spc_agent.storage import MAX_JSON_BYTES, Store, new_id, now


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


@pytest.fixture
def identities(store):
    definitions = {
        "operator": ("operator", ["plant-a", "plant-b"]),
        "other": ("operator", ["plant-a"]),
        "reviewer": ("reviewer", ["plant-a"]),
        "admin": ("admin", ["plant-a", "plant-b"]),
        "limited-admin": ("admin", ["plant-a"]),
        "outside": ("operator", ["plant-c"]),
    }
    tokens = {name: store.provision_user(name, role, plants)
              for name, (role, plants) in definitions.items()}
    return tokens, {name: store.authenticate(token) for name, token in tokens.items()}


def dataset_value(dataset_id="dataset-1", plant_id="plant-a"):
    # Original tiny JSON fixtures: this suite exercises storage, not SPC validation.
    return {"dataset_id": dataset_id, "plant_id": plant_id, "process_id": "synthetic-process",
            "characteristic": "Synthetic length", "unit": "mm", "source": "original_synthetic",
            "sampling_seconds": 60, "groups": [{"values": [1.0, 2.0, 3.0, 4.0, 5.0]}]}


def assert_error(code, function, *args, status=None, **kwargs):
    with pytest.raises(DomainError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status
    return caught.value


def test_exact_schema_and_connection_settings(store):
    expected = {
        "users": "actor_id token_hash role plants acl_version enabled",
        "datasets": "dataset_id owner plant_id payload_ref content_hash created_at",
        "baselines": "baseline_id owner plant_id payload_ref content_hash status review_json created_at",
        "evaluations": "evaluation_id owner plant_id dataset_id baseline_id payload_ref content_hash created_at",
        "risks": "risk_id owner plant_id evaluation_id event_key payload_ref created_at",
        "proposals": "proposal_id owner plant_id risk_id state_json version",
        "investigations": "investigation_id owner plant_id risk_id state_json version lease_owner lease_until",
        "calls": "call_id investigation_id context_ref response_ref status tokens error_code created_at",
        "receipts": "owner action_id request_hash result_json",
        "audit": "id object_id actor_id event detail_json created_at",
    }
    integer_fields = {"acl_version", "enabled", "version", "tokens", "id"}
    nullable = {"review_json", "lease_owner", "lease_until", "response_ref", "tokens", "error_code",
                "object_id", "id"}
    for _ in range(2):
        with store.connect() as db:
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA trusted_schema").fetchone()[0] == 0
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            for table, names in expected.items():
                columns = db.execute(f"PRAGMA table_info({table})").fetchall()
                assert [c["name"] for c in columns] == names.split()
                for column in columns:
                    name = column["name"]
                    expected_type = "TEXT"
                    if name in integer_fields:
                        expected_type = "INTEGER"
                    elif name in {"created_at", "lease_until"}:
                        expected_type = "REAL"
                    assert column["type"] == expected_type
                    assert bool(column["notnull"]) == (name not in nullable)
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
        db.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?)",
                   ("orphan", "missing-owner", "plant-a", "{}", "hash", now()))


def test_schema_relationships_and_uniqueness(store, identities):
    actor = identities[1]["operator"]
    store.put_dataset(dataset_value(), actor)
    with store.transaction() as db:
        db.execute("INSERT INTO baselines VALUES(?,?,?,?,?,?,?,?)",
                   ("baseline-1", actor.actor_id, "plant-a", "{}", "hash", "draft", None, now()))
        db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)",
                   ("evaluation-1", actor.actor_id, "plant-a", "dataset-1", "baseline-1", "{}", "hash", now()))
        db.execute("INSERT INTO risks VALUES(?,?,?,?,?,?,?)",
                   ("risk-1", actor.actor_id, "plant-a", "evaluation-1", "event-1", "{}", now()))
        db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                   ("proposal-1", actor.actor_id, "plant-a", "risk-1", "{}", 1))
        db.execute("INSERT INTO investigations VALUES(?,?,?,?,?,?,?,?)",
                   ("investigation-1", actor.actor_id, "plant-a", "risk-1", "{}", 1, None, None))
        db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)",
                   ("call-1", "investigation-1", "{}", None, "pending", None, None, now()))
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
        db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)",
                   ("evaluation-2", actor.actor_id, "plant-a", "dataset-1", "baseline-1", "{}", "hash", now()))
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
        db.execute("INSERT INTO risks VALUES(?,?,?,?,?,?,?)",
                   ("risk-2", actor.actor_id, "plant-a", "evaluation-1", "event-1", "{}", now()))
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as db:
        db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                   ("proposal-null", actor.actor_id, "plant-a", "risk-1", None, 1))


def test_tokens_are_hash_only_and_users_are_insert_only(store, identities):
    tokens, actors = identities
    with store.connect() as db:
        rows = db.execute("SELECT * FROM users").fetchall()
        raw_dump = "\n".join(db.iterdump())
    for row in rows:
        token = tokens[row["actor_id"]]
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in raw_dump
        for path in store.root.iterdir():
            if path.is_file():
                assert token.encode() not in path.read_bytes()
    assert_error("user_exists", store.provision_user, "operator", "admin", ["plant-a"], status=409)
    assert store.authenticate(tokens["operator"]) == actors["operator"]
    for invalid in (None, "", "short", "x" * 201, "x" * 40, "\ud800" * 32):
        assert_error("unauthorized", store.authenticate, invalid, status=401)


@pytest.mark.parametrize(("actor_id", "role", "plants"), [
    ("../user", "operator", ["plant-a"]), ("user", "owner", ["plant-a"]),
    ("user", "operator", []), ("user", "operator", ["plant-a", "plant-a"]),
    ("user", "operator", ["../plant"]), ("user", "operator", "plant-a"),
])
def test_invalid_user_input(store, actor_id, role, plants):
    assert_error("invalid_user", store.provision_user, actor_id, role, plants)


def test_authorization_uses_database_identity_not_caller_claims(store, identities):
    actors = identities[1]
    actor = actors["operator"]
    assert store.authorize(actor, "plant-a", ["operator"]) == actor
    assert_error("forbidden", store.authorize, actor, "plant-c", status=403)
    assert_error("forbidden", store.authorize, actor, "plant-a", ["reviewer"], status=403)
    for update in ({"role": "admin"}, {"plants": ["plant-c"]}, {"acl_version": 2}):
        forged = actor.model_copy(update=update)
        assert_error("identity_changed", store.current_actor, forged, status=403)
        assert_error("identity_changed", store.list_datasets, forged, status=403)
    assert_error("identity_changed", store.current_actor, actor.model_dump(), status=403)
    missing = Actor(actor_id="missing", role="admin", plants=["plant-a"], acl_version=1)
    assert_error("identity_changed", store.current_actor, missing, status=403)
    assert_error("forbidden", store.set_user, "other", actor,
                 enabled=True, plants=["plant-a"], role="admin", status=403)


def test_admin_scope_revocation_and_acl_version(store, identities):
    tokens, actors = identities
    actor = actors["operator"]
    for target, plants in (("operator", ["plant-a"]), ("other", ["plant-a", "plant-b"]),
                           ("outside", ["plant-a"]), ("missing", ["plant-a"])):
        assert_error("forbidden", store.set_user, target, actors["limited-admin"],
                     enabled=True, plants=plants, role="operator", status=403)
    assert_error("invalid_user", store.set_user, "other", actors["admin"],
                 enabled="false", plants=["plant-a"], role="operator")
    store.set_user("operator", actors["admin"], enabled=True, plants=["plant-a"], role="reviewer")
    updated = store.authenticate(tokens["operator"])
    assert (updated.role, updated.plants, updated.acl_version) == ("reviewer", ["plant-a"], 2)
    assert_error("identity_changed", store.current_actor, actor, status=403)
    store.set_user("operator", actors["admin"], enabled=False, plants=["plant-a"], role="reviewer")
    assert_error("unauthorized", store.authenticate, tokens["operator"], status=401)
    assert_error("identity_changed", store.current_actor, updated, status=403)
    store.set_user("operator", actors["admin"], enabled=True, plants=["plant-a"], role="reviewer")
    assert store.authenticate(tokens["operator"]).acl_version == 4
    assert_error("identity_changed", store.current_actor, updated, status=403)


def test_transaction_rollback_and_audit_position_last(store, identities):
    actor = identities[1]["operator"]

    class Interrupted(BaseException):
        pass

    with pytest.raises(Interrupted), store.transaction() as db:
        store.audit(db, actor.actor_id, "test_event", {"safe": True}, "object-1")
        store.save_receipt(db, actor, "action-1", {}, {})
        raise Interrupted
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
        assert store.receipt(db, actor, "action-1", {}) is None
    with store.transaction() as db:
        store.audit(db, actor.actor_id, "test_event", {"safe": True}, "object-2")
        store.save_receipt(db, actor, "action-1", {}, {})
    with store.connect() as db:
        row = db.execute("SELECT * FROM audit").fetchone()
        assert row["object_id"] == "object-2"
        assert row["actor_id"] == actor.actor_id
        assert json.loads(row["detail_json"]) == {"safe": True}
        assert isinstance(row["created_at"], float)
        assert store.receipt(db, actor, "action-1", {}) == {}


@pytest.mark.parametrize("result", [{}, [], False, 0, "", {"accepted": True}])
def test_receipts_preserve_falsey_results_and_are_immutable(store, identities, result):
    actor = identities[1]["operator"]
    request = {"a": 1, "b": 2}
    with store.transaction() as db:
        assert store.receipt(db, actor, "action-1", request) is None
        store.save_receipt(db, actor, "action-1", request, result)
        actual = store.receipt(db, actor, "action-1", {"b": 2, "a": 1})
        assert actual == result and type(actual) is type(result)
        store.save_receipt(db, actor, "action-1", request, result)
        assert_error("idempotency_conflict", store.receipt, db, actor, "action-1", {"a": 3}, status=409)
        assert_error("idempotency_conflict", store.save_receipt, db, actor,
                     "action-1", request, {"different": True}, status=409)
        other = identities[1]["other"]
        assert store.receipt(db, other, "action-1", request) is None
        store.save_receipt(db, other, "action-1", request, {"different": True})
        assert_error("invalid_receipt", store.save_receipt, db, actor, "null-action", {}, None)
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2


def test_receipt_rechecks_identity_and_rejects_invalid_json(store, identities):
    actor = identities[1]["operator"]
    with store.transaction() as db:
        store.save_receipt(db, actor, "action-1", {}, {"ok": True})
        assert_error("invalid_json", store.receipt, db, actor, "action-2", {"x": float("nan")})
        assert_error("invalid_json", store.save_receipt, db, actor,
                     "action-2", {}, {"x": float("inf")})
        db.execute("UPDATE receipts SET result_json='NaN' WHERE owner=?", (actor.actor_id,))
        assert_error("receipt_integrity", store.receipt, db, actor, "action-1", {}, status=409)
    store.set_user(actor.actor_id, identities[1]["admin"], enabled=False,
                   role="operator", plants=actor.plants)
    with store.connect() as db:
        assert_error("identity_changed", store.receipt, db, actor, "action-1", {}, status=403)
        assert_error("identity_changed", store.save_receipt, db, actor,
                     "action-2", {}, {"ok": True}, status=403)


def test_artifact_content_addressing_and_no_overwrite(store):
    value = {"测量": [1, 2, 3], "valid": True}
    before = now()
    reference = store.write_artifact("dataset", value)
    content = canonical({"kind": "dataset", "value": value}).encode()
    assert reference == {"sha256": hashlib.sha256(content).hexdigest(),
                         "kind": "dataset", "bytes": len(content)}
    path = store.artifacts / (reference["sha256"] + ".json")
    info = path.stat()
    assert path.read_bytes() == content
    assert info.st_mode & 0o777 == 0o600
    assert store.read_artifact(reference) == value
    assert store.write_artifact("dataset", value) == reference
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (info.st_ino, info.st_mtime_ns)
    assert not list(store.artifacts.glob(".pending-*"))
    assert now() >= before
    assert new_id("dataset") != new_id("dataset")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                   {"v": float("nan")}, {1: "integer-key"}, {"v": (1, 2)},
                                   {"v": b"bytes"}, "\ud800"])
def test_artifact_rejects_non_json_and_non_finite_values(store, value):
    assert_error("invalid_json", store.write_artifact, "dataset", value)
    assert list(store.artifacts.iterdir()) == []


def test_canonical_rejects_nan_and_size_and_nesting_are_bounded(store):
    with pytest.raises(ValueError):
        canonical({"v": float("nan")})
    overhead = len(canonical({"kind": "dataset", "value": ""}).encode())
    reference = store.write_artifact("dataset", "x" * (MAX_JSON_BYTES - overhead))
    assert reference["bytes"] == MAX_JSON_BYTES
    assert_error("artifact_too_large", store.write_artifact, "dataset",
                 "x" * (MAX_JSON_BYTES - overhead + 1), status=413)
    assert_error("artifact_too_large", store.write_artifact, "dataset",
                 "汉" * (MAX_JSON_BYTES // 3), status=413)
    cycle = []
    cycle.append(cycle)
    assert_error("invalid_json", store.write_artifact, "dataset", cycle)
    # Reject repeated data before building a multi-gigabyte canonical string.
    repeated = ["x" * 100_000] * 100_000
    assert_error("artifact_too_large", store.write_artifact, "dataset", repeated, status=413)
    assert_error("invalid_json", store.write_artifact, "dataset", 10 ** 5000)


@pytest.mark.parametrize("patch", [
    {"sha256": "../outside"}, {"sha256": "a" * 63 + "/"}, {"sha256": "A" * 64},
    {"sha256": 123}, {"sha256": None}, {"bytes": True}, {"bytes": 0},
    {"bytes": MAX_JSON_BYTES + 1}, {"bytes": "12"}, {"kind": "../dataset"},
    {"kind": None}, {"unexpected": "path"},
])
def test_artifact_reference_cannot_escape_or_change_types(store, patch):
    reference = store.write_artifact("dataset", {"a": 1})
    assert_error("artifact_integrity", store.read_artifact, {**reference, **patch}, status=409)


def test_artifact_detects_bytes_kind_hash_and_file_mutations(store):
    reference = store.write_artifact("dataset", {"a": 1})
    for bad in (None, [], "path", {}, {**reference, "bytes": reference["bytes"] + 1},
                {**reference, "kind": "baseline"}, {**reference, "sha256": "0" * 64}):
        assert_error("artifact_integrity", store.read_artifact, bad, status=409)
    path = store.artifacts / (reference["sha256"] + ".json")
    content = path.read_bytes().replace(b'"a":1', b'"a":2')
    path.write_bytes(content)
    assert_error("artifact_integrity", store.read_artifact, reference, status=409)
    assert_error("artifact_integrity", store.write_artifact, "dataset", {"a": 1}, status=409)
    assert path.read_bytes() == content
    assert not list(store.artifacts.glob(".pending-*"))


@pytest.mark.parametrize("content", [b'{"kind":"dataset","value":NaN}',
                                     b'{"kind":"dataset","value":1,"value":2}',
                                     b'{"kind":"dataset"}', b'not-json', b'\xff'])
def test_artifact_rejects_malformed_payload_even_with_matching_hash(store, content):
    sha = hashlib.sha256(content).hexdigest()
    (store.artifacts / (sha + ".json")).write_bytes(content)
    assert_error("artifact_integrity", store.read_artifact,
                 {"sha256": sha, "kind": "dataset", "bytes": len(content)}, status=409)


@pytest.mark.parametrize("file_type", ["symlink", "directory", "fifo", "oversized"])
def test_artifact_rejects_unsafe_existing_files_without_replacing_them(store, tmp_path, file_type):
    value = {"a": 1}
    content = canonical({"kind": "dataset", "value": value}).encode()
    reference = {"sha256": hashlib.sha256(content).hexdigest(), "kind": "dataset", "bytes": len(content)}
    path = store.artifacts / (reference["sha256"] + ".json")
    if file_type == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(content)
        path.symlink_to(outside)
    elif file_type == "directory":
        path.mkdir()
    elif file_type == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as stream:
            stream.truncate(MAX_JSON_BYTES + 1)
    inode = path.lstat().st_ino
    assert_error("artifact_integrity", store.read_artifact, reference, status=409)
    assert_error("artifact_integrity", store.write_artifact, "dataset", value, status=409)
    assert path.lstat().st_ino == inode


def test_artifact_atomic_publication_and_fsync(store, monkeypatch):
    link = os.link
    fsync = os.fsync
    events = []

    def observe_fsync(fd):
        events.append("fsync")
        return fsync(fd)

    def observe_link(source, destination, **kwargs):
        assert events == ["fsync"]
        assert (store.artifacts / source).read_bytes() == canonical(
            {"kind": "dataset", "value": {"a": 1}}).encode()
        assert not (store.artifacts / destination).exists()
        events.append("link")
        return link(source, destination, **kwargs)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(os, "link", observe_link)
    reference = store.write_artifact("dataset", {"a": 1})
    assert events == ["fsync", "link", "fsync"]
    assert store.read_artifact(reference) == {"a": 1}
    assert not list(store.artifacts.glob(".pending-*"))


def test_artifact_failed_publication_cleans_temp_without_publishing(store, monkeypatch):
    def fail_link(*args, **kwargs):
        raise OSError("simulated local link failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="simulated"):
        store.write_artifact("dataset", {"a": 1})
    assert list(store.artifacts.iterdir()) == []


def test_artifact_concurrent_collision_preserves_existing_file(store, monkeypatch):
    link = os.link

    def insert_collision(source, destination, **kwargs):
        (store.artifacts / destination).write_bytes(b"concurrent different content")
        return link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", insert_collision)
    assert_error("artifact_integrity", store.write_artifact, "dataset", {"a": 1}, status=409)
    paths = list(store.artifacts.iterdir())
    assert len(paths) == 1 and paths[0].read_bytes() == b"concurrent different content"


def test_concurrent_identical_artifacts_and_datasets_are_idempotent(store, identities):
    payload = dataset_value()
    actor = identities[1]["operator"]
    with ThreadPoolExecutor(max_workers=6) as executor:
        references = list(executor.map(lambda _: store.write_artifact("dataset", payload), range(12)))
    assert all(reference == references[0] for reference in references)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: store.put_dataset(payload, actor), range(12)))
    assert all(result == results[0] for result in results)
    assert store.read_artifact(references[0]) == payload
    assert len(list(store.artifacts.iterdir())) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='dataset_registered'").fetchone()[0] == 1


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("replacement", ["missing", "regular"])
def test_sqlite_sidecar_unlink_during_lstat_is_rechecked(store, monkeypatch, suffix, replacement):
    path = Path(str(store.database) + suffix)
    path.write_bytes(b"")
    lstat = Path.lstat
    calls = 0

    def unlink_during_lstat(current, *args, **kwargs):
        nonlocal calls
        if current == path:
            calls += 1
            if calls == 1:
                # Darwin can resolve the inode before SQLite unlinks it and
                # return its post-unlink attributes rather than ENOENT.
                with path.open("rb") as stream:
                    path.unlink()
                    unlinked = os.fstat(stream.fileno())
                    assert unlinked.st_nlink == 0
                    if replacement == "regular":
                        path.write_bytes(b"")
                    return unlinked
        return lstat(current, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unlink_during_lstat)
    store._check_database_files()
    assert calls == 2
    # Do not feed the synthetic empty sidecar into SQLite itself.
    path.unlink(missing_ok=True)
    with store.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "directory"])
def test_sqlite_sidecar_zero_link_retry_still_rejects_unsafe_replacement(
        store, tmp_path, monkeypatch, suffix, replacement):
    path = Path(str(store.database) + suffix)
    path.write_bytes(b"")
    target = tmp_path / "untouched-sidecar-target"
    target.write_bytes(b"untouched")
    lstat = Path.lstat
    calls = 0

    def replace_during_lstat(current, *args, **kwargs):
        nonlocal calls
        if current == path:
            calls += 1
            if calls == 1:
                with path.open("rb") as stream:
                    path.unlink()
                    unlinked = os.fstat(stream.fileno())
                    assert unlinked.st_nlink == 0
                    if replacement == "symlink":
                        path.symlink_to(target)
                    elif replacement == "hardlink":
                        os.link(target, path)
                    else:
                        path.mkdir()
                    return unlinked
        return lstat(current, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", replace_during_lstat)
    assert_error("unsafe_storage", store._check_database_files, status=409)
    assert calls == 2
    assert target.read_bytes() == b"untouched"
    assert_error("unsafe_storage", store._check_database_files, status=409)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_sqlite_zero_link_retry_is_bounded_and_not_used_for_primary_database(
        store, tmp_path, monkeypatch, suffix):
    unlinked_path = tmp_path / "unlinked-metadata"
    unlinked_path.write_bytes(b"")
    with unlinked_path.open("rb") as stream:
        unlinked_path.unlink()
        unlinked = os.fstat(stream.fileno())
    assert unlinked.st_nlink == 0
    path = Path(str(store.database) + suffix)
    lstat = Path.lstat
    calls = 0

    def always_unlinked(current, *args, **kwargs):
        nonlocal calls
        if current == path:
            calls += 1
            return unlinked
        return lstat(current, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", always_unlinked)
    assert_error("unsafe_storage", store._check_database_files, status=409)
    assert calls == (3 if suffix else 1)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
def test_sqlite_primary_and_sidecar_links_are_always_rejected(store, tmp_path, suffix, link_type):
    path = Path(str(store.database) + suffix)
    if path.exists():
        path.unlink()
    target = tmp_path / "untouched-database-target"
    target.write_bytes(b"untouched")
    if link_type == "symlink":
        path.symlink_to(target)
    else:
        os.link(target, path)
    assert_error("unsafe_storage", store._check_database_files, status=409)
    with pytest.raises(DomainError, match="Unsafe database file"), store.connect():
        pass
    assert target.read_bytes() == b"untouched"


def test_sqlite_repeated_concurrent_connect_close_preserves_integrity(store):
    token = store.provision_user("concurrent-owner", "operator", ["plant-a"])
    actor = store.authenticate(token)

    def authenticate(_):
        assert store.authenticate(token) == actor

    # Last-connection closes delete WAL/SHM while other threads preflight/open.
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(authenticate, range(128)))
    with store.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("variant", ["root", "parent", "broken", "artifacts", "database", "wal"])
def test_storage_rejects_symlink_roots_parents_and_internal_paths(tmp_path, variant):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "store"
    if variant in {"root", "parent", "broken"}:
        root.symlink_to(outside if variant != "broken" else tmp_path / "missing", target_is_directory=True)
        candidate = root / "nested" if variant == "parent" else root
    else:
        root.mkdir()
        candidate = root
        if variant == "artifacts":
            (root / "artifacts").symlink_to(outside, target_is_directory=True)
        else:
            target = outside / "untouched"
            target.write_bytes(b"untouched")
            name = "spc.sqlite3" if variant == "database" else "spc.sqlite3-wal"
            (root / name).symlink_to(target)
    assert_error("unsafe_storage", Store, candidate, status=409)
    assert not (outside / "nested").exists()
    if (outside / "untouched").exists():
        assert (outside / "untouched").read_bytes() == b"untouched"


def test_storage_allows_relative_root_but_rejects_parent_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(Path("relative-store"))
    assert store.root == (tmp_path / "relative-store").resolve()
    assert store.read_artifact(store.write_artifact("data", {})) == {}
    assert_error("unsafe_storage", Store, tmp_path / "safe" / ".." / "escape", status=409)


def test_replaced_artifact_directory_and_root_are_rechecked(store, tmp_path):
    reference = store.write_artifact("dataset", {})
    artifacts = store.artifacts
    moved = tmp_path / "moved-artifacts"
    artifacts.rename(moved)
    artifacts.symlink_to(moved, target_is_directory=True)
    assert_error("unsafe_storage", store.read_artifact, reference, status=409)
    assert_error("unsafe_storage", store.write_artifact, "dataset", {}, status=409)
    artifacts.unlink()
    moved.rename(artifacts)
    root = store.root
    saved_root = tmp_path / "moved-root"
    root.rename(saved_root)
    root.symlink_to(saved_root, target_is_directory=True)
    assert_error("unsafe_storage", store.read_artifact, reference, status=409)
    with pytest.raises(DomainError, match="symlinks"), store.connect():
        pass


def test_dataset_roundtrip_catalog_and_same_content_idempotency(store, identities):
    actor = identities[1]["operator"]
    payload = dataset_value()
    first = store.put_dataset(payload, actor)
    assert first["dataset_id"] == payload["dataset_id"]
    assert first["plant_id"] == "plant-a"
    assert first["content_hash"] == digest(payload)
    assert first["payload_ref"]["kind"] == "dataset"
    assert first["content_hash"] != first["payload_ref"]["sha256"]
    assert first["group_count"] == 1
    assert first["process_id"] == "synthetic-process"
    assert "owner" not in first and "groups" not in first
    reordered = dict(reversed(list(payload.items())))
    assert store.put_dataset(reordered, actor) == first
    assert store.dataset(payload["dataset_id"], actor) == payload
    with store.connect() as db:
        assert store.dataset(payload["dataset_id"], actor, db) == payload
    assert store.list_datasets(actor) == [first]
    payload["groups"][0]["values"][0] = 100.0
    assert store.dataset(payload["dataset_id"], actor)["groups"][0]["values"][0] == 1.0
    assert_error("dataset_conflict", store.put_dataset, payload, actor, status=409)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='dataset_registered'").fetchone()[0] == 1


def test_dataset_accepts_minimal_json_without_domain_dependency(store, identities):
    payload = {"dataset_id": "minimal", "plant_id": "plant-a"}
    metadata = store.put_dataset(payload, identities[1]["operator"])
    assert set(metadata) == {"dataset_id", "plant_id", "content_hash", "payload_ref", "created_at"}
    assert store.dataset("minimal", identities[1]["operator"]) == payload


@pytest.mark.parametrize("payload", [
    {}, {"dataset_id": "dataset-1"}, {"plant_id": "plant-a"},
    {"dataset_id": "../dataset-1", "plant_id": "plant-a"},
    {"dataset_id": "folder/dataset-1", "plant_id": "plant-a"},
    {"dataset_id": "x" * 81, "plant_id": "plant-a"},
    {"dataset_id": True, "plant_id": "plant-a"},
    {"dataset_id": "dataset-1", "plant_id": "../plant"},
])
def test_dataset_validates_required_safe_identifiers(store, identities, payload):
    assert_error("invalid_identifier", store.put_dataset, payload, identities[1]["operator"])
    assert store.list_datasets(identities[1]["operator"]) == []
    assert list(store.artifacts.iterdir()) == []


def test_dataset_validates_shape_size_finiteness_and_plant(store, identities):
    actor = identities[1]["operator"]
    assert_error("invalid_dataset", store.put_dataset, [], actor)
    assert_error("artifact_too_large", store.put_dataset,
                 {**dataset_value(), "large": "x" * MAX_JSON_BYTES}, actor, status=413)
    assert_error("invalid_json", store.put_dataset,
                 {**dataset_value(), "bad": float("nan")}, actor)
    assert_error("forbidden", store.put_dataset, dataset_value(plant_id="plant-c"), actor, status=403)
    assert list(store.artifacts.iterdir()) == []


def test_dataset_owner_isolation_including_reviewers_and_admins(store, identities):
    actors = identities[1]
    payload = dataset_value()
    store.put_dataset(payload, actors["operator"])
    for name in ("other", "reviewer", "admin", "outside"):
        stranger = actors[name]
        assert store.list_datasets(stranger) == []
        hidden = assert_error("not_found", store.dataset, "dataset-1", stranger, status=404)
        missing = assert_error("not_found", store.dataset, "missing", stranger, status=404)
        assert hidden.message == missing.message
        if name != "outside":
            error = assert_error("dataset_conflict", store.put_dataset, payload, stranger, status=409)
            assert "operator" not in error.message
    store.put_dataset(dataset_value("other-data"), actors["other"])
    assert [item["dataset_id"] for item in store.list_datasets(actors["operator"])] == ["dataset-1"]
    assert [item["dataset_id"] for item in store.list_datasets(actors["other"])] == ["other-data"]


def test_dataset_acl_rechecked_and_catalog_filters_revoked_plants(store, identities):
    tokens, actors = identities
    actor = actors["operator"]
    store.put_dataset(dataset_value("a", "plant-a"), actor)
    store.put_dataset(dataset_value("b", "plant-b"), actor)
    store.set_user(actor.actor_id, actors["admin"], enabled=True, plants=["plant-a"], role="operator")
    assert_error("identity_changed", store.dataset, "a", actor, status=403)
    assert_error("identity_changed", store.list_datasets, actor, status=403)
    assert_error("identity_changed", store.put_dataset, dataset_value("c"), actor, status=403)
    updated = store.authenticate(tokens["operator"])
    assert [item["dataset_id"] for item in store.list_datasets(updated)] == ["a"]
    assert_error("forbidden", store.dataset, "b", updated, status=403)


@pytest.mark.parametrize("mutation", ["content_hash", "wrong_payload", "wrong_kind", "invalid_ref", "file"])
def test_dataset_catalog_and_reads_verify_content_hash_and_binding(store, identities, mutation):
    actor = identities[1]["operator"]
    metadata = store.put_dataset(dataset_value(), actor)
    with store.transaction() as db:
        if mutation == "content_hash":
            db.execute("UPDATE datasets SET content_hash=?", ("0" * 64,))
        elif mutation == "wrong_payload":
            changed = dataset_value("other-dataset")
            reference = store.write_artifact("dataset", changed)
            db.execute("UPDATE datasets SET payload_ref=?,content_hash=?", (canonical(reference), digest(changed)))
        elif mutation == "wrong_kind":
            reference = store.write_artifact("baseline", dataset_value())
            db.execute("UPDATE datasets SET payload_ref=?", (canonical(reference),))
        elif mutation == "invalid_ref":
            db.execute("UPDATE datasets SET payload_ref='broken'")
        else:
            path = store.artifacts / (metadata["payload_ref"]["sha256"] + ".json")
            path.write_bytes(path.read_bytes().replace(b"Synthetic length", b"Synthetic change"))
    code = "artifact_integrity" if mutation == "file" else "dataset_integrity"
    assert_error(code, store.dataset, "dataset-1", actor, status=409)
    assert_error(code, store.list_datasets, actor, status=409)


def test_dataset_rechecks_actor_after_artifact_io_without_write_lock(store, identities, monkeypatch):
    actor = identities[1]["operator"]
    write = store.write_artifact

    def revoke_after_write(kind, value):
        reference = write(kind, value)
        store.set_user(actor.actor_id, identities[1]["admin"], enabled=False,
                       plants=actor.plants, role=actor.role)
        return reference

    monkeypatch.setattr(store, "write_artifact", revoke_after_write)
    assert_error("identity_changed", store.put_dataset, dataset_value(), actor, status=403)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='dataset_registered'").fetchone()[0] == 0
    # An immutable unreferenced artifact is safe; no authoritative row was published.
    assert len(list(store.artifacts.iterdir())) == 1


@pytest.mark.parametrize("operation", ["dataset", "list_datasets"])
def test_dataset_reads_recheck_revocation_after_artifact_io(store, identities, monkeypatch, operation):
    actor = identities[1]["operator"]
    store.put_dataset(dataset_value(), actor)
    read = store.read_artifact

    def revoke_after_read(reference):
        result = read(reference)
        store.set_user(actor.actor_id, identities[1]["admin"], enabled=False,
                       plants=actor.plants, role=actor.role)
        return result

    monkeypatch.setattr(store, "read_artifact", revoke_after_read)
    if operation == "dataset":
        assert_error("identity_changed", store.dataset, "dataset-1", actor, status=403)
    else:
        assert_error("identity_changed", store.list_datasets, actor, status=403)


def test_dataset_conflicting_concurrent_writes_have_one_winner(store, identities):
    actor = identities[1]["operator"]
    payloads = [{**dataset_value(), "sequence": index} for index in range(6)]

    def put(payload):
        try:
            return store.put_dataset(payload, actor)
        except DomainError as error:
            assert (error.code, error.status) == ("dataset_conflict", 409)
            return None

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(put, payloads))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert digest(store.dataset("dataset-1", actor)) == winner["content_hash"]
    assert store.list_datasets(actor) == [winner]


def test_dataset_registration_and_audit_roll_back_together(store, identities, monkeypatch):
    def fail_audit(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(store, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated"):
        store.put_dataset(dataset_value(), identities[1]["operator"])
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
