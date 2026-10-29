"""Inert browser-smoke contract checks: never start Chrome, HTTP, runtime or threads."""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from html.parser import HTMLParser
from pathlib import Path

import pytest
import uvicorn

from spc_agent.domain import Dataset
from spc_agent.fixtures import baseline_dataset, demo_datasets
from spc_agent.statistics import analyze, build_baseline

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "browser_smoke.mjs"
NODE = shutil.which("node")


def node(*args):
    if NODE is None:
        pytest.skip("Installed Node required for inert script checks")
    return subprocess.run(
        [NODE, *map(str, args)], cwd=PROJECT, capture_output=True, text=True,
        timeout=15, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )


@pytest.fixture(scope="module")
def exports():
    result = node(
        "--input-type=module", "-e",
        f"const m=await import({json.dumps(SCRIPT.as_uri())});"
        "console.log(JSON.stringify({python:m.PY_SERVER,selectors:m.SELECTORS,project:m.PROJECT}));",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_node_syntax_and_safe_no_argument_default():
    syntax = node("--check", SCRIPT)
    assert syntax.returncode == 0, syntax.stderr
    default = node(SCRIPT)
    assert default.returncode == 0
    assert "No browser starts without --run-chrome" in default.stdout
    assert "--artifacts-parent" in default.stdout


def test_embedded_inert_self_tests():
    result = node(SCRIPT, "--self-test")
    assert result.returncode == 0, result.stderr
    assert "PASS inert" in result.stdout


@pytest.mark.parametrize("args", [
    ["--run-chrome"],
    ["--artifacts-parent", "/private/tmp"],
    ["--run-chrome", "--artifacts-parent", str(PROJECT)],
    ["--run-chrome", "--artifacts-parent", str(PROJECT / "scripts")],
    ["--run-chrome", "--artifacts-parent", "relative"],
    ["--run-chrome", "--artifacts-parent", "/private/tmp", "--run-chrome"],
    ["--model-url", "http://127.0.0.1:8000"],
    ["--help", "--run-chrome"],
])
def test_invalid_commands_fail_before_any_launch(args):
    result = node(SCRIPT, *args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr


def test_python_bootstrap_compiles_and_has_no_original_project_import(exports):
    compile(exports["python"], "<SPC TEST smoke bootstrap>", "exec")
    assert "industrial_diagnosis" not in exports["python"]
    assert "ManagedLocalProvider" not in exports["python"]
    assert "LocalProvider(" not in exports["python"]
    assert "SyntheticProvider()" in exports["python"]
    assert "initialize(data_root)" in exports["python"]
    assert "server.run(sockets=[sock])" in exports["python"]


def test_selectors_reference_the_actual_shipped_ui(exports):
    html = (PROJECT / "src/spc_agent/templates/index.html").read_text()
    javascript = (PROJECT / "src/spc_agent/static/app.js").read_text()
    for selector in exports["selectors"].values():
        for element_id in re.findall(r"#([a-z][a-z0-9-]*)", selector):
            assert f'id="{element_id}"' in html, selector
    for css_class in ["chart-point", "chart-series", "chart-missing", "authority-panel", "draft-panel"]:
        assert css_class in javascript


@pytest.mark.parametrize("select_id", ["baseline-decision", "proposal-decision"])
def test_explicit_review_options_are_real_opening_tags(select_id):
    class Options(HTMLParser):
        selected = False

        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.selected = attrs.get("id") == select_id
            elif tag == "option" and self.selected:
                self.values.append(attrs.get("value"))

        def handle_endtag(self, tag):
            if tag == "select":
                self.selected = False

    parser = Options()
    parser.feed((PROJECT / "src/spc_agent/templates/index.html").read_text())
    assert parser.values == ["", "approved", "rejected"]


def test_bootstrap_inputs_only_and_fixed_independent_followup(exports, monkeypatch):
    """Execute actual init/import; replace every process/network/thread endpoint inertly."""
    def forbidden(*args, **kwargs):
        pytest.fail("Smoke contract test attempted network, process or background thread")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    runs = []

    class FakeSocket:
        def __init__(self, family, kind):
            assert (family, kind) == (socket.AF_INET, socket.SOCK_STREAM)

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def listen(self, backlog):
            assert backlog == 128

        def getsockname(self):
            return ("127.0.0.1", 54321)

    class FakeServer:
        def __init__(self, config):
            assert config.access_log is False
            assert config.proxy_headers is False
            assert config.server_header is False

        def run(self, sockets):
            assert len(sockets) == 1 and isinstance(sockets[0], FakeSocket)
            runs.append(True)

    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    with tempfile.TemporaryDirectory(prefix="spc-browser-inert-") as directory:
        root = Path(directory).resolve()
        assert not root.is_relative_to(PROJECT.resolve())
        channel = io.StringIO()
        monkeypatch.setattr(sys, "argv", ["<inert-smoke>", str(root / "app")])
        monkeypatch.setattr(sys, "stdout", channel)
        namespace = {"__name__": "__inert_smoke__"}
        try:
            exec(compile(exports["python"], "<inert smoke bootstrap>", "exec"), namespace)
        finally:
            monkeypatch.setattr(sys, "stdout", channel)
        assert runs == [True]
        message = json.loads(channel.getvalue())
        assert message["type"] == "ready" and message["provider"]["testOnly"] is True
        assert message["datasets"]["baseline"] == baseline_dataset().dataset_id
        app = namespace["app"]
        actor = app.state.store.authenticate(message["token"])
        assert app.state.service.list_baselines(actor) == []
        assert app.state.service.list_evaluations(actor) == []
        assert app.state.service.list_risks(actor) == []
        assert app.state.actions.list(actor) == []
        followup = Dataset.model_validate(app.state.store.dataset(message["datasets"]["followup"], actor))
        original = demo_datasets()["mean_shift"]
        assert followup == demo_datasets()["followup"]
        assert len(app.state.store.list_datasets(actor)) == len(demo_datasets()) + 1
        assert min(g.timestamp_ms for g in followup.groups) > message["execution_ms"]
        assert message["execution_ms"] > max(g.timestamp_ms for g in original.groups)
        assert {g.group_id for g in followup.groups}.isdisjoint(g.group_id for g in original.groups)
        assert [g.values for g in followup.groups] != [g.values for g in original.groups]
        assert [g.values for g in followup.groups] != [g.values for g in demo_datasets()["normal"].groups]
        analysis = analyze(followup, build_baseline(baseline_dataset(), "inert-browser-baseline"))
        assert analysis["status"] == "stable"
        assert analysis["issues"] == [] and analysis["rule_hits"] == []
        assert analysis["capability"]["status"] == "available"
        # Credentials may traverse the private pipe, never CLI arguments/env or disk.
        assert message["token"] not in " ".join(sys.argv)
        assert message["token"] not in str(os.environ)
        for file in root.rglob("*"):
            if file.is_file():
                assert message["token"].encode() not in file.read_bytes()


def test_test_server_delays_only_a_real_get_response(exports):
    tree = ast.parse(exports["python"])
    delayed = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "DelayedResponse")
    source = ast.get_source_segment(exports["python"], delayed)
    assert "await app(scope, receive, send_response)" in source
    assert "await reply(message)" in source
    assert "'/api/datasets'" in source and "'GET'" in source
    assert "http.response.start" in source
    assert "release.wait, 15" in source
    assert "json.dumps" not in source


def test_script_has_no_unowned_process_or_profile_operations():
    source = SCRIPT.read_text()
    for forbidden in ["killall", "pkill", "--no-sandbox", "--disable-web-security", "puppeteer", "playwright",
                      "Application Support/Google/Chrome", "process.env", "shell: true", "ManagedLocalProvider"]:
        assert forbidden not in source
    assert "--user-data-dir=${profile}" in source
    assert "await realpath(this.options.parent)" in source
    assert "mkdtemp(join(parent, 'spc-browser-'))" in source
    assert "ownedDirectory(this.runtime)" in source
    assert "row.started === owned.started" in source
    assert "constants.O_NOFOLLOW" in source
    assert "Network.getResponseBody" in source
    assert "Input.dispatchMouseEvent" in source and "Input.insertText" in source
    assert "this.requestBody(response, ['action_id', 'followup_hash', 'note'])" in source
    assert "no_plaintext_tokens_in_owned_files" in source
    assert "no_visible_raw_JSON_dump_or_persistent_browser_credentials" in source


def test_svg_segmentation_invalid_null_and_break_before(exports):
    result = node(
        "--input-type=module", "-e",
        f"const m=await import({json.dumps(SCRIPT.as_uri())});"
        "const p=[{range:1},{range:2},{range:3,valid:false},{range:4},{range:5},"
        "{range:6,break_before:true},{range:7},{range:null},{range:9}];"
        "console.log(JSON.stringify(m.seriesRuns(p,'range')));",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [[0, 1], [3, 4], [5, 6]]


def test_chart_monitoring_cases_match_declared_smoke_expectations():
    baseline = build_baseline(baseline_dataset(), "inert-browser-baseline")
    fixtures = demo_datasets()
    normal = analyze(fixtures["normal"], baseline)
    assert normal["status"] == "stable"
    shift = analyze(fixtures["mean_shift"], baseline)
    assert shift["status"] == "signals" and shift["rule_hits"]
    variance = analyze(fixtures["variance"], baseline)
    assert any(hit["chart"] == "r" for hit in variance["rule_hits"])
    missing = analyze(fixtures["missing"], baseline)
    assert missing["status"] == "not_evaluable"
    assert sum(point["valid"] for point in missing["points"]) == 23


def test_child_environment_is_minimal_and_contains_no_tokens(exports):
    result = node(
        "--input-type=module", "-e",
        f"const m=await import({json.dumps(SCRIPT.as_uri())});const s=new m.Smoke({{}});"
        "s.runtime='/private/tmp/OWNED';s.secrets=['not-an-environment-variable'];"
        "console.log(JSON.stringify(s.environment()));",
    )
    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)
    assert set(environment) == {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"}
    assert environment["HOME"] == environment["TMPDIR"] == "/private/tmp/OWNED"
    assert environment["PYTHONNOUSERSITE"] == environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "token" not in result.stdout.lower()


def test_descendant_tracking_survives_reparenting_and_rejects_pid_reuse(exports):
    result = node(
        "--input-type=module", "-e",
        f"const m=await import({json.dumps(SCRIPT.as_uri())});const s=new m.Smoke({{}});"
        "const child={pid:100,exitCode:null,signalCode:null};s.children=[child];"
        "s.processTable=async()=>[{pid:100,parent:1,started:'A'},{pid:101,parent:100,started:'B'},"
        "{pid:102,parent:101,started:'C'},{pid:200,parent:1,started:'UNOWNED'}];"
        "const initial=await s.snapshotOwnedProcesses();child.exitCode=1;"
        "s.processTable=async()=>[{pid:101,parent:1,started:'REUSED'},{pid:102,parent:1,started:'C'},"
        "{pid:200,parent:1,started:'UNOWNED'}];"
        "const after=await s.snapshotOwnedProcesses();"
        "console.log(JSON.stringify({initial:initial.map(r=>r.pid),after:after.map(r=>r.pid)}));",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"initial": [100, 101, 102], "after": [102]}


def test_safe_errors_never_dump_compared_payloads(exports):
    result = node(
        "--input-type=module", "-e",
        f"const m=await import({json.dumps(SCRIPT.as_uri())});const s=new m.Smoke({{}});"
        "s.secrets=['SECRET'];console.log(JSON.stringify(s.safe('SECRET mismatch\\n+ PRIVATE PAYLOAD')));",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == "[REDACTED] mismatch"
