"""Real API/service/SQLite tests with an explicitly selected test-only provider; no LLM calls."""

import json

import pytest
from fastapi.testclient import TestClient

from spc_agent.api import MAX_BODY, MAX_JSON_DEPTH, create_app
from spc_agent.errors import DomainError
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.provider import LocalProvider, ProviderFailure, SyntheticProvider

BASE_URL = "http://127.0.0.1:8004"
POST_ROUTES = [
    "/api/datasets", "/api/baselines", "/api/baselines/missing/review", "/api/evaluations",
    "/api/proposals", "/api/proposals/missing/review", "/api/proposals/missing/execute",
    "/api/proposals/missing/followup", "/api/proposals/missing/close",
    "/api/investigations", "/api/investigations/missing/run",
    "/api/investigations/missing/reconcile", "/api/investigations/missing/cancel",
]


def auth(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def setup(tmp_path):
    application = create_app(tmp_path, SyntheticProvider())
    store = application.state.store
    tokens = {name: store.provision_user(name, role, [baseline_dataset().plant_id])
              for name, role in (("admin", "admin"), ("other", "admin"), ("operator", "operator"))}
    with TestClient(application, base_url=BASE_URL) as client:
        yield application, client, tokens


def post(client, path, payload, token):
    response = client.post(path, json=payload, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def baseline_and_risk(client, token):
    baseline_data = baseline_dataset()
    monitoring = demo_datasets()["mean_shift"]
    for dataset in (baseline_data, monitoring):
        post(client, "/api/datasets", dataset.model_dump(mode="json"), token)
    baseline = post(client, "/api/baselines", {"action_id": "build-one", "dataset_id": baseline_data.dataset_id}, token)
    assert baseline["status"] == "draft"
    request = {"action_id": "evaluate-one", "dataset_id": monitoring.dataset_id,
               "baseline_id": baseline["baseline_id"]}
    denied = client.post("/api/evaluations", json=request, headers=auth(token))
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "baseline_not_approved"
    reviewed = post(client, f"/api/baselines/{baseline['baseline_id']}/review", {
        "action_id": "review-one", "content_hash": baseline["content_hash"],
        "decision": "approved", "note": "Human synthetic baseline review, not production approval."}, token)
    assert reviewed["status"] == "approved"
    evaluation = post(client, "/api/evaluations", request, token)
    assert evaluation["analysis"]["status"] == "signals" and evaluation["risk_id"]
    assert post(client, "/api/evaluations", request, token) == evaluation
    return reviewed, evaluation


def test_no_automatic_seed_identity_approval_or_inference(tmp_path):
    provider = SyntheticProvider()
    application = create_app(tmp_path, provider)
    assert application.docs_url is application.redoc_url is application.openapi_url is None
    assert application.state.actions.service is application.state.service
    assert application.state.investigations.service is application.state.service
    with application.state.store.connect() as db:
        for table in ("users", "datasets", "baselines", "evaluations", "calls"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    with TestClient(application, base_url=BASE_URL) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "mode": "synthetic-test", "testOnly": True,
                                 "fingerprint": provider.fingerprint, "inferenceChecked": False}
        assert str(tmp_path) not in health.text
        for path in ("/docs", "/redoc", "/openapi.json", "/api/health", "/unknown"):
            assert client.get(path).status_code == 401
    forbidden = tmp_path / "no-seed"
    with pytest.raises(DomainError, match="explicit local init"):
        create_app(forbidden, provider, seed_demo=True)
    assert not forbidden.exists()


def test_packaged_index_and_two_allowlisted_assets_are_public_and_hardened(setup):
    _, client, _ = setup
    for path, media in (("/", "text/html"), ("/static/app.js", "application/javascript"),
                        ("/static/style.css", "text/css")):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.content and response.headers["content-type"].startswith(media)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    page = client.get("/").text
    assert '/static/app.js' in page and '/static/style.css' in page
    assert client.get("/static/secret.json").status_code == 401


def test_local_health_does_not_expose_configuration_or_contact_model(tmp_path, monkeypatch):
    provider = LocalProvider("http://127.0.0.1:19999")
    monkeypatch.setattr(provider, "_post", lambda *args: pytest.fail("health must not call a model"))
    with TestClient(create_app(tmp_path, provider), base_url=BASE_URL) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "local" and response.json()["testOnly"] is False
    assert "19999" not in response.text and "endpoint" not in response.text and "path" not in response.text


def test_real_baseline_evaluation_risk_owner_and_audit(setup):
    application, client, tokens = setup
    token, other = tokens["admin"], auth(tokens["other"])
    baseline, evaluation = baseline_and_risk(client, token)
    baseline_id, evaluation_id, risk_id = baseline["baseline_id"], evaluation["evaluation_id"], evaluation["risk_id"]
    for collection, identity in (("baselines", baseline_id), ("evaluations", evaluation_id), ("risks", risk_id)):
        assert len(client.get(f"/api/{collection}", headers=auth(token)).json()[collection]) == 1
        assert client.get(f"/api/{collection}/{identity}", headers=auth(token)).status_code == 200
        assert client.get(f"/api/{collection}/{identity}", headers=other).status_code == 404
        assert client.get(f"/api/{collection}", headers=other).json() == {collection: []}
        assert client.get(f"/api/audit/{identity}", headers=other).status_code == 404
    assert client.get("/api/datasets", headers=other).json() == {"datasets": []}
    audit = client.get(f"/api/audit/{baseline_id}", headers=auth(token)).json()["audit"]
    assert {row["event"] for row in audit} == {"baseline_built", "baseline_reviewed"}
    assert token not in json.dumps(audit)
    actor = application.state.store.authenticate(token)
    assert application.state.service.get_baseline(baseline_id, actor)["content_hash"] == baseline["content_hash"]


def test_actor_role_and_plant_are_never_trusted_from_request(setup):
    application, client, tokens = setup
    operator = tokens["operator"]
    me = client.get("/api/me", headers=auth(operator)).json()
    assert me["actor"]["role"] == "operator"
    original = baseline_dataset().model_dump(mode="json")
    sentinel = "PRIVATE_" + "SENTINEL"
    hostile = {**original, "owner": "admin", "actor": {"role": "admin"}, "secret": sentinel}
    response = client.post("/api/datasets", json=hostile, headers={**auth(operator), "X-Role": "admin"})
    assert response.status_code == 422 and "PRIVATE_SENTINEL" not in response.text
    response = client.post("/api/datasets", json={**original, "plant_id": "different-plant"}, headers=auth(operator))
    assert response.status_code == 403
    post(client, "/api/datasets", original, operator)
    baseline = post(client, "/api/baselines", {"action_id": "operator-build", "dataset_id": original["dataset_id"]}, operator)
    response = client.post(f"/api/baselines/{baseline['baseline_id']}/review", json={
        "action_id": "operator-review", "content_hash": baseline["content_hash"],
        "decision": "approved", "note": "An operator cannot grant their own review permission."},
        headers={**auth(operator), "X-Role": "admin", "X-Actor": "admin"})
    assert response.status_code == 403
    with application.state.store.transaction() as db:
        db.execute("UPDATE users SET enabled=0 WHERE actor_id='operator'")
    assert client.get("/api/me", headers=auth(operator)).status_code == 401


@pytest.mark.parametrize("prefix,suffix", [
    ("", ""), ("Basic ", ""), ("Bearer  ", ""), ("Bearer\t", ""),
    ("Bearer ", " "), ("Bearer ", ",other"), ("Bearer ", "\t"),
])
def test_strict_bearer(setup, prefix, suffix):
    _, client, tokens = setup
    response = client.get("/api/me", headers={"Authorization": prefix + tokens["admin"] + suffix})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert tokens["admin"] not in response.text


def test_duplicate_auth_cookie_query_and_unknown_routes_require_token(setup):
    _, client, tokens = setup
    token = tokens["admin"]
    duplicate = client.get("/api/me", headers=[("Authorization", "Bearer " + token)] * 2)
    assert duplicate.status_code == 401
    assert client.get("/api/me?token=" + token, headers={"Cookie": "token=" + token}).status_code == 401
    assert client.get("/api/me", headers={"Authorization": "bEaReR " + token}).status_code == 200
    for path in ("/unknown", "/static/private.txt", "/openapi.json"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth(token)).status_code == 404
    assert client.request("TRACE", "/api/me", headers=auth(token)).status_code == 405


@pytest.mark.parametrize("host", [
    "example.com", "127.0.0.1.evil", "localhost.evil", "0.0.0.0", "[::]", "2130706433",
    "127.1", "localhost@evil", "evil@127.0.0.1", "localhost.", "127.0.0.1:0",
    "127.0.0.1:65536", "127.0.0.1:", "localhost/path", "localhost#fragment",
])
def test_host_rebinding_and_malformed_authority_rejected(setup, host):
    _, client, _ = setup
    response = client.get("/health", headers={"Host": host})
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8004", "127.0.0.2:8004", "[::1]:8004", "localhost:8004"])
def test_only_loopback_or_localhost_ui_hosts(setup, host):
    _, client, _ = setup
    assert client.get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("extra", [
    {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": ""},
    {"Origin": "http://localhost:8004"}, {"Origin": "http://127.0.0.1:8005"},
    {"Origin": BASE_URL + "/path"}, {"Origin": BASE_URL + "/"},
    {"Origin": "https://127.0.0.1:8004"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"}, {"Referer": "http://evil.example/private"},
])
def test_cross_origin_mutations_rejected_without_side_effects(setup, extra):
    application, client, tokens = setup
    response = client.post("/api/datasets", json=baseline_dataset().model_dump(mode="json"),
                           headers={**auth(tokens["admin"]), **extra})
    assert response.status_code == 403
    assert not application.state.store.list_datasets(application.state.store.authenticate(tokens["admin"]))
    assert "access-control-allow-origin" not in response.headers


def test_same_origin_and_nonbrowser_authenticated_clients_allowed(setup):
    _, client, tokens = setup
    for extra in ({"Origin": BASE_URL, "Sec-Fetch-Site": "same-origin"},
                  {"Referer": BASE_URL + "/", "Sec-Fetch-Site": "same-origin"}, {}):
        response = client.post("/api/datasets", json=baseline_dataset().model_dump(mode="json"),
                               headers={**auth(tokens["admin"]), **extra})
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("route", POST_ROUTES)
def test_every_post_requires_json_object_including_empty_control_actions(setup, route):
    _, client, tokens = setup
    for raw in (b"", b"[]", b"null", b'"text"', b"true"):
        response = client.post(route, content=raw,
                               headers={**auth(tokens["admin"]), "Content-Type": "application/json"})
        assert response.status_code == 400, (route, response.text)
    response = client.post(route, content=b"{}", headers=auth(tokens["admin"]))
    assert response.status_code == 415


@pytest.mark.parametrize("raw", [
    b'{"a":1,"a":2}', b'{"a":{"nested":1,"nested":2}}', b'{"a":1,"\\u0061":2}',
    b'{"a":[NaN]}', b'{"a":[Infinity]}', b'{"a":[-Infinity]}', b'{"a":[1e9999]}',
    b'{"a":[-1e9999]}', b'{"a":' + b'9' * 400 + b'}',
    b'{"a":"\\ud800"}', b'{"a":"\xff"}',
    b'{"a":' + b'[' * MAX_JSON_DEPTH + b'0' + b']' * MAX_JSON_DEPTH + b'}',
])
def test_duplicate_nonfinite_overflow_encoding_and_depth_rejected(setup, raw):
    _, client, tokens = setup
    response = client.post("/api/datasets", content=raw,
                           headers={**auth(tokens["admin"]), "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_streamed_actual_size_limit_and_exact_boundary(setup):
    _, client, tokens = setup
    headers = {**auth(tokens["admin"]), "Content-Type": "application/json"}

    def stream():
        yield b'{"x":"'
        for _ in range(5):
            yield b"x" * 65536
        yield b'"}'

    response = client.post("/api/datasets", content=stream(), headers={**headers, "Content-Length": "2"})
    assert response.status_code == 413
    response = client.post("/api/datasets", content=b"{}", headers={**headers, "Content-Length": str(MAX_BODY + 1)})
    assert response.status_code == 413
    exact = b" " * (MAX_BODY - 2) + b"{}"
    assert client.post("/api/datasets", content=exact, headers=headers).status_code == 422
    for value in ("-1", "invalid", "1,1", "+2"):
        response = client.post("/api/datasets", content=b"{}", headers={**headers, "Content-Length": value})
        assert response.status_code == 400
    response = client.post("/api/datasets", content=b"{}", headers={**headers, "Content-Encoding": "gzip"})
    assert response.status_code == 415


def test_unknown_failure_and_all_error_responses_are_hardened(setup, monkeypatch, caplog):
    application, client, tokens = setup

    def failure(*args, **kwargs):
        raise RuntimeError("PRIVATE_SENTINEL " + tokens["admin"])

    monkeypatch.setattr(application.state.store, "list_datasets", failure)
    unexpected = client.get("/api/datasets", headers=auth(tokens["admin"]))
    assert unexpected.status_code == 500
    responses = [unexpected, client.get("/api/me"), client.get("/health"),
                 client.get("/health", headers={"Host": "evil.example"}),
                 client.post("/api/datasets", content="{}", headers=auth(tokens["admin"]))]
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert "PRIVATE_SENTINEL" not in response.text and tokens["admin"] not in response.text
    assert "PRIVATE_SENTINEL" not in caplog.text and tokens["admin"] not in caplog.text


def test_investigation_requires_explicit_run_empty_object_and_owner(setup):
    application, client, tokens = setup
    _, evaluation = baseline_and_risk(client, tokens["admin"])
    request = {"action_id": "investigate-one", "risk_id": evaluation["risk_id"]}
    investigation = post(client, "/api/investigations", request, tokens["admin"])
    identity = investigation["investigation_id"]
    assert investigation["calls"] == 0 and investigation["status"] == "ready"
    assert post(client, "/api/investigations", request, tokens["admin"]) == investigation
    assert client.get(f"/api/investigations/{identity}", headers=auth(tokens["other"])).status_code == 404
    for operation in ("run", "cancel"):
        path = f"/api/investigations/{identity}/{operation}"
        assert client.post(path, json={"owner": "admin"}, headers=auth(tokens["admin"])).status_code == 422
        assert client.post(path, json={}, headers=auth(tokens["other"])).status_code == 404
    result = post(client, f"/api/investigations/{identity}/run", {}, tokens["admin"])
    assert result["status"] in {"completed", "escalated"}, result
    assert result["calls"] > 0 and result["report"]
    assert client.get(f"/api/audit/{identity}", headers=auth(tokens["admin"])).status_code == 200
    second = post(client, "/api/investigations", {**request, "action_id": "cancel-one"}, tokens["admin"])
    result = post(client, f"/api/investigations/{second['investigation_id']}/cancel", {}, tokens["admin"])
    assert result["status"] == "cancelled" and result["calls"] == 0
    with application.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_reconcile_unknown_call_is_explicit_bound_and_does_not_retry(setup, monkeypatch):
    application, client, tokens = setup
    token = tokens["admin"]
    _, evaluation = baseline_and_risk(client, token)
    calls = []

    def unknown(context):
        calls.append(context)
        raise ProviderFailure("provider_unknown", "Explicit synthetic unknown outcome")

    monkeypatch.setattr(application.state.provider, "decision", unknown)
    created = post(client, "/api/investigations", {
        "action_id": "unknown-investigation", "risk_id": evaluation["risk_id"]}, token)
    identity = created["investigation_id"]
    blocked = post(client, f"/api/investigations/{identity}/run", {}, token)
    assert blocked["status"] == "blocked" and len(calls) == 1
    response = client.post(f"/api/investigations/{identity}/run", json={}, headers=auth(token))
    assert response.status_code == 200 and response.json()["status"] == "blocked"
    assert len(calls) == 1
    request = {"action_id": "reconcile-one", "call_id": blocked["pending_call_id"],
               "no_result": True, "tokens": 0,
               "note": "Human test-only reconciliation confirms there is no usable result."}
    path = f"/api/investigations/{identity}/reconcile"
    assert client.post(path, json=request, headers=auth(tokens["other"])).status_code == 404
    for altered in ({"no_result": False}, {"tokens": True}, {"owner": "admin"}):
        assert client.post(path, json={**request, **altered}, headers=auth(token)).status_code == 422
    reconciled = post(client, path, request, token)
    assert reconciled["status"] == "ready" and reconciled["pending_call_id"] is None
    assert post(client, path, request, token) == reconciled
    assert len(calls) == 1


def test_proposal_review_execute_followup_and_close_routes_real_service(setup):
    _, client, tokens = setup
    token = tokens["admin"]
    _, evaluation = baseline_and_risk(client, token)
    proposal = post(client, "/api/proposals", {
        "action_id": "proposal-one", "risk_id": evaluation["risk_id"],
        "description": "Review the synthetic gauge evidence before any operational change.",
        "expected_result": "Subsequent independent measurements show no statistical signal."}, token)
    identity = proposal["proposal_id"]
    assert proposal["status"] == "proposed"
    assert len(client.get("/api/proposals", headers=auth(token)).json()["proposals"]) == 1
    assert client.get(f"/api/proposals/{identity}", headers=auth(tokens["other"])).status_code == 404
    reviewed = post(client, f"/api/proposals/{identity}/review", {
        "action_id": "proposal-review", "content_hash": proposal["content_hash"],
        "decision": "approved", "note": "Explicit human review of this original synthetic proposal."}, token)
    assert reviewed["status"] == "approved"
    original = demo_datasets()["mean_shift"]
    executed_at = max(group.timestamp_ms for group in original.groups) + 1
    executed = post(client, f"/api/proposals/{identity}/execute", {
        "action_id": "execute-one", "proposal_hash": proposal["content_hash"],
        "observed_at_ms": executed_at, "confirmed": True,
        "note": "Human synthetic attestation only, no real action was performed."}, token)
    assert executed["status"] == "executed"
    followup = demo_datasets()["normal"].model_dump(mode="json")
    followup["dataset_id"] = "independent-followup"
    for group in followup["groups"]:
        group["timestamp_ms"] += 60 * 60 * 1000
    post(client, "/api/datasets", followup, token)
    followed = post(client, f"/api/proposals/{identity}/followup", {
        "action_id": "followup-one", "dataset_id": followup["dataset_id"]}, token)
    followup_record = followed["followups"][-1]
    assert followup_record["payload"]["eligible"] is True
    closed = post(client, f"/api/proposals/{identity}/close", {
        "action_id": "close-one", "followup_hash": followup_record["followup_hash"],
        "note": "Human closure review based on the new synthetic follow-up only."}, token)
    assert closed["status"] == "closed"
    assert client.get(f"/api/audit/{identity}", headers=auth(token)).status_code == 200
