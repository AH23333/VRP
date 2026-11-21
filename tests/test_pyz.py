"""U13 vrp.pyz zipapp 分发守卫（构想v12 甜点）。

守卫重点：构建确定性（同源双构建 sha256 全等——freeze graph.db.zst
字节级复现先例）、zip 布局（vrp/ 包全量+数据目录+根 __main__ 启动器，
__pycache__ 排除）、E2E 双段（--help 走通自解压+re-exec；cochange 真
命令走通解压树 schema 读+git 子进程+sqlite 写——老库自升级场景穿透
pyz 全链）、缓存键控复用（.complete 标记+二次运行不重解压）、无标记
残局自愈（崩溃残留目录重装）。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

from vrp.build_pyz import _LAUNCHER_SRC, build_pyz
from vrp import cochange as coc

from tests.git_helpers import _git, _init_repo

SCHEMA = Path(coc.__file__).parent / "schema" / "facts_schema.sql"


# ---------- 合成 git 仓 fixture（test_cochange 同型） ----------


def _commit(repo: Path, files: dict[str, str], msg: str, date: str | None = None) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    env = None
    if date:
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    _git(repo, "commit", "-q", "-m", msg, env=env)


def _run_pyz(pyz: Path, cache: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "VRP_PYZ_CACHE": str(cache)}
    return subprocess.run(
        [sys.executable, str(pyz), *args],
        capture_output=True, timeout=120, env=env,
    )


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace")


# ---------- 构建面 ----------


def test_build_deterministic(tmp_path):
    """确定性红线：同源双构建 sha256 全等（arcname 排序+固定时间戳）。"""
    s1 = build_pyz(tmp_path / "a" / "vrp.pyz")
    s2 = build_pyz(tmp_path / "b" / "vrp.pyz")
    assert s1["sha256"] == s2["sha256"]
    assert s1["bytes"] == s2["bytes"]
    assert s1["files"] == s2["files"]
    assert s1["files"] > 100  # vrp/ 包全量入 zip


def test_zip_layout(tmp_path):
    """布局：根 __main__ 启动器（内容==常量零漂移）+vrp/ 包+关键数据
    资源（schema/archify.mjs/echarts/rules）；__pycache__/pyc 排除。
    """
    dest = tmp_path / "vrp.pyz"
    build_pyz(dest)
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert zf.read("__main__.py").decode("utf-8") == _LAUNCHER_SRC
        for required in (
            "vrp/cli.py",
            "vrp/build_pyz.py",
            "vrp/schema/facts_schema.sql",
            "vrp/vendor/archify/bin/archify.mjs",
            "vrp/assets/echarts.min.js",
        ):
            assert required in names, f"missing {required}"
        assert any(n.startswith("vrp/rules/s2/") for n in names)
        assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
        assert zf.testzip() is None  # CRC 全验


# ---------- E2E：自解压 + re-exec 双段 ----------


def test_pyz_help_e2e(tmp_path):
    """E2E 第一段：python vrp.pyz --help → 解压缓存+re-exec+usage rc 0。"""
    dest = tmp_path / "vrp.pyz"
    build_pyz(dest)
    cache = tmp_path / "cache"
    proc = _run_pyz(dest, cache, "--help")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "usage" in _out(proc)


def test_pyz_cochange_e2e(tmp_path):
    """E2E 第二段（真命令全链）：老库（缺 co_changes 表）经 pyz 跑
    cochange → 解压树 ensure_schema 读 SCHEMA_PATH+git 子进程+sqlite
    写+freshness 全部走通——#161 老库自升级场景穿透 pyz。
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"a.py": "1", "b.py": "1"}, "c0", "2026-01-01T10:00:00+08:00")
    _commit(repo, {"a.py": "2", "b.py": "2"}, "c1", "2026-02-01T10:00:00+08:00")
    facts = repo / ".vrp" / "facts.db"
    facts.parent.mkdir()
    con = sqlite3.connect(facts)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("DROP TABLE co_changes")  # 模拟老 schema
    head = _git(repo, "rev-parse", "HEAD")["stdout"].strip()
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', ?)", (head,))
    con.commit()
    con.close()
    dest = tmp_path / "vrp.pyz"
    build_pyz(dest)
    proc = _run_pyz(dest, tmp_path / "cache", "cochange", str(repo), "--min-support", "1")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "a.py <-> b.py" in _out(proc)
    con = sqlite3.connect(facts)
    assert con.execute("SELECT COUNT(*) FROM co_changes").fetchone()[0] == 1
    con.close()


# ---------- 缓存键控与自愈 ----------


def test_pyz_cache_marker_and_reuse(tmp_path):
    """缓存键控：解压目标=缓存/vrp-<sha256[:16]> 且带 .complete 标记；
    二次运行复用（标记在位）不重解压。
    """
    dest = tmp_path / "vrp.pyz"
    build_pyz(dest)
    cache = tmp_path / "cache"
    proc = _run_pyz(dest, cache, "--help")
    assert proc.returncode == 0
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:16]
    target = cache / f"vrp-{digest}"
    assert (target / ".complete").is_file()
    assert (target / "vrp" / "cli.py").is_file()
    marker_mtime = (target / ".complete").stat().st_mtime_ns
    proc = _run_pyz(dest, cache, "--help")
    assert proc.returncode == 0
    assert (target / ".complete").stat().st_mtime_ns == marker_mtime  # 未重解压


def test_pyz_incomplete_cache_recovered(tmp_path):
    """自愈：无 .complete 残局（崩溃残留）被清掉重装，运行照常成功。"""
    dest = tmp_path / "vrp.pyz"
    build_pyz(dest)
    cache = tmp_path / "cache"
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:16]
    target = cache / f"vrp-{digest}"
    target.mkdir(parents=True)
    (target / "garbage.txt").write_text("crash leftover", encoding="utf-8")
    proc = _run_pyz(dest, cache, "--help")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert (target / ".complete").is_file()
    assert not (target / "garbage.txt").exists()  # 残局被清
