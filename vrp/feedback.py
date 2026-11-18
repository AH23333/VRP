"""S7（构想v12）：finding 反馈环降权——治理闭环原创点。

decisions 表底座（curate.reject 先例）上新增 finding 面反馈手势：
`--dismiss <id>` 记一条 decision（kind='finding'，decision='dismiss'），
不碰 findings 行；rank 视图按 kind 类聚合历史 dismiss 次数，
weight = 0.5 ** 类 dismiss 次数——**红线：降权不删除**（finding 永远
在列表，只调序+留痕原因，删 finding 只能显式动库）。

确定性（复现性红线）：rank 输出不含任何时间戳——同 facts.db 同输出；
排序键 (类 dismiss 次数升序, id 升序) 即 (weight 降序, id 升序) 的整数
等价形式（0.5^n 单调），无浮点排序面。

范围（v1）：反馈源只有 decisions（dismiss 手势）；verdict 历史反馈与
S5 聚合/S6 基础评分立项后接入（本批只落反馈环本体，基础序=id 序）。
桶=kind（debug-print/token-clone/dead-code-candidate 等引擎细类）——
"AI 老拒绝某类 finding"的"类"；pathology 是粗粒度阶段码不作桶。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import dbpath, scoring

FINDING_DECISION_KIND = "finding"
DISMISS_DECISION = "dismiss"
DOWNWEIGHT = 0.5
DEFAULT_LIMIT = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dismiss_finding(con: sqlite3.Connection, finding_id: int, note: str = "") -> dict:
    """dismiss 手势：decisions 留痕，findings 行零改动（降权不删除红线）。

    返回收据（含该 kind 类降权后的权重——反馈效果立即可见）。
    不存在的 finding id 诚实 SystemExit（curate.reject rowcount=0 同型）。
    """
    row = con.execute(
        "SELECT pathology, kind FROM findings WHERE id = ?", (finding_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"no such finding: {finding_id}")
    pathology, kind = row
    con.execute(
        "INSERT INTO decisions (kind, target_id, decision, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (FINDING_DECISION_KIND, str(finding_id), DISMISS_DECISION, note, _now()),
    )
    con.commit()
    count = con.execute(
        "SELECT COUNT(*) FROM decisions d"
        " JOIN findings f ON f.id = CAST(d.target_id AS INTEGER)"
        " WHERE d.kind = ? AND d.decision = ? AND f.kind = ?",
        (FINDING_DECISION_KIND, DISMISS_DECISION, kind),
    ).fetchone()[0]
    return {
        "finding": finding_id,
        "pathology": pathology,
        "kind": kind,
        "decision": DISMISS_DECISION,
        "note": note,
        "class_dismissals": count,
        "class_weight": round(DOWNWEIGHT ** count, 4),
        "red_line": "finding kept - downweight reorders, never deletes",
    }


def _class_dismiss_counts(con: sqlite3.Connection) -> dict[str, int]:
    """按 kind 类聚合 dismiss 历史（decisions JOIN findings 反查类）。"""
    rows = con.execute(
        "SELECT f.kind, COUNT(*) FROM decisions d"
        " JOIN findings f ON f.id = CAST(d.target_id AS INTEGER)"
        " WHERE d.kind = ? AND d.decision = ?"
        " GROUP BY f.kind ORDER BY f.kind",
        (FINDING_DECISION_KIND, DISMISS_DECISION),
    ).fetchall()
    return {kind: count for kind, count in rows}


def rank_findings(con: sqlite3.Connection) -> dict:
    """反馈环排序视图：weight=0.5^类 dismiss 次数；排序
    (类次数升序, score 降序, id 升序)——基础序自 S6 落地起接评分
    （#159 预告"基础序=id 序（S6 评分落地后接入）"），零分量 fixture
    （无边/无重复锚）score 全 0 自然退化回 id 序。

    只读；decisions 里 kind='finding' 但 target_id 非数字（手改库）经
    CAST 归 0 后 JOIN 不中任何 findings 行，安全忽略。
    """
    counts = _class_dismiss_counts(con)
    scored = scoring.score_findings(con, dict(scoring.DEFAULT_WEIGHTS))
    scores = {e["id"]: (e["score"], e["components"]) for e in scored["findings"]}
    entries: list[dict] = []
    kinds: set[str] = set()
    for fid_, pathology, kind, source, anchor, path, start, end in con.execute(
        "SELECT id, pathology, kind, source, anchor_fid, file_path,"
        " start_line, end_line FROM findings ORDER BY id"
    ).fetchall():
        kinds.add(kind)
        n = counts.get(kind, 0)
        score, components = scores.get(fid_, (0.0, {"impact": 0.0, "evidence": 0.0, "span": 0.0}))
        entries.append(
            {
                "id": fid_,
                "pathology": pathology,
                "kind": kind,
                "source": source,
                "anchor_fid": anchor,
                "file_path": path,
                "start_line": start,
                "end_line": end,
                "weight": round(DOWNWEIGHT ** n, 4),
                "class_dismissals": n,
                "score": score,
                "components": components,
                "reason": (
                    f"downweighted: {n} dismissal(s) on kind '{kind}'" if n else ""
                ),
            }
        )
    entries.sort(key=lambda e: (e["class_dismissals"], -e["score"], e["id"]))
    summary = {
        "total": len(entries),
        "kinds": len(kinds),
        "dismiss_decisions": sum(counts.values()),
        "dismissed_kinds": {k: counts[k] for k in sorted(counts)},
        "downweight_factor": DOWNWEIGHT,
        "base_order": "s6 score desc, tie id asc (zero-component fixtures degrade to id order)",
        "red_line": "downweight reorders, never deletes",
    }
    return {"summary": summary, "findings": entries}


def run_findings(repo: Path, limit: int = DEFAULT_LIMIT) -> dict:
    """`vrp findings` 只读入口：facts.db 缺失诚实 SystemExit；limit 截断
    展示面（summary.total 恒为真总数——截断不撒谎）。
    """
    repo = Path(repo).resolve()
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        ranked = rank_findings(con)
    finally:
        con.close()
    shown = ranked["findings"][: max(limit, 0)]
    return {
        "repo": str(repo),
        "summary": ranked["summary"],
        "limit": limit,
        "shown": len(shown),
        "findings": shown,
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同报告链）。"""
    s = result["summary"]
    lines = [f"vrp findings - {Path(result['repo']).name}"]
    lines.append(
        f"  findings    : {s['total']} total ({s['kinds']} kinds),"
        f" showing {result['shown']}"
    )
    lines.append(
        f"  dismissals  : {s['dismiss_decisions']} decision(s)"
        f" - weight = {s['downweight_factor']}^class_dismissals"
        f" (downweight never deletes)"
    )
    for kind, count in s["dismissed_kinds"].items():
        lines.append(f"    {kind} dismissed {count}x")
    for e in result["findings"]:
        loc = e["file_path"] or "-"
        if e["start_line"]:
            loc += f":{e['start_line']}"
        lines.append(
            f"  #{e['id']:<6} {e['weight']:.4f}  score {e['score']:7.4f}  {e['pathology']}/{e['kind']}  {loc}"
        )
    return "\n".join(lines)
