"""S5（构想v12）：finding 聚合——同根因归组（Sentry fingerprint 语义）。

指纹=根因符号集：锚定对 (anchor_fid, related_fid) **无序归一**（互环
a→b/b→a 与克隆族同键——lusiem 实证 token-clone 同无序对最高 11 条，
"一个根因产生 N 条 finding"的真实形态）；无 fid 的 finding
（unused-file 等）回退 (file_path, start_line) 键。
group_id = sha256(canonical)[:12] 稳定短 ID（跨 etl 复跑不变）。

红线（同 S7）：**归组不删行**——findings 表零改动，本模块是纯只读
查询面（ro_connect）；S6 评分/dossier 聚合呈现接入各自立项。

确定性（复现性红线）：输出不含时间戳——同 facts.db 同输出；
排序 (-members, group_id) 纯确定；组内 member_ids 按 id 升序
（cap MEMBER_IDS_CAP，summary 记 cap 恒真）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from . import dbpath

MEMBER_IDS_CAP = 10
DEFAULT_LIMIT = 50


def group_key(
    pathology: str,
    kind: str,
    anchor: str | None,
    related: str | None,
    file_path: str | None,
    start_line: int | None,
) -> str:
    """指纹：fid 对无序归一；无 fid 回退 (file_path, start_line)。"""
    fids = sorted({f for f in (anchor, related) if f})
    if fids:
        parts = [pathology, kind, "fids", *fids]
    else:
        parts = [pathology, kind, "path", file_path or "", str(start_line or 0)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def group_findings(con: sqlite3.Connection) -> dict:
    """全量 findings 按指纹归组；只读（红线：findings 行零改动）。"""
    groups: dict[str, dict] = {}
    total = 0
    for fid_, pathology, kind, anchor, related, path, start in con.execute(
        "SELECT id, pathology, kind, anchor_fid, related_fid, file_path, start_line"
        " FROM findings ORDER BY id"
    ).fetchall():
        total += 1
        gid = group_key(pathology, kind, anchor, related, path, start)
        g = groups.get(gid)
        if g is None:
            g = groups[gid] = {
                "group_id": gid,
                "pathology": pathology,
                "kind": kind,
                "members": 0,
                "member_ids": [],
                "sample": {"file_path": path, "start_line": start},
            }
        g["members"] += 1
        if len(g["member_ids"]) < MEMBER_IDS_CAP:
            g["member_ids"].append(fid_)
    ordered = sorted(groups.values(), key=lambda g: (-g["members"], g["group_id"]))
    multi = [g for g in ordered if g["members"] > 1]
    summary = {
        "total_findings": total,
        "total_groups": len(ordered),
        "multi_member_groups": len(multi),
        "findings_in_multi": sum(g["members"] for g in multi),
        "member_ids_cap": MEMBER_IDS_CAP,
        "red_line": "grouping never deletes - findings rows untouched",
    }
    return {"summary": summary, "groups": ordered}


def run_groups(repo: Path, limit: int = DEFAULT_LIMIT) -> dict:
    """`vrp groups` 只读入口：facts.db 缺失诚实 SystemExit；limit 截断
    展示面（summary 恒为真总数——截断不撒谎）。
    """
    repo = Path(repo).resolve()
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        grouped = group_findings(con)
    finally:
        con.close()
    shown = grouped["groups"][: max(limit, 0)]
    return {
        "repo": str(repo),
        "summary": grouped["summary"],
        "limit": limit,
        "shown": len(shown),
        "groups": shown,
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同 findings/report 链）。"""
    s = result["summary"]
    lines = [f"vrp groups - {Path(result['repo']).name}"]
    lines.append(
        f"  findings  : {s['total_findings']} total -> {s['total_groups']} groups"
        f" ({s['multi_member_groups']} multi-member,"
        f" {s['findings_in_multi']} findings share a root cause)"
    )
    lines.append(
        f"  showing   : {result['shown']} groups (limit {result['limit']},"
        " grouping never deletes)"
    )
    for g in result["groups"]:
        loc = g["sample"]["file_path"] or "-"
        if g["sample"]["start_line"]:
            loc += f":{g['sample']['start_line']}"
        ids = ", ".join(str(i) for i in g["member_ids"])
        if g["members"] > len(g["member_ids"]):
            ids += f", +{g['members'] - len(g['member_ids'])} more"
        lines.append(
            f"  {g['group_id']}  x{g['members']:<4} {g['pathology']}/{g['kind']}"
            f"  {loc}  ids: {ids}"
        )
    return "\n".join(lines)
