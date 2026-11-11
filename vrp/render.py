"""S8 render - 中间产物静态报告（构想v7 §5）。

三特征：静态产物、无服务端、无前端构建链；数据真值 100% 来自 facts.db；
渲染执行委托外部工具（archify 工具组，缺失降级 Mermaid-only 不阻塞）。

- IR 层：facts.db → archify input IR JSON（确定性——同 facts.db 字节级同 IR）
- archify 接入：VRP_ARCHIFY_MJS / work 沙箱发现 → `archify validate --json` →
  `archify render`；缺失/失败单件降级 Mermaid-only
- Mermaid 降级链：IR → .mmd 源文本（零依赖，核心红线内）
- SARIF 2.1.0：findings 手写 JSON（不引 sarif-om，零依赖）
- report.html：目录页 + T4/open 清单 + Mermaid 内嵌（CDN script，无网显源码）
- R7' 多级颗粒度：report.html（L0）→ modules/m{i}-{arch_id}.html（L1 模块页）
  → symbols/{fid}.html（L2 符号页 + 源码片段），静态多页互链（主链优先）
- R7'(2) 模块交互图链：archify 可用且链成功 → chain/（index → 模块图链
  → 整文件源码页+符号锚点，render_chain 三段式原子提交）替代 L1/L2；
  失败/缺 archify → L1/L2 Mermaid 兜底（降级记录诚实标注）
- 复现性：report.html 不带时间戳，带 facts.git_head + IR sha256（可溯源）
- 产物落 <repo>/.vrp/report/（.vrp/ 已进目标仓 info/exclude，#76 纪律）

纯标准库（archify 经子进程调用）。
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import render_chain
from . import s6_transact

REPORT_DIR = ".vrp/report"
ARCHIFY_TIMEOUT = 120
SEQUENCE_SLICES_CAP = 10
SARIF_FINDINGS_CAP = 2000
VERDICTS_CAP = 200
FID_CHUNK = 500
MODULE_PAGES_CAP = 50
MODULE_SYMBOLS_TABLE_CAP = 500
MODULE_GRAPH_NODES_CAP = 40
MODULE_TOP_SYMBOL_PAGES = 20
SYMBOL_PAGES_GLOBAL_CAP = 300
SYMBOL_NEIGHBORS_PAGE_CAP = 20
MODULE_FINDINGS_TABLE_CAP = 100
FINDINGS_INDEX_CAP = 200
SNIPPET_CONTEXT = 3
SNIPPET_MAX_LINES = 80
SYMBOL_PAGE_SLICES_CAP = 10

# 病理码人话图例（用户产物翻译层：P 码 → vibe 开发者可读的一句话；
# 覆盖 S2 检测器全集，体检报告与引导节共用）
PATHOLOGY_LEGEND: dict[str, str] = {
    "P1": "debug residue - leftover prints, breakpoints or debug switches that should not ship",
    "P2": "near-duplicate code - almost identical blocks in different places; candidates to unify",
    "P3": "prototype debris - commented-out or abandoned experiment code",
    "P4": "dead code / dependency bloat - unused symbols, unreachable code or needless dependencies",
    "P8": "tautological tests - tests whose assertions can hardly fail",
    "P9": "layer violation - a dependency that goes against the declared layer rules",
}

# finding 来源的置信注记（诚实分级：启发式候选 ≠ 已验证死码）
FINDING_SOURCE_NOTES: dict[str, str] = {
    "s2-vulture": "heuristic candidates - verify before deleting",
    "s2-deadcode": "usage-verified dead code",
    "s2-astgrep": "structural pattern match",
    "s2-jscpd": "token-level structural similarity",
    "etl": "derived from the symbol/call graph",
}

_MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"

_ARCHIFY_MJS_REL = ("archify", "bin", "archify.mjs")
_WORK_SANDBOX = ("work", "archify") + _ARCHIFY_MJS_REL


def _connect_ro(repo: Path) -> sqlite3.Connection:
    db = Path(repo) / ".vrp" / "facts.db"
    if not db.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {db}")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    """旧库（pre-R2'）无 slices/structure/decisions：render 只读降级，不 migrate。"""
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _dumps_canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------- IR 层


def _fid_to_mod_map(payload: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for mod in payload.get("modules", []):
        for fid in mod.get("symbol_fids", []):
            out[fid] = _arch_id(mod.get("module_id", ""))
    return out


def build_architecture_ir(repo: Path, con: sqlite3.Connection) -> dict:
    """L0 全局架构 IR：structure active 优先；缺失/不可用时目录聚合兜底（§5.3）。"""
    if not _has_table(con, "structure"):
        return _architecture_ir_dir_fallback(repo, con)
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            payload = None
        if payload is not None and payload.get("modules"):
            return _architecture_ir_from_structure(con, payload)
    return _architecture_ir_dir_fallback(repo, con)


def _grid_pos(index: int, per_row: int = 4, x0: int = 40, y0: int = 40, dx: int = 240, dy: int = 140) -> list[int]:
    """archify 渲染阶段强制要求显式 pos；网格布局（pos[0] 被渲染器直接读取）。"""
    return [x0 + (index % per_row) * dx, y0 + (index // per_row) * dy]


def _clean_component(comp: dict) -> dict:
    """去 None/空 sublabel（archify validate 拒空值）。"""
    out = {"id": comp["id"], "type": comp.get("type", "backend"), "label": comp.get("label", comp["id"])}
    if comp.get("sublabel"):
        out["sublabel"] = comp["sublabel"]
    return out


def _arch_id(raw: str) -> str:
    """archify id 硬约束 ^[a-zA-Z][a-zA-Z0-9_-]*$（纯 ASCII，schema38）：非法字符转 '-'，
    首字符非字母加前缀。str.isalnum 放过 Unicode 字母——中文目录/模块名（如 01_动效库）
    曾生成含中文 id 被 validate 拒绝致 L0 降级（R-H #79），故须 isascii() 守卫。
    """
    cleaned = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-" for ch in str(raw))
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "n" + cleaned
    return cleaned


def _fit_label(raw: str, max_chars: int = 24) -> str:
    """archify label 宽度检查（~7px/字符 vs 组件宽）：超宽截断，全文保留在 sublabel。"""
    text = str(raw)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _module_edges(con: sqlite3.Connection, fid_mod: dict[str, str]) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    fids = list(fid_mod)
    for i in range(0, len(fids), 500):
        chunk = fids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for src, dst in con.execute(
            f"SELECT src_fid, dst_fid FROM edges WHERE src_fid IN ({marks}) AND dst_fid IN ({marks})",
            (*chunk, *chunk),
        ):
            a, b = fid_mod.get(src), fid_mod.get(dst)
            if a and b and a != b:
                counts[(a, b)] = counts.get((a, b), 0) + 1
    return [
        {"id": f"e{i}", "from": a, "to": b, "label": str(n)}
        for i, ((a, b), n) in enumerate(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:60])
    ]


def _layout_components(components: list[dict]) -> list[dict]:
    """给每个 component 加 pos/size（archify 渲染必需，TypeError (reading '0')）。"""
    laid = []
    for i, comp in enumerate(components):
        c = dict(comp)
        c["pos"] = _grid_pos(i)
        c["size"] = [200, 64]
        laid.append(c)
    return laid


def _architecture_ir_from_structure(con: sqlite3.Connection, payload: dict) -> dict:
    layers = [layer for layer in payload.get("layers", []) if isinstance(layer, dict) and layer.get("name")]
    modules = [m for m in payload.get("modules", []) if isinstance(m, dict) and m.get("module_id")]
    layer_order = {layer["name"]: i for i, layer in enumerate(layers)}
    ordered = sorted(modules, key=lambda m: (layer_order.get(m.get("layer", ""), len(layers)), m["module_id"]))
    components = _layout_components([
        _clean_component({
            "id": _arch_id(mod["module_id"]),
            "type": "backend",
            "label": _fit_label(mod.get("name") or mod["module_id"]),
            "sublabel": _fit_label(mod.get("responsibility") or "", 28),
        })
        for mod in ordered
    ])
    boundaries = []
    for layer in layers:
        members = [_arch_id(m["module_id"]) for m in payload.get("modules", [])
                   if isinstance(m, dict) and m.get("layer") == layer["name"] and m.get("module_id")]
        if members:
            boundaries.append({"kind": "region", "label": layer["name"], "wraps": members})
    fid_mod = _fid_to_mod_map(payload)
    connections = _module_edges(con, fid_mod)
    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {"title": "L0 structure (curated)", "output": "architecture.html"},
        "components": components,
        "boundaries": boundaries,
        "connections": connections,
    }


def _architecture_ir_dir_fallback(repo: Path, con: sqlite3.Connection) -> dict:
    sym_by_file: dict[str, int] = {}
    for r in con.execute(
        "SELECT file_path, COUNT(*) AS n FROM symbols"
        " WHERE file_path != '' AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'"
        " GROUP BY file_path"
    ):
        sym_by_file[r["file_path"].replace("\\", "/")] = r["n"]
    dirs: dict[str, dict] = {}
    for fpath, n in sym_by_file.items():
        d = fpath.rsplit("/", 1)[0] if "/" in fpath else "."
        slot = dirs.setdefault(d, {"symbols": 0, "files": 0})
        slot["symbols"] += n
        slot["files"] += 1
    top = sorted(dirs.items(), key=lambda kv: (-kv[1]["symbols"], kv[0]))[:30]
    components = _layout_components([
        {"id": _arch_id(d), "type": "backend", "label": _fit_label(d or "(root)"),
         "sublabel": f"{v['files']} files / {v['symbols']} symbols"}
        for d, v in top
    ])
    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {"title": "L0 directory aggregation (fallback)", "output": "architecture.html"},
        "components": components,
        "boundaries": [],
        "connections": [],
    }


