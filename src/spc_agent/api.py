"""Loopback-only HTTP boundary; identity and authorization stay on the server."""

import ipaddress
import json
import math
import re
from importlib.resources import files
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .actions import ActionService
from .domain import Dataset
from .errors import DomainError
from .investigation import InvestigationService
from .schemas import (
    Actor,
    BaselineRequest,
    CloseRequest,
    EvaluateRequest,
    ExecuteRequest,
    FollowupRequest,
    InvestigationRequest,
    ProposalRequest,
    ReconcileRequest,
    ReviewRequest,
    StrictModel,
)
from .service import SPCService
from .storage import Store

MAX_BODY = 256 * 1024
MAX_JSON_DEPTH = 32
ASSETS = {"app.js": "application/javascript", "style.css": "text/css"}
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
}


def provider_metadata(provider) -> dict:
    test_only = bool(provider.is_test_provider)
    return {"mode": "synthetic-test" if test_only else "local",
            "fingerprint": provider.fingerprint, "testOnly": test_only}


def _error(code: str, message: str, status: int) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite")
    return result


def _finite_int(value: str) -> int:
    result = int(value)
    try:
        finite = math.isfinite(result)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("numeric overflow")
    return result


def _reject_constant(value: str):
    raise ValueError("non-finite")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json_object(content: bytes) -> dict:
    text = content.decode("utf-8", errors="strict")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("excessive nesting")
        elif char in "]}":
            depth -= 1
    result = json.loads(text, object_pairs_hook=_unique_object, parse_int=_finite_int,
                        parse_float=_finite_float, parse_constant=_reject_constant)
    if not isinstance(result, dict):
        raise ValueError("object required")
    # Escaped lone surrogates otherwise survive the JSON parser and fail during persistence.
    json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    return result


def _authority(value: str, scheme: str) -> tuple[str, int]:
    if (not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or any(c in value for c in "\\/@?#%") or value.endswith(":")):
        raise ValueError("invalid host")
    parsed = urlsplit("//" + value)
    host = parsed.hostname
    if not host or parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
        raise ValueError("invalid host")
    if host != "localhost":
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("non-loopback host")
        host = address.compressed
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return host, port if port is not None else (443 if scheme == "https" else 80)


def _same_origin(value: str, scheme: str, authority, *, referer=False) -> bool:
    try:
        if any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value:
            return False
        parsed = urlsplit(value)
        return (parsed.scheme == scheme and not parsed.fragment
                and (referer or (not parsed.path and not parsed.query))
                and _authority(parsed.netloc, parsed.scheme) == authority)
    except ValueError:
        return False


class LocalBoundary:
    """Authenticate all non-public paths and bound the actual ASGI stream before routing."""

    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.decode("latin-1").lower() not in SECURITY_HEADERS]
                message["headers"] = headers + [
                    (key.encode("ascii"), value.encode("ascii"))
                    for key, value in SECURITY_HEADERS.items()
                ]
            await send(message)

        try:
            await self._http(scope, receive, secure_send)
        except DomainError as exc:
            if not started:
                await _error(exc.code, exc.message, exc.status)(scope, receive, secure_send)
        except Exception:
            # Do not re-raise into Uvicorn's traceback/access logging with request data.
            if not started:
                await _error("internal_error", "The request could not be completed", 500)(
                    scope, receive, secure_send)

    async def _http(self, scope, receive, send):
        headers = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.lower(), []).append(value.decode("latin-1"))

        def single(name, default=""):
            values = headers.get(name, [])
            if len(values) > 1:
                status = 401 if name == b"authorization" else 400
                raise DomainError("invalid_header", "Invalid request headers", status)
            return values[0] if values else default

        scheme = scope.get("scheme", "http")
        try:
            authority = _authority(single(b"host"), scheme)
        except ValueError:
            raise DomainError("invalid_host", "A loopback Host is required", 400) from None
        method, path = scope["method"], scope["path"]
        if method == "TRACE":
            raise DomainError("method_not_allowed", "Method not allowed", 405)
        public = method in {"GET", "HEAD"} and (
            path in {"/", "/health"} or path in {f"/static/{name}" for name in ASSETS})
        if not public:
            authorization = single(b"authorization")
            if not re.fullmatch(r"Bearer [A-Za-z0-9._~+/\-]+=*", authorization,
                                flags=re.IGNORECASE) or len(authorization) > 207:
                raise DomainError("unauthorized", "Authentication required", 401)
            current = await run_in_threadpool(self.store.authenticate, authorization[7:])
            scope.setdefault("state", {})["actor"] = current
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = single(b"origin")
            if b"origin" in headers and not _same_origin(origin, scheme, authority):
                raise DomainError("cross_origin", "Same-origin requests are required", 403)
            if single(b"sec-fetch-site") not in {"", "same-origin", "none"}:
                raise DomainError("cross_origin", "Same-origin requests are required", 403)
            referer = single(b"referer")
            if not origin and referer and not _same_origin(referer, scheme, authority, referer=True):
                raise DomainError("cross_origin", "Same-origin requests are required", 403)
            declared = single(b"content-length", "0")
            if not re.fullmatch(r"[0-9]{1,20}", declared):
                raise DomainError("invalid_body", "Invalid content length", 400)
            if int(declared) > MAX_BODY:
                raise DomainError("body_too_large", "Request exceeds 256 KiB", 413)
            if single(b"content-encoding", "identity").lower() != "identity":
                raise DomainError("json_required", "Uncompressed application/json is required", 415)
            if single(b"content-type").split(";", 1)[0].strip().lower() != "application/json":
                raise DomainError("json_required", "An application/json object is required", 415)
            content = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(content) + len(chunk) > MAX_BODY:
                    raise DomainError("body_too_large", "Request exceeds 256 KiB", 413)
                content.extend(chunk)
                if not message.get("more_body", False):
                    break
            try:
                _json_object(bytes(content))
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise DomainError("invalid_json", "A bounded, unique-key, finite JSON object is required",
                                  400) from None
            delivered = False
            original_receive = receive

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(content), "more_body": False}
                return await original_receive()

            receive = replay
        await self.app(scope, receive, send)


