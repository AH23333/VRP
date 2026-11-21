"""D11 迁移进度追踪（构想v10 §D11）——seams 快照→strangler 完成度%。

自产面（A1 时间线+seams 组合，无独立轮子）：跨界边定期快照→收缩曲线。
seams 单源=nav.nav_seams（#143 只读聚合——本模块零重复实现，跨界边
总数与模块对面直接取聚合结果）。

快照载体=.vrp/migprog-history.jsonl（**追加审计语义**——runtime_evidence
先例：运行审计追加不清理，非 ledger 状态面；用户触发节奏=定期快照的
"定期"由人定，工具只记账）。条目含 ts（J10 perf.jsonl 同先例——运行
审计时间戳不违反产物确定性红线：facts.db 派生产物不含 ts，审计流含）。

完成度%=（基线跨界边-当前跨界边）/基线跨界边×100（strangler 迁移推进
=缝合线收缩；负值=耦合增长诚实上报）。基线=history 首条（迁移起点）；
零基线完成度 None（诚实未定义）。无快照→诚实提示先 --snapshot。
rc 恒 0（追踪面非门）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import nav as nav_mod

HISTORY_NAME = ".vrp/migprog-history.jsonl"


def snapshot(repo: Path) -> dict:
    """当前跨界边快照（nav_seams 单源）。"""
    seams = nav_mod.nav_seams(repo)
    if seams.get("structure") != "active":
        return {
            "structure": seams.get("structure", "missing"),
            "cross_edges": None,
            "pairs": [],
            "note": seams.get("note", "no active structure - run 'vrp curate-auto' first"),
        }
    pairs = [
        {"from": s["from"], "to": s["to"], "edges": s["edges"]}
        for s in seams.get("seams", [])
    ]
    return {
        "structure": "active",
        "cross_edges": sum(p["edges"] for p in pairs),
        "pairs": pairs,
    }


def save_snapshot(repo: Path) -> dict:
    """快照追加进 history jsonl（审计流）。"""
    snap = snapshot(repo)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structure": snap["structure"],
        "cross_edges": snap["cross_edges"],
        "pairs": snap["pairs"],
    }
    history_path = repo / HISTORY_NAME
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"saved": True, "history_path": str(history_path), **entry}


def read_history(repo: Path) -> list[dict]:
    """宽容解析 history jsonl（坏行跳过）。"""
    path = repo / HISTORY_NAME
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def progress(repo: Path) -> dict:
    """当前 vs 基线（history 首条=迁移起点）+vs 上一条（阶段增量）。"""
    current = snapshot(repo)
    history = read_history(repo)
    if not history:
        return {
            **current,
            "snapshots": 0,
            "baseline": None,
            "progress_pct": None,
            "vs_last": None,
            "note": "no snapshots yet - run 'vrp migprog --snapshot' to record the migration start",
        }
    baseline = history[0]
    last = history[-1]
    base_edges = baseline.get("cross_edges")
    cur_edges = current["cross_edges"]

    def _pct(base: int | None, cur: int | None) -> float | None:
        if not isinstance(base, int) or not isinstance(cur, int) or base == 0:
            return None
        return round((base - cur) * 100.0 / base, 2)

    vs_last = None
    if last is not baseline:
        vs_last = {
            "ts": last.get("ts"),
            "cross_edges": last.get("cross_edges"),
            "delta_pct": _pct(last.get("cross_edges"), cur_edges),
        }
    return {
        **current,
        "snapshots": len(history),
        "baseline": {"ts": baseline.get("ts"), "cross_edges": base_edges},
        "progress_pct": _pct(base_edges, cur_edges),
        "vs_last": vs_last,
        "note": "progress_pct = (baseline - current) / baseline cross edges (strangler seam shrink; negative = coupling grew, honest)",
    }


def render_text(result: dict) -> str:
    lines = ["== migprog =="]
    if result.get("cross_edges") is None:
        lines.append(f"structure: {result.get('structure')} - {result.get('note', '')}")
        return "\n".join(lines)
    lines.append(f"cross_edges: {result['cross_edges']} over {len(result['pairs'])} module pair(s)")
    if result.get("snapshots"):
        lines.append(f"snapshots: {result['snapshots']}")
        base = result["baseline"]
        lines.append(f"baseline: cross_edges={base['cross_edges']} at {base['ts']}")
        pct = result["progress_pct"]
        pct_s = f"{pct:+.2f}%" if pct is not None else "n/a (zero baseline)"
        lines.append(f"progress: {pct_s} seam shrink since baseline")
        if result.get("vs_last"):
            vs = result["vs_last"]
            delta = vs["delta_pct"]
            delta_s = f"{delta:+.2f}%" if delta is not None else "n/a"
            lines.append(f"vs last snapshot ({vs['ts']}): cross_edges={vs['cross_edges']}, delta {delta_s}")
    else:
        lines.append(str(result.get("note", "")))
    return "\n".join(lines)
