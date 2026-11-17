"""I4（构想v11 甜点）：`vrp doctor` 工具链环境自诊断测试。

- 单一事实源：聚合各适配器 find_*（monkeypatch 注入替身——不碰真实环境，
  确定性+快；真实环境面由手工 `vrp doctor` 冒烟覆盖）
- 分层语义：core 缺失 rc=1；engine 缺失只降级 rc=0
- 版本面：git/node --version 探针（run_tree 替身）；探针失败版本 None 但
  ok 仍 True（可用性才是判定）
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from vrp import doctor
from vrp.cli import build_parser, cmd_doctor
from vrp.cli import main as cli_main


def _fake_run_tree_ok(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
    if cmd[:2] == ["git", "--version"]:
        return {"returncode": 0, "timed_out": False, "stdout": "git version 2.44.0\n", "stderr": ""}
    if cmd[:2] == ["node", "--version"]:
        return {"returncode": 0, "timed_out": False, "stdout": "v22.11.0\n", "stderr": ""}
    return {"returncode": 0, "timed_out": False, "stdout": "", "stderr": ""}


def _patch_all_present(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/bin/{name}.exe" if name in ("git", "node", "scc") else None)
    monkeypatch.setattr(doctor.procs, "run_tree", _fake_run_tree_ok)
    for spec in doctor.CORE_SPEC + doctor.ENGINE_SPEC:
        finder = spec["finder"]
        if finder is not None:
            module, func = finder
            monkeypatch.setattr(module, func, lambda: "C:/tools/fake-bin")


def test_all_present_ok(monkeypatch):
    _patch_all_present(monkeypatch)
    result = doctor.run_doctor()
    assert result["missing_core"] == []
    assert result["missing_engine"] == []
    assert result["ok"] == result["checked"]
    assert doctor.doctor_rc(result) == 0
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["git"]["version"] == "2.44.0"
    assert by_name["node"]["version"] == "22.11.0"


def test_tier_split_counts():
    """core 4 件（python/git/node/cbm）+ engine 15 件（21 引擎外链+scc）。"""
    core_names = [s["name"] for s in doctor.CORE_SPEC]
    engine_names = [s["name"] for s in doctor.ENGINE_SPEC]
    assert core_names == ["python", "git", "node", "cbm"]
    assert "scc" in engine_names and "astgrep" in engine_names and "code-atlas" in engine_names
    assert not set(core_names) & set(engine_names)


def test_missing_core_fails_rc(monkeypatch):
    _patch_all_present(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "C:/bin/node.exe" if name == "node" else None)
    result = doctor.run_doctor()
    assert result["missing_core"] == ["git"]
    assert doctor.doctor_rc(result) == 1
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["git"]["hint"] and by_name["git"]["located"] is None


def test_cbm_not_found_honest(monkeypatch):
    """find_cbm 抛 FileNotFoundError → 诚实转 missing，不裸崩。"""
    _patch_all_present(monkeypatch)

    def _raise():
        raise FileNotFoundError("codebase-memory-mcp binary not found")

    monkeypatch.setattr(doctor.s1_cbm, "find_cbm", _raise)
    result = doctor.run_doctor()
    assert "cbm" in result["missing_core"]
    assert doctor.doctor_rc(result) == 1


def test_missing_engine_degrades_rc0(monkeypatch):
    _patch_all_present(monkeypatch)
    monkeypatch.setattr(doctor.s2_astgrep, "find_astgrep", lambda: None)
    result = doctor.run_doctor()
    assert result["missing_core"] == []
    assert result["missing_engine"] == ["astgrep"]
    assert doctor.doctor_rc(result) == 0
    by_name = {c["name"]: c for c in result["checks"]}
    assert "VRP_ASTGREP_BIN" in by_name["astgrep"]["hint"]


def test_version_probe_commands_exact(monkeypatch):
    seen = []

    def fake(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        seen.append(list(cmd))
        return {"returncode": 0, "timed_out": False, "stdout": "x 1.0\n", "stderr": ""}

    monkeypatch.setattr(doctor.procs, "run_tree", fake)
    _patch_all_present(monkeypatch)
    monkeypatch.setattr(doctor.procs, "run_tree", fake)
    doctor.run_doctor()
    assert ["git", "--version"] in seen
    assert ["node", "--version"] in seen
    assert len(seen) == 2


def test_probe_failure_version_none_but_ok(monkeypatch):
    """探针非零/超时/空输出 → version None；可用性判定不受版本探针影响。"""
    _patch_all_present(monkeypatch)
    monkeypatch.setattr(
        doctor.procs,
        "run_tree",
        lambda cmd, cwd=None, timeout=None, env=None, normal_priority=False: {
            "returncode": 1, "timed_out": False, "stdout": "", "stderr": "boom",
        },
    )
    result = doctor.run_doctor()
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["git"]["ok"] is True
    assert by_name["git"]["version"] is None


def test_probe_timeout_version_none(monkeypatch):
    _patch_all_present(monkeypatch)
    monkeypatch.setattr(
        doctor.procs,
        "run_tree",
        lambda cmd, cwd=None, timeout=None, env=None, normal_priority=False: {
            "returncode": -1, "timed_out": True, "stdout": "", "stderr": "",
        },
    )
    result = doctor.run_doctor()
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["node"]["ok"] is True
    assert by_name["node"]["version"] is None


def test_python_check_from_sys(monkeypatch):
    _patch_all_present(monkeypatch)
    result = doctor.run_doctor()
    py = next(c for c in result["checks"] if c["name"] == "python")
    assert py["ok"] is True
    assert py["version"] == platform.python_version()
    assert py["located"] == sys.executable
    assert result["python_ok"] is True


def test_deterministic(monkeypatch):
    _patch_all_present(monkeypatch)
    assert doctor.run_doctor() == doctor.run_doctor()


def test_render_text_shape(monkeypatch):
    _patch_all_present(monkeypatch)
    monkeypatch.setattr(doctor.s2_astgrep, "find_astgrep", lambda: None)
    text = doctor.render_text(doctor.run_doctor())
    assert "CORE" in text and "ENGINE" in text
    assert "[ok]" in text and "[missing]" in text
    assert "summary: " in text
    assert "VRP_ASTGREP_BIN" in text  # 缺失项提示可见
    assert "engine missing: astgrep" in text


def test_parser_no_repo_arg():
    args = build_parser().parse_args(["doctor"])
    assert args.func is cmd_doctor
    assert not hasattr(args, "repo")
    args_json = build_parser().parse_args(["doctor", "--json"])
    assert args_json.json is True


def test_cli_end_to_end_json(monkeypatch, capsys):
    fixed = {
        "checked": 2,
        "ok": 2,
        "missing_core": [],
        "missing_engine": [],
        "python_ok": True,
        "checks": [
            {"name": "python", "tier": "core", "ok": True, "located": "py", "version": "3.11.0", "hint": None},
            {"name": "git", "tier": "core", "ok": True, "located": "C:/git.exe", "version": "2.44.0", "hint": None},
        ],
    }
    monkeypatch.setattr(doctor, "run_doctor", lambda: fixed)
    rc = cli_main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] == 2
    assert out["checks"][0]["name"] == "python"


def test_cli_end_to_text_rc_on_core_missing(monkeypatch, capsys):
    fixed = {
        "checked": 1,
        "ok": 0,
        "missing_core": ["cbm"],
        "missing_engine": [],
        "python_ok": True,
        "checks": [
            {"name": "cbm", "tier": "core", "ok": False, "located": None, "version": None, "hint": "install cbm"},
        ],
    }
    monkeypatch.setattr(doctor, "run_doctor", lambda: fixed)
    rc = cli_main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "install cbm" in out