def build_sequence_ir(repo: Path, con: sqlite3.Connection, slice_id: str) -> dict:
    """业务线时序 IR：steps→participants、相邻步→messages（y 等距）、acts→segments。

    坏 payload_json → SystemExit（调用方 render_report 跳过该 slice，不崩整报告）。
    """
    if not _has_table(con, "slices"):
        raise SystemExit(f"render_slice: slices table missing (pre-R2' facts.db): {slice_id}")
    row = con.execute(
        "SELECT name, payload_json FROM slices WHERE slice_id = ? AND status = 'active'", (slice_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"render_slice: unknown or inactive slice_id: {slice_id}")
    try:
        payload = json.loads(row[1])
    except json.JSONDecodeError as e:
        raise SystemExit(f"render_slice: corrupt payload for slice_id: {slice_id} ({e})")
    steps = payload.get("steps", [])
    participants = []
    seen: set[str] = set()
    for i, step in enumerate(steps):
        fid = step.get("fid", "")
        sym = con.execute(
            "SELECT qualified_name, name, file_path FROM symbols WHERE fid = ?", (fid,)
        ).fetchone()
        label = sym["name"] if sym is not None else f"step-{i}"
        pid = _arch_id(sym["name"] if sym is not None else f"step_{i}")
        if pid in seen:
            pid = f"{pid}_{i}"
        seen.add(pid)
        entry = {"id": pid, "type": "backend", "label": label}
        if sym is not None and sym["file_path"]:
            entry["sublabel"] = sym["file_path"]
        elif sym is None:
            entry["sublabel"] = "symbol drifted"
        participants.append(entry)
    messages = []
    for i in range(len(steps) - 1):
        messages.append({
            "id": f"m{i}",
            "from": participants[i]["id"],
            "to": participants[i + 1]["id"],
            "y": 160 + i * 50,
            "label": (steps[i + 1].get("note") or steps[i + 1].get("role") or "call"),
        })
    segments = []
    for act in payload.get("acts", []):
        lo = max(0, act.get("from_step", 0))
        hi = min(len(steps) - 1, act.get("to_step", len(steps) - 1))
        if lo <= hi:
            segments.append({"from": 160 + lo * 50 - 20, "to": 160 + hi * 50 + 20, "label": act.get("name", "")})
    return {
        "schema_version": 1,
        "diagram_type": "sequence",
        "meta": {"title": f"{row[0]} ({slice_id})", "output": f"slice-{slice_id}.html"},
        "participants": participants,
        "segments": segments,
        "messages": messages,
    }


_KIND_BY_STATUS = {
    "committed": "recovery",
    "needs_confirmation": "alert",
    "rolled_back": "alert",
    "aborted": "alert",
}


def build_timeline_ir(verdicts: list[dict]) -> dict | None:
    """verdict 时间线 IR：事件按 load_verdicts 顺序（verdict_at 升序）呈现；
    kind 由状态映射（alert/recovery/change）。

    无 verdict_at 的占位（load_verdicts 对损坏 verdict 文件的 "unreadable"
    条目 verdict_at=""）不进时间线：timeline 事件必须是时间事实，损坏占位
    不是——at 空串违反 archify timeline schema 的 ISO 8601 pattern（必须带
    Z 或 ±HH:MM 偏移），一个损坏文件曾使整段 timeline validate 失败、
    html/timeline.html 降级消失（timeline 无 mermaid 兜底）。诚实面不动：
    CLI verdicts 清单与 session_report 表格仍完整展示 unreadable 占位。
    全部无时间戳时返回 None（空 events 被 archify minItems 拒，不出 timeline）。

    事件 id 必须过 _arch_id 清洗：common.schema.json $defs/id 约束
    ^[a-zA-Z][a-zA-Z0-9_-]*$（字母开头纯 ASCII），而 verdict tid 是任意
    字符串（MCP 层 TID_SAFE 只清洗文件名，id 字段不变形）——数字开头/
    含点/非 ASCII tid 直出会使 archify validate 拒、timeline 整段降级
    （与上方 at 字段同链换字段复发）。title 保留原始 tid 供人类辨认；
    清洗碰撞（两 tid 同清洗值）可接受——schema 无 uniqueness 约束。
    """
    events = []
    for v in verdicts[:VERDICTS_CAP]:
        at = v.get("verdict_at") or ""
        if not at:
            continue
        events.append({
            "id": _arch_id(v.get("tid", "unknown")),
            "at": at,
            "title": f"{v.get('tid', '?')} [{v.get('type', '?')}] {v.get('status', '?')}",
            "lane": "verdicts",
            "kind": _KIND_BY_STATUS.get(v.get("status", ""), "change"),
        })
    if not events:
        return None
    return {
        "schema_version": 1,
        "diagram_type": "timeline",
        "meta": {
            "title": "Transaction verdicts",
            "subtitle": "S6 verdict timeline from .vrp/verdicts",
            "evidence": "observed",
            "output": "timeline.html",
        },
        "lanes": [{"id": "verdicts", "label": "Verdicts"}],
        "events": events,
    }


def build_sarif(repo: Path, con: sqlite3.Connection, tool_version: str) -> dict:
    """findings → SARIF 2.1.0（手写 JSON，行级诊断；L3 明细层，cap SARIF_FINDINGS_CAP；
    超限截断并在 runs[0].properties 标注 findings 总数，SARIF propertyBag 标准字段）。
    """
    total = con.execute(
        "SELECT COUNT(*) FROM findings WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchone()[0]
    results = []
    for r in con.execute(
        "SELECT pathology, kind, source, file_path, start_line, payload_json FROM findings"
        " WHERE file_path IS NOT NULL AND file_path != '' ORDER BY pathology, file_path, start_line"
        " LIMIT ?",
        (SARIF_FINDINGS_CAP,),
    ):
        results.append({
            "ruleId": r["pathology"] or "unknown",
            "level": "warning",
            "message": {"text": f"{r['pathology']} {r['kind'] or ''} (source: {r['source']})".strip()},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": r["file_path"].replace("\\", "/")},
                    "region": {"startLine": max(1, r["start_line"] or 1)},
                }
            }],
        })
    run: dict = {
        "tool": {"driver": {"name": "vrp", "version": tool_version, "informationUri": "https://github.com/AH23333/VRP"}},
        "results": results,
    }
    if total > SARIF_FINDINGS_CAP:
        run["properties"] = {"findings_total": total, "results_cap": SARIF_FINDINGS_CAP}
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [run],
    }


# ------------------------------------------------------------ Mermaid 降级链


def mermaid_architecture(ir: dict) -> str:
    lines = ["graph TD"]
    for comp in ir.get("components", []):
        cid = _mmd_id(comp["id"])
        sub = comp.get("sublabel") or ""
        label = comp.get("label", comp["id"]).replace('"', "'")
        if sub:
            lines.append(f'    {cid}["{label}<br/>{sub.replace(chr(34), chr(39))}"]')
        else:
            lines.append(f'    {cid}["{label}"]')
    for conn in ir.get("connections", []):
        label = (conn.get("label") or "").replace('"', "'")
        arrow = f' -->|"{label}"| ' if label else " --> "
        lines.append(f"    {_mmd_id(conn['from'])}{arrow}{_mmd_id(conn['to'])}")
    for b in ir.get("boundaries", []):
        wraps = " & ".join(_mmd_id(w) for w in b.get("wraps", []))
        if wraps:
            lines.append(f'    subgraph {b.get("label", "group").replace(" ", "_")}')
            lines.append(f"        {wraps}")
            lines.append("    end")
    return "\n".join(lines) + "\n"


def mermaid_sequence(ir: dict) -> str:
    lines = ["sequenceDiagram"]
    for p in ir.get("participants", []):
        lines.append(f'    participant {_mmd_id(p["id"])} as {p.get("label", p["id"]).replace(chr(34), chr(39))}')
    parts = ir.get("participants") or []
    segments = ir.get("segments") or []
    if segments and parts:
        for seg in segments:
            lo = max(0, min(len(parts) - 1, round((seg.get("from", 140) - 140) / 50)))
            hi = max(0, min(len(parts) - 1, round((seg.get("to", 180) - 180) / 50)))
            lines.append("    rect rgb(235, 235, 245)")
            lines.append(
                f'    Note over {_mmd_id(parts[lo]["id"])},{_mmd_id(parts[hi]["id"])}: '
                f'{(seg.get("label") or "").replace(chr(34), chr(39))}'
            )
            lines.append("    end")
    for m in ir.get("messages", []):
        label = (m.get("label") or "").replace("\n", " ").replace('"', "'")
        arrow = "->>" if m.get("variant") != "return" else "-->>"
        lines.append(f'    {_mmd_id(m["from"])}{arrow}{_mmd_id(m["to"])}: {label}')
    return "\n".join(lines) + "\n"


def _mmd_id(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(raw))
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = "n_" + cleaned
    return cleaned


# ------------------------------------------------------------- archify 接入


def find_archify_mjs() -> Path | None:
    """archify.mjs 发现：VRP_ARCHIFY_MJS → vrp vendor 收编 → vrp 仓 work 沙箱。"""
    env = os.environ.get("VRP_ARCHIFY_MJS")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    vendored = Path(__file__).resolve().parent / "vendor" / "archify" / "bin" / "archify.mjs"
    if vendored.is_file():
        return vendored
    sandbox = Path(__file__).resolve().parent.parent.joinpath(*_WORK_SANDBOX)
    if sandbox.is_file():
        return sandbox
    return None


