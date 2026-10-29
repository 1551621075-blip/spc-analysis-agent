from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from .errors import DomainError
from .schemas import Actor, canonical, digest

MAX_JSON_BYTES = 4_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  actor_id TEXT PRIMARY KEY NOT NULL, token_hash TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL, plants TEXT NOT NULL, acl_version INTEGER NOT NULL,
  enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS datasets(
  dataset_id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL REFERENCES users(actor_id),
  plant_id TEXT NOT NULL, payload_ref TEXT NOT NULL, content_hash TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS baselines(
  baseline_id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL REFERENCES users(actor_id),
  plant_id TEXT NOT NULL, payload_ref TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL, review_json TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations(
  evaluation_id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL REFERENCES users(actor_id),
  plant_id TEXT NOT NULL, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
  baseline_id TEXT NOT NULL REFERENCES baselines(baseline_id), payload_ref TEXT NOT NULL,
  content_hash TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(owner,dataset_id,baseline_id));
CREATE TABLE IF NOT EXISTS risks(
  risk_id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL REFERENCES users(actor_id),
  plant_id TEXT NOT NULL, evaluation_id TEXT NOT NULL REFERENCES evaluations(evaluation_id),
  event_key TEXT UNIQUE NOT NULL, payload_ref TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(
  proposal_id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL REFERENCES users(actor_id),
  plant_id TEXT NOT NULL, risk_id TEXT NOT NULL REFERENCES risks(risk_id),
  state_json TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS investigations(
  investigation_id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL REFERENCES users(actor_id),
  plant_id TEXT NOT NULL, risk_id TEXT NOT NULL REFERENCES risks(risk_id),
  state_json TEXT NOT NULL, version INTEGER NOT NULL, lease_owner TEXT, lease_until REAL);
CREATE TABLE IF NOT EXISTS calls(
  call_id TEXT PRIMARY KEY NOT NULL,
  investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
  context_ref TEXT NOT NULL, response_ref TEXT, status TEXT NOT NULL,
  tokens INTEGER, error_code TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(
  owner TEXT NOT NULL REFERENCES users(actor_id), action_id TEXT NOT NULL,
  request_hash TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(owner,action_id));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT, actor_id TEXT NOT NULL,
  event TEXT NOT NULL, detail_json TEXT NOT NULL, created_at REAL NOT NULL);
"""


def new_id(prefix: str) -> str:
    value = prefix + "-" + uuid.uuid4().hex
    _identifier(value)
    return value


def now() -> float:
    return time.time()


def _identifier(value, label: str = "identifier") -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DomainError("invalid_identifier", f"Invalid {label}")
    return value


def _json_text(value) -> str:
    """Accept JSON types only, without non-finite numbers, cycles or excessive depth."""
    remaining = MAX_JSON_BYTES

    def charge(size):
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise DomainError("artifact_too_large", "JSON exceeds local size limit", 413)

    def visit(item, depth):
        if depth > 64:
            raise DomainError("invalid_json", "JSON exceeds the nesting limit")
        if item is None or type(item) in (bool, int):
            charge(len(canonical(item)))
            return
        if type(item) is str:
            if len(item) > remaining:
                raise DomainError("artifact_too_large", "JSON exceeds local size limit", 413)
            charge(len(canonical(item).encode("utf-8")))
            return
        if type(item) is float and math.isfinite(item):
            charge(len(canonical(item)))
            return
        if type(item) is dict:
            charge(2 + max(0, len(item) - 1) + len(item))
            for key, child in item.items():
                if type(key) is not str:
                    raise DomainError("invalid_json", "JSON object keys must be strings")
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        if type(item) is list:
            charge(2 + max(0, len(item) - 1))
            for child in item:
                visit(child, depth + 1)
            return
        raise DomainError("invalid_json", "Only finite JSON values are accepted")

    try:
        # Count encoded bytes before allocating the complete canonical string;
        # repeated/shared containers cannot expand beyond the local bound.
        visit(value, 0)
        text = canonical(value)
        size = len(text.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DomainError("invalid_json", "Invalid JSON value") from None
    if size > MAX_JSON_BYTES:
        raise DomainError("artifact_too_large", "JSON exceeds local size limit", 413)
    return text


def _directory(path: Path, *, missing: bool = False):
    """Reject user-selected symlinks, including ancestors, before resolving paths.

    macOS system aliases /var, /tmp and /etc are accepted only as ancestors and
    only with their standard targets. This is not protection from a hostile OS.
    """
    system_aliases = {"/var": "/private/var", "/tmp": "/private/tmp", "/etc": "/private/etc"}
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise DomainError("unsafe_storage", "Storage directory is unavailable", 409) from None
        if stat.S_ISLNK(info.st_mode):
            expected = system_aliases.get(str(component))
            if component != path and expected and str(component.resolve()) == expected:
                continue
            raise DomainError("unsafe_storage", "Storage symlinks are not allowed", 409)
        if not stat.S_ISDIR(info.st_mode):
            raise DomainError("unsafe_storage", "Storage path is not a directory", 409)


class Store:
    def __init__(self, root: str | Path):
        candidate = Path(root).expanduser().absolute()
        if ".." in candidate.parts:
            raise DomainError("unsafe_storage", "Parent traversal is not allowed", 409)
        _directory(candidate, missing=True)
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        _directory(candidate)
        self.root = candidate.resolve(strict=True)
        self.artifacts = self.root / "artifacts"
        _directory(self.artifacts, missing=True)
        self.artifacts.mkdir(exist_ok=True, mode=0o700)
        _directory(self.artifacts)
        self.database = self.root / "spc.sqlite3"
        self._check_database_files()
        try:
            fd = os.open(self.database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    def _check_database_files(self):
        _directory(self.root)
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.database) + suffix)
            for attempt in range(3):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    break
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    break
                # SQLite unlinks sidecars when the last connection closes.
                # On Darwin, lstat can resolve that inode before unlink and
                # return its post-unlink nlink=0 instead of ENOENT. Recheck the
                # pathname; never accept zero links or a replacement unchecked.
                if (suffix and stat.S_ISREG(info.st_mode) and info.st_nlink == 0
                        and attempt < 2):
                    continue
                raise DomainError("unsafe_storage", "Unsafe database file", 409)

    @contextmanager
    def connect(self):
        self._check_database_files()
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def provision_user(self, actor_id: str, role: str, plants: list[str]) -> str:
        """Trusted local bootstrap, not an unauthenticated service endpoint."""
        try:
            actor = Actor(actor_id=actor_id, role=role, plants=plants, acl_version=1)
        except ValidationError:
            raise DomainError("invalid_user", "Invalid user identity or permissions") from None
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            try:
                db.execute("INSERT INTO users VALUES(?,?,?,?,1,1)", (
                    actor.actor_id, hashlib.sha256(token.encode()).hexdigest(), actor.role,
                    canonical(actor.plants)))
            except sqlite3.IntegrityError:
                raise DomainError("user_exists", "User already exists", 409) from None
        return token

    @staticmethod
    def _actor(row) -> Actor:
        try:
            return Actor(actor_id=row["actor_id"], role=row["role"],
                         plants=json.loads(row["plants"]), acl_version=row["acl_version"])
        except (TypeError, ValueError):
            raise DomainError("identity_changed", "Current identity or permissions changed", 403) from None

    def authenticate(self, token: str) -> Actor:
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise DomainError("unauthorized", "Authentication required", 401)
        try:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
        except UnicodeError:
            raise DomainError("unauthorized", "Authentication required", 401) from None
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE token_hash=? AND enabled=1",
                             (token_hash,)).fetchone()
        if row is None:
            raise DomainError("unauthorized", "Authentication required", 401)
        return self._actor(row)

    def current_actor(self, actor: Actor, db=None) -> Actor:
        if not isinstance(actor, Actor):
            raise DomainError("identity_changed", "Current identity or permissions changed", 403)
        if db is None:
            with self.connect() as connection:
                return self.current_actor(actor, connection)
        row = db.execute("SELECT * FROM users WHERE actor_id=? AND enabled=1",
                         (actor.actor_id,)).fetchone()
        if row is None or self._actor(row) != actor:
            raise DomainError("identity_changed", "Current identity or permissions changed", 403)
        return self._actor(row)

    def authorize(self, actor: Actor, plant: str, roles=None, db=None) -> Actor:
        actor = self.current_actor(actor, db)
        if plant not in actor.plants or (roles is not None and actor.role not in roles):
            raise DomainError("forbidden", "Action outside current authorization", 403)
        return actor

    def set_user(self, target: str, actor: Actor, *, enabled: bool, plants: list[str], role: str):
        actor = self.current_actor(actor)
        if actor.role != "admin":
            raise DomainError("forbidden", "Administrator required", 403)
        if type(enabled) is not bool:
            raise DomainError("invalid_user", "Enabled must be a boolean")
        try:
            checked = Actor(actor_id=target, role=role, plants=plants, acl_version=1)
        except ValidationError:
            raise DomainError("invalid_user", "Invalid user identity or permissions") from None
        with self.transaction() as db:
            actor = self.current_actor(actor, db)
            row = db.execute("SELECT * FROM users WHERE actor_id=?", (target,)).fetchone()
            if (row is None or not set(json.loads(row["plants"])) <= set(actor.plants)
                    or not set(checked.plants) <= set(actor.plants)):
                raise DomainError("forbidden", "User outside administrator plants", 403)
            db.execute("UPDATE users SET enabled=?,role=?,plants=?,acl_version=acl_version+1 "
                       "WHERE actor_id=?", (int(enabled), checked.role, canonical(checked.plants), target))
            self.audit(db, actor.actor_id, "user_permissions", {"target": target, "enabled": enabled})

    @contextmanager
    def _artifact_directory(self):
        _directory(self.artifacts)
        fd = os.open(self.artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _artifact_bytes(directory: int, filename: str) -> bytes:
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
                    raise DomainError("artifact_integrity", "Artifact is unavailable", 409)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    content = stream.read(MAX_JSON_BYTES + 1)
            finally:
                os.close(fd)
        except OSError:
            raise DomainError("artifact_integrity", "Artifact is unavailable", 409) from None
        if len(content) > MAX_JSON_BYTES:
            raise DomainError("artifact_integrity", "Artifact exceeds local size limit", 409)
        return content

    def write_artifact(self, kind: str, value) -> dict:
        _identifier(kind, "artifact kind")
        content = _json_text({"kind": kind, "value": value}).encode("utf-8")
        sha = hashlib.sha256(content).hexdigest()
        filename = sha + ".json"
        with self._artifact_directory() as directory:
            temporary = ".pending-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    if self._artifact_bytes(directory, filename) != content:
                        raise DomainError("artifact_integrity", "Artifact content collision", 409) from None
                os.fsync(directory)
            finally:
                os.unlink(temporary, dir_fd=directory)
        return {"sha256": sha, "kind": kind, "bytes": len(content)}

    def read_artifact(self, reference: dict):
        if (type(reference) is not dict or set(reference) != {"sha256", "kind", "bytes"}
                or not isinstance(reference.get("sha256"), str)
                or _SHA256.fullmatch(reference["sha256"]) is None
                or not isinstance(reference.get("kind"), str)
                or _IDENTIFIER.fullmatch(reference["kind"]) is None
                or type(reference.get("bytes")) is not int
                or not 0 < reference["bytes"] <= MAX_JSON_BYTES):
            raise DomainError("artifact_integrity", "Invalid artifact reference", 409)
        with self._artifact_directory() as directory:
            content = self._artifact_bytes(directory, reference["sha256"] + ".json")
        if len(content) != reference["bytes"] or hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise DomainError("artifact_integrity", "Artifact hash mismatch", 409)
        try:
            payload = json.loads(content.decode("utf-8"))
            if (type(payload) is not dict or set(payload) != {"kind", "value"}
                    or payload["kind"] != reference["kind"]
                    or _json_text(payload).encode("utf-8") != content):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError, DomainError):
            raise DomainError("artifact_integrity", "Invalid artifact payload", 409) from None
        return payload["value"]

    @staticmethod
    def audit(db, actor_id: str, event: str, detail: dict, object_id: str | None = None):
        _identifier(actor_id, "actor ID")
        if object_id is not None:
            _identifier(object_id, "object ID")
        _identifier(event, "audit event")
        db.execute("INSERT INTO audit(object_id,actor_id,event,detail_json,created_at) VALUES(?,?,?,?,?)",
                   (object_id, actor_id, event, _json_text(detail), now()))

    def receipt(self, db, actor: Actor, action_id: str, request: dict):
        actor = self.current_actor(actor, db)
        _identifier(action_id, "action ID")
        _json_text(request)
        row = db.execute("SELECT * FROM receipts WHERE owner=? AND action_id=?",
                         (actor.actor_id, action_id)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != digest(request):
            raise DomainError("idempotency_conflict", "Action ID reused with different content", 409)
        try:
            result = json.loads(row["result_json"])
            if result is None or _json_text(result) != row["result_json"]:
                raise ValueError
        except (ValueError, TypeError, RecursionError, DomainError):
            raise DomainError("receipt_integrity", "Invalid stored receipt", 409) from None
        return result

    def save_receipt(self, db, actor: Actor, action_id: str, request: dict, result):
        # None is reserved for "no receipt". False, 0, {}, [] and "" are valid results.
        if result is None:
            raise DomainError("invalid_receipt", "Receipt result must not be null")
        result_json = _json_text(result)
        existing = self.receipt(db, actor, action_id, request)
        if existing is not None:
            if canonical(existing) != result_json:
                raise DomainError("idempotency_conflict", "Receipt result is immutable", 409)
            return
        db.execute("INSERT INTO receipts VALUES(?,?,?,?)",
                   (actor.actor_id, action_id, digest(request), result_json))

    @staticmethod
    def _dataset_metadata(row, payload: dict) -> dict:
        result = {key: row[key] for key in ("dataset_id", "plant_id", "content_hash", "created_at")}
        result["payload_ref"] = json.loads(row["payload_ref"])
        for key in ("process_id", "characteristic", "unit", "source", "sampling_seconds"):
            if key in payload:
                result[key] = payload[key]
        if isinstance(payload.get("groups"), list):
            result["group_count"] = len(payload["groups"])
        return result

    @staticmethod
    def _same_dataset(row, actor: Actor, payload: dict, content_hash: str):
        if row is not None and (row["owner"] != actor.actor_id
                                or row["plant_id"] != payload["plant_id"]
                                or row["content_hash"] != content_hash):
            raise DomainError("dataset_conflict", "Dataset ID is already registered", 409)

    def put_dataset(self, dataset: dict, actor: Actor) -> dict:
        actor = self.current_actor(actor)
        if type(dataset) is not dict:
            raise DomainError("invalid_dataset", "Dataset must be a JSON object")
        payload = json.loads(_json_text(dataset))
        dataset_id = _identifier(payload.get("dataset_id"), "dataset ID")
        plant_id = _identifier(payload.get("plant_id"), "plant ID")
        content_hash = digest(payload)
        with self.connect() as db:
            self.authorize(actor, plant_id, db=db)
            row = db.execute("SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            self._same_dataset(row, actor, payload, content_hash)
        # Publish before opening the short write transaction. A rollback can leave
        # an unreferenced immutable artifact, but never a row pointing at a partial file.
        reference = self.write_artifact("dataset", payload)
        reference_json = canonical(reference)
        with self.transaction() as db:
            self.authorize(actor, plant_id, db=db)
            row = db.execute("SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            self._same_dataset(row, actor, payload, content_hash)
            if row is None:
                db.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?)",
                           (dataset_id, actor.actor_id, plant_id, reference_json, content_hash, now()))
                self.audit(db, actor.actor_id, "dataset_registered",
                           {"content_hash": content_hash, "plant_id": plant_id}, dataset_id)
                row = db.execute("SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
            elif row["payload_ref"] != reference_json:
                raise DomainError("dataset_integrity", "Dataset artifact reference mismatch", 409)
            return self._dataset_metadata(row, payload)

    def _dataset_payload(self, row) -> dict:
        try:
            reference = json.loads(row["payload_ref"])
            if type(reference) is not dict or reference.get("kind") != "dataset":
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise DomainError("dataset_integrity", "Invalid dataset artifact reference", 409) from None
        payload = self.read_artifact(reference)
        if (type(payload) is not dict or payload.get("dataset_id") != row["dataset_id"]
                or payload.get("plant_id") != row["plant_id"] or digest(payload) != row["content_hash"]):
            raise DomainError("dataset_integrity", "Dataset content hash or identity mismatch", 409)
        return payload

    def dataset(self, dataset_id: str, actor: Actor, db=None) -> dict:
        if db is None:
            with self.connect() as connection:
                return self.dataset(dataset_id, actor, connection)
        actor = self.current_actor(actor, db)
        _identifier(dataset_id, "dataset ID")
        row = db.execute("SELECT * FROM datasets WHERE dataset_id=? AND owner=?",
                         (dataset_id, actor.actor_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Dataset not found", 404)
        self.authorize(actor, row["plant_id"], db=db)
        payload = self._dataset_payload(row)
        self.authorize(actor, row["plant_id"], db=db)
        return payload

    def list_datasets(self, actor: Actor) -> list[dict]:
        with self.connect() as db:
            actor = self.current_actor(actor, db)
            placeholders = ",".join("?" for _ in actor.plants)
            rows = db.execute(
                f"SELECT * FROM datasets WHERE owner=? AND plant_id IN ({placeholders}) "
                "ORDER BY created_at,dataset_id", (actor.actor_id, *actor.plants)).fetchall()
            result = [self._dataset_metadata(row, self._dataset_payload(row)) for row in rows]
            self.current_actor(actor, db)
            return result
