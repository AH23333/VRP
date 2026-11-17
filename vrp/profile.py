"""R4（构想v12）：仓库数字画像——描述性统计体检（纯 stdlib）。

census 的统计深化：耦合分布 Gini 系数（入度/出度/文件符号数三面）、
集中度 top1%/top10% 累计占比、边密度、扇入扇出枢纽 top。

诚实边界记档：**不做幂律拟合**（Clauset-Shalizi-Newman 需 MLE+KS
检验+p-value 重型统计，v1 集中度指标是充分的描述性替代——真"拟合
检验"需求出现再立项）；Gini 对全零分布诚实 0.0；密度 n<=1 诚实 0.0。

只读查询面（ro_connect，红线同 groups：行零改动）；确定性（复现性
红线）：输出不含时间戳——同 facts.db 同输出；枢纽排序 (count DESC,
fid) 纯确定。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import dbpath

TOP_K = 5
DEFAULT_LIMIT = 10


def gini(values: list[int]) -> float:
    """Gini 系数（相对均值绝对差一半的规范形式）。

    全零/单元素/全等 → 0.0（诚实：无差异即无不均）。
    """
    n = len(values)
    if n < 2:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    ordered = sorted(values)
    cum = 0.0
    weighted = 0.0
    for i, v in enumerate(ordered):
        cum += v
        weighted += (i + 1) * v
    return round((2 * weighted) / (n * total) - (n + 1) / n, 4)


def concentration(values: list[int]) -> dict[str, float | int]:
    """集中度：降序累计 top1%/top10% 占比（幂律拟合的诚实替代）。"""
    n = len(values)
    total = sum(values)
    if n == 0 or total == 0:
        return {"count": 0, "sum": 0, "top1pct_share": 0.0, "top10pct_share": 0.0}
    ordered = sorted(values, reverse=True)
    k1 = max(1, round(n * 0.01))
    k10 = max(1, round(n * 0.10))
    top1 = sum(ordered[:k1]) / total
    top10 = sum(ordered[:k10]) / total
    return {
        "count": n,
        "sum": total,
        "top1pct_share": round(top1, 4),
        "top10pct_share": round(top10, 4),
    }


def describe(values: list[int]) -> dict:
    """一面分布的完整描述：gini+集中度+极值均值。"""
    stats = concentration(values)
    return {
        **stats,
        "gini": gini(values),
        "max": max(values) if values else 0,
        "mean": round(sum(values) / len(values), 4) if values else 0.0,
    }


def profile_facts(con: sqlite3.Connection) -> dict:
    """三面分布+密度+枢纽；只读（红线：行零改动）。"""
    n_symbols = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    n_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    in_degrees: dict[str, int] = {}
    out_degrees: dict[str, int] = {}
    for src, dst in con.execute("SELECT src_fid, dst_fid FROM edges").fetchall():
        out_degrees[src] = out_degrees.get(src, 0) + 1
        in_degrees[dst] = in_degrees.get(dst, 0) + 1
    file_counts: dict[str, int] = {}
    for (fp,) in con.execute(
        "SELECT file_path FROM symbols WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchall():
        file_counts[fp] = file_counts.get(fp, 0) + 1
    density = (
        round(n_edges / (n_symbols * (n_symbols - 1)), 6)
        if n_symbols > 1 and n_edges
        else 0.0
    )
    top_fan_in = sorted(in_degrees.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_K]
    top_fan_out = sorted(out_degrees.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_K]
    names = {
        row[0]: row[1]
        for row in con.execute("SELECT fid, qualified_name FROM symbols").fetchall()
    }
    return {
        "summary": {
            "symbols": n_symbols,
            "edges": n_edges,
            "files_with_symbols": len(file_counts),
            "edge_density": density,
            "power_law_note": "no CSN fit - top1%/top10% concentration is the honest descriptive proxy",
        },
        "distributions": {
            "in_degree": describe(list(in_degrees.values())),
            "out_degree": describe(list(out_degrees.values())),
            "file_symbols": describe(list(file_counts.values())),
        },
        "hubs": {
            "top_fan_in": [
                {"fid": fid, "qualified_name": names.get(fid, "?"), "in_edges": n}
                for fid, n in top_fan_in
            ],
            "top_fan_out": [
                {"fid": fid, "qualified_name": names.get(fid, "?"), "out_edges": n}
                for fid, n in top_fan_out
            ],
        },
    }


def run_profile(repo: Path) -> dict:
    """`vrp profile` 只读入口：facts.db 缺失诚实 SystemExit。"""
    repo = Path(repo).resolve()
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        result = profile_facts(con)
    finally:
        con.close()
    return {"repo": str(repo), **result}


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同 groups/findings 链）。"""
    s = result["summary"]
    lines = [f"vrp profile - {Path(result['repo']).name}"]
    lines.append(
        f"  scale      : {s['symbols']} symbols, {s['edges']} edges,"
        f" {s['files_with_symbols']} files (density {s['edge_density']})"
    )
    for name, d in result["distributions"].items():
        lines.append(
            f"  {name:<12}: gini {d['gini']:.4f}  top1% {d['top1pct_share']:.4f}"
            f"  top10% {d['top10pct_share']:.4f}  max {d['max']}  mean {d['mean']}"
        )
    lines.append(f"  note       : {s['power_law_note']}")
    hubs = result["hubs"]
    for hub in hubs["top_fan_in"][:DEFAULT_LIMIT]:
        lines.append(
            f"  fan-in  x{hub['in_edges']:<5} {hub['qualified_name']}"
        )
    for hub in hubs["top_fan_out"][:DEFAULT_LIMIT]:
        lines.append(
            f"  fan-out x{hub['out_edges']:<5} {hub['qualified_name']}"
        )
    return "\n".join(lines)