def _run_archify(mjs: Path, args: list[str], timeout: int = ARCHIFY_TIMEOUT) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["node", str(mjs), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _archify_error_text(stdout: str, stderr: str, limit: int = 120) -> str:
    """archify validate --json 的错误在 stdout JSON 的 error 字段（stderr 恒空——
    R7' tsa 链 degraded 空括号病根）；纯文本模式在 stderr。两通道都兜。"""
    out = (stdout or "").strip()
    if out:
        try:
            doc = json.loads(out)
            err = doc.get("error")
            if err:
                return str(err).strip()[:limit]
        except json.JSONDecodeError:
            pass
        return out[:limit]
    return (stderr or "").strip()[:limit]


# --------------------------- R7'：L1 模块页 / L2 符号页（静态多页互链，主链优先）

_PATHOLOGY_COLORS = {
    "P1": "#b3261e",
    "P2": "#e8710a",
    "P3": "#616161",
    "P4": "#7b1fa2",
    "P5": "#00695c",
    "P6": "#4e342e",
    "P7": "#37474f",
    "P8": "#1565c0",
    "P9": "#ad1457",
}


def _in_chunks(items: list, chunk: int = FID_CHUNK):
    """fid 列表分块（IN 子句防超长；大仓 interface 模块 ~30k fids → 60 块）。"""
    for i in range(0, len(items), chunk):
        yield items[i:i + chunk]


def _iter_modules(con: sqlite3.Connection) -> list[dict]:
    """L1 页模块来源：structure active 优先（层序 + module_id 排序，与 L0 IR 同源）；
    缺失/无 active/空 modules 走目录聚合兜底。
    统一形状：{module_id, name, layer, responsibility, fids, source}。"""
    if _has_table(con, "structure"):
        row = con.execute(
            "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            try:
                payload = json.loads(row[0])
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                layers = [l.get("name") for l in payload.get("layers", [])
                          if isinstance(l, dict) and l.get("name")]
                order = {name: i for i, name in enumerate(layers)}
                mods = []
                for m in payload.get("modules", []):
                    if not isinstance(m, dict) or not m.get("module_id"):
                        continue
                    mods.append({
                        "module_id": str(m["module_id"]),
                        "name": str(m.get("name") or m["module_id"]),
                        "layer": str(m.get("layer") or ""),
                        "responsibility": str(m.get("responsibility") or ""),
                        "fids": [f for f in m.get("symbol_fids", []) if isinstance(f, str)],
                        "source": "structure",
                    })
                if mods:
                    mods.sort(key=lambda m: (order.get(m["layer"], len(order)), m["module_id"]))
                    return mods
    return _dir_modules(con)


def _dir_modules(con: sqlite3.Connection) -> list[dict]:
    """目录聚合兜底：符号文件按目录聚合，符号数 top MODULE_PAGES_CAP。"""
    by_dir: dict[str, list[str]] = {}
    for fid, fp in con.execute(
        "SELECT fid, file_path FROM symbols"
        " WHERE file_path != '' AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'"
    ):
        norm = fp.replace("\\", "/")
        d = norm.rsplit("/", 1)[0] if "/" in norm else "."
        by_dir.setdefault(d, []).append(fid)
    mods = []
    for d in sorted(by_dir, key=lambda k: (-len(by_dir[k]), k))[:MODULE_PAGES_CAP]:
        mods.append({
            "module_id": d,
            "name": d,
            "layer": "",
            "responsibility": f"Directory fallback: {len(by_dir[d])} symbols",
            "fids": sorted(by_dir[d]),
            "source": "dirs",
        })
    return mods


def _rows_by_fid(con: sqlite3.Connection, fids: list[str]) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for chunk in _in_chunks(list(fids)):
        marks = ",".join("?" * len(chunk))
        for row in con.execute(
            "SELECT fid, qualified_name, name, file_path, start_line, end_line FROM symbols"
            f" WHERE fid IN ({marks})",
            chunk,
        ):
            out[row["fid"]] = row
    return out


def _degrees(con: sqlite3.Connection, fids: list[str]) -> dict[str, int]:
    deg: dict[str, int] = {}
    for chunk in _in_chunks(list(fids)):
        marks = ",".join("?" * len(chunk))
        for (fid,) in con.execute(f"SELECT src_fid FROM edges WHERE src_fid IN ({marks})", chunk):
            deg[fid] = deg.get(fid, 0) + 1
        for (fid,) in con.execute(f"SELECT dst_fid FROM edges WHERE dst_fid IN ({marks})", chunk):
            deg[fid] = deg.get(fid, 0) + 1
    return deg


def _findings_by_anchor(con: sqlite3.Connection, fids: list[str]) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list[sqlite3.Row]] = {}
    for chunk in _in_chunks(list(fids)):
        marks = ",".join("?" * len(chunk))
        for row in con.execute(
            "SELECT pathology, kind, source, anchor_fid, file_path, start_line FROM findings"
            f" WHERE anchor_fid IN ({marks})"
            " ORDER BY pathology, kind, source, file_path, start_line, id",
            chunk,
        ):
            out.setdefault(row["anchor_fid"], []).append(row)
    return out


def _findings_by_files(con: sqlite3.Connection, files: list[str]) -> tuple[int, list[sqlite3.Row]]:
    """模块文件命中的 findings：总量 + 截断页（两查防大仓全量载入内存）。"""
    total = 0
    rows: list[sqlite3.Row] = []
    for chunk in _in_chunks(list(files)):
        marks = ",".join("?" * len(chunk))
        total += con.execute(
            f"SELECT COUNT(*) FROM findings WHERE file_path IN ({marks})", chunk
        ).fetchone()[0]
        if len(rows) >= MODULE_FINDINGS_TABLE_CAP:
            continue
        for row in con.execute(
            "SELECT pathology, kind, source, anchor_fid, file_path, start_line FROM findings"
            f" WHERE file_path IN ({marks})"
            " ORDER BY pathology, kind, source, file_path, start_line, id LIMIT ?",
            (*chunk, MODULE_FINDINGS_TABLE_CAP - len(rows)),
        ):
            rows.append(row)
    return total, rows


def _module_page_data(con: sqlite3.Connection, index: int, mod: dict) -> dict:
    """L1 模块页数据（连接存活期收完；行存 sqlite3.Row——关连接后仍可读，
    大仓 66k 行不转 dict 防内存膨胀）。"""
    fids = [f for f in mod.get("fids", []) if isinstance(f, str)]
    rows = _rows_by_fid(con, fids)
    deg = _degrees(con, fids)
    anchored = _findings_by_anchor(con, fids)
    files = sorted({r["file_path"] for r in rows.values() if r["file_path"]})
    findings_total, findings_rows = _findings_by_files(con, files)
    ordered = sorted(fids, key=lambda f: (-deg.get(f, 0), f))
    graph_fids = ordered[:MODULE_GRAPH_NODES_CAP]
    graph_edges: list[tuple[str, str]] = []
    for chunk in _in_chunks(graph_fids):
        marks = ",".join("?" * len(chunk))
        for src, dst in con.execute(
            f"SELECT src_fid, dst_fid FROM edges WHERE src_fid IN ({marks}) AND dst_fid IN ({marks})",
            (*chunk, *chunk),
        ):
            graph_edges.append((src, dst))
    return {
        "index": index,
        "module": mod,
        "page_file": f"m{index}-{_arch_id(mod['module_id'])}.html",
        "rows": rows,
        "degrees": deg,
        "top_table": ordered[:MODULE_SYMBOLS_TABLE_CAP],
        "graph_fids": graph_fids,
        "graph_edges": sorted(set(graph_edges)),
        "files": files,
        "findings_total": findings_total,
        "findings_rows": findings_rows,
        "anchored": anchored,
        "drifted": [f for f in fids if f not in rows],
    }


def _select_symbol_pages(module_datas: list[dict]) -> tuple[dict[str, str], bool]:
    """符号页选择：每模块度 top MODULE_TOP_SYMBOL_PAGES ∪ 锚定 findings fids（fid 字典序），
    全局 cap SYMBOL_PAGES_GLOBAL_CAP；返回 (fid→模块页文件, 是否截断)。"""
    selected: dict[str, str] = {}
    truncated = False
    for md in module_datas:
        cands = set(md["top_table"][:MODULE_TOP_SYMBOL_PAGES]) | set(md["anchored"])
        for fid in sorted(cands):
            if fid in selected:
                continue
            if len(selected) >= SYMBOL_PAGES_GLOBAL_CAP:
                truncated = True
                break
            selected[fid] = md["page_file"]
    return selected, truncated


def _dedupe_by_fid(rows: list) -> list:
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["fid"] in seen:
            continue
        seen.add(r["fid"])
        out.append(r)
    return out


def _slices_of_fid(con: sqlite3.Connection, fid: str) -> list[dict]:
    """包含该符号的 active slices：LIKE 粗筛 + 步骤精确核对，cap SYMBOL_PAGE_SLICES_CAP。"""
    if not _has_table(con, "slices"):
        return []
    out: list[dict] = []
    for slice_id, name, payload_json in con.execute(
        "SELECT slice_id, name, payload_json FROM slices"
        " WHERE status = 'active' AND payload_json LIKE ? ORDER BY slice_id",
        (f"%{fid}%",),
    ):
        if len(out) >= SYMBOL_PAGE_SLICES_CAP:
            break
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        steps = payload.get("steps", []) if isinstance(payload, dict) else []
        if any(isinstance(s, dict) and s.get("fid") == fid for s in steps):
            out.append({"slice_id": slice_id, "name": name})
    return out


def _symbol_page_data(repo: Path, con: sqlite3.Connection, fid: str, module_page: str) -> dict:
    """L2 符号页数据：行/邻域/slices/findings/源码片段（连接存活期收完）。"""
    row = con.execute(
        "SELECT fid, qualified_name, name, file_path, start_line, end_line FROM symbols WHERE fid = ?",
        (fid,),
    ).fetchone()
    outs_all = con.execute(
        "SELECT e.dst_fid AS fid, e.type AS etype, s.qualified_name AS qualified_name, s.name AS name"
        " FROM edges e LEFT JOIN symbols s ON s.fid = e.dst_fid WHERE e.src_fid = ?"
        " ORDER BY e.dst_fid, e.type",
        (fid,),
    ).fetchall()
    ins_all = con.execute(
        "SELECT e.src_fid AS fid, e.type AS etype, s.qualified_name AS qualified_name, s.name AS name"
        " FROM edges e LEFT JOIN symbols s ON s.fid = e.src_fid WHERE e.dst_fid = ?"
        " ORDER BY e.src_fid, e.type",
        (fid,),
    ).fetchall()
    outs = _dedupe_by_fid(outs_all)
    ins = _dedupe_by_fid(ins_all)
    return {
        "fid": fid,
        "row": row,
        "module_page": module_page,
        "outs": outs[:SYMBOL_NEIGHBORS_PAGE_CAP],
        "outs_total": len(outs),
        "ins": ins[:SYMBOL_NEIGHBORS_PAGE_CAP],
        "ins_total": len(ins),
        "slices": _slices_of_fid(con, fid),
        "findings": con.execute(
            "SELECT pathology, kind, source, file_path, start_line FROM findings WHERE anchor_fid = ?"
            " ORDER BY pathology, kind, source, file_path, start_line, id",
            (fid,),
        ).fetchall(),
        "snippet": (
            _code_snippet(repo, row["file_path"], row["start_line"] or 1, row["end_line"] or row["start_line"] or 1)
            if row is not None and row["file_path"] else None
        ),
    }


def _code_snippet(repo: Path, file_path: str, start: int, end: int) -> dict | None:
    """符号源码片段：[start,end] ± SNIPPET_CONTEXT 行，cap SNIPPET_MAX_LINES；
    truncated = hi < want_hi（同时覆盖 cap 与 EOF 截断）。
    路径守卫（normrel 战果 #59/#60 同族）：绝对/含 ':'/'..'/尖括号 synthetic 全拒。"""
    fp = str(file_path or "").replace("\\", "/")
    if not fp or ":" in fp or ".." in fp or fp.startswith(("/", "<", ">")) or os.path.isabs(fp):
        return None
    try:
        text = (repo / fp).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    s = int(start or 1)
    e = max(s, int(end or s))
    lo = max(1, s - SNIPPET_CONTEXT)
    want_hi = e + SNIPPET_CONTEXT
    hi = min(len(lines), want_hi, lo + SNIPPET_MAX_LINES - 1)
    if lo > len(lines):
        return None
    return {"text": "\n".join(lines[lo - 1:hi]), "lo": lo, "hi": hi, "truncated": hi < want_hi}


def mermaid_module_graph(data: dict) -> str:
    """L1 模块内符号图（内嵌 Mermaid，零子进程）：度 top 节点 + 其间边，
    病理 classDef 着色（archify 保持 L0 专用——性能：N 页×子进程不可行）。"""
    rows = data["rows"]
    anchored = data["anchored"]
    id_of = {f: f"s{i}" for i, f in enumerate(data["graph_fids"])}
    lines = ["graph LR"]
    for code, color in sorted(_PATHOLOGY_COLORS.items()):
        lines.append(f"    classDef {code.lower()} fill:#fdf0f0,stroke:{color},stroke-width:2px")
    for f in data["graph_fids"]:
        row = rows.get(f)
        label = str(row["name"]) if row is not None else f"drifted {f[:8]}"
        lines.append(f'    {id_of[f]}["{label.replace(chr(34), chr(39))}"]')
    for src, dst in data["graph_edges"]:
        lines.append(f"    {id_of[src]} --> {id_of[dst]}")
    for f in data["graph_fids"]:
        found = anchored.get(f)
        if found and found[0]["pathology"] in _PATHOLOGY_COLORS:
            lines.append(f"    class {id_of[f]} {found[0]['pathology'].lower()}")
    return "\n".join(lines) + "\n"


def mermaid_symbol_graph(data: dict) -> str:
    """L2 符号邻域图：焦点 s0 + 出入邻居；焦点病理着色。"""
    row = data["row"]
    focal = str(row["name"]) if row is not None else f"drifted {data['fid'][:8]}"
    id_of: dict[str, str] = {}
    lines = ["graph LR"]
    for code, color in sorted(_PATHOLOGY_COLORS.items()):
        lines.append(f"    classDef {code.lower()} fill:#fdf0f0,stroke:{color},stroke-width:2px")
    lines.append(f'    s0["{focal.replace(chr(34), chr(39))}"]')
    i = 1
    for r in list(data["ins"]) + list(data["outs"]):
        nfid = r["fid"]
        if nfid not in id_of:
            id_of[nfid] = f"s{i}"
            name = str(r["name"]) if r["name"] else f"drifted {nfid[:8]}"
            lines.append(f'    {id_of[nfid]}["{name.replace(chr(34), chr(39))}"]')
            i += 1
    for r in data["ins"]:
        lines.append(f"    {id_of[r['fid']]} --> s0")
    for r in data["outs"]:
        lines.append(f"    s0 --> {id_of[r['fid']]}")
    found = data["findings"]
    if found and found[0]["pathology"] in _PATHOLOGY_COLORS:
        lines.append(f"    class s0 {found[0]['pathology'].lower()}")
    return "\n".join(lines) + "\n"


def _page_shell(title: str, crumb: str, body: str) -> str:
    """L1/L2 共用页面壳（面包屑导航 + Mermaid CDN，样式与 report.html 同族；
    crumb 为预构建 HTML（含互链），title 转义）。"""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html_mod.escape(title)}</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}",
        "h1,h2{border-bottom:2px solid #ddd;padding-bottom:.3rem}",
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}",
        ".meta{color:#666;font-size:.9rem}",
        ".crumb{font-size:.9rem;margin:0 0 1rem}",
        "pre.mmd,pre.code{background:#f6f6f6;padding:1rem;overflow-x:auto;border-radius:6px;font-size:.85rem}",
        ".badge{display:inline-block;padding:0 .4rem;border-radius:4px;font-size:.8rem;font-weight:600}",
        "</style>",
        f'<script src="{_MERMAID_CDN}"></script>',
        "<script>if(window.mermaid){mermaid.initialize({startOnLoad:true,securityLevel:'loose'});}</script>",
        "</head>",
        "<body>",
        f'<p class="crumb">{crumb}</p>',
        f"<h1>{html_mod.escape(title)}</h1>",
        body,
        "</body></html>",
    ]
    return "\n".join(parts) + "\n"


