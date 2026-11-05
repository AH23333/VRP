"""S8 render - 中间产物静态报告（构想v7 §5）。

三特征：静态产物、无服务端、无前端构建链；数据真值 100% 来自 facts.db；
渲染执行委托外部工具（archify 工具组，缺失降级 Mermaid-only 不阻塞）。

- IR 层：facts.db → archify input IR JSON（确定性——同 facts.db 字节级同 IR）
- archify 接入：VRP_ARCHIFY_MJS / work 沙箱发现 → `archify validate --json` →
  `archify render`；缺失/失败单件降级 Mermaid-only
- Mermaid 降级链：IR → .mmd 源文本（零依赖，核心红线内）
- SARIF 2.1.0：findings 手写 JSON（不引 sarif-om，零依赖）
- report.html：目录页 + T4/open 清单 + Mermaid 内嵌（CDN script，无网显源码）
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

from . import s6_transact

REPORT_DIR = ".vrp/report"
ARCHIFY_TIMEOUT = 120
SEQUENCE_SLICES_CAP = 10
SARIF_FINDINGS_CAP = 2000
VERDICTS_CAP = 200

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
    """archify id 硬约束 ^[a-zA-Z][a-zA-Z0-9_-]*$：非法字符转 '-'，首字符非字母加前缀。"""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(raw))
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
    """业务线时序 IR：steps→participants、相邻步→messages（y 等距）、acts→segments。"""
    if not _has_table(con, "slices"):
        raise SystemExit(f"render_slice: slices table missing (pre-R2' facts.db): {slice_id}")
    row = con.execute(
        "SELECT name, payload_json FROM slices WHERE slice_id = ? AND status = 'active'", (slice_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"render_slice: unknown or inactive slice_id: {slice_id}")
    payload = json.loads(row[1])
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


def build_timeline_ir(verdicts: list[dict]) -> dict:
    """verdict 时间线 IR：open 优先在前，committed/rolled_back 按 verdict_at。"""
    events = []
    for v in verdicts[:VERDICTS_CAP]:
        events.append({
            "id": v.get("tid", "unknown"),
            "at": v.get("verdict_at", "1970-01-01T00:00:00"),
            "title": f"{v.get('tid', '?')} [{v.get('type', '?')}] {v.get('status', '?')}",
            "lane": "verdicts",
            "kind": _KIND_BY_STATUS.get(v.get("status", ""), "change"),
        })
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
    """findings → SARIF 2.1.0（手写 JSON，行级诊断；L3 明细层零筛选）。"""
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
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {"name": "vrp", "version": tool_version, "informationUri": "https://github.com/DeusData/vibe-restruct-pipe"}},
            "results": results,
        }],
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
    """archify.mjs 发现：VRP_ARCHIFY_MJS → vrp 仓 work 沙箱（editable 装态）。"""
    env = os.environ.get("VRP_ARCHIFY_MJS")
    if env:
        path = Path(env)
        return path if path.is_file() else None
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


# ----------------------------------------------------------------- report


def _report_html(repo: Path, ir_hash: str, git_head: str, sections: list[dict], staleness: dict | None = None) -> str:
    """静态目录页：数据层说明 + 陈旧横幅 + T4/open 清单 + 产物链接；无时间戳保字节级复现
    （陈旧横幅随渲染时仓 HEAD 变化，是复现契约的有意例外——见 _stale_banner_html）。"""
    esc = html_mod.escape
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh">',
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
        f" | IR sha256: <code>{esc(ir_hash[:16])}...</code> | 数据真值 100% 来自 facts.db</p>",
        _stale_banner_html(staleness),
        "<h2>数据层</h2>",
        "<p>本报告为静态产物（无服务端/无前端构建链）；数据层即 <code>.vrp/facts.db</code>（sqlite 单文件，"
        "可用 Datasette 直读：<code>datasette .vrp/facts.db</code>）；渲染委托 archify/Mermaid，"
        "archify 缺失时 Mermaid 源内嵌本页（无网环境显示源码）。</p>",
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
        "<p>T4（improvement）放行需人工 CLI <code>vrp transact --confirm</code>；"
        f"open verdicts 共 {len(open_items)} 条（T4 待放行 {len(t4)} 条），自动回馈进 dossier evidence。</p>"
        + ("<table><tr><th>tid</th><th>type</th><th>status</th><th>failed_gate</th></tr>"
           + "".join(rows) + "</table>" if rows else "<p>无 open verdicts。</p>")
    )
    return {"title": "verdict 清单（T4 待放行优先）", "body": body}


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
        detail = f"落后当前 HEAD 约 {staleness['behind']} 个提交"
    else:
        detail = "与当前 HEAD 不一致（非简单落后，可能分叉）"
    return (
        '<div style="background:#fde8e8;border:2px solid #b3261e;padding:.8rem 1rem;'
        'border-radius:6px;margin:1rem 0">'
        "<strong>⚠ 陈旧报告</strong>：facts.git_head "
        f'<code>{esc(staleness["facts_head"][:12])}…</code> {detail}'
        f'（当前 <code>{esc(staleness["head"][:12])}…</code>）——结论可能过期；'
        "重跑 <code>vrp run</code> 后 <code>vrp report</code> 刷新。</div>"
    )


def _clean_render_out(out: Path) -> None:
    """渲染前清自有产物（R6' 病根四：上轮孤儿混入本轮快照——timeline.html
    渲了没链接即两轮渲染混目录）。只清 render 拥有的名字，外部文件不动。"""
    for sub in ("ir", "html"):
        d = out / sub
        if d.is_dir():
            shutil.rmtree(d)
    for pattern in ("*.mmd", "findings.sarif", "report.html"):
        for f in out.glob(pattern):
            f.unlink()


def render_report(repo: Path, out_dir: Path | None = None) -> dict:
    """S8 全量报告：IR → archify/Mermaid/SARIF → .vrp/report/report.html。"""
    repo = Path(repo)
    con = _connect_ro(repo)
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
    finally:
        con.close()

    out = Path(out_dir) if out_dir else repo / REPORT_DIR
    staleness = _staleness(repo, git_head)
    _clean_render_out(out)
    (out / "ir").mkdir(parents=True, exist_ok=True)
    (out / "html").mkdir(parents=True, exist_ok=True)

    irs: dict[str, dict] = {"architecture": arch_ir, **{f"slice-{k}": v for k, v in seq_irs.items()}}
    if timeline_ir is not None:  # 空 events 被 archify minItems 拒；无 verdicts 不出 timeline
        irs["timeline"] = timeline_ir
    ir_hash = hashlib.sha256(_dumps_canonical(irs).encode("utf-8")).hexdigest()
    for name, ir in irs.items():
        (out / "ir" / f"{name}.json").write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "findings.sarif").write_text(json.dumps(sarif, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts: list[str] = ["ir/architecture.json", "findings.sarif"]
    degraded_reasons: list[str] = []

    # Mermaid 源（零依赖，恒出）
    mmd_arch = mermaid_architecture(arch_ir)
    (out / "architecture.mmd").write_text(mmd_arch, encoding="utf-8")
    artifacts.append("architecture.mmd")
    for name, ir in seq_irs.items():
        (out / f"slice-{name}.mmd").write_text(mermaid_sequence(ir), encoding="utf-8")
        artifacts.append(f"slice-{name}.mmd")

    # archify 工具组（缺失/失败降级 Mermaid-only，不阻塞）
    mjs = find_archify_mjs()
    archify_html: list[str] = []
    if mjs is None:
        degraded_reasons.append("archify not found (VRP_ARCHIFY_MJS / work sandbox)")
    else:
        for name, ir in irs.items():
            ir_path = out / "ir" / f"{name}.json"
            target = out / "html" / f"{name}.html"
            try:
                rc, _, err = _run_archify(mjs, ["validate", ir["diagram_type"], str(ir_path), "--json"])
                if rc != 0:
                    degraded_reasons.append(f"archify validate failed: {name} ({err.strip()[:120]})")
                    continue
                rc, _, err = _run_archify(mjs, ["render", ir["diagram_type"], str(ir_path), str(target)])
                if rc != 0 or not target.is_file():
                    degraded_reasons.append(f"archify render failed: {name} ({err.strip()[:120]})")
                    continue
                archify_html.append(f"html/{name}.html")
                artifacts.append(f"html/{name}.html")
            except (subprocess.TimeoutExpired, OSError) as exc:
                degraded_reasons.append(f"archify error: {name} ({exc})")

    sections = [
        _verdicts_section(verdicts),
        {
            "title": "架构图（L0）",
            "body": "<p>archify 渲染缺失时 Mermaid 源如下（本页内嵌，联网渲染）：</p>"
                    f'<pre class="mermaid">\n{html_mod.escape(mmd_arch)}</pre>',
        },
    ]
    for name, ir in seq_irs.items():
        mmd = (out / f"slice-{name}.mmd").read_text(encoding="utf-8")
        sections.append({
            "title": f"业务线：{name}",
            "body": f'<pre class="mermaid">\n{html_mod.escape(mmd)}</pre>',
        })
    if archify_html:
        links = "".join(f'<li><a href="{html_mod.escape(h)}">{html_mod.escape(h)}</a></li>' for h in archify_html)
        sections.insert(1, {"title": "archify 渲染产物", "body": f"<ul>{links}</ul>"})
    if degraded_reasons:
        items = "".join(f"<li>{html_mod.escape(r)}</li>" for r in degraded_reasons)
        sections.append({"title": "降级记录（诚实失败）", "body": f"<ul>{items}</ul><p>降级不阻塞（rc=0）。</p>"})

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
        "counts": {"slices": len(seq_irs), "verdicts": len(verdicts), "sarif_results": len(sarif["runs"][0]["results"])},
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
        "note": "mermaid 源可贴 https://mermaid.live 渲染；分幕 rect 对应 acts",
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
        "note": "T4 放行只能人工 CLI vrp transact --confirm；open 项自动回馈 dossier evidence",
    }
