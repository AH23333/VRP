"""L14（构想v11 甜点）：CBM 缓存库 GC。

三段式（npm cache clean 治理形态）：
- 审计查询（默认）：cache_dir()/*.db 逐库溯源（store_meta/vrp_repo_path，
  H3 溯源章单源复用）+仓库存活性核对+体量/节点数——陈旧=真陈旧不是猜
- ``--dry-run``：只列 GC 候选与可释放字节，不动盘
- ``--yes``：真删（候选 db + -wal/-shm 边车）；删除失败逐件记录 rc=1

判定纪律（宁保守勿误删）：
- ``repo-missing``（vrp_repo_path 章指向不存在的目录）→ 唯一自动候选
- ``unknown-source``（无 vrp 章的旧版/他源库）→ 报告不自动收（#77 教训：
  artifact 复活通道源于盲删，删库必须先有溯源）
- ``unreadable``/``foreign``（无 db_uid 非法库）→ 报告不自动收
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import s1_cbm

CANDIDATE_STATUSES = frozenset({"repo-missing"})


def _human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def _node_count(db_path: Path) -> int | None:
    try:
        con = s1_cbm.connect_ro(db_path)
        try:
            return int(con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        finally:
            con.close()
    except (RuntimeError, sqlite3.Error):
        return None


def _classify(db_path: Path) -> dict:
    entry: dict = {
        "name": db_path.name,
        "path": str(db_path),
        "size_bytes": db_path.stat().st_size,
        "mtime": int(db_path.stat().st_mtime),
        "repo_path": None,
        "nodes": None,
        "status": "unreadable",
        "reason": "",
    }
    try:
        meta = s1_cbm.read_store_meta(db_path)
    except (RuntimeError, sqlite3.Error) as error:
        entry["reason"] = f"not a readable CBM db: {error}"
        return entry
    repo_path = meta.get(s1_cbm.REPO_PATH_META_KEY) or None
    entry["repo_path"] = repo_path
    entry["nodes"] = _node_count(db_path)
    if repo_path is None:
        entry["status"] = "unknown-source"
        entry["reason"] = "no vrp_repo_path provenance (old version or foreign index)"
    elif Path(repo_path).is_dir():
        entry["status"] = "ok"
        entry["reason"] = ""
    else:
        entry["status"] = "repo-missing"
        entry["reason"] = f"recorded repo no longer exists: {repo_path}"
    return entry


def audit_cache(cache_dir: Path | None = None) -> dict:
    """审计段：cache_dir() 下 *.db 逐库分类（名序确定）。"""
    cdir = cache_dir if cache_dir is not None else s1_cbm.cache_dir()
    dbs: list[dict] = []
    if cdir.is_dir():
        for db_path in sorted(cdir.glob("*.db")):
            if not db_path.is_file():
                continue
            dbs.append(_classify(db_path))
    return {
        "cache_dir": str(cdir),
        "dbs": dbs,
        "total": len(dbs),
        "by_status": _count_by_status(dbs),
        "candidates": [d["name"] for d in dbs if d["status"] in CANDIDATE_STATUSES],
        "candidate_bytes": sum(d["size_bytes"] for d in dbs if d["status"] in CANDIDATE_STATUSES),
    }


def _count_by_status(dbs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for db in dbs:
        counts[db["status"]] = counts.get(db["status"], 0) + 1
    return counts


def _sidecars(db_path: Path) -> list[Path]:
    return [p for p in (Path(f"{db_path}-wal"), Path(f"{db_path}-shm")) if p.exists()]


def run_gc(cache_dir: Path | None = None, yes: bool = False, dry_run: bool = False) -> dict:
    """三段式入口：恒先审计；--dry-run 只列将删清单；--yes 才动盘（候选 db+边车逐件真删，失败逐件记录）。"""
    result = audit_cache(cache_dir)
    result["mode"] = "gc" if yes else ("dry-run" if dry_run else "audit")
    result["deleted"] = []
    result["failed"] = []
    result["freed_bytes"] = 0
    if dry_run and not yes:
        would: list[str] = []
        total = 0
        for db in result["dbs"]:
            if db["status"] not in CANDIDATE_STATUSES:
                continue
            db_path = Path(db["path"])
            would.append(str(db_path))
            total += db_path.stat().st_size
            for side in _sidecars(db_path):
                would.append(str(side))
                total += side.stat().st_size
        result["would_delete"] = would
        result["would_free_bytes"] = total
        return result
    if not yes:
        return result
    for db in result["dbs"]:
        if db["status"] not in CANDIDATE_STATUSES:
            continue
        db_path = Path(db["path"])
        targets = [db_path, *_sidecars(db_path)]
        for target in targets:
            try:
                size = target.stat().st_size
                target.unlink()
                result["deleted"].append(str(target))
                result["freed_bytes"] += size
            except OSError as error:
                result["failed"].append({"path": str(target), "error": str(error)})
    return result


def gc_rc(result: dict) -> int:
    if result["mode"] != "gc":
        return 0
    return 1 if result["failed"] else 0


def render_text(result: dict) -> str:
    mode = result.get("mode", "audit")
    lines = [f"vrp gc - CBM 缓存审计", f"  cache_dir: {result['cache_dir']}  mode: {mode}"]
    if not result["dbs"]:
        lines.append("  (no cached dbs)")
    for db in result["dbs"]:
        mark = "[GC] " if db["status"] in CANDIDATE_STATUSES else "[keep]"
        repo = db["repo_path"] or "-"
        nodes = "?" if db["nodes"] is None else str(db["nodes"])
        lines.append(
            f"  {mark} {db['name']:<40} {_human_size(db['size_bytes']):>10}"
            f"  nodes={nodes:<8} {db['status']:<15} {repo}"
        )
        if db["reason"]:
            lines.append(f"         reason: {db['reason']}")
    freed = ""
    if mode == "gc":
        freed = f"  deleted={len(result['deleted'])} freed={_human_size(result['freed_bytes'])}"
        if result["failed"]:
            freed += f"  failed={len(result['failed'])}"
    lines.append(
        f"  summary: {result['total']} dbs"
        f"  candidates={len(result['candidates'])} ({_human_size(result['candidate_bytes'])}){freed}"
    )
    return "\n".join(lines)