def _pathology_badge(code: str) -> str:
    color = _PATHOLOGY_COLORS.get(code, "#616161")
    return (
        f'<span class="badge" style="background:{color}1a;color:{color}">'
        f"{html_mod.escape(code)}</span>"
    )


def _module_page_html(repo_name: str, data: dict, selected: dict[str, str]) -> str:
    """L1 模块页：meta + 模块内符号图 + 符号表（度 top）+ findings 表；
    出链 ../report.html、../symbols/{fid}.html。"""
    esc = html_mod.escape
    m = data["module"]
    src_label = "structure (curated)" if m["source"] == "structure" else "directory fallback"
    crumb = f'<a href="../report.html">report</a> / modules / {esc(m["module_id"])}'
    meta = [f"Source: {src_label}"]
    if m["layer"]:
        meta.append(f"Layer: {esc(m['layer'])}")
    meta.append(f"Symbols: {len(m['fids'])}")
    meta.append(f"findings (by file): {data['findings_total']}")
    parts = [f'<p class="meta">{" | ".join(meta)}</p>']
    if m["responsibility"]:
        parts.append(f"<p>{esc(m['responsibility'])}</p>")
    if data["drifted"]:
        parts.append(f'<p class="meta">{len(data["drifted"])} fid(s) may have drifted (not in symbols table).</p>')
    parts.append("<h2>Module symbol graph</h2>")
    if data["graph_fids"]:
        if len(m["fids"]) > MODULE_GRAPH_NODES_CAP:
            parts.append(
                f'<p class="meta">Showing top {len(data["graph_fids"])} of {len(m["fids"])} symbols by degree (truncated).</p>'
            )
        parts.append(f'<pre class="mermaid">\n{html_mod.escape(mermaid_module_graph(data))}</pre>')
    else:
        parts.append('<p class="meta">No graphable symbols in this module.</p>')
    parts.append("<h2>Symbols</h2>")
    if data["top_table"]:
        if len(m["fids"]) > MODULE_SYMBOLS_TABLE_CAP:
            parts.append(
                f'<p class="meta">Showing {len(data["top_table"])} of {len(m["fids"])} symbols (truncated by degree).</p>'
            )
        rows_html = []
        for f in data["top_table"]:
            row = data["rows"].get(f)
            deg = data["degrees"].get(f, 0)
            if row is not None:
                name_html = (
                    f'<a href="../symbols/{esc(f)}.html">{esc(row["name"])}</a>'
                    if f in selected else esc(row["name"])
                )
                qn = esc(row["qualified_name"])
                loc = f'{esc(row["file_path"])}:{row["start_line"] or "?"}'
            else:
                name_html = esc(f"drifted {f[:12]}…")
                qn = "—"
                loc = "—"
            badges = "".join(_pathology_badge(r["pathology"]) for r in data["anchored"].get(f, []))
            rows_html.append(f"<tr><td>{name_html}{badges}</td><td>{qn}</td><td>{loc}</td><td>{deg}</td></tr>")
        parts.append(
            "<table><tr><th>Symbol</th><th>qualified_name</th><th>Location</th><th>Degree</th></tr>"
            + "".join(rows_html) + "</table>"
        )
    else:
        parts.append('<p class="meta">No symbols in this module.</p>')
    parts.append("<h2>findings</h2>")
    if data["findings_rows"]:
        if data["findings_total"] > len(data["findings_rows"]):
            parts.append(
                f'<p class="meta">Showing {len(data["findings_rows"])} of {data["findings_total"]} findings (truncated).</p>'
            )
        frows = []
        for r in data["findings_rows"]:
            fp = str(r["file_path"]).replace("\\", "/")
            loc = f"{esc(fp)}:{r['start_line'] or '?'}"
            anchor = ""
            if r["anchor_fid"] and r["anchor_fid"] in selected:
                anchor = f' → <a href="../symbols/{esc(r["anchor_fid"])}.html">symbol page</a>'
            frows.append(
                f'<tr><td>{_pathology_badge(r["pathology"])}</td><td>{esc(r["kind"] or "")}</td>'
                f"<td>{loc}</td><td>{esc(r['source'] or '')}</td><td>{anchor}</td></tr>"
            )
        parts.append(
            "<table><tr><th>Pathology</th><th>kind</th><th>Location</th><th>source</th><th>Symbol page</th></tr>"
            + "".join(frows) + "</table>"
        )
    else:
        parts.append('<p class="meta">No findings.</p>')
    return _page_shell(f"{m['name']} - {repo_name}", crumb, "\n".join(parts))


