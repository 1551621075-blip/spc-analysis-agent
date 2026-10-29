"""Generated boundary fixtures only; no runtime discovery, model, build or publication."""
from __future__ import annotations

import gzip
import importlib
import io
import os
import stat
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT / "scripts"))
    return importlib.import_module("check_release")


@pytest.mark.parametrize("name", [
    "README.md", ".gitignore", "pyproject.toml", "uv.lock", "scripts/check_release.py",
    "scripts/check_install.py", "scripts/run_acceptance.py", "tests/test_release.py",
    "tests/conftest.py", "docs/runtime.md", "src/spc_agent/statistics.py",
    "src/spc_agent/templates/index.html", "src/spc_agent/static/app.js",
])
def test_allowlist(release, name):
    assert release.allowed_path(name)


@pytest.mark.parametrize("name", [
    "runtime/config.json", "models/model.gguf", ".env", "secret.json", "LICENSE",
    "docs/notes.txt", "src/spc_agent/local.so", "src/spc_agent/local.jar",
    "src/spc_agent/__pycache__/api.pyc", "src/spc_agent/private/data.py",
    "other_project/module.py", "../README.md", "/README.md", "docs/../README.md",
    "docs\\private.md", "docs//runtime.md", "docs/x\n.md", "docs/./runtime.md",
])
def test_reject_nonpublic_paths(release, name):
    try:
        assert not release.allowed_path(name)
    except release.ReleaseError:
        pass


@pytest.mark.parametrize("value", [
    "sk-" + "a" * 32,
    "ghp_" + "b" * 36,
    "AKIA" + "A" * 16,
    "-----BEGIN " + "PRIVATE KEY-----",
    "token = '" + "fixture-sensitive-" * 3 + "'",
    "https://" + "user:password@example.invalid/path",
    "http://127.0.0.1/?" + "token=x",
    "Bearer " + "fixture-sensitive-" * 3,
    "eyJ" + "a" * 20 + "." + "b" * 24 + "." + "c" * 24,
    "/" + "Users/fixture/private.json",
    "file:///" + "home/fixture/private.json",
    "%252F" + "Users%252Ffixture%252Fprivate.json",
    "C:" + "\\Users\\fixture\\private.json",
    "~" + "/private.json",
    "https://" + "cdn." + "jsdelivr.net/npm/tool",
    '<script src="' + "https://assets.example.invalid/tool.js" + '"></script>',
    '@import "' + "//assets.example.invalid/style.css" + '";',
    "fetch('" + "https://assets.example.invalid/data" + "')",
])
def test_rules_have_no_test_exceptions_or_payload_output(release, value):
    for name in ("README.md", "tests/test_provider.py", "scripts/check_release.py"):
        findings = release.scan_content(name, value.encode())
        assert findings and all(value not in issue for issue in findings)


def test_public_reference_links_and_self_scan(release):
    assert not release.scan_content("docs/dependencies.md", b"https://github.com/ggml-org/llama.cpp")
    for name in ("scripts/check_release.py", "scripts/check_install.py", "tests/test_release.py"):
        assert not release.scan_content(name, (PROJECT / name).read_bytes())


@pytest.mark.parametrize("content", [b"\x00binary", b"\xff", b"x" * (2 * 1024 * 1024 + 1)])
def test_binary_and_size_limits(release, content):
    assert release.scan_content("README.md", content)


@pytest.mark.parametrize("kind", ["symlink", "parent-link", "hardlink", "fifo"])
def test_regular_files_no_links_or_devices(release, tmp_path, kind):
    root = tmp_path.resolve()
    target = root / "target"
    target.mkdir()
    (target / "README.md").write_text("public fixture")
    if kind == "parent-link":
        (root / "docs").symlink_to(target, target_is_directory=True)
        name = "docs/README.md"
    else:
        name = "README.md"
        if kind == "symlink":
            (root / name).symlink_to(target / name)
        elif kind == "hardlink":
            os.link(target / name, root / name)
        else:
            os.mkfifo(root / name)
    with pytest.raises(release.ReleaseError):
        release.read_regular(root, name)