def _actor(request: Request) -> Actor:
    return request.state.actor


Authorized = Annotated[Actor, Depends(_actor)]


class EmptyRequest(StrictModel):
    pass


def create_app(data_root, provider, *, seed_demo=False) -> FastAPI:
    if seed_demo:
        raise DomainError("explicit_init_required", "Use the explicit local init command to import fixtures")
    store = Store(data_root)
    service = SPCService(store)
    actions = ActionService(service)
    investigations = InvestigationService(service, provider)
    app = FastAPI(title="SPC analysis", docs_url=None, redoc_url=None, openapi_url=None,
                  debug=False, redirect_slashes=False)
    app.state.store, app.state.service = store, service
    app.state.actions, app.state.investigations = actions, investigations
    app.state.provider = provider
    app.add_middleware(LocalBoundary, store=store)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return _error(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return _error("validation_error", "Invalid fields, types or ranges", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return _error("http_error", "Resource or method unavailable", exc.status_code)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(files("spc_agent").joinpath("templates/index.html").read_text("utf-8"))

    @app.get("/static/{name}")
    def asset(name: str):
        if name not in ASSETS:
            raise DomainError("not_found", "Resource not found", 404)
        return Response(files("spc_agent").joinpath("static", name).read_bytes(), media_type=ASSETS[name])

    @app.get("/health")
    def health():
        return {"status": "ok", **provider_metadata(provider), "inferenceChecked": False}

    @app.get("/api/me")
    def me(current: Authorized):
        return {"actor": current.model_dump(mode="json"), "provider": provider_metadata(provider)}

    @app.get("/api/datasets")
    def datasets(current: Authorized):
        return {"datasets": store.list_datasets(current)}

    @app.post("/api/datasets")
    def import_dataset(payload: Dataset, current: Authorized):
        return service.import_dataset(payload.model_dump(mode="json"), current)

    @app.get("/api/baselines")
    def baselines(current: Authorized):
        return {"baselines": service.list_baselines(current)}

    @app.post("/api/baselines")
    def create_baseline(payload: BaselineRequest, current: Authorized):
        return service.create_baseline(payload, current)

    @app.get("/api/baselines/{baseline_id}")
    def baseline(baseline_id: str, current: Authorized):
        return service.get_baseline(baseline_id, current)

    @app.post("/api/baselines/{baseline_id}/review")
    def review_baseline(baseline_id: str, payload: ReviewRequest, current: Authorized):
        return service.review_baseline(baseline_id, payload, current)

    @app.get("/api/evaluations")
    def evaluations(current: Authorized):
        return {"evaluations": service.list_evaluations(current)}

    @app.post("/api/evaluations")
    def evaluate(payload: EvaluateRequest, current: Authorized):
        return service.evaluate(payload, current)

    @app.get("/api/evaluations/{evaluation_id}")
    def evaluation(evaluation_id: str, current: Authorized):
        return service.get_evaluation(evaluation_id, current)

    @app.get("/api/risks")
    def risks(current: Authorized):
        return {"risks": service.list_risks(current)}

    @app.get("/api/risks/{risk_id}")
    def risk(risk_id: str, current: Authorized):
        return service.get_risk(risk_id, current)

    @app.get("/api/proposals")
    def proposals(current: Authorized):
        return {"proposals": actions.list(current)}

    @app.post("/api/proposals")
    def create_proposal(payload: ProposalRequest, current: Authorized):
        return actions.propose(payload, current)

    @app.get("/api/proposals/{proposal_id}")
    def proposal(proposal_id: str, current: Authorized):
        return actions.get(proposal_id, current)

    @app.post("/api/proposals/{proposal_id}/review")
    def review_proposal(proposal_id: str, payload: ReviewRequest, current: Authorized):
        return actions.review(proposal_id, payload, current)

    @app.post("/api/proposals/{proposal_id}/execute")
    def execute(proposal_id: str, payload: ExecuteRequest, current: Authorized):
        return actions.confirm_execution(proposal_id, payload, current)

    @app.post("/api/proposals/{proposal_id}/followup")
    def followup(proposal_id: str, payload: FollowupRequest, current: Authorized):
        return actions.followup(proposal_id, payload, current)

    @app.post("/api/proposals/{proposal_id}/close")
    def close(proposal_id: str, payload: CloseRequest, current: Authorized):
        return actions.close(proposal_id, payload, current)

    @app.post("/api/investigations")
    def create_investigation(payload: InvestigationRequest, current: Authorized):
        return investigations.create(payload, current)

    @app.get("/api/investigations/{investigation_id}")
    def investigation(investigation_id: str, current: Authorized):
        return investigations.get(investigation_id, current)

    @app.post("/api/investigations/{investigation_id}/run")
    def run(investigation_id: str, payload: EmptyRequest, current: Authorized):
        return investigations.run(investigation_id, current)

    @app.post("/api/investigations/{investigation_id}/reconcile")
    def reconcile(investigation_id: str, payload: ReconcileRequest, current: Authorized):
        return investigations.reconcile(investigation_id, payload, current)

    @app.post("/api/investigations/{investigation_id}/cancel")
    def cancel(investigation_id: str, payload: EmptyRequest, current: Authorized):
        return investigations.cancel(investigation_id, current)

    @app.get("/api/audit/{object_id}")
    def audit(object_id: str, current: Authorized):
        return {"audit": service.audit(object_id, current)}

    return app