def _symbol_page_html(repo_name: str, data: dict, selected: dict[str, str],
                      page_to_module_id: dict[str, str]) -> str:
    """L2 符号页：源码片段（行号）+ 邻域图 + 出入邻居 + slices + findings；
    出链 ../report.html、../modules/...、邻居 ../symbols/{fid}.html。"""
    esc = html_mod.escape
    row = data["row"]
    fid = data["fid"]
    title = str(row["qualified_name"]) if row is not None else f"drifted symbol {fid[:16]}…"
    mod_id = page_to_module_id.get(data["module_page"], "module")
    crumb = (
        f'<a href="../report.html">report</a> / '
        f'<a href="../modules/{esc(data["module_page"])}">{esc(mod_id)}</a> / {esc(title)}'
    )
    parts = []
    if row is not None:
        loc = f"{esc(row['file_path'])}:{row['start_line'] or '?'}"
        if row["end_line"]:
            loc = f"{loc}-{row['end_line']}"
        parts.append(f'<p class="meta">fid: <code>{esc(fid)}</code> | location: <code>{loc}</code></p>')
    else:
        parts.append(f'<p class="meta">fid: <code>{esc(fid)}</code> (drifted: not in symbols table)</p>')
    if data["snippet"] is not None:
        sn = data["snippet"]
        note = " (truncated)" if sn["truncated"] else ""
        parts.append("<h2>Source snippet</h2>")
        parts.append(f'<p class="meta">Lines {sn["lo"]}-{sn["hi"]}{note}; may be misaligned when facts are stale - the in-repo file is authoritative.</p>')
        numbered = "\n".join(
            f"{n:>5} | {html_mod.escape(line)}"
            for n, line in zip(range(sn["lo"], sn["hi"] + 1), sn["text"].split("\n"))
        )
        parts.append(f'<pre class="code">{numbered}</pre>')
    else:
        parts.append('<p class="meta">No source snippet (file missing / path rejected by guard / fid drifted).</p>')
    if data["ins"] or data["outs"]:
        parts.append("<h2>Neighborhood graph</h2>")
        parts.append(f'<pre class="mermaid">\n{html_mod.escape(mermaid_symbol_graph(data))}</pre>')
    else:
        parts.append('<p class="meta">No incoming or outgoing edges.</p>')
    if data["outs_total"] or data["ins_total"]:

        def _neighbor_rows(items: list) -> str:
            out = []
            for r in items:
                nfid = r["fid"]
                name = str(r["name"]) if r["name"] else f"drifted {nfid[:12]}…"
                name_html = (
                    f'<a href="../symbols/{esc(nfid)}.html">{esc(name)}</a>'
                    if nfid in selected else esc(name)
                )
                out.append(
                    f"<tr><td>{name_html}</td><td>{esc(r['qualified_name'] or '—')}</td>"
                    f"<td>{esc(r['etype'] or '—')}</td></tr>"
                )
            return ("<table><tr><th>Symbol</th><th>qualified_name</th><th>Edge type</th></tr>"
                    + "".join(out) + "</table>")

        parts.append("<h2>Call relations</h2>")
        if data["outs"]:
            if data["outs_total"] > len(data["outs"]):
                parts.append(
                    f'<p class="meta">Showing {len(data["outs"])} of {data["outs_total"]} out-neighbors (truncated).</p>'
                )
            parts.append("<h3>Outgoing (calls/references)</h3>")
            parts.append(_neighbor_rows(data["outs"]))
        if data["ins"]:
            if data["ins_total"] > len(data["ins"]):
                parts.append(
                    f'<p class="meta">Showing {len(data["ins"])} of {data["ins_total"]} in-neighbors (truncated).</p>'
                )
            parts.append("<h3>Incoming (called by)</h3>")
            parts.append(_neighbor_rows(data["ins"]))
    if data["slices"]:
        parts.append("<h2>Slices containing this symbol</h2>")
        if len(data["slices"]) >= SYMBOL_PAGE_SLICES_CAP:
            parts.append(f'<p class="meta">At most {SYMBOL_PAGE_SLICES_CAP} shown (may be truncated).</p>')
        items = "".join(
            f'<li><code>{esc(s["slice_id"])}</code> {esc(s["name"])}</li>' for s in data["slices"]
        )
        parts.append(f"<ul>{items}</ul>")
    if data["findings"]:
        parts.append("<h2>Pathology findings</h2>")
        frows = []
        for r in data["findings"]:
            fp = str(r["file_path"] or "").replace("\\", "/")
            loc = f"{esc(fp)}:{r['start_line'] or '?'}" if fp else "—"
            frows.append(
                f'<tr><td>{_pathology_badge(r["pathology"])}</td><td>{esc(r["kind"] or "")}</td>'
                f"<td>{loc}</td><td>{esc(r['source'] or '')}</td></tr>"
            )
        parts.append(
            "<table><tr><th>Pathology</th><th>kind</th><th>Location</th><th>source</th></tr>"
            + "".join(frows) + "</table>"
        )
    return _page_shell(f"{title} - {repo_name}", crumb, "\n".join(parts))


def _modules_index_section(datas: list[dict], modules_truncated: bool,
                           symbol_pages: int, symbols_truncated: bool) -> dict:
    esc = html_mod.escape
    if not datas:
        return {"title": "Module pages (L1)", "body": "<p>No renderable module data (no active structure and empty directory aggregation).</p>"}
    rows = []
    for d in datas:
        m = d["module"]
        src = "structure" if m["source"] == "structure" else "dir fallback"
        rows.append(
            f'<tr><td><a href="modules/{esc(d["page_file"])}">{esc(m["module_id"])}</a></td>'
            f"<td>{esc(m['layer'] or '—')}</td><td>{len(m['fids'])}</td>"
            f"<td>{d['findings_total']}</td><td>{esc(src)}</td></tr>"
        )
    notes = []
    if modules_truncated:
        notes.append(f"showing {MODULE_PAGES_CAP} module pages (more modules not rendered, truncated)")
    if symbols_truncated:
        notes.append(f"symbol pages capped at {SYMBOL_PAGES_GLOBAL_CAP} (filled in module order, truncated)")
    note_html = f'<p class="meta">{"; ".join(notes)}.</p>' if notes else ""
    body = (
        "<p>Multi-level drill-down: L0 architecture → L1 module pages → L2 symbol pages (with source snippets); "
        f"this run produced {len(datas)} module pages and {symbol_pages} symbol pages.</p>" + note_html
        + "<table><tr><th>Module</th><th>Layer</th><th>Symbols</th><th>findings</th><th>Source</th></tr>"
        + "".join(rows) + "</table>"
    )
    return {"title": "Module pages (L1)", "body": body}


def _findings_index_rows(con: sqlite3.Connection) -> tuple[int, list]:
    total = con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    rows = con.execute(
        "SELECT pathology, kind, source, anchor_fid, file_path, start_line FROM findings"
        " ORDER BY pathology, kind, source, file_path, start_line, id LIMIT ?",
        (FINDINGS_INDEX_CAP,),
    ).fetchall()
    return total, rows


def _findings_index_section(rows: list, total: int, selected: dict[str, str],
                            file_links: dict[str, str], link_label: str = "detail page") -> dict:
    esc = html_mod.escape
    if total == 0:
        return {"title": "Pathology findings index", "body": "<p>No findings.</p>"}
    items = []
    for r in rows:
        fp = str(r["file_path"] or "").replace("\\", "/")
        loc = f"<code>{esc(fp)}:{r['start_line'] or '?'}</code>" if fp else "<code>—</code>"
        link = ""
        if r["anchor_fid"] and r["anchor_fid"] in selected:
            link = f' → <a href="symbols/{esc(r["anchor_fid"])}.html">symbol page</a>'
        elif fp:
            href = file_links.get(fp)
            if href:
                link = f' → <a href="{esc(href)}">{esc(link_label)}</a>'
        items.append(
            f"<li>{_pathology_badge(r['pathology'])} {esc(r['kind'] or '')} {loc}"
            f" (source: {esc(r['source'] or '?')}) {link}</li>"
        )
    note = f'<p class="meta">Showing {len(rows)} of {total} findings (truncated).</p>' if total > len(rows) else ""
    # 全清单降权：折叠进 details（体检报告是用户主视图，裸清单供审计下钻）
    return {"title": "Pathology findings index",
            "body": note + "<details><summary>Full findings index - click to expand</summary>"
            "<ul>" + "".join(items) + "</ul></details>"}


