"""S1: CBM runner - locate binary, index repo, guard schema, open CBM SQLite."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

DEFAULT_INSTALL = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "codebase-memory-mcp" / "codebase-memory-mcp.exe"


def find_cbm() -> Path:
    for name in ("codebase-memory-mcp", "cbm"):
        found = shutil.which(name)
        if found:
            return Path(found)
    if DEFAULT_INSTALL.exists():
        return DEFAULT_INSTALL
    raise FileNotFoundError(
        "codebase-memory-mcp binary not found (PATH or %LOCALAPPDATA%\\Programs\\codebase-memory-mcp)"
    )


def cache_dir() -> Path:
    env = os.environ.get("CBM_CACHE_DIR")
    if env:
        return Path(env)
    home = Path.home()
    return home / ".cache" / "codebase-memory-mcp"


def cbm_db_path(name: str) -> Path:
    return cache_dir() / f"{name}.db"


def find_cbm_db(name: str, repo_path: str) -> Path | None:
    """Locate the CBM db for a repo.

    Preferred form is the legacy ``{name}.db``. CBM may however rename the
    project (observed: a workspace context under ``D:\\Visual Studio Code\\Something``
    makes CBM derive the project from the full repo path, ignoring ``--name``),
    so fall back to scanning the cache dir for a db whose recorded provenance
    matches ``repo_path`` (dogfood batch 2026-11-04).
    """
    db = cbm_db_path(name)
    if db.exists():
        return db
    cdir = cache_dir()
    if cdir.is_dir():
        for cand in sorted(cdir.glob("*.db")):
            try:
                if _recorded_repo_path(cand) == repo_path:
                    return cand
            except (RuntimeError, sqlite3.Error):
                continue
    return None


REPO_PATH_META_KEY = "vrp_repo_path"


def _recorded_repo_path(db_path: Path) -> str | None:
    """Repo path recorded by a previous vrp indexing run, or None if unknown."""
    try:
        meta = read_store_meta(db_path)
    except (RuntimeError, sqlite3.Error):
        return None
    return meta.get(REPO_PATH_META_KEY) or None


def _record_repo_path(db_path: Path, repo_path: str) -> None:
    """Stamp the indexed repo's resolved path into CBM store_meta (H3)."""
    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(store_meta)")]
        if not cols:
            con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cols = ["key", "value"]
        if len(cols) < 2:
            return
        # 列名来自 db 自身 PRAGMA——标识符不能参数化，内插前按 SQLite
        # 双引号规则转义（畸形列名含引号时不产生语法错/注入面）
        c0 = str(cols[0]).replace('"', '""')
        c1 = str(cols[1]).replace('"', '""')
        con.execute(
            f'INSERT OR REPLACE INTO store_meta ("{c0}", "{c1}") VALUES (?, ?)',
            (REPO_PATH_META_KEY, repo_path),
        )
        con.commit()
    finally:
        con.close()


def run_cli(args: list[str], timeout: int = 3600) -> dict:
    exe = find_cbm()
    proc = subprocess.run(
        [str(exe), "cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cbm cli failed ({proc.returncode}): {' '.join(args)}\nstdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return _parse_json_stdout(proc.stdout)


def _parse_json_stdout(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"no JSON object in cbm stdout: {stdout[:500]}")


def index_repository(repo: Path, name: str, reindex: bool = False, timeout: int = 3600) -> dict:
    """S1 索引：恒调 CBM CLI（CBM 自身增量刷新）。

    R6' 病根五（dogfood 抓出）：旧版缓存 db 存在即 "reused" 短路、从不重扫——
    仓库演进后陈旧 CBM 内容被 etl 盖上当前 git_head，M4 新鲜度闸门被击穿
    （facts.git_head=37d1c6f 而零 R4'-R6' 符号）。reindex 参数保留 API 兼容
    （短路已取消，两者等价——恒增量刷新）。溯源护栏保留（#81）：缓存 db
    溯源缺失/异仓时标注 rebuild_reason 提示，不再静默。
    """
    repo_path = str(Path(repo).resolve())
    rebuild_reason = None
    db = find_cbm_db(name, repo_path)
    if db is not None:
        recorded = _recorded_repo_path(db)
        if recorded is None:
            rebuild_reason = "unknown-source"
        elif recorded != repo_path:
            rebuild_reason = "repo-path-mismatch"
    result = run_cli(
        ["index_repository", "--repo-path", str(repo), "--name", name], timeout=timeout
    )
    project = str(result.get("project") or name)
    if "/" in project or "\\" in project:
        # CBM 报告的 project 名直接拼 cache 路径——含分隔符会写出
        # cache 子目录（路径逃逸面），宁可拒绝也不猜测
        raise RuntimeError(f"cbm reported unsafe project name: {project!r}")
    db = cache_dir() / f"{project}.db"
    result["status"] = "indexed"
    result["project"] = project
    result["db"] = str(db)
    if not db.exists():
        raise RuntimeError(f"cbm reported success but db is missing: {db}")
    _record_repo_path(db, repo_path)
    if rebuild_reason:
        result["rebuild_reason"] = rebuild_reason
        result["note"] = (
            f"cache db '{name}.db' belonged to another repo or lacked provenance "
            f"({rebuild_reason}); re-indexed"
        )
    return result


CBM_SCHEMA_CONTRACT: dict[str, tuple[str, ...]] = {
    "nodes": ("id", "project", "label", "name", "qualified_name", "file_path", "start_line", "end_line", "properties"),
    "edges": ("project", "source_id", "target_id", "type", "properties"),
    "index_coverage": ("project", "rel_path", "kind", "detail"),
    "file_hashes": ("project", "rel_path", "sha256"),
    "store_meta": (),
}


def verify_cbm_schema(db_path: Path) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        problems: list[str] = []
        for table, columns in CBM_SCHEMA_CONTRACT.items():
            if table not in tables:
                problems.append(f"missing table {table}")
                continue
            if not columns:
                continue
            present = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            missing = [column for column in columns if column not in present]
            if missing:
                problems.append(f"table {table} missing columns {missing}")
        if problems:
            raise RuntimeError(
                "CBM store schema drift (downgrade or foreign db): "
                + "; ".join(problems)
                + f" - re-index with 'vrp index <repo>' against the current CBM version: {db_path}"
            )
    finally:
        con.close()


def read_store_meta(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.execute("SELECT * FROM store_meta")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()
    if len(cols) < 2:
        raise RuntimeError(f"unexpected store_meta schema: {cols}")
    meta = {}
    for row in rows:
        meta[str(row[0])] = str(row[1]) if row[1] is not None else ""
    if "db_uid" not in meta:
        raise RuntimeError(f"store_meta/db_uid missing - not a CBM database: {db_path}")
    return meta


def connect_ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con
