"""S6（构想v12）：finding 优先级评分——公式开放可解释。

构想：置信度 x 影响面 / 修复成本 -> finding 排序（现在是无序清单）。
学 SonarQube 评分维度设计（quality gate 语义），造开放公式——不学黑盒：
每个 finding 输出全部分量明细（components），人可手算复验；权重可配
（--weights JSON 覆盖默认，公式行随权重实时渲染）。

三分量（全从 facts.db 只读导出，零 N+1——两份聚合单遍预取）：
  impact   = log1p(锚符号入度)  —— edges dst 计数（v_hotspots 入向信号同源）；
             被引用多的符号出 finding，修复影响面大 -> 升优先级。
  evidence = log1p(同锚独立 kind 数 - 1) —— 第二类信号起才是交叉佐证（第一类
             是信号本身，单类锚 evidence 诚实 0）-> 升优先级（DISTINCT kind 而
             非条数：同类 81 条重复信号不抬置信，lusiem x81 组实录）。
  span     = log1p(end_line - start_line + 1) —— 修复成本 proxy（改动行面大
             = 成本高）-> 在分母：线性形态下取负号。

score = w_impact * impact + w_evidence * evidence - w_span * span
默认权重 {impact: 1.0, evidence: 0.5, span: 0.25}；无锚 finding（unused-file
等）impact/evidence 诚实 0，span 仍按行面计。

确定性（复现性红线）：全函数纯读——同 facts.db 同输出；排序 (-score, id)
平局 id 升序纯确定；round(score, 4) 仅展示面。
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from . import dbpath

DEFAULT_LIMIT = 50
WEIGHT_KEYS = ("impact", "evidence", "span")
DEFAULT_WEIGHTS = {"impact": 1.0, "evidence": 0.5, "span": 0.25}


def parse_weights(raw: str | None) -> dict[str, float]:
    """--weights JSON 解析：未知键诚实拒绝（防 typo 静默走默认）；None=默认。"""
    if raw is None:
        return dict(DEFAULT_WEIGHTS)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"--weights is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("--weights must be a JSON object like '{\"impact\": 1.0}'")
    for key in data:
        if key not in WEIGHT_KEYS:
            raise SystemExit(f"unknown weight key '{key}' - valid keys: {', '.join(WEIGHT_KEYS)}")
        if not isinstance(data[key], (int, float)) or isinstance(data[key], bool):
            raise SystemExit(f"weight '{key}' must be a number")
    weights = dict(DEFAULT_WEIGHTS)
    weights.update({key: float(data[key]) for key in data})
    return weights


def _indegrees(con: sqlite3.Connection) -> dict[str, int]:
    """锚入度单遍预取（edges 25 万边级大仓一次 GROUP BY，勿 N+1）。"""
    return {dst: count for dst, count in con.execute(
        "SELECT dst_fid, COUNT(*) FROM edges GROUP BY dst_fid"
    ).fetchall()}


def _anchor_evidence(con: sqlite3.Connection) -> dict[str, int]:
    """同锚独立 kind 数预取（交叉佐证分量）。"""
    return {fid_: count for fid_, count in con.execute(
        "SELECT anchor_fid, COUNT(DISTINCT kind) FROM findings"
        " WHERE anchor_fid IS NOT NULL GROUP BY anchor_fid"
    ).fetchall()}


def score_components(
    row: tuple, indegrees: dict[str, int], evidence: dict[str, int], weights: dict[str, float]
) -> tuple[float, dict]:
    """单 finding 评分：返回 (score, components)——分量明细全量给出，
    人可按公式行手算复验（公式开放可解释红线）。

    span 下限 0：end 缺失/行序异常不产生负成本；无锚 impact/evidence 诚实 0。
    """
    _fid_, kind, anchor, start, end = row
    impact = math.log1p(indegrees.get(anchor, 0)) if anchor else 0.0
    evid = math.log1p(max(evidence.get(anchor, 0) - 1, 0)) if anchor else 0.0
    if start is not None and end is not None and end >= start:
        span = math.log1p(end - start + 1)
    else:
        span = 0.0
    score = (
        weights["impact"] * impact
        + weights["evidence"] * evid
        - weights["span"] * span
    )
    components = {
        "impact": round(impact, 4),
        "evidence": round(evid, 4),
        "span": round(span, 4),
    }
    return score, components


def formula_text(weights: dict[str, float]) -> str:
    """公式行随权重实时渲染（人读面第一行——开放可解释的入口）。"""
    return (
        f"score = {weights['impact']:.2f}*impact"
        f" + {weights['evidence']:.2f}*evidence"
        f" - {weights['span']:.2f}*span"
    )


def score_findings(con: sqlite3.Connection, weights: dict[str, float]) -> dict:
    """评分视图：只读；排序 (-score, id) 平局 id 升序纯确定。"""
    indegrees = _indegrees(con)
    evidence = _anchor_evidence(con)
    entries: list[dict] = []
    kinds: set[str] = set()
    for fid_, pathology, kind, source, anchor, path, start, end in con.execute(
        "SELECT id, pathology, kind, source, anchor_fid, file_path,"
        " start_line, end_line FROM findings ORDER BY id"
    ).fetchall():
        kinds.add(kind)
        score, components = score_components(
            (fid_, kind, anchor, start, end), indegrees, evidence, weights
        )
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
                "score": round(score, 4),
                "components": components,
            }
        )
    entries.sort(key=lambda e: (-e["score"], e["id"]))
    return {
        "summary": {
            "total": len(entries),
            "kinds": len(kinds),
            "formula": formula_text(weights),
            "weights": dict(weights),
        },
        "findings": entries,
    }


def run_score(repo: Path, limit: int = DEFAULT_LIMIT, weights: dict[str, float] | None = None) -> dict:
    """`vrp score` 只读入口：facts.db 缺失诚实 SystemExit；limit 截断展示面
    （summary.total 恒为真总数——截断不撒谎，findings/groups 同哲学）。
    """
    if weights is None:
        weights = dict(DEFAULT_WEIGHTS)
    repo = Path(repo).resolve()
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        scored = score_findings(con, weights)
    finally:
        con.close()
    shown = scored["findings"][: max(limit, 0)]
    return {
        "repo": str(repo),
        "summary": scored["summary"],
        "limit": limit,
        "shown": len(shown),
        "findings": shown,
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同报告链；首行公式=可解释入口）。"""
    s = result["summary"]
    lines = [f"vrp score - {Path(result['repo']).name}"]
    lines.append(f"  formula    : {s['formula']}")
    lines.append(
        f"  findings   : {s['total']} total ({s['kinds']} kinds),"
        f" showing {result['shown']}"
    )
    for e in result["findings"]:
        loc = e["file_path"] or "-"
        if e["start_line"]:
            loc += f":{e['start_line']}"
        c = e["components"]
        lines.append(
            f"  #{e['id']:<6} {e['score']:7.4f}  {e['pathology']}/{e['kind']}  {loc}"
            f"  [impact {c['impact']} evidence {c['evidence']} span {c['span']}]"
        )
    return "\n".join(lines)