def _chain_section(chain: dict) -> dict:
    """R7'(2) report.html「模块交互图链」节：chain/index.html + 模块入口清单。"""
    esc = html_mod.escape
    cards = chain["cards"]
    counts = chain["counts"]
    rows = "".join(
        f'<li><a href="chain/{esc(c["entry"])}">{esc(c["module_id"])}</a>'
        f" ({esc(c['kind'])}, {c['n_files']} files / {c['n_syms']} symbols / {c['n_dirs']} dirs)</li>"
        for c in cards
    )
    body = (
        '<p>Module interaction chain (archify-rendered, static multi-page links): '
        '<a href="chain/index.html">chain/index.html</a>; click a node in a diagram to select it, '
        "click again to drill down to code (the only code view = full-file source page + symbol anchors).</p>"
        f'<p class="meta">{counts["modules"]} modules / {counts["diagrams"]} diagrams / '
        f'{counts["file_pages"]} code pages / {counts["pages"]} pages total.</p><ul>{rows}</ul>'
    )
    if chain["notes"]:
        notes = "".join(f"<li>{esc(n)}</li>" for n in chain["notes"])
        body += f'<p class="meta">Notes:</p><ul style="color:#666;font-size:.9rem">{notes}</ul>'
    return {"title": "Module interaction chain", "body": body}


# ----------------------------------------------------------------- report


def _active_structure_payload(con: sqlite3.Connection) -> dict | None:
    """structure active 载荷（overview 节数据源；无/坏 → None）。"""
    if not _has_table(con, "structure"):
        return None
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _how_to_read_section() -> dict:
    """引导节：阅读路径 + P 码图例 + 术语速查（小白第一屏，用户产物定位）。"""
    esc = html_mod.escape
    legend_rows = "".join(
        f"<tr><td><b>{esc(code)}</b></td><td>{esc(desc)}</td></tr>"
        for code, desc in PATHOLOGY_LEGEND.items()
    )
    body = (
        "<p>This report is how the AI has organized and health-checked your repository. "
        "Read it in this order:</p>"
        "<ol>"
        "<li><b>Project overview</b> - your repo as the AI curated it: modules, layers, "
        "one-line responsibilities.</li>"
        "<li><b>Architecture</b> - the same structure as a diagram.</li>"
        "<li><b>Module interaction chain</b> - click into a module, follow the data flow, "
        "drill down to real code lines.</li>"
        "<li><b>Pathology checkup</b> - what the detectors found, in plain language, "
        "worst first.</li>"
        "<li><b>Refactor decisions (verdicts)</b> - the audit trail of applied/rejected "
        "refactors, including anything awaiting your confirmation.</li>"
        "</ol>"
        '<details><summary>Pathology codes (P-codes) legend</summary>'
        f"<table><tr><th>code</th><th>meaning</th></tr>{legend_rows}</table>"
        "<p class='meta'>P-codes classify what kind of problem a finding is; "
        "the checkup section below explains each one where it appears.</p></details>"
        "<details><summary>Glossary</summary><ul>"
        "<li><b>module</b> - a group of symbols the AI curated as one cohesive unit "
        "(one card in the chain index).</li>"
        "<li><b>layer</b> - a declared dependency tier; lower layers should not import "
        "higher ones.</li>"
        "<li><b>slice</b> - one business line end-to-end: a chain of steps across modules, "
        "read as a sequence diagram.</li>"
        "<li><b>verdict</b> - the receipt of one refactor transaction: what was changed, "
        "which gates passed/failed, and its current status.</li>"
        "<li><b>facts.db</b> - the single source of truth (one sqlite file); "
        "this report is a static, reproducible view of it.</li>"
        "</ul></details>"
    )
    return {"title": "How to read this report", "body": body}


def _overview_section(payload: dict | None, n_slices: int, n_verdicts: int) -> dict:
    """项目总览：AI 梳理产出主呈现（模块职责表 + 层次规则）。

    无 curated structure 时诚实降级（overview 缺席说明），不编造。
    """
    esc = html_mod.escape
    if not payload or not payload.get("modules"):
        body = (
            "<p>No curated structure yet - the overview appears once the AI curation "
            "(structure payload) is stored; until then the Architecture section falls back "
            "to directory aggregation.</p>"
        )
        return {"title": "Project overview", "body": body}
    layers = [l for l in payload.get("layers", []) if isinstance(l, dict) and l.get("name")]
    mods = [m for m in payload.get("modules", []) if isinstance(m, dict) and m.get("module_id")]
    layer_order = {l["name"]: i for i, l in enumerate(layers)}
    ordered = sorted(mods, key=lambda m: (layer_order.get(m.get("layer", ""), len(layers)),
                                          str(m["module_id"])))
    rows = "".join(
        "<tr>"
        f"<td>{esc(str(m['module_id']))}</td>"
        f"<td>{esc(str(m.get('layer') or '—'))}</td>"
        f"<td>{esc(str(m.get('responsibility') or '—'))}</td>"
        f"<td>{len([f for f in m.get('symbol_fids', []) if isinstance(f, str)])}</td>"
        "</tr>"
        for m in ordered
    )
    rules = "".join(
        f"<li><b>{esc(l['name'])}</b> may depend on: "
        + (esc(", ".join(d for d in l.get("may_depend_on", []) if isinstance(d, str)))
           or "<i>(nothing - it is a foundation layer)</i>")
        + "</li>"
        for l in layers
    )
    body = (
        f"<p>{len(mods)} modules across {len(layers)} layers"
        + (f", {n_slices} business slice(s), {n_verdicts} refactor verdict(s)" if n_slices or n_verdicts else "")
        + ". Each module is one unit the AI curated from your code; "
        "the responsibility column is the AI's one-line summary of what it does.</p>"
        f"<table><tr><th>Module</th><th>Layer</th><th>Responsibility (AI-curated)</th>"
        f"<th>Symbols</th></tr>{rows}</table>"
        + (f"<h3>Layer rules</h3><ul>{rules}</ul>" if rules else "")
        + "<p class='meta'>Open the module interaction chain to browse any module in depth.</p>"
    )
    return {"title": "Project overview", "body": body}


def _pathology_agg(con: sqlite3.Connection) -> tuple[list, dict, dict]:
    """体检聚合：per code 计数 / per code+source 细分 / per code top 文件。

    三查全 GROUP BY（SQLite 侧聚合，大仓 findings 万级行不落 Python 循环）。
    """
    per_code = con.execute(
        "SELECT pathology, COUNT(*) AS c FROM findings GROUP BY pathology ORDER BY c DESC, pathology"
    ).fetchall()
    per_source: dict[str, list] = {}
    for r in con.execute(
        "SELECT pathology, source, COUNT(*) AS c FROM findings"
        " GROUP BY pathology, source ORDER BY pathology, c DESC, source"
    ):
        per_source.setdefault(str(r["pathology"]), []).append(r)
    top_files: dict[str, list] = {}
    for r in con.execute(
        "SELECT pathology, file_path, COUNT(*) AS c FROM findings"
        " WHERE file_path != '' GROUP BY pathology, file_path"
        " ORDER BY pathology, c DESC, file_path"
    ):
        bucket = top_files.setdefault(str(r["pathology"]), [])
        if len(bucket) < 3:  # 每码 top 3 文件
            bucket.append(r)
    return per_code, per_source, top_files


def _pathology_checkup_section(per_code: list, per_source: dict, top_files: dict,
                               file_links: dict[str, str],
                               link_label: str = "file page") -> dict:
    """病理体检报告：S 阶段 findings 的用户语言翻译层。

    每码 = 人话定义 + 计数 + 来源细分（启发式来源诚实标注）+ top 文件
    （可点进代码页核对）——让 vibe 开发者看懂"项目哪里有病、有多重、
    证据在哪"，而不是 969 条 P 码裸清单。
    """
    esc = html_mod.escape
    if not per_code:
        return {"title": "Pathology checkup", "body": (
            "<p>No findings - the detectors found none of the known vibe-coding "
            "pathologies in this repository.</p>")}
    parts = [
        "<p>Counts come straight from the findings table in facts.db. Sources marked as "
        "<i>heuristic</i> produce candidates, not verdicts - always open the file and "
        "confirm before acting on them.</p>"
    ]
    for r in per_code:
        code = str(r["pathology"])
        human = PATHOLOGY_LEGEND.get(code, "unknown pathology code")
        parts.append(f"<h3>{esc(code)} - {esc(human)}</h3>")
        src_bits = []
        for sr in per_source.get(code, []):
            src = str(sr["source"] or "?")
            note = FINDING_SOURCE_NOTES.get(src)
            label = f"{esc(src)}: {sr['c']}" + (f" ({esc(note)})" if note else "")
            src_bits.append(label)
        parts.append(f"<p><b>{r['c']} finding(s)</b>"
                     + (f" - {'; '.join(src_bits)}" if src_bits else "") + "</p>")
        files = top_files.get(code, [])
        if files:
            lis = []
            for fr in files:
                fp = str(fr["file_path"]).replace("\\", "/")
                href = file_links.get(fp)
                link = (f'<a href="{esc(href)}">{esc(fp)}</a>' if href
                        else f"<code>{esc(fp)}</code>")
                lis.append(f"<li>{link}: {fr['c']} finding(s) → {esc(link_label)}</li>")
            parts.append("<ul>" + "".join(lis) + "</ul>")
    return {"title": "Pathology checkup", "body": "".join(parts)}


def _deep_data_section(deep_links: list[str], archify_html: list[str],
                       degraded_reasons: list[str]) -> dict:
    """深层数据节（AI 材料/审计面）：IR/SARIF/Datasette/降级记录归拢折叠——
    供 AI agent 与审计者下钻，不占 vibe 开发者主视觉。"""
    esc = html_mod.escape
    links = "".join(f'<li><a href="{esc(h)}">{esc(h)}</a></li>' for h in deep_links)
    arch = "".join(f"<li>{esc(h)}</li>" for h in archify_html)
    deg = "".join(f"<li>{esc(r)}</li>" for r in degraded_reasons)
    body = (
        "<details><summary>Deep data (for AI agents & auditing) - click to expand</summary>"
        "<p>The data layer is <code>.vrp/facts.db</code> (a single sqlite file, readable "
        "directly with Datasette: <code>datasette .vrp/facts.db</code>); this report is a "
        "static, reproducible view of it. Raw machine-readable artifacts:</p>"
        f"<ul>{links}</ul>"
        + (f"<p>archify render outputs: <ul>{arch}</ul></p>" if arch else "")
        + ("<p>Degradation record (honest failure, does not block rc=0):"
           f"<ul>{deg}</ul></p>" if deg else "")
        + "</details>"
    )
    return {"title": "Deep data (for AI agents & auditing)", "body": body}


