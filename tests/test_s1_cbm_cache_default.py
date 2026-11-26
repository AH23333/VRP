"""R-AJ：CBM 缓存目录零配置默认——<repo>/.vrp/cache（env 覆盖链+子进程注入）。

用户此前必须手动设置 CBM_CACHE_DIR（README "必须显式设置"）；本批把默认
落进被分析仓的 vrp 项目目录（.vrp/cache，与 facts.db 同居、随 .vrp 被
gitignore），env 仍可显式覆盖（deep 测试共享缓存等场景）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vrp import gc as gc_mod
from vrp import s1_cbm
from vrp.cli import main as cli_main


def test_cache_dir_default_is_repo_vrp_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    repo = tmp_path / "repo"
    assert s1_cbm.cache_dir(repo) == repo / ".vrp" / "cache"


def test_cache_dir_env_overrides_repo(tmp_path, monkeypatch):
    env_dir = tmp_path / "env-cache"
    monkeypatch.setenv("CBM_CACHE_DIR", str(env_dir))
    repo = tmp_path / "repo"
    assert s1_cbm.cache_dir(repo) == env_dir, "显式 env 必须压过 per-repo 默认"


def test_cache_dir_no_repo_falls_back_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert s1_cbm.cache_dir() == home / ".cache" / "codebase-memory-mcp"


def test_cbm_db_path_threads_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    repo = tmp_path / "repo"
    assert s1_cbm.cbm_db_path("proj", repo) == repo / ".vrp" / "cache" / "proj.db"


def test_run_cli_injects_cache_dir_env(tmp_path, monkeypatch):
    """per-repo 默认不在 CBM 自身默认面里——run_cli 必须把解析值注入子进程 env。"""
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    captured: dict = {}

    def fake_run_tree(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        captured["env"] = env
        return {"returncode": 0, "timed_out": False, "stdout": '{"ok": true}', "stderr": ""}

    monkeypatch.setattr(s1_cbm.procs, "run_tree", fake_run_tree)
    monkeypatch.setattr(s1_cbm, "find_cbm", lambda: tmp_path / "cbm.exe")
    cache_dir = tmp_path / "repo" / ".vrp" / "cache"
    s1_cbm.run_cli(["x"], cache_dir=cache_dir)
    assert captured["env"]["CBM_CACHE_DIR"] == str(cache_dir)

    s1_cbm.run_cli(["x"])
    assert captured["env"] is None, "无 cache_dir 时必须继承宿主 env（不改写）"


def test_index_repository_lands_in_repo_vrp_cache(tmp_path, monkeypatch):
    """无 env 时全链：索引产物 db 必须落 <repo>/.vrp/cache/（mkdir+注入+定位三面一致）。"""
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    def fake_run_tree(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        cdir = Path(env["CBM_CACHE_DIR"])
        db = cdir / "proj.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO store_meta VALUES ('db_uid', 'uid-1')")
        con.commit()
        con.close()
        return {"returncode": 0, "timed_out": False, "stdout": '{"ok": true, "project": "proj"}', "stderr": ""}

    monkeypatch.setattr(s1_cbm.procs, "run_tree", fake_run_tree)
    monkeypatch.setattr(s1_cbm, "find_cbm", lambda: tmp_path / "cbm.exe")

    result = s1_cbm.index_repository(repo, "proj")
    expected = repo / ".vrp" / "cache" / "proj.db"
    assert result["db"] == str(expected)
    assert expected.is_file(), "CBM 缓存 db 必须落 per-repo .vrp/cache（零 env 配置）"
    assert s1_cbm.find_cbm_db("proj", str(repo.resolve())) == expected


def test_gc_audits_repo_vrp_cache_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = repo / ".vrp" / "cache"
    cache.mkdir(parents=True)
    db = cache / "gone.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany(
        "INSERT INTO store_meta VALUES (?,?)",
        [("db_uid", "u1"), (s1_cbm.REPO_PATH_META_KEY, str(tmp_path / "no-longer-exists"))],
    )
    con.commit()
    con.close()

    result = gc_mod.run_gc(repo=repo, dry_run=True)
    assert result["cache_dir"] == str(cache)
    assert result["candidates"] == ["gone.db"]
    assert result["would_delete"] == [str(db)]


def test_cli_gc_accepts_positional_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = repo / ".vrp" / "cache"
    cache.mkdir(parents=True)

    rc = cli_main(["gc", str(repo), "--json"])
    out = __import__("json").loads(capsys.readouterr().out)
    assert rc == 0
    assert out["cache_dir"] == str(cache)
    assert out["total"] == 0, "空 per-repo 缓存诚实报零"
