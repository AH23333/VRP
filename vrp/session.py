"""R3.5' session — 信任分级交付·全信任模式（构想v7 §3.3）。

多轮重构会话的初末态对比交付（诚实版，不做图 diff）：
- session_begin：facts.db 文件快照（sqlite backup API 安全复制）+ 会话元数据
- session_end：末态快照 + 初末计数 diff（节点/边/病理/xcheck 四组数字）
- session_report：初末计数 diff + 双 L0 架构图并排（archify 渲染，缺失降级
  Mermaid 内嵌）+ verdict 时间线（会话窗口内）
- 会话落 <repo>/.vrp/sessions/<session_id>/：session.json + begin.db + end.db
- 同一会话 report.html 字节级复现（无渲染时刻时间戳；began_at/ended_at 是会话事实）
- 不做图 diff：fid 是内容敏感 sha256，重构后必变；符号匹配+结构对齐是研究级
  问题，远期再议——诚实版并排+计数足够拍板交付（§3.3）

纯标准库（archify 经 vrp.render 既有子进程链复用，不入核心）。
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import render as render_mod
from . import dbpath
from . import s6_transact

SESSIONS_DIR = ".vrp/sessions"
DIFF_KEYS = ("symbols", "edges", "findings", "discrepancy_ledger")
VERDICTS_WINDOW_CAP = 200

_MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_ro(db: Path) -> sqlite3.Connection:
    con = dbpath.ro_connect(db)
    con.row_factory = sqlite3.Row
    return con


def _snapshot_facts(repo: Path, dest: Path) -> None:
    """facts.db 文件快照：sqlite backup API（安全复制，避开裸 copy 的写中一致性坑）。"""
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
    src = _open_ro(facts)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _facts_summary(db: Path) -> dict:
    """快照摘要：四组计数 + facts.git_head + 快照 sha256（溯源）。"""
    con = _open_ro(db)
    try:
        counts = {k: con.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in DIFF_KEYS}
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'git_head'").fetchone()
        except sqlite3.OperationalError:
            row = None
        git_head = row[0] if row is not None else ""
    finally:
        con.close()
    return {"counts": counts, "git_head": git_head, "sha256": _sha256_file(db)}


# ------------------------------------------------------------- 会话存取


def _session_path(repo: Path, session_id: str) -> Path:
    return repo / SESSIONS_DIR / session_id / "session.json"


def _read_session(repo: Path, session_id: str) -> tuple[dict, Path]:
    path = _session_path(repo, session_id)
    if not path.is_file():
        raise SystemExit(f"unknown session: {session_id} ({path})")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"unreadable session.json for {session_id}: {exc}") from exc
    return doc, path.parent


def _list_sessions(repo: Path) -> list[tuple[dict, Path]]:
    """按 id 降序列全部会话；损坏 session.json 跳过（扫描容错，显式访问仍硬拒）。"""
    root = repo / SESSIONS_DIR
    if not root.is_dir():
        return []
    out: list[tuple[dict, Path]] = []
    for sdir in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
        path = sdir / "session.json"
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append((doc, sdir))
    return out


def _find_active(repo: Path) -> tuple[dict, Path] | None:
    for doc, sdir in _list_sessions(repo):
        if doc.get("ended_at") is None:
            return doc, sdir
    return None


def _write_session(sdir: Path, doc: dict) -> None:
    """原子写（tmp + os.replace，etl H2 同型保证）：session_end 覆写既有
    珍贵状态——非原子 write_text 先截断后写，半写崩溃会使 _list_sessions
    静默跳过（扫描容错按设计）、活动会话孤儿化（begin/end.db 无人认领）；
    半写只允许发生在 tmp，目标文件要么旧要么新。"""
    path = sdir / "session.json"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _next_session_id(sessions_root: Path, now: datetime) -> str:
    """id = UTC 时间戳；同秒冲突走 -2/-3 后缀（目录已存在即冲突）。"""
    base = now.strftime("%Y%m%d-%H%M%S")
    sid = base
    n = 2
    while (sessions_root / sid).exists():
        sid = f"{base}-{n}"
        n += 1
    return sid


def _diff(begin_counts: dict, end_counts: dict) -> dict:
    out: dict = {}
    for key in DIFF_KEYS:
        b = begin_counts.get(key, 0)
        e = end_counts.get(key, 0)
        out[key] = {"begin": b, "end": e, "delta": e - b}
    return out


# ------------------------------------------------------------- 三入口


def session_begin(repo: Path, note: str = "") -> dict:
    """开场：facts.db 快照 + 会话元数据；同一 repo 同时只允许一个活动会话。"""
    repo = Path(repo)
    active = _find_active(repo)
    if active is not None:
        raise SystemExit(f"session already active: {active[0]['id']} (session_end it first)")
    sessions_root = repo / SESSIONS_DIR
    sessions_root.mkdir(parents=True, exist_ok=True)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    sid = _next_session_id(sessions_root, now_dt)
    sdir = sessions_root / sid
    sdir.mkdir()
    _snapshot_facts(repo, sdir / "begin.db")
    begin = _facts_summary(sdir / "begin.db")
    doc = {"id": sid, "began_at": now, "ended_at": None, "note": note, "begin": begin, "end": None}
    _write_session(sdir, doc)
    return {
        "session_id": sid,
        "began_at": now,
        "note": note,
        "begin": begin,
        "dir": str(sdir),
        "verdicts_window": f"{now} <= verdict_at",  # 窗口语义提示
    }


def session_end(repo: Path, note: str = "") -> dict:
    """收场：末态快照 + ended_at + 初末计数 diff。"""
    repo = Path(repo)
    active = _find_active(repo)
    if active is None:
        raise SystemExit("no active session (session_begin first)")
    doc, sdir = active
    _snapshot_facts(repo, sdir / "end.db")
    end = _facts_summary(sdir / "end.db")
    now = _utc_now()
    doc["ended_at"] = now
    doc["end"] = end
    if note:
        doc["note"] = f"{doc['note']} | end: {note}" if doc.get("note") else f"end: {note}"
    _write_session(sdir, doc)
    return {
        "session_id": doc["id"],
        "began_at": doc["began_at"],
        "ended_at": now,
        "diff": _diff(doc["begin"]["counts"], end["counts"]),
        "end": end,
        "dir": str(sdir),
    }


def session_report(repo: Path, session_id: str = "", out_dir: Path | None = None) -> dict:
    """交付：初末计数 diff + 双 L0 架构图并排 + verdict 时间线 → report.html。"""
    repo = Path(repo)
    if session_id:
        doc, sdir = _read_session(repo, session_id)
    else:
        ended = [(d, s) for d, s in _list_sessions(repo) if d.get("ended_at") is not None]
        if not ended:
            raise SystemExit("no ended session (session_begin + session_end first)")
        doc, sdir = ended[0]
    if doc.get("ended_at") is None or doc.get("end") is None:
        raise SystemExit(f"session not ended yet: {doc['id']} (session_end first)")

    diff = _diff(doc["begin"]["counts"], doc["end"]["counts"])
    verdicts = s6_transact.load_verdicts(repo)
    in_window = [
        v for v in verdicts
        if doc["began_at"] <= (v.get("verdict_at") or "") <= doc["ended_at"]
    ]
    shown = in_window[:VERDICTS_WINDOW_CAP]

    out = Path(out_dir) if out_dir else sdir
    (out / "ir").mkdir(parents=True, exist_ok=True)
    (out / "html").mkdir(parents=True, exist_ok=True)

    begin_con = _open_ro(sdir / "begin.db")
    try:
        begin_ir = render_mod.build_architecture_ir(repo, begin_con)
    finally:
        begin_con.close()
    end_con = _open_ro(sdir / "end.db")
    try:
        end_ir = render_mod.build_architecture_ir(repo, end_con)
    finally:
        end_con.close()
    (out / "ir" / "begin-architecture.json").write_text(
        json.dumps(begin_ir, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "ir" / "end-architecture.json").write_text(
        json.dumps(end_ir, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    mmd_begin = render_mod.mermaid_architecture(begin_ir)
    mmd_end = render_mod.mermaid_architecture(end_ir)
    (out / "begin.mmd").write_text(mmd_begin, encoding="utf-8", newline="\n")
    (out / "end.mmd").write_text(mmd_end, encoding="utf-8", newline="\n")

    artifacts = [
        "ir/begin-architecture.json", "ir/end-architecture.json", "begin.mmd", "end.mmd",
    ]
    degraded: list[str] = []
    archify_html: list[str] = []
    mjs = render_mod.find_archify_mjs()
    if mjs is None:
        degraded.append("archify not found (VRP_ARCHIFY_MJS / work sandbox)")
    else:
        for label, ir in (("begin", begin_ir), ("end", end_ir)):
            ir_path = out / "ir" / f"{label}-architecture.json"
            target = out / "html" / f"{label}.html"
            try:
                rc, _, err = render_mod._run_archify(
                    mjs, ["validate", ir["diagram_type"], str(ir_path), "--json"]
                )
                if rc != 0:
                    degraded.append(f"archify validate failed: {label} ({err.strip()[:120]})")
                    continue
                rc, _, err = render_mod._run_archify(
                    mjs, ["render", ir["diagram_type"], str(ir_path), str(target)]
                )
                if rc != 0 or not target.is_file():
                    degraded.append(f"archify render failed: {label} ({err.strip()[:120]})")
                    continue
                archify_html.append(f"html/{label}.html")
                artifacts.append(f"html/{label}.html")
            except (subprocess.TimeoutExpired, OSError) as exc:
                degraded.append(f"archify error: {label} ({exc})")

    report_path = out / "report.html"
    report_path.write_text(
        _report_html(repo, doc, diff, shown, len(in_window), mmd_begin, mmd_end, archify_html, degraded),
        encoding="utf-8", newline="\n",
    )
    artifacts.insert(0, "report.html")

    return {
        "session_id": doc["id"],
        "began_at": doc["began_at"],
        "ended_at": doc["ended_at"],
        "report": str(report_path),
        "artifacts": artifacts,
        "diff": diff,
        "verdicts_in_window": len(in_window),
        "verdicts_truncated": len(in_window) > len(shown),
        "git_head": {"begin": doc["begin"]["git_head"], "end": doc["end"]["git_head"]},
        "archify": bool(archify_html),
        "degraded": degraded,
        "note": "no diagram diff (fid content sensitive, §3.3 honest mode); byte-reproducible within the same session",
    }


# ------------------------------------------------------------- report html


def _delta_str(delta: int) -> str:
    return f"+{delta}" if delta > 0 else str(delta)


def _report_html(
    repo: Path,
    doc: dict,
    diff: dict,
    verdicts: list[dict],
    verdicts_total: int,
    mmd_begin: str,
    mmd_end: str,
    archify_html: list[str],
    degraded: list[str],
) -> str:
    """静态并排页：计数 diff + verdict 窗口 + 双 L0 图；无渲染时刻时间戳保复现。"""
    esc = html_mod.escape
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>vrp session {esc(doc['id'])} - {esc(repo.name)}</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#222}",
        "h1,h2{border-bottom:2px solid #ddd;padding-bottom:.3rem}",
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}",
        ".meta{color:#666;font-size:.9rem}",
        ".delta-pos{color:#0a7d32;font-weight:600}.delta-neg{color:#b3261e;font-weight:600}",
        ".cols{display:flex;gap:1rem;flex-wrap:wrap}",
        ".col{flex:1 1 460px;min-width:380px}",
        ".col iframe{width:100%;height:560px;border:1px solid #ddd;border-radius:6px}",
        "pre.mmd{background:#f6f6f6;padding:1rem;overflow-x:auto;border-radius:6px;font-size:.8rem}",
        "</style>",
        f'<script src="{_MERMAID_CDN}"></script>',
        "<script>if(window.mermaid){mermaid.initialize({startOnLoad:true,securityLevel:'loose'});}</script>",
        "</head>",
        "<body>",
        f"<h1>vrp session - {esc(repo.name)}</h1>",
        f'<p class="meta">session: <code>{esc(doc["id"])}</code>'
        f" | began_at: <code>{esc(doc['began_at'])}</code> | ended_at: <code>{esc(doc['ended_at'])}</code></p>",
        f'<p class="meta">begin.git_head: <code>{esc(doc["begin"]["git_head"] or "(none)")}</code>'
        f' | end.git_head: <code>{esc(doc["end"]["git_head"] or "(none)")}</code>'
        " | no diagram diff (fid content sensitive; honest side-by-side + counts, §3.3)</p>",
        "<h2>Count diff begin vs end (four groups)</h2>",
        "<table><tr><th>Group</th><th>Begin</th><th>End</th><th>delta</th></tr>",
    ]
    for key in DIFF_KEYS:
        d = diff[key]
        cls = "delta-pos" if d["delta"] > 0 else ("delta-neg" if d["delta"] < 0 else "")
        parts.append(
            f'<tr><td>{esc(key)}</td><td>{d["begin"]}</td><td>{d["end"]}</td>'
            f'<td class="{cls}">{esc(_delta_str(d["delta"]))}</td></tr>'
        )
    parts.append("</table>")
    parts.append("<h2>Verdict timeline (session window)</h2>")
    if verdicts:
        parts.append(f"<p>{verdicts_total} verdicts in window"
                     + (" (display truncated)" if verdicts_total > len(verdicts) else "") + ".</p>")
        parts.append("<table><tr><th>tid</th><th>type</th><th>status</th><th>verdict_at</th></tr>")
        for v in verdicts:
            parts.append(
                f'<tr><td>{esc(str(v.get("tid", "?")))}</td><td>{esc(str(v.get("type", "?")))}</td>'
                f'<td>{esc(str(v.get("status", "?")))}</td><td>{esc(str(v.get("verdict_at", "")))}</td></tr>'
            )
        parts.append("</table>")
    else:
        parts.append("<p>No verdicts in the session window.</p>")
    parts.append("<h2>Side-by-side L0 architecture (begin | end)</h2>")
    if len(archify_html) == 2:
        parts.append('<div class="cols">')
        parts.append(f'<div class="col"><h3>Begin</h3><iframe src="{esc(archify_html[0])}"></iframe></div>')
        parts.append(f'<div class="col"><h3>End</h3><iframe src="{esc(archify_html[1])}"></iframe></div>')
        parts.append("</div>")
    else:
        parts.append('<div class="cols">')
        parts.append(f'<div class="col"><h3>Begin</h3><pre class="mermaid">\n{esc(mmd_begin)}</pre></div>')
        parts.append(f'<div class="col"><h3>End</h3><pre class="mermaid">\n{esc(mmd_end)}</pre></div>')
        parts.append("</div>")
    if degraded:
        items = "".join(f"<li>{esc(r)}</li>" for r in degraded)
        parts.append("<h2>Degradation record (honest failure)</h2>")
        parts.append(f"<ul>{items}</ul><p>Degradation does not block (rc=0).</p>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
