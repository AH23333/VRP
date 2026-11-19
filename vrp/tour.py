"""T11（构想v12）：代码导览报告——面向新人的模块导览。

curate 消费面新增导览模板（curated 数据已有，叙事模板不同——与
v10-E14 skill 区分：这是**输出产物**非操作协议）。形态参照：VS Code
Code Tour（导览步骤化）；Diátaxis tutorial（教学文档结构——由浅入深）。

数据源：facts.db `structure` 表 active 载荷（curate 产物，模块
name/responsibility 人写语义单源直引——**每模块一句话职责**）+
`symbols` join（成员符号 → 文件分布/代表符号——新人具体锚点）。

**建议阅读顺序**（deterministic，教学序=由浅入深）：
1. 层拓扑序——`layers[].may_depend_on` 方向：X 依赖 Y 则 Y 先读
   （被依赖的地基先读；Kahn 拓扑+字典序 tie-break；环诚实保留原序
   不猜——结构契约层间无环是 curate 验证面）；
2. 层内符号数升序（小模块先读——教程"从小可消化的开始"），
   平局 module_id 字典序。

rc 语义：报告面恒 0；structure 缺失诚实退出（先跑 curate）。
确定性：只读查询零写入；同 facts.db → 同输出（无时间戳全排序）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import dbpath

REPRESENTATIVE_CAP = 5  # 每模块代表符号数
TOP_FILES_CAP = 3  # 每模块主要文件数


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    return dbpath.ro_connect(db)


def _load_structure(con: sqlite3.Connection) -> dict:
    """active structure 载荷；无 active 回退最新一条；全缺诚实退出。"""
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = con.execute(
            "SELECT payload_json FROM structure ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise SystemExit("no curated structure in facts.db - run curate first")
    payload = json.loads(row[0])
    if not isinstance(payload, dict) or not payload.get("modules"):
        raise SystemExit("curated structure has no modules")
    return payload


def _layer_order(payload: dict) -> list[str]:
    """层拓扑序（被依赖者先读）：Kahn+字典序 tie-break；环残余按字典序
    尾附（curate 契约层间无环，残余是防御呈现非预期路径）。"""
    layers = payload.get("layers") or []
    deps: dict[str, list[str]] = {l["name"]: list(l.get("may_depend_on") or []) for l in layers}
    order: list[str] = []
    pending = dict(deps)
    while pending:
        ready = sorted(n for n, d in pending.items() if not d)
        if not ready:
            order.extend(sorted(pending))
            break
        for n in ready:
            order.append(n)
            del pending[n]
        for n in pending:
            pending[n] = [d for d in pending[n] if d in pending]
    return order


def run_tour(repo: Path) -> dict:
    """`vrp tour` 入口。"""
    repo = Path(repo)
    con = _connect_ro(repo)
    try:
        payload = _load_structure(con)
        modules = payload["modules"]
        fid_mod: dict[str, str] = {}
        for m in modules:
            for fid in m.get("symbol_fids", []):
                fid_mod[fid] = m["module_id"]
        marks = ",".join("?" * len(fid_mod)) or "''"
        members: dict[str, list[dict]] = {m["module_id"]: [] for m in modules}
        if fid_mod:
            for fid, name, fp in con.execute(
                f"SELECT fid, name, file_path FROM symbols WHERE fid IN ({marks})",
                sorted(fid_mod),
            ):
                members[fid_mod[fid]].append({"name": name or "", "file": fp or ""})
        layer_seq = _layer_order(payload)
        layer_rank = {n: i for i, n in enumerate(layer_seq)}
        # 阅读序：层拓扑序 → 层内符号数升序 → module_id 字典序
        enriched = []
        for m in modules:
            mid = m["module_id"]
            mem = members[mid]
            file_counts: dict[str, int] = {}
            for s in mem:
                if s["file"]:
                    file_counts[s["file"]] = file_counts.get(s["file"], 0) + 1
            top_files = sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FILES_CAP]
            enriched.append({
                "module_id": mid,
                "name": m.get("name", mid),
                "responsibility": m.get("responsibility", ""),
                "layer": m.get("layer", ""),
                "symbols": len(mem),
                "top_files": [{"file": f, "count": c} for f, c in top_files],
                "representatives": sorted({s["name"] for s in mem if s["name"]})[:REPRESENTATIVE_CAP],
            })
        ordered = sorted(
            enriched,
            key=lambda e: (layer_rank.get(e["layer"], len(layer_rank)), e["symbols"], e["module_id"]),
        )
        for i, e in enumerate(ordered, 1):
            e["reading_step"] = i
        return {
            "repo": str(repo),
            "modules": len(ordered),
            "layers": layer_seq,
            "tour": ordered,
            "note": "newcomer module tour from curated structure - reading order:"
            " layer topo (depended-on first) then ascending symbol count"
            " (small modules first, tutorial style); responsibility text is"
            " curated human semantics (single source)",
        }
    finally:
        con.close()


def render_text(result: dict) -> str:
    """人读呈现：导览正文（阅读序+每模块一句话职责+具体锚点）。
    VS Code Code Tour 步骤化形态：Step N 标号逐模块。"""
    lines = [f"# Code tour - {Path(result['repo']).name}"]
    lines.append(
        f"newcomer module tour: {result['modules']} modules,"
        f" {len(result['layers'])} layers (reading order: foundations first,"
        " then small modules before large)"
    )
    lines.append("")
    for step in result["tour"]:
        lines.append(
            f"## Step {step['reading_step']}: {step['name']} ({step['module_id']})"
            f" [{step['layer'] or 'no layer'}]"
        )
        resp = step["responsibility"] or "(no curated responsibility yet)"
        lines.append(f"{resp}")
        bits = [f"{step['symbols']} symbols"]
        if step["top_files"]:
            bits.append("main files: " + ", ".join(
                f"{f['file']} ({f['count']})" for f in step["top_files"]))
        if step["representatives"]:
            bits.append("representatives: " + ", ".join(step["representatives"]))
        lines.append(" | ".join(bits))
        lines.append("")
    return "\n".join(lines)
