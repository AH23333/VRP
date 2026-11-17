"""R1（构想v12）：网络科学指标——调用图 PageRank/LPA 社区检测 + active
structure 模块对账（"AI 梳理 vs 图论"交叉验证，分歧即发现）。

纯标准库确定性实现（复现性红线：同 facts.db → 同输出，无随机数/时间戳）：
- PageRank 幂迭代：dangling mass 均匀回注（标准处理），节点按 fid 排序
  初始化与迭代，L1 早停；
- LPA 标签传播确定性变体：无向邻接（调用图双向影响——社区是互相关注
  簇，方向信息留给 PageRank），轮内按 fid 排序异步更新，平局取字典序
  最小标签；原版 LPA 随机序/随机平局，本变体收敛即稳定（语义等价：
  社区划分质量不依赖随机性来源）。

诚实局限：LPA 无模块度分辨率保护——强团弱桥稳定（测试锁），全连通
稀疏图可能整图吞并为单社区（不假装 Louvain，输出 lpa_rounds 与社区
数供人判读）。只读：ro_connect，不写任何表。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import curate as curate_mod
from . import dbpath

PAGERANK_DAMPING = 0.85
PAGERANK_MAX_ITER = 100
PAGERANK_TOL = 1e-6
LPA_MAX_ROUNDS = 50
TOP_RANK_CAP = 20


def load_call_graph(con: sqlite3.Connection) -> dict:
    """edges CALLS/CALL_REFERENCE → 有向出邻接（PageRank 用）+无向权重图
    （LPA 用）。JOIN symbols 双端滤空 file_path 与 `<...>`/`>...` 合成桩
    家族（v_hotspots 同口径——#96 桩噪音教训：`<python-builtins>` 桩
    fan-in 巨大会吞没 PageRank top 榜）；ORDER BY 定序。
    """
    rows = con.execute(
        "SELECT e.src_fid, e.dst_fid, COUNT(*) AS w"
        " FROM edges e"
        " JOIN symbols s ON s.fid = e.src_fid"
        " JOIN symbols d ON d.fid = e.dst_fid"
        " WHERE e.type IN ('CALLS','CALL_REFERENCE')"
        " AND s.file_path != '' AND d.file_path != ''"
        " AND s.file_path NOT GLOB '<*' AND d.file_path NOT GLOB '<*'"
        " AND s.file_path NOT GLOB '>*' AND d.file_path NOT GLOB '>*'"
        " GROUP BY e.src_fid, e.dst_fid"
        " ORDER BY e.src_fid, e.dst_fid"
    ).fetchall()
    out_adj: dict[str, dict[str, int]] = {}
    und: dict[str, dict[str, int]] = {}
    nodes: set[str] = set()
    for src, dst, w in rows:
        out_adj.setdefault(src, {})[dst] = w
        u_src = und.setdefault(src, {})
        u_src[dst] = u_src.get(dst, 0) + w
        u_dst = und.setdefault(dst, {})
        u_dst[src] = u_dst.get(src, 0) + w
        nodes.add(src)
        nodes.add(dst)
    return {"nodes": sorted(nodes), "out": out_adj, "undirected": und, "edge_pairs": len(rows)}


def pagerank(
    graph: dict,
    damping: float = PAGERANK_DAMPING,
    max_iter: int = PAGERANK_MAX_ITER,
    tol: float = PAGERANK_TOL,
) -> tuple[dict[str, float], int]:
    """确定性 PageRank（幂迭代）。返回 (rank, iterations)。"""
    nodes = graph["nodes"]
    n = len(nodes)
    if n == 0:
        return {}, 0
    out = graph["out"]
    rank = {fid: 1.0 / n for fid in nodes}
    out_weight = {fid: sum(out.get(fid, {}).values()) for fid in nodes}
    in_adj: dict[str, list[tuple[str, int]]] = {fid: [] for fid in nodes}
    for src, targets in out.items():
        for dst, w in targets.items():
            in_adj[dst].append((src, w))
    iterations = 0
    for _ in range(max_iter):
        iterations += 1
        dangling = sum(rank[fid] for fid in nodes if out_weight[fid] == 0)
        base = (1.0 - damping) / n + damping * dangling / n
        new: dict[str, float] = {}
        for fid in nodes:
            acc = base
            for src, w in in_adj[fid]:
                acc += damping * rank[src] * w / out_weight[src]
            new[fid] = acc
        delta = sum(abs(new[fid] - rank[fid]) for fid in nodes)
        rank = new
        if delta < tol:
            break
    return rank, iterations


def lpa(graph: dict, max_rounds: int = LPA_MAX_ROUNDS) -> tuple[dict[str, str], int]:
    """确定性 LPA：返回 ({fid: canonical_community_id}, rounds)。

    canonical id=社区内字典序最小 fid（稳定命名）；轮内按 fid 排序异步
    更新，平局取字典序最小标签。
    """
    nodes = graph["nodes"]
    und = graph["undirected"]
    labels = {fid: fid for fid in nodes}
    rounds = 0
    for _ in range(max_rounds):
        rounds += 1
        changed = False
        for fid in nodes:
            counts: dict[str, int] = {}
            for nbr, w in und.get(fid, {}).items():
                lbl = labels[nbr]
                counts[lbl] = counts.get(lbl, 0) + w
            if not counts:
                continue
            best = max(sorted(counts), key=lambda l: counts[l])
            if best != labels[fid]:
                labels[fid] = best
                changed = True
        if not changed:
            break
    members: dict[str, list[str]] = {}
    for fid, lbl in labels.items():
        members.setdefault(lbl, []).append(fid)
    canon = {lbl: min(fids) for lbl, fids in members.items()}
    communities = {fid: canon[lbl] for fid, lbl in labels.items()}
    return communities, rounds


def structure_modules(con: sqlite3.Connection) -> list[dict] | None:
    """active structure 模块：[{module_id, fids}]；最新 structure 非 active
    或缺失 → None（对账跳过——与 s7 同口径：只 active 是有效口径）。
    """
    info = curate_mod.get_structure(con)
    if info is None or info["payload"] is None or info["status"] != "active":
        return None
    mods: list[dict] = []
    for mod in info["payload"].get("modules", []):
        mods.append(
            {
                "module_id": mod.get("module_id") or "",
                "fids": list(mod.get("symbol_fids") or []),
            }
        )
    return mods


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def reconcile(communities: dict[str, str], modules: list[dict], graph_nodes: list[str]) -> dict:
    """模块×社区 Jaccard 对账：每模块最佳社区 + 每社区 top 模块 + summary。"""
    comm_members: dict[str, set[str]] = {}
    for fid, cid in communities.items():
        comm_members.setdefault(cid, set()).add(fid)
    node_set = set(graph_nodes)
    per_module: list[dict] = []
    per_community: dict[str, list[tuple[str, float]]] = {cid: [] for cid in comm_members}
    for mod in modules:
        fids = set(mod["fids"])
        best: dict | None = None
        for cid in sorted(comm_members):
            members = comm_members[cid]
            inter = len(fids & members)
            if inter == 0:
                continue
            j = _jaccard(fids, members)
            if best is None or j > best["jaccard"]:
                best = {"community": cid, "jaccard": round(j, 4), "overlap": inter}
        entry = {
            "module_id": mod["module_id"],
            "module_fids": len(fids),
            "in_graph": len(fids & node_set),
            "best_community": best["community"] if best else None,
            "jaccard": best["jaccard"] if best else 0.0,
            "overlap": best["overlap"] if best else 0,
        }
        per_module.append(entry)
        if best:
            per_community[best["community"]].append((mod["module_id"], best["jaccard"]))
    communities_out = [
        {
            "community": cid,
            "size": len(comm_members[cid]),
            "top_modules": sorted(per_community[cid], key=lambda t: (-t[1], t[0]))[:3],
        }
        for cid in comm_members
    ]
    communities_out.sort(key=lambda c: (-c["size"], c["community"]))
    matched = [m for m in per_module if m["best_community"]]
    summary = {
        "modules": len(per_module),
        "modules_with_overlap": len(matched),
        "zero_overlap_modules": [m["module_id"] for m in per_module if not m["best_community"]],
        "avg_best_jaccard": (
            round(sum(m["jaccard"] for m in matched) / len(matched), 4) if matched else 0.0
        ),
        "communities": len(comm_members),
    }
    per_module.sort(key=lambda m: (-m["jaccard"], m["module_id"]))
    return {"summary": summary, "modules": per_module, "communities": communities_out}


def run_graph(repo: Path) -> dict:
    """`vrp graph` 入口：只读网络科学视图。facts.db 缺失诚实 SystemExit。"""
    repo = Path(repo).resolve()
    facts_path = repo / ".vrp" / "facts.db"
    if not facts_path.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts_path}")
    con = dbpath.ro_connect(facts_path)
    try:
        graph = load_call_graph(con)
        rank, pr_iters = pagerank(graph)
        communities, lpa_rounds = lpa(graph)
        modules = structure_modules(con)
        recon = reconcile(communities, modules, graph["nodes"]) if modules else None
        top = sorted(rank, key=lambda f: (-rank[f], f))[:TOP_RANK_CAP]
        info: dict[str, tuple] = {}
        if top:
            rows = con.execute(
                "SELECT fid, qualified_name, name, file_path FROM symbols"
                f" WHERE fid IN ({','.join('?' * len(top))})",
                top,
            ).fetchall()
            info = {r[0]: (r[1], r[2], r[3]) for r in rows}
        pagerank_top = [
            {
                "fid": fid,
                "qualified_name": info.get(fid, ("", "", ""))[0],
                "name": info.get(fid, ("", "", ""))[1],
                "file_path": info.get(fid, ("", "", ""))[2],
                "score": round(rank[fid], 6),
            }
            for fid in top
        ]
        comm_sizes: dict[str, int] = {}
        for cid in communities.values():
            comm_sizes[cid] = comm_sizes.get(cid, 0) + 1
        return {
            "repo": str(repo),
            "graph": {"nodes": len(graph["nodes"]), "edge_pairs": graph["edge_pairs"]},
            "pagerank": {"iterations": pr_iters, "top": pagerank_top},
            "communities": {
                "count": len(comm_sizes),
                "lpa_rounds": lpa_rounds,
                "largest": max(comm_sizes.values()) if comm_sizes else 0,
            },
            "structure_reconcile": recon,
        }
    finally:
        con.close()


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同报告链）。"""
    lines = [f"vrp graph - {Path(result['repo']).name}"]
    g = result["graph"]
    lines.append(f"  graph       : {g['nodes']} nodes / {g['edge_pairs']} weighted pairs")
    pr = result["pagerank"]
    lines.append(f"  pagerank    : {pr['iterations']} iterations (damping {PAGERANK_DAMPING})")
    for e in pr["top"][:10]:
        nm = e["qualified_name"] or e["fid"]
        lines.append(f"    {e['score']:.6f}  {nm}  ({e['file_path']})")
    c = result["communities"]
    lines.append(
        f"  communities : {c['count']} (LPA {c['lpa_rounds']} rounds, largest {c['largest']})"
    )
    recon = result["structure_reconcile"]
    if recon is None:
        lines.append(
            "  structure   : no active structure - reconciliation skipped (run 'vrp curate')"
        )
    else:
        s = recon["summary"]
        lines.append(
            f"  structure   : {s['modules']} modules vs {s['communities']} communities"
            f" - avg best Jaccard {s['avg_best_jaccard']},"
            f" zero-overlap {len(s['zero_overlap_modules'])}"
        )
        for m in recon["modules"][:10]:
            best = m["best_community"] or "-"
            lines.append(
                f"    {m['module_id']:<28} jaccard={m['jaccard']:<8}"
                f" overlap={m['overlap']:<5} in_graph={m['in_graph']}/{m['module_fids']} best={best}"
            )
        if s["zero_overlap_modules"]:
            lines.append("    zero-overlap: " + ", ".join(s["zero_overlap_modules"]))
    return "\n".join(lines)
