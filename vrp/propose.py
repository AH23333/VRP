"""#143 B 件：重组切片提议（学 unwind slices propose）。

确定性分区提议：候选=真实文件非伪符号（label 排除 File/Module——
auto_curate grounding 同口径）；种子=顶层目录（unwind "seeded by
top-level directories"）；贪心凝聚合并——种子对按跨种子调用边权
（SLICE_EDGE_TYPES 同口径，单一权威从 curate 导入）降序合并至目标
模块数 PROPOSE_TARGET_MODULES，同权字典序 tie-break；平衡帽
PROPOSE_BALANCE_CAP（合并后符号数占比上限，unwind "balanced by
candidate count"）禁并超帽合并；超帽单目录先按子目录切分（一层）再
参与合并。

输出 curate structure 输入格式草案：modules[].name/responsibility 为
[PROPOSED] 占位（非空合法但明示待 AI/人补语义）、单层 proposed。
写 .vrp/proposals/structure-proposal.json（**不在 .vrp/ai/**——curate
扫 .vrp/ai/ 全收，误放=未经审阅自动提交；提议与采纳分离）。采纳路径：
改写草案（补语义/调边界）放 .vrp/ai/ 走既有 curate 全量验证——
提议不旁路，重提仍全量重验（remap 同哲学）。

同 facts.db → 字节级同提议文件（无时间戳，全排序确定性）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import dbpath
from .curate import SLICE_EDGE_TYPES

PROPOSE_TARGET_MODULES = 8
PROPOSE_BALANCE_CAP = 0.6
PSEUDO_LABELS = ("File", "Module")  # 伪符号排除——auto_curate grounding 同口径

PROPOSAL_DEFAULT_PATH = Path(".vrp") / "proposals" / "structure-proposal.json"


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    return dbpath.ro_connect(db)


def _seed_of(file_path: str) -> str:
    """顶层目录种子；根级文件归 (root)。"""
    parts = file_path.replace("\\", "/").split("/")
    return parts[0] if len(parts) > 1 else "(root)"


def propose_structure(
    repo: Path,
    out: Path | None = None,
    target_modules: int | None = None,
    balance_cap: float | None = None,
) -> dict:
    """确定性 structure 分区提议（详见模块 docstring）。返回 {ok, proposal, ...}。"""
    repo = Path(repo)
    target = PROPOSE_TARGET_MODULES if target_modules is None else target_modules
    if target < 1:
        raise SystemExit("propose: target_modules must be >= 1")
    ratio = PROPOSE_BALANCE_CAP if balance_cap is None else balance_cap
    if not 0 < ratio <= 1.0:
        raise SystemExit("propose: balance_cap must be in (0, 1.0]")
    con = _connect_ro(repo)
    try:
        rows = con.execute(
            "SELECT fid, file_path FROM symbols"
            " WHERE label NOT IN (?, ?) AND file_path IS NOT NULL AND file_path != ''"
            " AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'",
            PSEUDO_LABELS,
        ).fetchall()
        seeds: dict[str, list[str]] = {}
        fid_seed: dict[str, str] = {}
        for fid, fp in rows:
            seed = _seed_of(fp)
            seeds.setdefault(seed, []).append(fid)
            fid_seed[fid] = seed
        total = len(rows)
        if total == 0:
            return {
                "ok": False,
                "reason": "no candidate symbols (real files, non-pseudo labels)",
                "note": "nothing to partition",
            }
        cap = max(1, int(ratio * total))
        # 超帽单目录按子目录切分（一层；单子目录不切——原名保留防改名噪声；
        # 切后再超帽如实保留——平衡帽禁并兜底）
        for seed in sorted(seeds):
            if len(seeds[seed]) <= cap or seed == "(root)":
                continue
            sub: dict[str, list[str]] = {}
            for fid, fp in rows:
                if fid_seed[fid] != seed:
                    continue
                parts = fp.replace("\\", "/").split("/")
                sub_name = f"{seed}/{parts[1]}" if len(parts) > 2 else f"{seed}/(direct)"
                sub.setdefault(sub_name, []).append(fid)
            if len(sub) <= 1:
                continue
            del seeds[seed]
            for name, fids_sub in sub.items():
                seeds[name] = fids_sub
                for fid in fids_sub:
                    fid_seed[fid] = name
        # 跨种子边权（无向对计数——合并亲和信号）
        marks = ",".join("?" * len(SLICE_EDGE_TYPES))
        weights: dict[tuple[str, str], int] = {}
        for src, dst in con.execute(
            f"SELECT src_fid, dst_fid FROM edges WHERE type IN ({marks})", SLICE_EDGE_TYPES
        ):
            a = fid_seed.get(src)
            b = fid_seed.get(dst)
            if not a or not b or a == b:
                continue
            key = (a, b) if a < b else (b, a)
            weights[key] = weights.get(key, 0) + 1
    finally:
        con.close()
    # 贪心凝聚：最大权对优先（同权字典序最小对），平衡帽禁并超帽合并；
    # 无可合并对（全被帽挡/权 0）即停——诚实少于目标数
    current = {s: sorted(f) for s, f in seeds.items()}
    sizes = {s: len(f) for s, f in current.items()}
    merges: list[dict] = []
    while len(current) > target:
        best: tuple[int, str, str] | None = None
        for (a, b), w in weights.items():
            if a not in current or b not in current or w <= 0:
                continue
            if sizes[a] + sizes[b] > cap:
                continue
            if (
                best is None
                or w > best[0]
                or (w == best[0] and (a, b) < (best[1], best[2]))
            ):
                best = (w, a, b)
        if best is None:
            break
        w, a, b = best
        # 合并后模块身份=较大者（符号数降序、平局字典序）
        keep, drop = (a, b) if (sizes[a] > sizes[b] or (sizes[a] == sizes[b] and a < b)) else (b, a)
        current[keep] = sorted(current[keep] + current[drop])
        sizes[keep] += sizes[drop]
        del current[drop]
        weights.pop((a, b) if a < b else (b, a), None)
        for x in list(current):
            if x == keep:
                continue
            k2_old = (drop, x) if drop < x else (x, drop)
            w2 = weights.pop(k2_old, 0)
            if w2:
                k2 = (keep, x) if keep < x else (x, keep)
                weights[k2] = weights.get(k2, 0) + w2
        merges.append({"kept": keep, "merged": drop, "weight": w})
    modules = []
    for mid in sorted(current):
        modules.append({
            "module_id": mid,
            "name": f"[PROPOSED] {mid}",
            "responsibility": f"[PROPOSED] deterministic partition seed: {mid} (rename + describe after review)",
            "symbol_fids": current[mid],
            "layer": "proposed",
        })
    payload = {
        "modules": modules,
        "layers": [{"name": "proposed", "may_depend_on": []}],
    }
    out_path = Path(out) if out else repo / PROPOSAL_DEFAULT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    out_path.write_text(text, encoding="utf-8", newline="\n")
    return {
        "ok": True,
        "proposal": str(out_path),
        "candidates": total,
        "modules": len(modules),
        "target_modules": target,
        "reached_target": len(modules) <= target,
        "balance_cap_symbols": cap,
        "merges": merges,
        "module_sizes": {m["module_id"]: len(m["symbol_fids"]) for m in modules},
        "note": "deterministic partition proposal (top-dir seeds, edge-weight greedy merge, balance cap); "
        "curate structure input draft - review, rename, describe, place in .vrp/ai/ and run curate "
        "(proposal does not bypass validation); oversized modules in module_sizes are honest "
        "(subdir split is one level)",
    }