def _report_html(repo: Path, ir_hash: str, git_head: str, sections: list[dict], staleness: dict | None = None) -> str:
    """静态目录页：数据层说明 + 陈旧横幅 + T4/open 清单 + 产物链接；无时间戳保字节级复现
    （陈旧横幅随渲染时仓 HEAD 变化，是复现契约的有意例外——见 _stale_banner_html）。"""
    esc = html_mod.escape
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>vrp report - {esc(repo.name)}</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}",
        "h1,h2{border-bottom:2px solid #ddd;padding-bottom:.3rem}",
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}",
        ".meta{color:#666;font-size:.9rem}",
        ".t4{background:#fde8e8}.open{background:#fff6e0}",
        "pre.mmd{background:#f6f6f6;padding:1rem;overflow-x:auto;border-radius:6px;font-size:.85rem}",
        "</style>",
        f'<script src="{_MERMAID_CDN}"></script>',
        "<script>if(window.mermaid){mermaid.initialize({startOnLoad:true,securityLevel:'loose'});}</script>",
        "</head>",
        "<body>",
        f"<h1>vrp report - {esc(repo.name)}</h1>",
        f'<p class="meta">facts.git_head: <code>{esc(git_head or "(none)")}</code>'
        f" | IR sha256: <code>{esc(ir_hash[:16])}...</code> | 100% of data truth comes from facts.db</p>",
        _stale_banner_html(staleness),
    ]
    for section in sections:
        parts.append(f"<h2>{esc(section['title'])}</h2>")
        parts.append(section["body"])
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def _verdicts_section(verdicts: list[dict]) -> dict:
    open_items = [v for v in verdicts if v.get("status") != "committed"]
    t4 = [v for v in open_items if v.get("status") == "needs_confirmation"]
    rows = []
    for v in t4:
        rows.append(
            f'<tr class="t4"><td>{html_mod.escape(v.get("tid", "?"))}</td>'
            f'<td>{html_mod.escape(str(v.get("type", "?")))}</td>'
            f'<td>{html_mod.escape(str(v.get("status", "?")))}</td>'
            f'<td>{html_mod.escape(str(v.get("failed_gate") or ""))}</td></tr>'
        )
    for v in open_items:
        if v.get("status") == "needs_confirmation":
            continue
        rows.append(
            f'<tr class="open"><td>{html_mod.escape(v.get("tid", "?"))}</td>'
            f'<td>{html_mod.escape(str(v.get("type", "?")))}</td>'
            f'<td>{html_mod.escape(str(v.get("status", "?")))}</td>'
            f'<td>{html_mod.escape(str(v.get("failed_gate") or ""))}</td></tr>'
        )
    body = (
        "<p>Every refactor attempt through the pipeline leaves a verdict: what was changed, "
        "which gates passed or failed, and whether it was committed, rolled back, or is "
        "waiting on you. T4 (improvement) verdicts require manual release via CLI "
        "<code>vrp transact --confirm</code>; "
        f"{len(open_items)} open verdicts ({len(t4)} T4 pending confirmation), automatically fed back into dossier evidence.</p>"
        + ("<table><tr><th>tid</th><th>type</th><th>status</th><th>failed_gate</th></tr>"
           + "".join(rows) + "</table>" if rows else "<p>No open verdicts.</p>")
    )
    return {"title": "Verdict list (T4 pending first)", "body": body}


