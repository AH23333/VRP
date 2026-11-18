"""L14（构想v11 甜点）：CBM 缓存 GC 测试。

- 审计面：五态分类（ok/repo-missing/unknown-source/foreign 无 db_uid/
  非 sqlite 损坏件）+名序+空缓存+nodes 计数
- 三段式：默认审计不动盘/--dry-run 只列 would_delete（含 -wal/-shm 边车）
  与 would_free_bytes/--yes 真删（候选+边车，非候选不动，freed 计实）
- 红线：删除失败逐件记录 rc=1；unknown-source 永不自动收（#77 教训）
- render/CLI E2E
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from vrp import gc, s1_cbm
from vrp.cli import build_parser, cmd_gc
from vrp.cli import main as cli_main


def _mk_cbm_db(cache: Path, name: str, repo_path: str | None, uid: str = "u1", nodes: int = 2) -> Path:
    """仿 CBM 缓存库：store_meta（db_uid 必有，vrp_repo_path 可选）+nodes 表。"""
    db = cache / f"{name}.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT)")
        for i in range(nodes):
            con.execute("INSERT INTO nodes (id, name) VALUES (?, ?)", (i + 1, f"n{i}"))
        con.execute("INSERT INTO store_meta (key, value) VALUES ('db_uid', ?)", (uid,))
        if repo_path is not None:
            con.execute("INSERT INTO store_meta (key, value) VALUES (?, ?)", (s1_cbm.REPO_PATH_META_KEY, repo_path))
        con.commit()
    finally:
        con.close()
    return db


def _mk_cache(tmp_path: Path, live_repo: Path) -> Path:
    cache = tmp_path / "cbm-cache"
    cache.mkdir()
    _mk_cbm_db(cache, "live", str(live_repo), nodes=5)
    _mk_cbm_db(cache, "gone", str(tmp_path / "deleted-repo"), nodes=3)
    _mk_cbm_db(cache, "old", None, nodes=1)  # unknown-source（无 vrp 章）
    foreign = cache / "foreign.db"
    con = sqlite3.connect(foreign)
    con.execute("CREATE TABLE junk (x TEXT)")
    con.commit()
    con.close()  # 有表但无 db_uid → foreign
    (cache / "broken.db").write_bytes(b"this is not sqlite at all" * 10)  # 非 sqlite
    return cache


def test_audit_classifies_all_five_states(tmp_path):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    result = gc.audit_cache(cache)
    assert result["total"] == 5
    assert [d["name"] for d in result["dbs"]] == sorted(d["name"] for d in result["dbs"])  # 名序
    by_name = {d["name"]: d for d in result["dbs"]}
    assert by_name["live.db"]["status"] == "ok"
    assert by_name["live.db"]["nodes"] == 5
    assert by_name["gone.db"]["status"] == "repo-missing"
    assert by_name["gone.db"]["nodes"] == 3
    assert by_name["old.db"]["status"] == "unknown-source"
    assert by_name["foreign.db"]["status"] == "unreadable"  # read_store_meta 无 db_uid → RuntimeError
    assert by_name["broken.db"]["status"] == "unreadable"
    assert result["candidates"] == ["gone.db"]  # 唯一自动候选
    assert result["candidate_bytes"] == by_name["gone.db"]["size_bytes"]


def test_audit_empty_and_missing_cache(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert gc.audit_cache(empty)["total"] == 0
    assert gc.audit_cache(tmp_path / "nope")["total"] == 0


def test_audit_ignores_non_db_files(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "notes.txt").write_text("x", encoding="utf-8")
    assert gc.audit_cache(cache)["total"] == 0


def test_run_gc_default_audit_no_touch(tmp_path):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    result = gc.run_gc(cache)
    assert result["mode"] == "audit"
    assert result["deleted"] == [] and result["freed_bytes"] == 0
    assert all((cache / n).exists() for n in ("live.db", "gone.db", "old.db", "foreign.db", "broken.db"))
    assert gc.gc_rc(result) == 0  # 审计非门


def test_run_gc_dry_run_lists_would_delete_with_sidecars(tmp_path):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    (cache / "gone.db-wal").write_bytes(b"w" * 100)
    (cache / "gone.db-shm").write_bytes(b"s" * 32)
    result = gc.run_gc(cache, dry_run=True)
    assert result["mode"] == "dry-run"
    assert sorted(Path(p).name for p in result["would_delete"]) == [
        "gone.db",
        "gone.db-shm",
        "gone.db-wal",
    ]
    assert result["would_free_bytes"] == (
        (cache / "gone.db").stat().st_size + 100 + (cache / "gone.db-shm").stat().st_size
    )  # -shm 被 sqlite ro 恢复重生为实件（非 32B 假件），审计后实量
    assert (cache / "gone.db").exists()  # 不动盘
    assert gc.gc_rc(result) == 0


def test_run_gc_yes_deletes_candidates_only(tmp_path):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    (cache / "gone.db-wal").write_bytes(b"w" * 100)
    gone_size = (cache / "gone.db").stat().st_size
    result = gc.run_gc(cache, yes=True)
    assert result["mode"] == "gc"
    # -shm 由 sqlite ro 连接对热 -wal 做 WAL 恢复时生成（诚实边车，GC 顺带清）
    assert sorted(Path(p).name for p in result["deleted"]) == [
        "gone.db",
        "gone.db-shm",
        "gone.db-wal",
    ]
    assert result["freed_bytes"] >= gone_size + 100
    assert not (cache / "gone.db").exists()
    assert not (cache / "gone.db-wal").exists()
    assert not (cache / "gone.db-shm").exists()
    # 非候选全部保留（unknown-source/unreadable 不自动收——#77 教训）
    assert all((cache / n).exists() for n in ("live.db", "old.db", "foreign.db", "broken.db"))
    assert gc.gc_rc(result) == 0


def test_run_gc_delete_failure_recorded(tmp_path, monkeypatch):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    real_unlink = Path.unlink

    def _flaky(self, *args, **kwargs):
        if self.name == "gone.db":
            raise OSError("permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky)
    result = gc.run_gc(cache, yes=True)
    assert result["failed"] == [{"path": str(cache / "gone.db"), "error": "permission denied"}]
    assert gc.gc_rc(result) == 1


def test_render_text(tmp_path):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    text = gc.render_text(gc.run_gc(cache))
    assert "[GC]" in text and "gone.db" in text and "repo-missing" in text
    assert "[keep]" in text and "live.db" in text
    assert "unknown-source" in text
    assert "candidates=1" in text
    gc_text = gc.render_text(gc.run_gc(cache, yes=True))
    assert "deleted=" in text or "deleted=" in gc_text
    assert "freed=" in gc_text
    empty_text = gc.render_text(gc.audit_cache(tmp_path / "nope"))
    assert "(no cached dbs)" in empty_text


def test_cli_gc_end_to_end(tmp_path, monkeypatch, capsys):
    live = tmp_path / "live-repo"
    live.mkdir()
    cache = _mk_cache(tmp_path, live)
    monkeypatch.setenv("CBM_CACHE_DIR", str(cache))
    rc = cli_main(["gc", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["total"] == 5 and out["candidates"] == ["gone.db"]
    rc = cli_main(["gc", "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["mode"] == "dry-run" and out["would_delete"]
    rc = cli_main(["gc", "--yes", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["freed_bytes"] > 0
    assert not (cache / "gone.db").exists()
    args = build_parser().parse_args(["gc", "--cache-dir", str(cache), "--yes"])
    assert args.cache_dir == str(cache) and args.yes is True
    assert args.func is cmd_gc
