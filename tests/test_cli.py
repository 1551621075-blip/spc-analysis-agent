"""Real local SQLite/CLI tests. No model is started or contacted."""

import json

import pytest
from typer.testing import CliRunner

from spc_agent.cli import app
from spc_agent.domain import Dataset
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.storage import Store

runner = CliRunner()


def init_data(path):
    result = runner.invoke(app, ["init", str(path)])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_init_new_directory_only_and_token_once(tmp_path):
    root = tmp_path / "data"
    result = runner.invoke(app, ["init", str(root)])
    assert result.exit_code == 0, result.output
    created = json.loads(result.stdout)
    token = created["token"]
    assert result.output.count(token) == 1
    assert created["actor_id"] == "local-admin"
    assert created["role"] == "admin"
    assert created["plants"] == [baseline_dataset().plant_id]
    store = Store(root)
    actor = store.authenticate(token)
    assert len(store.list_datasets(actor)) == 1 + len(demo_datasets())
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
        assert db.execute("SELECT token_hash FROM users").fetchone()[0] != token
    for file in root.rglob("*"):
        if file.is_file():
            assert token.encode() not in file.read_bytes()
    again = runner.invoke(app, ["init", str(root)])
    assert again.exit_code == 1 and "init_exists" in again.output
    assert token not in again.output
    assert store.authenticate(token) == actor


@pytest.mark.parametrize("kind", ["empty", "nonempty", "file", "symlink", "dangling", "parent-link"])
def test_init_refuses_existing_paths_and_symlinks(tmp_path, kind):
    target = tmp_path / "target"
    target.mkdir()
    path = tmp_path / "data"
    if kind == "empty":
        path.mkdir()
    elif kind == "nonempty":
        path.mkdir()
        (path / "marker").write_text("retain", encoding="utf-8")
    elif kind == "file":
        path.write_text("retain", encoding="utf-8")
    elif kind == "symlink":
        path.symlink_to(target, target_is_directory=True)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "absent")
    else:
        path.symlink_to(target, target_is_directory=True)
        path = path / "child"
    result = runner.invoke(app, ["init", str(path)])
    assert result.exit_code == 1
    assert "token" not in result.output
    assert not list(target.iterdir())
    assert not (tmp_path / "absent").exists()
    assert not (target / "child").exists()


def test_fixture_exports_dataset_only_without_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cases = {"baseline": baseline_dataset(), **demo_datasets()}
    for case, expected in cases.items():
        result = runner.invoke(app, ["fixture", "--case", case])
        assert result.exit_code == 0, result.output
        assert Dataset.model_validate(json.loads(result.stdout)) == expected
    assert not list(tmp_path.iterdir())
    invalid = runner.invoke(app, ["fixture", "--case", "PRIVATE_UNKNOWN"])
    assert invalid.exit_code == 1
    assert "PRIVATE_UNKNOWN" not in invalid.output


def test_real_build_explicit_approval_and_evaluation(tmp_path):
    root = tmp_path / "data"
    created = init_data(root)
    token = created["token"]
    built = runner.invoke(app, ["build", str(root), baseline_dataset().dataset_id], input=token + "\n")
    assert built.exit_code == 0, built.output
    assert token not in built.output
    baseline = json.loads(built.stdout[built.stdout.index("{"):])
    assert baseline["status"] == "draft"
    request = ["evaluate", str(root), baseline["baseline_id"], demo_datasets()["mean_shift"].dataset_id]
    blocked = runner.invoke(app, request, input=token + "\n")
    assert blocked.exit_code == 1 and "baseline_not_approved" in blocked.output
    approval = ["approve", str(root), baseline["baseline_id"], baseline["content_hash"],
                "human-review", "Explicit synthetic baseline review for CLI tests."]
    approved = runner.invoke(app, approval, input=token + "\n")
    assert approved.exit_code == 0, approved.output
    assert '"status": "approved"' in approved.output
    repeated = runner.invoke(app, approval, input=token + "\n")
    assert repeated.exit_code == 0
    evaluated = runner.invoke(app, request, input=token + "\n")
    assert evaluated.exit_code == 0, evaluated.output
    result = json.loads(evaluated.stdout[evaluated.stdout.index("{"):])
    assert result["risk_id"] and result["analysis"]["status"] == "signals"
    assert token not in approved.output + evaluated.output + repeated.output