def _staleness(repo: Path, facts_head: str) -> dict | None:
    """facts.git_head vs 仓当前 HEAD：展示路径的诚实新鲜度信号。

    M4 freshness 只保护 rw 消费方（transact/curate 硬拒陈旧 facts）；
    render 只读被豁免——但报告页必须诚实标旧（R6' 病根二：b5957b53 快照
    被平静打开 5 批次无人知）。不可判定（非 git 仓/git 失败）返回 None。
    """
    if not facts_head:
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if head.returncode != 0:
        return None
    current = head.stdout.strip()
    if current == facts_head:
        return None
    behind: int | None = None
    try:
        count = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", f"{facts_head}..{current}"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if count.returncode == 0:
            behind = int(count.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return {"facts_head": facts_head, "head": current, "behind": behind}


def _stale_banner_html(staleness: dict | None) -> str:
    """陈旧横幅：插在 meta 行后。随渲染时仓 HEAD 变化——新鲜度信号是
    report.html 复现契约的**有意例外**（同 HEAD 下两次渲染仍字节级全等）。"""
    if not staleness:
        return ""
    esc = html_mod.escape
    if staleness["behind"] is not None:
        detail = f"about {staleness['behind']} commit(s) behind current HEAD"
    else:
        detail = "inconsistent with current HEAD (not a simple lag; possibly diverged)"
    return (
        '<div style="background:#fde8e8;border:2px solid #b3261e;padding:.8rem 1rem;'
        'border-radius:6px;margin:1rem 0">'
        "<strong>⚠ Stale report</strong>: facts.git_head "
        f'<code>{esc(staleness["facts_head"][:12])}…</code> {detail} '
        f'(current <code>{esc(staleness["head"][:12])}…</code>) - conclusions may be outdated; '
        "re-run <code>vrp run</code> then <code>vrp report</code> to refresh.</div>"
    )


def _clean_render_out(out: Path) -> None:
    """渲染前清自有产物（R6' 病根四：上轮孤儿混入本轮快照——timeline.html
    渲了没链接即两轮渲染混目录）。只清 render 拥有的名字，外部文件不动。"""
    for sub in ("ir", "html", "modules", "symbols", "chain"):
        d = out / sub
        if d.is_dir():
            shutil.rmtree(d)
    for pattern in ("*.mmd", "findings.sarif", "report.html"):
        for f in out.glob(pattern):
            f.unlink()


def render_report(repo: Path, out_dir: Path | None = None) -> dict:
    """S8 全量报告：IR → archify/Mermaid/SARIF → .vrp/report/report.html。

    R7'(2) 分流：archify 可用且模块交互图链成功 → chain/ 替代 L1/L2
    （modules/symbols 不出，三段式原子提交见 render_chain）；
    失败/缺 archify → L1/L2 Mermaid 兜底（降级记录诚实标注）。
    """
    repo = Path(repo)
    con = _connect_ro(repo)
    out = Path(out_dir) if out_dir else repo / REPORT_DIR
    # 链在 con 存活期写入（build_chain 自开只读连接）：先清后建
    _clean_render_out(out)
    try:
        git_head = _facts_git_head(con)
        arch_ir = build_architecture_ir(repo, con)
        seq_irs = {}
        if _has_table(con, "slices"):
            slice_rows = con.execute(
                "SELECT slice_id FROM slices WHERE status = 'active' ORDER BY slice_id LIMIT ?",
                (SEQUENCE_SLICES_CAP,),
            ).fetchall()
            for r in slice_rows:
                try:
                    seq_irs[r["slice_id"]] = build_sequence_ir(repo, con, r["slice_id"])
                except SystemExit:
                    continue
        verdicts = s6_transact.load_verdicts(repo)
        timeline_ir = build_timeline_ir(verdicts) if verdicts else None
        sarif = build_sarif(repo, con, _tool_version())
        # R7'(2) 模块交互图链：成功替代 L1/L2（数据收集一并跳过）
        mjs = find_archify_mjs()
        chain: dict | None = None
        if mjs is not None:
            try:
                chain = render_chain.build_chain(repo, out / "chain", mjs)
            except Exception as exc:  # 链意外失败不阻塞报告（降级 L1/L2）
                chain = {"ok": False, "index": None, "cards": [], "file_pages": {},
                         "counts": {"modules": 0, "diagrams": 0, "file_pages": 0, "pages": 0},
                         "notes": [f"module chain crashed: {exc}"]}
        use_chain = bool(chain and chain.get("ok"))
        # R7' L1/L2：模块页/符号页数据（连接存活期收完；Row 关连接后仍可读）
        module_datas: list[dict] = []
        symbol_datas: list[dict] = []
        selected: dict[str, str] = {}
        modules_truncated = False
        symbols_truncated = False
        page_to_module_id: dict[str, str] = {}
        if not use_chain:
            modules_all = _iter_modules(con)
            modules_truncated = len(modules_all) > MODULE_PAGES_CAP
            module_datas = [_module_page_data(con, i, m) for i, m in enumerate(modules_all[:MODULE_PAGES_CAP])]
            selected, symbols_truncated = _select_symbol_pages(module_datas)
            symbol_datas = [_symbol_page_data(repo, con, fid, selected[fid]) for fid in sorted(selected)]
            page_to_module_id = {md["page_file"]: md["module"]["module_id"] for md in module_datas}
        findings_total, findings_rows = _findings_index_rows(con)
        # 用户产物翻译层原料（con 存活期收完）：AI 梳理载荷 + 病理体检聚合
        structure_payload = _active_structure_payload(con)
        path_per_code, path_per_source, path_top_files = _pathology_agg(con)
    finally:
        con.close()

    staleness = _staleness(repo, git_head)
    (out / "ir").mkdir(parents=True, exist_ok=True)
    (out / "html").mkdir(parents=True, exist_ok=True)
    if use_chain:
        file_links: dict[str, str] = {fp: f"chain/{stem}" for fp, stem in chain["file_pages"].items()}
    else:
        (out / "modules").mkdir(parents=True, exist_ok=True)
        (out / "symbols").mkdir(parents=True, exist_ok=True)
        file_links = {}
        for md in module_datas:
            for f in md["files"]:
                file_links.setdefault(f.replace("\\", "/"), f"modules/{md['page_file']}")

    irs: dict[str, dict] = {"architecture": arch_ir, **{f"slice-{k}": v for k, v in seq_irs.items()}}
    if timeline_ir is not None:  # 空 events 被 archify minItems 拒；无 verdicts 不出 timeline
        irs["timeline"] = timeline_ir
    if use_chain:
        # R7'(2) chain-index：进 ir_sha256（复现契约），不进 archify 循环（无 diagram_type）
        index_ir = {
            "schema_version": 1,
            "kind": "chain-index",
            "modules": [
                {"module_id": c["module_id"], "entry": c["entry"], "kind": c["kind"],
                 "n_files": c["n_files"], "n_syms": c["n_syms"], "n_dirs": c["n_dirs"],
                 "responsibility": c.get("responsibility", ""),
                 "layer": c.get("layer", ""),
                 "depends_on": c.get("depends_on", [])}
                for c in chain["cards"]
            ],
            "counts": chain["counts"],
            "notes": chain["notes"],
            "file_pages": sorted(chain["file_pages"]),
        }
        index_name, index_hash_key = "chain-index", "chain-index"
    else:
        # R7' modules-index：进 ir_sha256（复现契约），不进 archify 循环（无 diagram_type）
        index_ir = {
            "schema_version": 1,
            "kind": "modules-index",
            "modules": [
                {
                    "page": md["page_file"],
                    "module_id": md["module"]["module_id"],
                    "layer": md["module"]["layer"],
                    "source": md["module"]["source"],
                    "symbols": len(md["module"]["fids"]),
                    "findings": md["findings_total"],
                }
                for md in module_datas
            ],
            "symbol_pages": sorted(selected),
            "symbol_pages_truncated": symbols_truncated,
        }
        index_name, index_hash_key = "modules-index", "modules-index"
    ir_hash = hashlib.sha256(
        _dumps_canonical({**irs, index_hash_key: index_ir}).encode("utf-8")
    ).hexdigest()
    for name, ir in irs.items():
        (out / "ir" / f"{name}.json").write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "ir" / f"{index_name}.json").write_text(
        json.dumps(index_ir, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "findings.sarif").write_text(json.dumps(sarif, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts: list[str] = [f"ir/architecture.json", f"ir/{index_name}.json", "findings.sarif"]
    if use_chain:
        artifacts.append("chain/index.html")
    degraded_reasons: list[str] = []

    # Mermaid 源（零依赖，恒出）
    mmd_arch = mermaid_architecture(arch_ir)
    (out / "architecture.mmd").write_text(mmd_arch, encoding="utf-8")
    artifacts.append("architecture.mmd")
    for name, ir in seq_irs.items():
        (out / f"slice-{name}.mmd").write_text(mermaid_sequence(ir), encoding="utf-8")
        artifacts.append(f"slice-{name}.mmd")

    # archify 工具组（缺失/失败降级 Mermaid-only，不阻塞）
    archify_html: list[str] = []
    if mjs is None:
        degraded_reasons.append("archify not found (VRP_ARCHIFY_MJS / vendored / work sandbox)")
    else:
        if not use_chain and chain is not None:
            reasons = "; ".join(chain.get("notes", [])[:5])
            degraded_reasons.append(f"module chain degraded to L1/L2 pages: {reasons}")
        for name, ir in irs.items():
            ir_path = out / "ir" / f"{name}.json"
            target = out / "html" / f"{name}.html"
            try:
                rc, a_out, a_err = _run_archify(mjs, ["validate", ir["diagram_type"], str(ir_path), "--json"])
                if rc != 0:
                    degraded_reasons.append(f"archify validate failed: {name} ({_archify_error_text(a_out, a_err)})")
                    continue
                rc, _, err = _run_archify(mjs, ["render", ir["diagram_type"], str(ir_path), str(target)])
                if rc != 0 or not target.is_file():
                    degraded_reasons.append(f"archify render failed: {name} ({err.strip()[:120]})")
                    continue
                archify_html.append(f"html/{name}.html")
                artifacts.append(f"html/{name}.html")
            except (subprocess.TimeoutExpired, OSError) as exc:
                degraded_reasons.append(f"archify error: {name} ({exc})")

    # R7' L1/L2 页写入（静态多页互链；链成功时不出，chain/ 已替代）
    if not use_chain:
        for md in module_datas:
            (out / "modules" / md["page_file"]).write_text(
                _module_page_html(repo.name, md, selected), encoding="utf-8", newline="\n"
            )
        for sd in symbol_datas:
            (out / "symbols" / f"{sd['fid']}.html").write_text(
                _symbol_page_html(repo.name, sd, selected, page_to_module_id), encoding="utf-8", newline="\n"
            )

    if use_chain:
        modules_section = _chain_section(chain)
    else:
        modules_section = _modules_index_section(module_datas, modules_truncated, len(symbol_datas), symbols_truncated)
    # 用户产物信息架构：引导 → 总览（AI 梳理）→ 模块链 → 架构 → 体检 →
    # findings 全清单（折叠）→ 重构决定 → 业务线 → 深层数据（折叠）
    esc = html_mod.escape
    arch_link = (f'<a href="html/architecture.html">html/architecture.html</a>'
                 if "html/architecture.html" in archify_html else "")
    timeline_link = ('<p class="meta">Visual timeline of all verdicts: '
                     '<a href="html/timeline.html">html/timeline.html</a></p>'
                     if "html/timeline.html" in archify_html else "")
    sections = [
        _how_to_read_section(),
        _overview_section(structure_payload, len(seq_irs), len(verdicts)),
        modules_section,
        {
            "title": "Architecture (L0)",
            "body": ("<p>The same structure as a diagram."
                     + (f" Rendered view: {arch_link}." if arch_link else "")
                     + " Mermaid source below renders online if archify rendering is missing:</p>"
                     + f'<pre class="mermaid">\n{esc(mmd_arch)}</pre>'),
        },
        _pathology_checkup_section(path_per_code, path_per_source, path_top_files,
                                   file_links, "file page" if use_chain else "module page"),
        _findings_index_section(findings_rows, findings_total, selected, file_links,
                                "file page" if use_chain else "module page"),
        _verdicts_section(verdicts),
    ]
    sections[-1] = {**sections[-1], "body": sections[-1]["body"] + timeline_link}
    for name, ir in seq_irs.items():
        mmd = (out / f"slice-{name}.mmd").read_text(encoding="utf-8")
        rendered = (f' Rendered view: <a href="html/slice-{esc(name)}.html">'
                    f'html/slice-{esc(name)}.html</a>.'
                    if f"html/slice-{name}.html" in archify_html else "")
        sections.append({
            "title": f"Slice: {name}",
            "body": (f"<p>One business line end-to-end.{rendered}</p>"
                     + f'<pre class="mermaid">\n{esc(mmd)}</pre>'),
        })
    deep_links = [f"ir/{n}.json" for n in irs] + [
        f"ir/{'chain-index' if use_chain else 'modules-index'}.json", "findings.sarif"]
    sections.append(_deep_data_section(deep_links, archify_html, degraded_reasons))

    report_path = out / "report.html"
    report_path.write_text(
        _report_html(repo, ir_hash, git_head, sections, staleness), encoding="utf-8", newline="\n"
    )
    artifacts.insert(0, "report.html")

    return {
        "report": str(report_path),
        "artifacts": artifacts,
        "archify": bool(archify_html),
        "degraded": degraded_reasons,
        "counts": {
            "slices": len(seq_irs),
            "verdicts": len(verdicts),
            "sarif_results": len(sarif["runs"][0]["results"]),
            "module_pages": len(module_datas),
            "symbol_pages": len(symbol_datas),
            "chain_modules": chain["counts"]["modules"] if use_chain else 0,
            "chain_pages": chain["counts"]["pages"] if use_chain else 0,
        },
        "chain": {
            "ok": use_chain,
            "index": chain["index"] if use_chain else None,
            "notes": chain["notes"] if chain is not None else [],
        },
        "ir_sha256": ir_hash,
    }


def _facts_git_head(con: sqlite3.Connection) -> str:
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'git_head'").fetchone()
    except sqlite3.OperationalError:
        return ""
    return row[0] if row is not None else ""


def _tool_version() -> str:
    from . import __version__

    return __version__


def render_slice(repo: Path, slice_id: str) -> dict:
    """单业务线材料（MCP 决策点快材料）：Mermaid 源 + IR，可贴 mermaid.live 渲染。"""
    repo = Path(repo)
    con = _connect_ro(repo)
    try:
        ir = build_sequence_ir(repo, con, slice_id)
    finally:
        con.close()
    return {
        "slice_id": slice_id,
        "mermaid": mermaid_sequence(ir),
        "participants": len(ir["participants"]),
        "messages": len(ir["messages"]),
        "acts": len(ir.get("segments") or []),
        "note": "mermaid source can be pasted into https://mermaid.live to render; act rects correspond to acts",
    }


def render_verdicts(repo: Path) -> dict:
    """verdict 时间线 + open/T4 清单（MCP 决策点快材料）。"""
    repo = Path(repo)
    verdicts = s6_transact.load_verdicts(repo)[:VERDICTS_CAP]
    open_items = [v for v in verdicts if v.get("status") != "committed"]
    t4 = [v for v in open_items if v.get("status") == "needs_confirmation"]
    return {
        "count": len(verdicts),
        "open": [
            {k: v.get(k) for k in ("tid", "type", "status", "failed_gate", "verdict_at")}
            for v in open_items
        ],
        "t4_pending": [
            {k: v.get(k) for k in ("tid", "type", "status", "summary", "verdict_at")}
            for v in t4
        ],
        "timeline": [
            {k: v.get(k) for k in ("tid", "type", "status", "verdict_at")}
            for v in verdicts
        ],
        "note": "T4 release requires manual CLI vrp transact --confirm; open items are automatically fed back into dossier evidence",
    }