def test_never_read_ignored_unapproved_or_parent_runtime(release, tmp_path, monkeypatch):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / ".gitignore").write_text("runtime/\n.env\n")
    (root / "README.md").write_text("public fixture")
    (root / "runtime").mkdir()
    private = root / "runtime" / "config.json"
    private.write_text("must not be read")
    (root / ".env").write_text("must not be read")
    (root / "private.json").write_text("must not be read")
    (root.parent / "runtime.json").write_text("must not be read")
    subprocess.run(["git", "-C", str(root), "add", "-f", "runtime/config.json"], check=True)
    original = release.read_regular
    opened = []

    def guard(base, name, *args):
        assert base == root and name in {".gitignore", "README.md"}
        opened.append(name)
        return original(base, name, *args)

    monkeypatch.setattr(release, "read_regular", guard)
    files, issues = release.source_candidates(root)
    assert set(files) == set(opened) == {".gitignore", "README.md"}
    assert any("ignored-but-tracked" in issue for issue in issues)
    assert any("outside-public-allowlist" in issue for issue in issues)
    assert len(issues) == 2
    with pytest.raises(release.ReleaseError, match="exact Git project root"):
        release.source_candidates(root / "runtime")


def test_git_environment_cannot_redirect_candidate_root(release, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("GIT_DIR", "untrusted-override")
    monkeypatch.setenv("GIT_INDEX_FILE", "untrusted-index")
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    release._git(tmp_path, "ls-files")
    assert not any(key.startswith("GIT_") for key in calls[0][1]["env"])


@pytest.mark.parametrize("name", ["../escape.py", "models/model.gguf", "spc_agent/local.so"])
def test_archive_rejects_unapproved_members(release, tmp_path, name):
    path = tmp_path.resolve() / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"not read")
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFDIR])
def test_zip_member_must_be_regular(release, tmp_path, mode):
    path = tmp_path.resolve() / "fixture.whl"
    info = zipfile.ZipInfo("spc_agent/api.py")
    info.create_system = 3
    info.external_attr = (mode | 0o644) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "fixture")
    with pytest.raises(release.ReleaseError, match="non-regular"):
        release.read_archive(path)


@pytest.mark.parametrize("kind", ["duplicate", "case", "private-directory", "size", "count"])
def test_zip_bounds(release, tmp_path, monkeypatch, kind):
    path = tmp_path.resolve() / "fixture.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("spc_agent/__init__.py", b"fixture")
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("spc_agent/__init__.py", b"")
        elif kind == "case":
            archive.writestr("spc_agent/__INIT__.py", b"")
        elif kind == "private-directory":
            archive.writestr("models/", b"")
        elif kind == "size":
            monkeypatch.setattr(release, "MAX_FILE_BYTES", 1)
        else:
            monkeypatch.setattr(release, "MAX_FILES", 0)
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


def source_fixture():
    return {"README.md": b"original SPC fixture", "scripts/check_release.py": b"# gate\n",
            "pyproject.toml": b'[project]\nname="spc-analysis-agent"\nversion="0.1.0"\n'
            b'requires-python=">=3.12,<3.13"\n[project.scripts]\n'
            b'spc-analysis="spc_agent.cli:main"\n'}


def write_sdist(path, files, link=False):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo("spc_analysis_agent-0.1.0/" + name)
            if link:
                info.type = tarfile.LNKTYPE
                info.linkname = "README.md"
            else:
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def identity_metadata(requires_python):
    return BytesParser().parsebytes(
        ("Metadata-Version: 2.3\nName: spc-analysis-agent\nVersion: 0.1.0\n"
         f"Requires-Python: {requires_python}\n").encode()
    )


@pytest.mark.parametrize("requires_python", [">=3.12,<3.13", "<3.13,>=3.12", "<3.13, >=3.12"])
def test_requires_python_clause_order_accepted(release, tmp_path, requires_python):
    source = source_fixture()
    metadata = identity_metadata(requires_python)
    # Wheel METADATA and sdist PKG-INFO share this identity gate.
    assert release._identity(metadata, source) == "0.1.0"
    files = {**source, "PKG-INFO": metadata.as_bytes()}
    path = tmp_path.resolve() / "spc_analysis_agent-0.1.0.tar.gz"
    write_sdist(path, files)
    assert release.inspect_sdist(path, source) == files


@pytest.mark.parametrize("requires_python", [
    ">=3.11,<3.13", ">=3.12,<3.14", ">3.12,<3.13", ">=3.12,<=3.13", ">=3.12",
    ">=3.12,<3.13,<3.13",
])
def test_requires_python_changed_clauses_rejected(release, requires_python):
    with pytest.raises(release.ReleaseError, match="artifact/source identity mismatch"):
        release._identity(identity_metadata(requires_python), source_fixture())