def test_hidden_prompt_does_not_read_token_environment(tmp_path, monkeypatch):
    root = tmp_path / "data"
    created = init_data(root)
    for key in ("SPC_TOKEN", "QA_TOKEN", "TOKEN"):
        monkeypatch.setenv(key, created["token"])
    denied = runner.invoke(app, ["build", str(root), baseline_dataset().dataset_id],
                           input="invalid-secret-token-value\n")
    assert denied.exit_code == 1 and "unauthorized" in denied.output
    assert created["token"] not in denied.output and "invalid-secret-token-value" not in denied.output
    validation = runner.invoke(app, ["approve", str(root), "baseline", "PRIVATE_BAD_HASH", "review",
                                    "Human note long enough for validation."])
    assert validation.exit_code == 2
    assert "validation_error" in validation.output and "PRIVATE_BAD_HASH" not in validation.output


@pytest.mark.parametrize("options", [
    [], ["--test-mode", "--model-url", "http://127.0.0.1:18080"],
    ["--runtime-config", "relative.json"],
    ["--test-mode", "--host", "0.0.0.0"], ["--test-mode", "--host", "localhost"],
    ["--test-mode", "--host", "::"], ["--test-mode", "--port", "0"],
    ["--test-mode", "--port", "65536"],
    ["--model-url", "https://example.com"], ["--model-url", "http://localhost:8080"],
])
def test_serve_rejects_unsafe_or_ambiguous_configuration_without_writes(tmp_path, options):
    root = tmp_path / "not-created"
    result = runner.invoke(app, ["serve", str(root), *options])
    assert result.exit_code == 1, result.output
    assert not root.exists()


@pytest.mark.parametrize("options,mode", [
    (["--test-mode"], "synthetic-test"),
    (["--model-url", "http://127.0.0.1:18080"], "local"),
])
def test_serve_explicit_mode_defaults_and_safe_logging(tmp_path, monkeypatch, options, mode):
    root = tmp_path / "data"
    created = init_data(root)
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda application, **kwargs: calls.append((application, kwargs)))
    for key in ("QA_RUNTIME_CONFIG", "SPC_RUNTIME_CONFIG", "SPC_MODEL_URL", "QA_PROVIDER"):
        monkeypatch.setenv(key, "PRIVATE_ENV_MUST_BE_IGNORED")
    result = runner.invoke(app, ["serve", str(root), *options])
    assert result.exit_code == 0, result.output
    application, kwargs = calls[0]
    assert kwargs == {"host": "127.0.0.1", "port": 8004, "access_log": False,
                      "proxy_headers": False, "server_header": False, "log_level": "warning"}
    assert json.loads(result.stderr)["mode"] == mode
    assert created["token"] not in result.output
    assert "PRIVATE_ENV" not in result.output and "18080" not in result.output
    assert not application.state.service.list_baselines(
        application.state.store.authenticate(created["token"]))


def test_serve_managed_runtime_uses_explicit_absolute_config_without_starting_models(tmp_path, monkeypatch):
    from spc_agent.provider import SyntheticProvider

    root = tmp_path / "data"
    init_data(root)
    config = tmp_path / "PRIVATE_RUNTIME_CONFIG.json"
    constructed, served = [], []

    def managed(path):
        constructed.append(path)
        return SyntheticProvider()

    monkeypatch.setattr("spc_agent.managed_provider.ManagedLocalProvider", managed)
    monkeypatch.setattr("uvicorn.run", lambda application, **kwargs: served.append(application))
    result = runner.invoke(app, ["serve", str(root), "--runtime-config", str(config)])
    assert result.exit_code == 0, result.output
    assert constructed == [config] and len(served) == 1
    assert str(config) not in result.output and "PRIVATE_RUNTIME_CONFIG" not in result.output


def test_unexpected_cli_failure_is_sanitized(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_PATH_OR_TOKEN")
    monkeypatch.setattr("spc_agent.cli.baseline_dataset", fail)
    result = runner.invoke(app, ["fixture"])
    assert result.exit_code == 1
    assert "local_error" in result.output and "PRIVATE_PATH_OR_TOKEN" not in result.output
