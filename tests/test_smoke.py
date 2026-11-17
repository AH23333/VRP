"""K12（构想v11 甜点）：跨仓冒烟一键化测试。

- 发现面：verify/ 含 .git 子目录自动发现（无注册表）；--only 过滤+未知名拒绝
- 冒烟面：全链四阶段（monkeypatch 替身不跑真管道）/阶段异常诚实 fail 带
  详情/content-repo skip=pass（P-2 语义）/零符号 fail/facts.db 缺失 fail
- --skip-run 快面：已有 facts.db+M4 freshness（陈旧 fail）
- 汇总面：pass/fail 计数+rc=1 on fail+render 表
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import smoke
from vrp.cli import build_parser, cmd_smoke
from vrp.cli import main as cli_main

SCHEMA = Path(smoke.__file__).parent / "schema" / "facts_schema.sql"


def _mk_facts(repo: Path, symbols: int = 3) -> None:
    facts = repo / ".vrp" / "facts.db"
    facts.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(facts)
    try:
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        for i in range(symbols):
            con.execute(
                "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"sym{i}", "p", f"n.sym{i}", f"sym{i}", f"sym{i}", "a.py", i + 1),
            )
        con.commit()
    finally:
        con.close()


def _mk_verify_root(tmp_path: Path) -> Path:
    root = tmp_path / "vroot"
    (root / "verify").mkdir(parents=True)
    for name in ("repo-b", "repo-a", "not-git"):
        d = root / "verify" / name
        d.mkdir()
        if name != "not-git":
            (d / ".git").mkdir()
            (d / "src.txt").write_text("x\n", encoding="utf-8")
    return root


def _patch_stages(monkeypatch, content=False) -> None:
    monkeypatch.setattr(
        smoke.s0_precheck,
        "precheck",
        lambda r: {
            "name": r.name,
            "content_repo": {"detected": content, "content_share_pct": 99 if content else 0},
        },
    )
    monkeypatch.setattr(smoke.s1_cbm, "index_repository", lambda r, name, reindex=False: {"status": "ok"})
    monkeypatch.setattr(smoke.etl_mod, "run_etl", lambda r, name: {"status": "ok", "symbols": 3, "edges": 2})
    monkeypatch.setattr(smoke.xcheck, "run_xcheck", lambda r, db, reindex=False: {"status": "ok", "agreement_pct": 88.8})


def test_discover_git_dirs_sorted(tmp_path):
    root = _mk_verify_root(tmp_path)
    repos = smoke.discover_verify_repos(root)
    assert [r["name"] for r in repos] == ["repo-a", "repo-b"]  # 名序+not-git 排除
    assert smoke.discover_verify_repos(tmp_path / "nope") == []


def test_smoke_repo_full_pass(tmp_path, monkeypatch):
    repo = tmp_path / "verify" / "ok-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _patch_stages(monkeypatch)
    _mk_facts(repo, symbols=5)
    result = smoke.smoke_repo(repo)
    assert result["status"] == "pass"
    assert result["skipped"] is None
    assert result["counts"]["symbols"] == 5
    assert set(result["stages"]) == {"precheck", "cbm_index", "etl", "xcheck"}
    assert result["stages"]["xcheck"]["agreement_pct"] == 88.8
    assert result["duration_s"] >= 0


def test_smoke_repo_content_skip_pass(tmp_path, monkeypatch):
    repo = tmp_path / "verify" / "content-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _patch_stages(monkeypatch, content=True)
    result = smoke.smoke_repo(repo)
    assert result["status"] == "pass"
    assert result["skipped"] == "content-repo"
    assert "cbm_index" not in result["stages"]


def test_smoke_repo_stage_exception_fail(tmp_path, monkeypatch):
    repo = tmp_path / "verify" / "boom-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _patch_stages(monkeypatch)

    def _boom(r, name, reindex=False):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(smoke.s1_cbm, "index_repository", _boom)
    result = smoke.smoke_repo(repo)
    assert result["status"] == "fail"
    assert "RuntimeError: index exploded" in result["error"]


def test_smoke_repo_zero_symbols_fail(tmp_path, monkeypatch):
    """管道跑完 facts.db 零符号=可疑事实面，诚实 fail。"""
    repo = tmp_path / "verify" / "empty-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _patch_stages(monkeypatch)
    _mk_facts(repo, symbols=0)
    result = smoke.smoke_repo(repo)
    assert result["status"] == "fail"
    assert "zero symbols" in result["error"]


def test_smoke_repo_missing_facts_fail(tmp_path, monkeypatch):
    repo = tmp_path / "verify" / "nofacts-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _patch_stages(monkeypatch)
    result = smoke.smoke_repo(repo)
    assert result["status"] == "fail"
    assert "not produced" in result["error"]


def test_smoke_repo_skip_run_fresh(tmp_path, monkeypatch):
    repo = tmp_path / "verify" / "fresh-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _mk_facts(repo, symbols=2)
    monkeypatch.setattr(smoke.etl_mod, "check_facts_freshness", lambda con, r: None)
    result = smoke.smoke_repo(repo, skip_run=True)
    assert result["status"] == "pass"
    assert result["mode"] == "skip-run"
    assert result["counts"]["symbols"] == 2


def test_smoke_repo_skip_run_stale_fail(tmp_path, monkeypatch):
    repo = tmp_path / "verify" / "stale-repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    _mk_facts(repo, symbols=2)

    def _stale(con, r):
        raise RuntimeError("facts.db stale (git_head mismatch)")

    monkeypatch.setattr(smoke.etl_mod, "check_facts_freshness", _stale)
    result = smoke.smoke_repo(repo, skip_run=True)
    assert result["status"] == "fail"
    assert "stale" in result["error"]


def test_run_smoke_aggregates_and_only(tmp_path, monkeypatch):
    root = _mk_verify_root(tmp_path)
    _patch_stages(monkeypatch)
    _mk_facts(root / "verify" / "repo-a", symbols=4)
    _mk_facts(root / "verify" / "repo-b", symbols=6)
    result = smoke.run_smoke(root)
    assert result["repos"] == 2
    assert result["pass"] == 2 and result["fail"] == 0
    assert smoke.smoke_rc(result) == 0
    only = smoke.run_smoke(root, only=["repo-b"])
    assert only["repos"] == 1
    assert only["results"][0]["counts"]["symbols"] == 6


def test_run_smoke_unknown_only_rejected(tmp_path):
    root = _mk_verify_root(tmp_path)
    with pytest.raises(SystemExit, match="unknown verify repos"):
        smoke.run_smoke(root, only=["ghost"])


def test_run_smoke_fail_rc(tmp_path, monkeypatch):
    root = _mk_verify_root(tmp_path)
    _patch_stages(monkeypatch)
    _mk_facts(root / "verify" / "repo-a", symbols=4)  # repo-b 无 facts → fail
    result = smoke.run_smoke(root)
    assert result["fail"] == 1
    assert smoke.smoke_rc(result) == 1
    text = smoke.render_text(result)
    assert "[FAIL]" in text and "error:" in text
    assert "1 pass / 1 fail / 2 repos" in text


def test_render_skip_line(tmp_path, monkeypatch):
    root = _mk_verify_root(tmp_path)
    _patch_stages(monkeypatch, content=True)
    result = smoke.run_smoke(root)
    text = smoke.render_text(result)
    assert "[skip]" in text and "content-repo" in text


def test_cli_smoke_end_to_end(tmp_path, monkeypatch, capsys):
    root = _mk_verify_root(tmp_path)
    _patch_stages(monkeypatch)
    _mk_facts(root / "verify" / "repo-a", symbols=4)
    _mk_facts(root / "verify" / "repo-b", symbols=6)
    rc = cli_main(["smoke", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["repos"] == 2 and out["pass"] == 2
    args = build_parser().parse_args(["smoke", str(root), "--only", "repo-a", "--skip-run"])
    assert args.only == ["repo-a"] and args.skip_run is True
    assert args.func is cmd_smoke
