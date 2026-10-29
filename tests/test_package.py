"""Artifact fixtures and installation contracts; never build or run a real model.

The installer subprocess tests are mocks, not claims of an actual wheel install.
Source resource/API tests complement, but never replace, the post-freeze wheel gate.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import json
import sys
import zipfile
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]
META = "spc_analysis_agent-0.1.0.dist-info"
WHEEL_NAME = "spc_analysis_agent-0.1.0-py3-none-any.whl"


@pytest.fixture
def checks(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT / "scripts"))
    return importlib.import_module("check_release"), importlib.import_module("check_install")


def fixture_payload(release):
    return {
        **{f"spc_agent/{module}.py": b"# original synthetic package fixture\n"
           for module in release.MODULES},
        "spc_agent/static/app.js": b"'use strict';",
        "spc_agent/static/style.css": b"body { color: black; }",
        "spc_agent/templates/index.html": b"<!doctype html><title>SPC fixture</title>",
        META + "/METADATA": b"Metadata-Version: 2.3\nName: spc-analysis-agent\n"
        b"Version: 0.1.0\nRequires-Python: >=3.12,<3.13\n",
        META + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        META + "/entry_points.txt": b"[console_scripts]\nspc-analysis = spc_agent.cli:main\n",
    }


def fixture_source(payload):
    return {**{"src/" + name: data for name, data in payload.items()
               if name.startswith("spc_agent/")},
            "pyproject.toml": b'[project]\nname="spc-analysis-agent"\nversion="0.1.0"\n'
            b'requires-python=">=3.12,<3.13"\n[project.scripts]\n'
            b'spc-analysis="spc_agent.cli:main"\n'}


def write_wheel(path, payload, *, mutate_record=None, tamper=False):
    content = dict(payload)
    rows = []
    for name, data in content.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append([name, "sha256=" + digest, str(len(data))])
    rows.append([META + "/RECORD", "", ""])
    if mutate_record:
        mutate_record(rows)
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    content[META + "/RECORD"] = output.getvalue().encode()
    if tamper:
        content["spc_agent/cli.py"] += b"# tampered\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in content.items():
            archive.writestr(name, data)
    return content


def test_wheel_identity_modules_resources_record_and_source_parity(checks, tmp_path):
    release, _ = checks
    payload = fixture_payload(release)
    path = tmp_path.resolve() / WHEEL_NAME
    expected = write_wheel(path, payload)
    assert release.inspect_wheel(path, fixture_source(payload)) == expected


@pytest.mark.parametrize("missing", [
    "spc_agent/templates/index.html", "spc_agent/static/app.js", "spc_agent/static/style.css",
    "spc_agent/statistics.py", "spc_agent/investigation.py", "spc_agent/managed_provider.py",
    META + "/entry_points.txt", META + "/WHEEL", META + "/METADATA",
])
def test_missing_required_payload_fails(checks, tmp_path, missing):
    release, _ = checks
    payload = fixture_payload(release)
    del payload[missing]
    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError):
        release.inspect_wheel(path)


@pytest.mark.parametrize("kind", ["empty-resource", "native-tag", "platlib", "extra-entry",
                                      "foreign-metadata", "duplicate-identity", "filename"])
def test_changed_wheel_contract_fails(checks, tmp_path, kind):
    release, _ = checks
    payload = fixture_payload(release)
    if kind == "empty-resource":
        payload["spc_agent/static/app.js"] = b""
    elif kind == "native-tag":
        payload[META + "/WHEEL"] = payload[META + "/WHEEL"].replace(
            b"py3-none-any", b"cp312-cp312-macosx_11_0_arm64")
    elif kind == "platlib":
        payload[META + "/WHEEL"] = payload[META + "/WHEEL"].replace(b"true", b"false")
    elif kind == "extra-entry":
        payload[META + "/entry_points.txt"] += b"other = spc_agent.cli:main\n"
    elif kind == "foreign-metadata":
        payload["spc_analysis_agent-9.9.dist-info/WHEEL"] = b"Wheel-Version: 1.0\n"
    elif kind == "duplicate-identity":
        payload[META + "/METADATA"] += b"Version: 0.1.0\n"
    path = tmp_path.resolve() / ("renamed.whl" if kind == "filename" else WHEEL_NAME)
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError):
        release.inspect_wheel(path)


@pytest.mark.parametrize("kind", ["tampered", "missing-row", "extra-row", "duplicate-row",
                                      "bad-size", "bad-hash", "self-hash"])
def test_record_exact_coverage_hash_and_size(checks, tmp_path, kind):
    release, _ = checks

    def mutate(rows):
        if kind == "missing-row":
            rows.pop(0)
        elif kind == "extra-row":
            rows.append(["spc_agent/extra.py", "", ""])
        elif kind == "duplicate-row":
            rows.append(rows[0])
        elif kind == "bad-size":
            rows[0][2] = "9999"
        elif kind == "bad-hash":
            rows[0][1] = "sha256=invalid"
        elif kind == "self-hash":
            rows[-1][1] = "sha256=invalid"

    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, fixture_payload(release), mutate_record=mutate, tamper=kind == "tampered")
    with pytest.raises(release.ReleaseError, match="RECORD"):
        release.inspect_wheel(path)


@pytest.mark.parametrize("kind", ["changed-source", "extra-source", "extra-wheel", "version"])
def test_wheel_source_parity_is_exact(checks, tmp_path, kind):
    release, _ = checks
    payload = fixture_payload(release)
    source = fixture_source(payload)
    if kind == "changed-source":
        source["src/spc_agent/cli.py"] += b"# new source\n"
    elif kind == "extra-source":
        source["src/spc_agent/new_module.py"] = b"# new source\n"
    elif kind == "extra-wheel":
        payload["spc_agent/new_module.py"] = b"# unreviewed artifact\n"
    else:
        source["pyproject.toml"] = source["pyproject.toml"].replace(b"0.1.0", b"0.2.0")
    path = tmp_path.resolve() / WHEEL_NAME
    write_wheel(path, payload)
    with pytest.raises(release.ReleaseError, match="parity|identity"):
        release.inspect_wheel(path, source)


def install_fixture(checks, tmp_path):
    release, install = checks
    base = tmp_path.resolve()
    root, work, wheelhouse = (base / name for name in ("source", "work", "wheelhouse"))
    for path in (root, work, wheelhouse):
        path.mkdir()
    payload = fixture_payload(release)
    (root / "dist").mkdir()
    wheel = root / "dist" / WHEEL_NAME
    write_wheel(wheel, payload)
    # Not a real dependency: command-contract tests mock installation, never execute this file.
    (wheelhouse / "generated_dependency.whl").write_bytes(b"fixture")
    return install, wheel, fixture_source(payload), root, wheelhouse, work


def good_report():
    return {"source_free": True, "fresh_venv": True, "new_os": False,
            "actual_distribution": True, "modules": True, "console_help": True,
            "fixture_json": True, "health": True, "static_templates": True,
            "inference_checked": False, "network_used": False}


@pytest.mark.parametrize("case", [None, "mean_shift"])
def test_install_new_venv_offline_isolated_contract(checks, tmp_path, monkeypatch, case):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    calls = []

    def run(command, **options):
        calls.append((command, options))
        if "venv" in command:
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"generated interpreter fixture; not executed")
        return SimpleNamespace(returncode=0, stdout=json.dumps(good_report()))

    monkeypatch.setenv("PYTHONPATH", "ambient-path-must-not-propagate")
    monkeypatch.setenv("HTTP_PROXY", "ambient-proxy-must-not-propagate")
    monkeypatch.setenv("SPC_RUNTIME_CONFIG", "ambient-config-must-not-propagate")
    monkeypatch.setattr(install.subprocess, "run", run)
    assert install.probe_install(wheel, source, root, wheelhouse, work, case) == good_report()
    create, install_call, dependency_check, probe = [command for command, _ in calls]
    assert create[:5] == [sys.executable, "-I", "-m", "venv", "--copies"]
    assert "--system-site-packages" not in create
    assert {"--no-index", "--only-binary=:all:", "--no-cache-dir", "--find-links"} <= set(install_call)
    assert install.INSTALL in install_call and "build" not in install_call
    assert dependency_check[-1] == "check"
    assert probe[1:5] == ["-I", "-B", "-c", install.PROBE]
    for _, options in calls:
        assert options["cwd"] == work and not options.get("shell")
        assert options["stdin"] == install.subprocess.DEVNULL
        assert not {"PYTHONPATH", "HTTP_PROXY", "SPC_RUNTIME_CONFIG"} & options["env"].keys()
    manifest = json.loads((work / "probe-manifest.json").read_text())
    assert manifest["case"] == case
    assert set(manifest["modules"]) == checks[0].MODULES
    assert (work / wheel.name).read_bytes() == wheel.read_bytes()


@pytest.mark.parametrize("failure", ["install", "dependency", "invalid-json", "incomplete"])
def test_install_fails_closed_with_redacted_subprocess_output(checks, tmp_path, monkeypatch, failure):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    calls = []

    def run(command, **options):
        calls.append(command)
        if "venv" in command:
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fixture")
        failed = (failure == "install" and "install" in command
                  or failure == "dependency" and command[-1] == "check")
        return SimpleNamespace(returncode=int(failed), stderr="withheld subprocess details",
                               stdout="not json" if failure == "invalid-json" else "{}")

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(checks[0].ReleaseError) as exc:
        install.probe_install(wheel, source, root, wheelhouse, work)
    assert "withheld subprocess details" not in str(exc.value)
    if failure == "install":
        assert len(calls) == 2


@pytest.mark.parametrize("kind", ["inside-source", "nonwheel", "link", "empty"])
def test_wheelhouse_boundary(checks, tmp_path, kind):
    release, install = checks
    root = tmp_path.resolve() / "source"
    root.mkdir()
    path = root / "wheelhouse" if kind == "inside-source" else tmp_path.resolve() / "wheelhouse"
    path.mkdir()
    if kind == "nonwheel":
        (path / "config.json").write_bytes(b"never read")
    elif kind == "link":
        (path / "dependency.whl").symlink_to(tmp_path / "missing")
    with pytest.raises(release.ReleaseError):
        install.validate_wheelhouse(path, root)


def test_no_source_tree_environment_or_invalid_case(checks, tmp_path):
    install, wheel, source, root, wheelhouse, work = install_fixture(checks, tmp_path)
    with pytest.raises(checks[0].ReleaseError, match="outside"):
        install.probe_install(wheel, source, root, wheelhouse, root)
    with pytest.raises(checks[0].ReleaseError, match="case"):
        install.probe_install(wheel, source, root, wheelhouse, work, "../private")


def test_probe_code_compiles_and_denies_external_effects(checks):
    _, install = checks
    compile(install.PROBE, "<installation-probe>", "exec")
    compile(install.INSTALL, "<offline-install>", "exec")
    for code in (install.PROBE, install.INSTALL):
        assert "sys.addaudithook" in code
        assert all(event in code for event in ("socket.connect", "subprocess.Popen", "os.system"))
    assert 'def decision(' in install.PROBE and 'def session(' in install.PROBE


def test_install_main_never_proceeds_after_failed_release(checks, tmp_path, monkeypatch, capsys):
    _, install = checks
    monkeypatch.setattr(install, "source_candidates", lambda _: ({}, ["blocked"]))
    monkeypatch.setattr(install, "probe_install", lambda *args: pytest.fail("install attempted"))
    assert install.main(["--root", str(tmp_path.resolve()), "--wheel", "dist/reviewed.whl",
                         "--wheelhouse", str(tmp_path.resolve())]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_source_resources_api_and_cli_without_inference(tmp_path, monkeypatch):
    """Actual source-level check; final source-free installed-wheel execution is a separate gate."""
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from spc_agent.api import create_app
    from spc_agent.cli import app
    from spc_agent.domain import Dataset
    from spc_agent.fixtures import baseline_dataset, demo_datasets
    from spc_agent.provider import SyntheticProvider

    def denied(*args, **kwargs):
        pytest.fail("resource and fixture checks must not invoke a model or start a process")

    monkeypatch.setattr(SyntheticProvider, "decision", denied)
    monkeypatch.setattr(SyntheticProvider, "session", denied)
    monkeypatch.setattr("subprocess.Popen", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    resources = {"/": "templates/index.html", "/static/app.js": "static/app.js",
                 "/static/style.css": "static/style.css"}
    for resource in resources.values():
        assert files("spc_agent").joinpath(resource).read_bytes()
    with TestClient(create_app(tmp_path / "data", SyntheticProvider()),
                    base_url="http://127.0.0.1") as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["inferenceChecked"] is False
        assert response.json()["testOnly"] is True
        for route, resource in resources.items():
            response = client.get(route)
            assert response.status_code == 200
            assert response.content == files("spc_agent").joinpath(resource).read_bytes()
        assert client.get("/api/me").status_code == 401
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    for case, expected in {"baseline": baseline_dataset(), **demo_datasets()}.items():
        args = ["fixture"] if case == "baseline" else ["fixture", "--case", case]
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        assert Dataset.model_validate(json.loads(result.stdout)) == expected
