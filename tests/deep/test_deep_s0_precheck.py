import json
import shutil
from pathlib import Path

import pytest

from vrp import s0_precheck
from tests.deep.conftest import git

pytestmark = pytest.mark.deep


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pkg").mkdir()
    (path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (path / "pkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (path / "test_core.py").write_text(
        "from pkg.core import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    (path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (path / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    return path


def test_git_snapshot_clean_repo(tmp_path):
    repo = _init_repo(tmp_path / "clean-repo")
    snap = s0_precheck.git_snapshot(repo)
    assert snap["is_repo"] is True
    assert len(snap["head"]) == 40
    assert snap["branch"]
    assert snap["dirty"] is False
    assert snap["dirty_count"] == 0
    assert snap["dirty_files"] == []


def test_git_snapshot_dirty_repo(tmp_path):
    repo = _init_repo(tmp_path / "dirty-repo")
    (repo / "pkg" / "core.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (repo / "note.txt").write_text("scratch\n", encoding="utf-8")
    snap = s0_precheck.git_snapshot(repo)
    assert snap["dirty"] is True
    assert snap["dirty_count"] == 2
    assert "pkg/core.py" in snap["dirty_files"]
    assert "note.txt" in snap["dirty_files"]


def test_git_snapshot_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert s0_precheck.git_snapshot(plain) == {"is_repo": False}


def test_discover_commands_pyproject_full(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n[tool.mypy]\n", encoding="utf-8"
    )
    assert s0_precheck.discover_commands(tmp_path) == {
        "test": ["pytest"],
        "lint": ["ruff check ."],
        "typecheck": ["mypy ."],
    }


def test_discover_commands_pyproject_test_only(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    assert s0_precheck.discover_commands(tmp_path) == {"test": ["pytest"]}


def test_discover_commands_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "tsc",
                    "test": "jest",
                    "test:unit": "jest unit",
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "check": "tsc --noEmit",
                    "custom": "echo hi",
                }
            }
        ),
        encoding="utf-8",
    )
    assert s0_precheck.discover_commands(tmp_path) == {
        "build": ["npm run build"],
        "test": ["npm run test", "npm run test:unit"],
        "lint": ["npm run lint"],
        "typecheck": ["npm run typecheck", "npm run check"],
    }


def test_discover_commands_makefile(tmp_path):
    (tmp_path / "Makefile").write_text(
        "test:\n\tpytest\nbuild:\n\tpython -m build\ndeploy:\n\techo deploy\n", encoding="utf-8"
    )
    assert s0_precheck.discover_commands(tmp_path) == {
        "test": ["make test"],
        "build": ["make build"],
    }


def test_discover_commands_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n", encoding="utf-8")
    assert s0_precheck.discover_commands(tmp_path) == {
        "build": ["cargo build"],
        "test": ["cargo test"],
        "lint": ["cargo clippy"],
    }


def test_discover_commands_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.21\n", encoding="utf-8")
    assert s0_precheck.discover_commands(tmp_path) == {
        "build": ["go build ./..."],
        "test": ["go test ./..."],
    }


def test_discover_commands_empty_repo(tmp_path):
    (tmp_path / "empty-dir").mkdir()
    assert s0_precheck.discover_commands(tmp_path / "empty-dir") == {}


def test_census_fallback_counts(tmp_path):
    repo = tmp_path / "census"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (repo / "b.js").write_text("a();\nb();\nc();\n", encoding="utf-8")
    (repo / "doc.md").write_text("# t\n\nbody\nmore\n", encoding="utf-8")
    (repo / "data.json").write_text("{}\n{}\n{}\n{}\n", encoding="utf-8")
    (repo / "notes.txt").write_text("skip\n" * 6, encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "nm.py").write_text("x = 1\n", encoding="utf-8")
    census = s0_precheck.census_fallback(repo)
    assert census["tool"] == "fallback-ext"
    langs = {entry["name"]: entry for entry in census["languages"]}
    assert set(langs) == {"Python", "JavaScript", "Markdown", "JSON"}
    assert langs["Python"] == {"name": "Python", "files": 1, "lines": 2, "code": 2}
    assert langs["JavaScript"]["files"] == 1
    assert langs["JavaScript"]["lines"] == 3
    assert langs["JSON"]["lines"] == 4
    codes = [entry["code"] for entry in census["languages"]]
    assert codes == sorted(codes, reverse=True)
    for entry in census["languages"]:
        assert entry["code"] == entry["lines"]


def test_census_scc_none_iff_scc_missing(tmp_path):
    repo = tmp_path / "census-scc"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    census = s0_precheck.census_scc(repo)
    if shutil.which("scc"):
        assert census is not None
        assert census["tool"] == "scc"
    else:
        assert census is None


def test_git_snapshot_dirty_files_nonascii(tmp_path):
    """R-N：非 ASCII 路径 dirty_files 保真（core.quotepath=false，不八进制转义）。

    repo 级钉 core.quotepath=true（git 默认）：本机全局配置 quotepath=false
    会掩盖修复面（环境性绿），钉默认值让守卫在任意机器配置下都真检验。
    """
    repo = _init_repo(tmp_path / "nonascii-repo")
    (repo / "文档.txt").write_text("中文\n", encoding="utf-8")
    git(repo, "config", "core.quotepath", "true")
    snap = s0_precheck.git_snapshot(repo)
    assert snap["dirty"] is True
    assert snap["dirty_count"] == 1
    assert snap["dirty_files"] == ["文档.txt"]


def test_precheck_end_to_end(tmp_path):
    repo = _init_repo(tmp_path / "precheck-repo")
    result = s0_precheck.precheck(repo)
    assert set(result) >= {"repo", "name", "git", "commands", "languages"}
    assert result["name"] == repo.name
    assert result["git"]["is_repo"] is True
    assert result["git"]["dirty"] is False
    assert len(result["git"]["head"]) == 40
    assert result["commands"] == {"test": ["pytest"]}
    assert result["languages"]["tool"] == "fallback-ext"
    python = next(e for e in result["languages"]["languages"] if e["name"] == "Python")
    assert python["files"] == 3
    written = json.loads((repo / ".vrp" / "precheck.json").read_text(encoding="utf-8"))
    assert written == result
