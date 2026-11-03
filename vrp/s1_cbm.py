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
        con.execute(
            f'INSERT OR REPLACE INTO store_meta ("{cols[0]}", "{cols[1]}") VALUES (?, ?)',
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
    db = cbm_db_path(name)
    repo_path = str(Path(repo).resolve())
    rebuild_reason = None
    if db.exists() and not reindex:
        recorded = _recorded_repo_path(db)
        if recorded is None:
            rebuild_reason = "unknown-source"
        elif recorded != repo_path:
            rebuild_reason = "repo-path-mismatch"
        else:
            return {"status": "reused", "project": name, "db": str(db)}
    result = run_cli(
        ["index_repository", "--repo-path", str(repo), "--name", name], timeout=timeout
    )
    result["status"] = "indexed"
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
