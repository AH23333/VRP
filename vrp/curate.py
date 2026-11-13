"""S2.5 curate - AI 梳理工件验收入库（构想v7 §2/§3.2）。

点线面三层中的线（slices）与面（structure）：AI 产出 JSON 文件落
`.vrp/ai/`，本模块确定性验证（全过才入库 / 任一不过整文件拒绝出收据），
`--reject` 用户否决留痕（decisions 表）。

R-B 批诚实性不变量：同批重复 slice_id / 重复 module_id 整批拒绝
（收据条数即入库条数，不静默覆盖）；structure 否决的数字 target_id
精确到行（decisions 留痕与实际否决行一致）。

纯标准库；写 facts.db 受 M4 freshness 硬拒（调用方用 _open_facts_rw）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 相邻 steps 之间视为"真实边"的边类型（v7 §2.2：CALLS/CALL_REFERENCE/HTTP_CALLS 任一方向）
SLICE_EDGE_TYPES = ("CALLS", "CALL_REFERENCE", "HTTP_CALLS")

SLICE_ROLES = frozenset({"entry", "process", "branch", "persist", "respond"})

AI_DIR_NAME = ".vrp/ai"
RECEIPT_NAME = ".vrp/curate-receipt.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _err(code: str, detail: str, **loc) -> dict:
    item = {"code": code, "detail": detail}
    item.update({k: v for k, v in loc.items() if v is not None})
    return item


# ---------------------------------------------------------------- validation


def _validate_one_slice(con: sqlite3.Connection, raw: object) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return [_err("SLICE_SCHEMA", "slice must be a JSON object")]
    slice_id = raw.get("slice_id")
    if not isinstance(slice_id, str) or not slice_id.strip():
        return [_err("SLICE_SCHEMA", "slice_id must be a non-empty string")]
    for field, code in (("name", "SLICE_EMPTY_NAME"), ("summary", "SLICE_EMPTY_SUMMARY")):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(_err(code, f"{field} must be a non-empty string", slice_id=slice_id))
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(_err("SLICE_SCHEMA", "steps must be a non-empty array", slice_id=slice_id))
        return errors
    fids: list[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("fid"), str) or not step["fid"]:
            errors.append(_err("SLICE_SCHEMA", f"steps[{i}] must be an object with a non-empty fid", slice_id=slice_id, step=i))
            fids.append("")
            continue
        fid = step["fid"]
        fids.append(fid)
        role = step.get("role", "process")
        if role not in SLICE_ROLES:
            errors.append(_err("SLICE_SCHEMA", f"steps[{i}].role invalid: {role!r}", slice_id=slice_id, step=i))
        if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is None:
            errors.append(_err("SLICE_UNKNOWN_FID", f"steps[{i}].fid not found in facts.db symbols", slice_id=slice_id, step=i))
    for i in range(len(fids) - 1):
        a, b = fids[i], fids[i + 1]
        if not a or not b:
            continue
        row = con.execute(
            "SELECT 1 FROM edges WHERE ((src_fid = ? AND dst_fid = ?) OR (src_fid = ? AND dst_fid = ?))"
            f" AND type IN ({','.join('?' * len(SLICE_EDGE_TYPES))})",
            (a, b, b, a, *SLICE_EDGE_TYPES),
        ).fetchone()
        if row is None:
            errors.append(
                _err("SLICE_MISSING_EDGE", f"no edge between steps[{i}] and steps[{i + 1}] ({'+'.join(SLICE_EDGE_TYPES)} in either direction)",
                     slice_id=slice_id, step=i)
            )
    acts = raw.get("acts")
    if acts is not None:
        if not isinstance(acts, list) or not acts:
            errors.append(_err("SLICE_ACT_BOUNDS", "acts must be a non-empty array when present", slice_id=slice_id))
        else:
            prev_to = -1
            for i, act in enumerate(acts):
                if not isinstance(act, dict) or not isinstance(act.get("name"), str) or not act["name"].strip():
                    errors.append(_err("SLICE_ACT_BOUNDS", f"acts[{i}] must be an object with a non-empty name", slice_id=slice_id))
                    continue
                lo, hi = act.get("from_step"), act.get("to_step")
                if not isinstance(lo, int) or not isinstance(hi, int) or not (0 <= lo <= hi < len(steps)):
                    errors.append(_err("SLICE_ACT_BOUNDS", f"acts[{i}] invalid bounds: [{lo}, {hi}]", slice_id=slice_id))
                    continue
                if lo != prev_to + 1:
                    errors.append(_err("SLICE_ACT_BOUNDS", f"acts[{i}] must follow the previous act without gaps (expected from_step={prev_to + 1})",
                                       slice_id=slice_id))
                prev_to = hi
            if prev_to != len(steps) - 1:
                errors.append(_err("SLICE_ACT_BOUNDS", f"acts must cover all steps (last act to_step should be {len(steps) - 1})",
                                   slice_id=slice_id))
    return errors


def _validate_slice_array(con: sqlite3.Connection, raw: list) -> list[dict]:
    """整批 slices 验证：逐条 _validate_one_slice + 跨条 slice_id 查重。

    同批重复 slice_id 原先静默后条覆盖前条（_ingest_slices DELETE+INSERT），
    收据却计 accepted=N——收据对入库条数撒谎；整批拒绝才是原子语义。
    """
    errors: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        errors.extend(_validate_one_slice(con, item))
        if isinstance(item, dict):
            sid = item.get("slice_id")
            if isinstance(sid, str) and sid.strip():
                if sid in seen:
                    errors.append(_err("SLICE_SCHEMA", f"duplicate slice_id in the same submission: {sid}", slice_id=sid))
                else:
                    seen.add(sid)
    return errors


def _validate_structure(con: sqlite3.Connection, raw: object) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return [_err("STRUCT_SCHEMA", "structure must be a JSON object")]
    modules = raw.get("modules")
    if not isinstance(modules, list) or not modules:
        return [_err("STRUCT_SCHEMA", "modules must be a non-empty array")]
    layers = raw.get("layers")
    if not isinstance(layers, list) or not layers:
        return errors + [_err("STRUCT_SCHEMA", "layers must be a non-empty array")]
    layer_names: list[str] = []
    deps: dict[str, list[str]] = {}
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), str) or not layer["name"].strip():
            errors.append(_err("STRUCT_SCHEMA", f"layers[{i}] must be an object with a non-empty name"))
            continue
        name = layer["name"]
        if name in deps:
            errors.append(_err("STRUCT_SCHEMA", f"duplicate layer name: {name}", layer=name))
            continue
        layer_names.append(name)
        may = layer.get("may_depend_on", [])
        if not isinstance(may, list) or any(not isinstance(x, str) for x in may):
            errors.append(_err("STRUCT_SCHEMA", f"layers[{name}].may_depend_on must be an array of strings", layer=name))
            continue
        deps[name] = may
    for name, may in deps.items():
        for target in may:
            if target not in deps:
                errors.append(_err("STRUCT_UNKNOWN_LAYER", f"layer {name} depends on unknown layer: {target}", layer=name))
    # 层依赖 DAG 检查（白灰黑 DFS）
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in deps}

    def has_cycle(node: str) -> str | None:
        color[node] = GRAY
        for nxt in deps.get(node, []):
            if nxt not in deps:
                continue
            if color[nxt] == GRAY:
                return f"{node} -> {nxt}"
            if color[nxt] == WHITE:
                found = has_cycle(nxt)
                if found:
                    return found
        color[node] = BLACK
        return None

    for name in deps:
        if color[name] == WHITE:
            cycle = has_cycle(name)
            if cycle:
                errors.append(_err("STRUCT_LAYER_CYCLE", f"may_depend_on contains a cycle: {cycle}"))
                break
    seen_fid: dict[str, str] = {}
    seen_mid: set[str] = set()
    for i, mod in enumerate(modules):
        if not isinstance(mod, dict):
            errors.append(_err("STRUCT_SCHEMA", f"modules[{i}] must be a JSON object"))
            continue
        mid = mod.get("module_id") if isinstance(mod.get("module_id"), str) else f"modules[{i}]"
        if isinstance(mod.get("module_id"), str) and mod.get("module_id", "").strip():
            if mid in seen_mid:
                errors.append(_err("STRUCT_SCHEMA", f"duplicate module_id in the same payload: {mid}", module=mid))
            else:
                seen_mid.add(mid)
        for field, code in (("name", "STRUCT_EMPTY_NAME"), ("responsibility", "STRUCT_EMPTY_RESPONSIBILITY"),
                            ("module_id", "STRUCT_SCHEMA")):
            value = mod.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(_err(code, f"{field} must be a non-empty string", module=mid))
        layer = mod.get("layer")
        if not isinstance(layer, str) or not layer.strip():
            errors.append(_err("STRUCT_SCHEMA", "layer must be a non-empty string", module=mid))
        elif layer not in deps:
            errors.append(_err("STRUCT_UNKNOWN_LAYER", f"module.layer not in layers: {layer}", module=mid))
        fids = mod.get("symbol_fids")
        if not isinstance(fids, list) or not fids or any(not isinstance(x, str) or not x for x in fids):
            errors.append(_err("STRUCT_SCHEMA", "symbol_fids must be a non-empty array of strings", module=mid))
            continue
        for fid in fids:
            if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is None:
                errors.append(_err("STRUCT_UNKNOWN_FID", "fid not found in facts.db symbols", module=mid, fid=fid))
            if fid in seen_fid:
                errors.append(_err("STRUCT_FID_OVERLAP", f"fid belongs to both {seen_fid[fid]} and {mid}", module=mid, fid=fid))
            else:
                seen_fid[fid] = mid
    rules = raw.get("rules")
    if rules is not None:
        if not isinstance(rules, list) or any(not isinstance(r, dict) for r in rules):
            errors.append(_err("STRUCT_SCHEMA", "rules must be an array of objects when present"))
        else:
            for i, rule in enumerate(rules):
                if not isinstance(rule.get("rule"), str) or not rule["rule"].strip():
                    errors.append(_err("STRUCT_SCHEMA", f"rules[{i}].rule must be a non-empty string"))
                for field in ("from_layer", "to_layer"):
                    value = rule.get(field)
                    if value is not None and (not isinstance(value, str) or value not in deps):
                        errors.append(_err("STRUCT_RULE_LAYER", f"rules[{i}].{field} not in layers: {value!r}"))
    return errors


# ------------------------------------------------------------------- ingest


def _ingest_slices(con: sqlite3.Connection, items: list[dict]) -> int:
    for raw in items:
        con.execute("DELETE FROM slices WHERE slice_id = ?", (raw["slice_id"],))
        con.execute(
            "INSERT INTO slices (slice_id, name, summary, payload_json, status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (raw["slice_id"], raw["name"], raw["summary"], json.dumps(raw, ensure_ascii=False), _now()),
        )
    con.commit()
    return len(items)


def _ingest_structure(con: sqlite3.Connection, raw: dict) -> None:
    con.execute("DELETE FROM structure")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', ?)",
        (json.dumps(raw, ensure_ascii=False), _now()),
    )
    con.commit()


# ----------------------------------------------------------- stale revalidate


def _revalidate_ingested(con: sqlite3.Connection) -> dict:
    """复验已入库 active 工件：fid/边失效者标 stale（防漂移，v7 §2.2）。"""
    stale_slices: list[str] = []
    for slice_id, payload_json in con.execute(
        "SELECT slice_id, payload_json FROM slices WHERE status = 'active'"
    ).fetchall():
        try:
            raw = json.loads(payload_json)
        except json.JSONDecodeError:
            stale_slices.append(slice_id)
            continue
        ok = True
        for step in raw.get("steps", []):
            if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (step.get("fid", ""),)).fetchone() is None:
                ok = False
                break
        if ok:
            fids = [s.get("fid", "") for s in raw.get("steps", [])]
            for i in range(len(fids) - 1):
                a, b = fids[i], fids[i + 1]
                row = con.execute(
                    "SELECT 1 FROM edges WHERE ((src_fid = ? AND dst_fid = ?) OR (src_fid = ? AND dst_fid = ?))"
                    f" AND type IN ({','.join('?' * len(SLICE_EDGE_TYPES))})",
                    (a, b, b, a, *SLICE_EDGE_TYPES),
                ).fetchone()
                if row is None:
                    ok = False
                    break
        if not ok:
            con.execute("UPDATE slices SET status = 'stale' WHERE slice_id = ?", (slice_id,))
            stale_slices.append(slice_id)
    structure_stale = False
    row = con.execute("SELECT id, payload_json FROM structure WHERE status = 'active'").fetchone()
    if row is not None:
        try:
            raw = json.loads(row[1])
        except json.JSONDecodeError:
            raw = None
        ok = raw is not None
        if ok:
            for mod in raw.get("modules", []):
                for fid in mod.get("symbol_fids", []):
                    if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is None:
                        ok = False
                        break
                if not ok:
                    break
        if not ok:
            con.execute("UPDATE structure SET status = 'stale' WHERE id = ?", (row[0],))
            structure_stale = True
    con.commit()
    return {"slices": stale_slices, "structure": structure_stale}


# ------------------------------------------------------------------ curate


def curate(repo: Path, con: sqlite3.Connection) -> dict:
    """扫描 .vrp/ai/ → 验证 → 入库/拒收；返回收据并落盘 .vrp/curate-receipt.json。"""
    ai_dir = repo / ".vrp" / "ai"
    receipt: dict = {"curated_at": _now(), "files": []}
    if ai_dir.is_dir():
        for filename, kind in (("slices.json", "slices"), ("structure.json", "structure")):
            path = ai_dir / filename
            if not path.is_file():
                continue
            entry = _curate_file(con, path, kind)
            receipt["files"].append(entry)
    receipt["stale"] = _revalidate_ingested(con)
    receipt_path = repo / RECEIPT_NAME
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return receipt


def _curate_file(con: sqlite3.Connection, path: Path, kind: str) -> dict:
    entry: dict = {"file": f".vrp/ai/{path.name}", "kind": kind, "status": "rejected",
                   "accepted": 0, "rejected": 0, "errors": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        entry["errors"].append(_err("FILE_NOT_JSON", f"cannot read/parse: {exc}"))
        return entry
    if kind == "slices":
        if not isinstance(raw, list):
            entry["errors"].append(_err("FILE_WRONG_SHAPE", "slices.json must be a JSON array"))
            return entry
        errors = _validate_slice_array(con, raw)
        if errors:
            entry["errors"].extend(errors)
            entry["rejected"] = len(raw)
        else:
            _ingest_slices(con, raw)
            entry["status"], entry["accepted"] = "accepted", len(raw)
    else:
        errors = _validate_structure(con, raw)
        if errors:
            entry["errors"].extend(errors)
            return entry
        _ingest_structure(con, raw)
        entry["status"], entry["accepted"] = "accepted", 1
    return entry


def curate_payload(con: sqlite3.Connection, kind: str, payload_json: str) -> dict:
    """MCP curate_submit 用：内存验证+入库，返回单文件收据（不入 .vrp/ai/）。

    入库成功后立即复验其余 active 工件（F2）：MCP 路径与 CLI curate 同权，
    收据带 stale 节，假 active 窗口不再依赖 CLI 路径被走到。
    """
    try:
        raw = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        return {"kind": kind, "status": "rejected", "accepted": 0, "rejected": 1,
                "errors": [_err("FILE_NOT_JSON", f"payload is not valid JSON: {exc}")]}
    if kind == "slices":
        if not isinstance(raw, list):
            return {"kind": kind, "status": "rejected", "accepted": 0, "rejected": 1,
                    "errors": [_err("FILE_WRONG_SHAPE", "slices payload must be a JSON array")]}
        errors: list[dict] = _validate_slice_array(con, raw)
        if errors:
            return {"kind": kind, "status": "rejected", "accepted": 0, "rejected": len(raw), "errors": errors}
        _ingest_slices(con, raw)
        return {"kind": kind, "status": "accepted", "accepted": len(raw), "rejected": 0, "errors": [],
                "stale": _revalidate_ingested(con)}
    if kind == "structure":
        errors = _validate_structure(con, raw)
        if errors:
            return {"kind": kind, "status": "rejected", "accepted": 0, "rejected": 1, "errors": errors}
        _ingest_structure(con, raw)
        return {"kind": kind, "status": "accepted", "accepted": 1, "rejected": 0, "errors": [],
                "stale": _revalidate_ingested(con)}
    raise SystemExit(f"kind must be slices or structure: {kind}")


def list_slices(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT slice_id, name, summary, status, created_at, payload_json FROM slices ORDER BY slice_id"
    ).fetchall()
    out: list[dict] = []
    for slice_id, name, summary, status, created_at, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            steps = 0
        else:
            # 合法 JSON 非 dict 同列 steps=0（R-X）：AttributeError 会击穿整个列表页
            steps = len(payload.get("steps", [])) if isinstance(payload, dict) else 0
        out.append({"slice_id": slice_id, "name": name, "summary": summary, "status": status,
                    "steps": steps, "created_at": created_at})
    return out


def get_structure(con: sqlite3.Connection) -> dict | None:
    row = con.execute(
        "SELECT payload_json, status, created_at FROM structure ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        payload = None
    return {"payload": payload, "status": row[1], "created_at": row[2]}


def read_receipt(repo: Path) -> dict | None:
    path = repo / RECEIPT_NAME
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def reject(con: sqlite3.Connection, kind: str, target_id: str, note: str = "") -> dict:
    """用户否决已入库工件：status→rejected + decisions 留痕（v7 §3.2）。

    structure 的 target_id：整数字符串（如 MCP curate_reject 传精确行 id）
    按该 id 精确否决，rowcount=0 诚实报错；非数字（CLI 惯例字面量
    'structure'）否决最新一版（与 get_structure 的最新语义一致）。
    decisions 恒记用户传入的原样 target_id。
    """
    if kind == "slice":
        cur = con.execute("UPDATE slices SET status = 'rejected' WHERE slice_id = ?", (target_id,))
    elif kind == "structure":
        try:
            sid = int(target_id)
        except ValueError:
            sid = None
        if sid is not None:
            cur = con.execute("UPDATE structure SET status = 'rejected' WHERE id = ?", (sid,))
        else:
            cur = con.execute("UPDATE structure SET status = 'rejected' WHERE id = (SELECT MAX(id) FROM structure)")
    else:
        raise SystemExit(f"kind must be slice or structure: {kind}")
    if cur.rowcount == 0:
        raise SystemExit(f"no rejectable artifact found: {kind} {target_id}")
    con.execute(
        "INSERT INTO decisions (kind, target_id, decision, note, created_at) VALUES (?, ?, 'reject', ?, ?)",
        (kind, target_id, note, _now()),
    )
    con.commit()
    return {"kind": kind, "target_id": target_id, "status": "rejected", "note": note}


# ------------------------------------------------------------- drift report
# F3 统一漂移查询面 + F1 读出辅助（R6' 诚实发现，构想v7 §12）：
# - 只读：不改任何 status，标记永远发生在写路径（curate/run_etl/MCP submit）
# - fid_ledger 身份史：旧 fid → (project, qualified_name, file_path) 反查，
#   再按 (project, qn) 正查现行 symbols 得唯一新 fid（remap）；
#   qn 消失（改名/删除）→ ledger 的文件域补位（cap，供 AI 人工判读）
# - remap 只是提议：边/acts 仍在 curate_submit 提交时全量重验，不构成旁路

SAME_FILE_CANDIDATE_CAP = 10


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _missing_fid_entry(
    con: sqlite3.Connection, fid: str, step: int | None, has_ledger: bool
) -> dict:
    """单 missing fid 条目：ledger 反查 + 重定位候选（F1 读出辅助）。"""
    entry: dict = {"fid": fid, "ledger": False, "candidates": []}
    if step is not None:
        entry["step"] = step
    if not has_ledger:
        return entry
    row = con.execute(
        "SELECT project, qualified_name, file_path FROM fid_ledger WHERE fid = ?", (fid,)
    ).fetchone()
    if row is None:
        return entry
    project, qn, file_path = row
    entry["ledger"] = True
    entry["ledger_qn"] = qn
    entry["ledger_file_path"] = file_path
    cur = con.execute(
        "SELECT fid, file_path FROM symbols WHERE project = ? AND qualified_name = ?",
        (project, qn),
    ).fetchone()
    if cur is not None:
        entry["candidates"] = [{"fid": cur[0], "file_path": cur[1], "match": "qualified_name"}]
        return entry
    # qn 已消亡（改名/删除）：ledger 记录的文件域补位（确定性排序，cap 诚实截断）
    same_file: list[dict] = []
    for r in con.execute(
        "SELECT fid, qualified_name, file_path FROM symbols WHERE file_path = ?"
        " ORDER BY qualified_name LIMIT ?",
        (file_path, SAME_FILE_CANDIDATE_CAP),
    ).fetchall():
        same_file.append(
            {"fid": r[0], "qualified_name": r[1], "file_path": r[2], "match": "same_file"}
        )
    entry["candidates"] = same_file
    return entry


def _apply_remap(item: dict) -> None:
    """唯一 qn 候选 → 直接给 old→new 映射（多候选/无候选诚实不映射）。"""
    remap: dict[str, str] = {}
    for entry in item.get("missing_fids", []):
        qn_c = [c for c in entry.get("candidates", []) if c.get("match") == "qualified_name"]
        if len(qn_c) == 1:
            remap[entry["fid"]] = qn_c[0]["fid"]
    if remap:
        item["remap"] = remap


def _edge_exists(con: sqlite3.Connection, a: str, b: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM edges WHERE ((src_fid = ? AND dst_fid = ?) OR (src_fid = ? AND dst_fid = ?))"
        f" AND type IN ({','.join('?' * len(SLICE_EDGE_TYPES))})",
        (a, b, b, a, *SLICE_EDGE_TYPES),
    ).fetchone()
    return row is not None


def _drift_slice(con: sqlite3.Connection, row: tuple, has_ledger: bool) -> dict:
    slice_id, name, status, payload_json = row
    item: dict = {
        "slice_id": slice_id,
        "name": name,
        "status": status,
        "fresh": True,
        "missing_fids": [],
        "missing_edges": [],
    }
    try:
        raw = json.loads(payload_json)
    except json.JSONDecodeError:
        item["fresh"] = False
        item["unparseable_payload"] = True
        return item
    steps = raw.get("steps", [])
    present: set[str] = set()
    for i, step in enumerate(steps):
        fid = step.get("fid", "") if isinstance(step, dict) else ""
        if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is not None:
            present.add(fid)
        else:
            item["fresh"] = False
            item["missing_fids"].append(_missing_fid_entry(con, fid, i, has_ledger))
    # 相邻边检查仅对两端 fid 均存活的步对；单边 fid 漂移时缺边是衍生信号，不重复报
    for i in range(len(steps) - 1):
        a = steps[i].get("fid", "") if isinstance(steps[i], dict) else ""
        b = steps[i + 1].get("fid", "") if isinstance(steps[i + 1], dict) else ""
        if a not in present or b not in present:
            continue
        if not _edge_exists(con, a, b):
            item["fresh"] = False
            item["missing_edges"].append({"step": i, "from_fid": a, "to_fid": b})
    _apply_remap(item)
    if status == "stale" and item["fresh"]:
        item["fids_recovered"] = True  # 内容回退使 fid 复活：原载荷可原样重提
    return item


def _drift_structure(con: sqlite3.Connection, row: tuple, has_ledger: bool) -> dict:
    sid, payload_json, status = row
    item: dict = {
        "id": sid,
        "status": status,
        "fresh": True,
        "modules": [],
        "missing_fids": [],
    }
    try:
        raw = json.loads(payload_json)
    except json.JSONDecodeError:
        item["fresh"] = False
        item["unparseable_payload"] = True
        return item
    for mod in raw.get("modules", []):
        mid = mod.get("module_id") if isinstance(mod, dict) else None
        mod_missing = []
        for fid in (mod.get("symbol_fids") or []) if isinstance(mod, dict) else []:
            if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is None:
                item["fresh"] = False
                entry = _missing_fid_entry(con, fid, None, has_ledger)
                if mid is not None:
                    entry["module"] = mid
                mod_missing.append(entry)
                item["missing_fids"].append(entry)
        if mod_missing:
            item["modules"].append({"module_id": mid, "missing": len(mod_missing)})
    _apply_remap(item)
    if status == "stale" and item["fresh"]:
        item["fids_recovered"] = True
    return item


def drift_report(con: sqlite3.Connection) -> dict:
    """统一漂移查询面（F3）：active/stale 梳理工件逐条 fresh/missing_fids/
    missing_edges + 重定位候选与 remap（F1 读出辅助）。只读，不落任何状态。

    rejected 不入（用户已裁决出列）；stored active 但实际漂移者在
    summary.active_but_drifted 单列——F2 写路径钩子在位时该列应为空，
    非空即复验链有洞，本面即是对账面。
    """
    has_ledger = _has_table(con, "fid_ledger")
    report: dict = {"slices": [], "structure": None}
    for row in con.execute(
        "SELECT slice_id, name, status, payload_json FROM slices"
        " WHERE status IN ('active', 'stale') ORDER BY slice_id"
    ).fetchall():
        report["slices"].append(_drift_slice(con, row, has_ledger))
    row = con.execute(
        "SELECT id, payload_json, status FROM structure"
        " WHERE status IN ('active', 'stale') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        report["structure"] = _drift_structure(con, row, has_ledger)
    structure = report["structure"]
    summary = {
        "slices_checked": len(report["slices"]),
        "slices_fresh": sum(1 for s in report["slices"] if s["fresh"]),
        "slices_drifted": sum(1 for s in report["slices"] if not s["fresh"]),
        "structure_drifted": bool(structure is not None and not structure["fresh"]),
    }
    active_but_drifted = [
        s["slice_id"] for s in report["slices"] if s["status"] == "active" and not s["fresh"]
    ]
    if structure is not None and structure["status"] == "active" and not structure["fresh"]:
        active_but_drifted.append("structure")
    summary["active_but_drifted"] = active_but_drifted
    report["summary"] = summary
    return report


# ------------------------------------------------------------- remap apply
# F1 产品化（R7' dogfood 手工回路的收编）：drift_report 给出的唯一候选
# remap 一键写回 .vrp/ai/ 工作工件；多候选/无候选诚实留 unresolved 待判读。
# 只改 ai/*.json 的 fid 引用，不触 facts.db——重提仍走 curate 全量重验，不构成旁路。


def _remap_file(path: Path, remapping: dict[str, str]) -> tuple[dict, set[str]]:
    """单个 ai 工件的重写：返回 (收据条目, 文件中仍存活的旧 fid 集)。

    remapped 按去重 fid 计（与 remap 语义一致）；文件缺失/不可解析诚实标注，
    不写不报替换。remaining = 文件重写后仍引用的（未映射）fid 集。
    """
    entry: dict = {"file": f"{AI_DIR_NAME}/{path.name}", "present": path.is_file(),
                   "remapped": 0, "unresolved": []}
    if not entry["present"]:
        return entry, set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entry["unparseable"] = True
        return entry, set()
    replaced: set[str] = set()
    remaining: set[str] = set()

    def _map_fid(fid: object) -> None:
        if not isinstance(fid, str):
            return
        if fid in remapping:
            replaced.add(fid)
        else:
            remaining.add(fid)

    if path.name == "structure.json":
        if isinstance(raw, dict):
            for mod in raw.get("modules", []):
                if not isinstance(mod, dict):
                    continue
                fids = mod.get("symbol_fids")
                if not isinstance(fids, list):
                    continue
                for i, fid in enumerate(fids):
                    if isinstance(fid, str) and fid in remapping:
                        fids[i] = remapping[fid]
                        replaced.add(fid)
                    else:
                        _map_fid(fid)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            steps = item.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                fid = step.get("fid")
                if isinstance(fid, str) and fid in remapping:
                    step["fid"] = remapping[fid]
                    replaced.add(fid)
                else:
                    _map_fid(fid)
    if replaced:
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    entry["remapped"] = len(replaced)
    return entry, remaining


def apply_remap_to_ai(repo: Path, report: dict) -> dict:
    """drift_report → .vrp/ai/*.json 一键 remap（F1 产品化，CLI `vrp remap`）。

    unresolved = drift 报出的 missing fid 中，重写后仍被工件引用者
    （多候选/无候选/文件缺失/载荷未含）——需 AI/人工判读，不自动猜测。
    """
    ai_dir = repo / AI_DIR_NAME
    structure_report = report.get("structure") or {}
    struct_remapping: dict[str, str] = dict(structure_report.get("remap") or {})
    struct_missing = sorted(
        e.get("fid") for e in structure_report.get("missing_fids", []) if isinstance(e, dict)
    )
    slice_remapping: dict[str, str] = {}
    for s in report.get("slices", []):
        if isinstance(s, dict):
            slice_remapping.update(s.get("remap") or {})
    slice_missing = sorted({
        e.get("fid")
        for s in report.get("slices", [])
        if isinstance(s, dict)
        for e in s.get("missing_fids", [])
        if isinstance(e, dict)
    })
    struct_entry, struct_remaining = _remap_file(ai_dir / "structure.json", struct_remapping)
    slice_entry, slice_remaining = _remap_file(ai_dir / "slices.json", slice_remapping)
    # 文件缺失/不可解析：无人能落笔重写，missing 全部 unresolved（DB 侧死引用待判读）
    if struct_entry.get("present") and not struct_entry.get("unparseable"):
        struct_entry["unresolved"] = [f for f in struct_missing if f in struct_remaining]
    else:
        struct_entry["unresolved"] = struct_missing
    if slice_entry.get("present") and not slice_entry.get("unparseable"):
        slice_entry["unresolved"] = [f for f in slice_missing if f in slice_remaining]
    else:
        slice_entry["unresolved"] = slice_missing
    result = {
        "structure": struct_entry,
        "slices": slice_entry,
        "total_remapped": struct_entry["remapped"] + slice_entry["remapped"],
        "unresolved_total": len(struct_entry["unresolved"]) + len(slice_entry["unresolved"]),
        "note": "re-run 'vrp curate' to re-ingest remapped artifacts (full re-validation)",
    }
    return result
