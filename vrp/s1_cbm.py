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
    if db.exists() and not reindex:
        return {"status": "reused", "project": name, "db": str(db)}
    result = run_cli(
        ["index_repository", "--repo-path", str(repo), "--name", name], timeout=timeout
    )
    result["status"] = "indexed"
    result["db"] = str(db)
    if not db.exists():
        raise RuntimeError(f"cbm reported success but db is missing: {db}")
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