@pytest.mark.parametrize("requires_python", [
    "", " ", ",>=3.12,<3.13", ">=3.12,<3.13,", ">=3.12,,<3.13", ">=3.12, ,<3.13",
])
@pytest.mark.parametrize("location", ["artifact", "source", "both", "without-source"])
def test_requires_python_empty_clauses_rejected(release, requires_python, location):
    source = source_fixture()
    if location in {"source", "both"}:
        source["pyproject.toml"] = source["pyproject.toml"].replace(
            b">=3.12,<3.13", requires_python.encode()
        )
    if location == "without-source":
        source = None
    metadata = identity_metadata(
        ">=3.12,<3.13" if location == "source" else requires_python
    )
    with pytest.raises(release.ReleaseError, match="Requires-Python|identity"):
        release._identity(metadata, source)


@pytest.mark.parametrize("change", [None, "missing", "extra", "changed", "link"])
def test_sdist_exact_candidates_excluding_pkg_info(release, tmp_path, change):
    source = source_fixture()
    files = {**source, "PKG-INFO": b"Metadata-Version: 2.3\nName: spc-analysis-agent\n"
             b"Version: 0.1.0\nRequires-Python: >=3.12,<3.13\n"}
    if change == "missing":
        del files["scripts/check_release.py"]
    elif change == "extra":
        files["docs/extra.md"] = b"not a source candidate"
    elif change == "changed":
        files["README.md"] += b"changed"
    path = tmp_path.resolve() / "spc_analysis_agent-0.1.0.tar.gz"
    write_sdist(path, files, link=change == "link")
    if change:
        with pytest.raises(release.ReleaseError):
            release.inspect_sdist(path, source)
    else:
        assert release.inspect_sdist(path, source) == files
    assert not (tmp_path / "spc_analysis_agent-0.1.0").exists()


@pytest.mark.parametrize("kind", ["zip-comment", "zip-extra", "tar-pax"])
def test_archive_hidden_metadata_rejected(release, tmp_path, kind):
    if kind == "tar-pax":
        path = tmp_path.resolve() / "spc_analysis_agent-0.1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("spc_analysis_agent-0.1.0/README.md")
            info.pax_headers = {"comment": "/" + "Users/fixture/private.json"}
            archive.addfile(info, io.BytesIO())
    else:
        path = tmp_path.resolve() / "fixture.whl"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("spc_agent/__init__.py")
            if kind == "zip-extra":
                info.extra = b"\x01\x00\x00\x00"
            else:
                archive.comment = b"unreviewed comment"
            archive.writestr(info, b"")
    with pytest.raises(release.ReleaseError):
        release.read_archive(path)


def test_archive_bounds_complete_gzip_stream(release, tmp_path, monkeypatch):
    path = tmp_path.resolve() / "spc_analysis_agent-0.1.0.tar.gz"
    path.write_bytes(gzip.compress(b"x" * 4096))
    monkeypatch.setattr(release, "MAX_TOTAL_BYTES", 1024)
    with pytest.raises(release.ReleaseError, match="expanded-stream"):
        release.read_archive(path)


def test_sdist_root_must_match_filename(release, tmp_path):
    path = tmp_path.resolve() / "spc_analysis_agent-0.2.0.tar.gz"
    write_sdist(path, source_fixture())
    with pytest.raises(release.ReleaseError, match="sdist root"):
        release.read_archive(path)


def test_candidate_count_total_bytes_and_large_file_before_open(release, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "README.md").write_bytes(b"x" * 64)
    monkeypatch.setattr(release, "MAX_FILES", 0)
    with pytest.raises(release.ReleaseError, match="file-count-limit"):
        release.source_candidates(root)
    monkeypatch.setattr(release, "MAX_FILES", 2000)
    monkeypatch.setattr(release, "MAX_TOTAL_BYTES", 32)
    monkeypatch.setattr(release, "read_regular", lambda *args: pytest.fail("oversize file opened"))
    files, issues = release.source_candidates(root)
    assert not files and any("total-size-limit" in issue for issue in issues)


def test_release_stops_before_archives_on_source_failure(release, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "source_candidates", lambda _: ({}, ["fixture blocked"]))
    monkeypatch.setattr(release, "explicit_archive", lambda *_: pytest.fail("archive read"))
    assert release.main(["--root", str(tmp_path.resolve()), "--archive", "dist/fixture.whl"]) == 1
    assert "archives not read" in capsys.readouterr().out
