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


def _per_slice_status(raw: list, errors: list[dict]) -> list[dict]:
    """#229 rider（#228 观察兑现）：整批拒绝时的 per-slice 归属面。

    文件级原子性下校验通过的 slice 被同文件他条连带拒（bystander），
    收据 rejected=N 不可见谁真错——LLM/人工需逐条推断。自身有错=
    self_invalid=true；自身无错=连带回滚 false（只需同文件真错者
    修复后整文件重提）。非 dict 条目/坏或空 slice_id 保守判自身错
    （对应无定位早退错误）。
    """
    bad = {e.get("slice_id") for e in errors if isinstance(e, dict) and isinstance(e.get("slice_id"), str)}
    out: list[dict] = []
    for item in raw:
        sid = item.get("slice_id") if isinstance(item, dict) else None
        if not isinstance(sid, str) or not sid.strip():
            out.append({"slice_id": sid if isinstance(sid, str) else None, "self_invalid": True})
            continue
        out.append({"slice_id": sid, "self_invalid": sid in bad})
    return out


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


def _snapshot_base(con: sqlite3.Connection, key: str, payload_json: str) -> None:
    """M16：ingest 覆盖前快照——将被顶掉的旧行搬入 curate_base（每次覆盖刷新）。

    base[k]=「最近一次被 ingest 覆盖前的样子」，是 curate-merge 三方合并的
    对比基线（非严格 git LCA，见设计稿-M16 §2）。调用方保证事务边界。
    """
    con.execute("INSERT OR REPLACE INTO curate_base (key, payload_json) VALUES (?, ?)", (key, payload_json))


def _ingest_slices(con: sqlite3.Connection, items: list[dict]) -> int:
    for raw in items:
        prev = con.execute(
            "SELECT payload_json FROM slices WHERE slice_id = ?", (raw["slice_id"],)
        ).fetchone()
        if prev is not None:
            _snapshot_base(con, f"slice:{raw['slice_id']}", prev[0])
        con.execute("DELETE FROM slices WHERE slice_id = ?", (raw["slice_id"],))
        con.execute(
            "INSERT INTO slices (slice_id, name, summary, payload_json, status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (raw["slice_id"], raw["name"], raw["summary"], json.dumps(raw, ensure_ascii=False), _now()),
        )
    con.commit()
    return len(items)


def _ingest_structure(con: sqlite3.Connection, raw: dict) -> None:
    prev = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if prev is not None:
        _snapshot_base(con, "structure", prev[0])
    con.execute("DELETE FROM structure")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', ?)",
        (json.dumps(raw, ensure_ascii=False), _now()),
    )
    con.commit()


# ----------------------------------------------------------- stale revalidate


def _revalidate_ingested(
    con: sqlite3.Connection, git_head: str = "", event: str = "revalidate"
) -> dict:
    """复验已入库 active 工件：fid/边失效者标 stale（防漂移，v7 §2.2）。

    批④ A3 前置：每次复验向 drift_history 落一行事件（复验统计快照+
    git_head 批次锚；event 区分时刻——etl-full/etl-incremental/curate/
    curate-merge/mcp）。历史跨 etl 经 OWNED_TABLES 存续；外推本体留触发池
    （≥10 批数据后启动），此处只积累数据。
    """
    stale_slices: list[str] = []
    slices_checked = 0
    for slice_id, payload_json in con.execute(
        "SELECT slice_id, payload_json FROM slices WHERE status = 'active'"
    ).fetchall():
        slices_checked += 1
        try:
            raw = json.loads(payload_json)
        except json.JSONDecodeError:
            stale_slices.append(slice_id)
            continue
        if not isinstance(raw, dict):
            # 合法 JSON 非 dict（用户可写 DB/legacy，R-X 族）：坏载荷不可复验，同 stale 语义
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
        ok = isinstance(raw, dict)
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
    _record_drift_history(
        con,
        git_head,
        event,
        {
            "slices_checked": slices_checked,
            "slices_new_stale": len(stale_slices),
            "new_stale_slice_ids": stale_slices,
            "structure_new_stale": structure_stale,
            "slices_active_after": con.execute(
                "SELECT COUNT(*) FROM slices WHERE status = 'active'"
            ).fetchone()[0],
            "slices_stale_after": con.execute(
                "SELECT COUNT(*) FROM slices WHERE status = 'stale'"
            ).fetchone()[0],
            "structure_active_after": con.execute(
                "SELECT COUNT(*) FROM structure WHERE status = 'active'"
            ).fetchone()[0],
        },
    )
    con.commit()
    return {"slices": stale_slices, "structure": structure_stale}


def _record_drift_history(con: sqlite3.Connection, git_head: str, event: str, payload: dict) -> None:
    """A3 前置：drift 历史事件落库（每次复验一行；payload sort_keys 字节确定性）。"""
    con.execute(
        "INSERT INTO drift_history (created_at, git_head, event, payload_json) VALUES (?,?,?,?)",
        (_now(), git_head, event, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )


def _meta_git_head(con: sqlite3.Connection) -> str:
    """git_head 批次锚读出（meta 无键/前 freshness 构建 → '' 诚实）。"""
    row = con.execute("SELECT value FROM meta WHERE key = 'git_head'").fetchone()
    return row[0] if row is not None else ""


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
    receipt["stale"] = _revalidate_ingested(
        con, git_head=_meta_git_head(con), event="curate"
    )
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
            entry["per_slice"] = _per_slice_status(raw, errors)
        else:
            _ingest_slices(con, raw)
            entry["status"], entry["accepted"] = "accepted", len(raw)
    else:
        errors = _validate_structure(con, raw)
        if errors:
            entry["errors"].extend(errors)
            entry["rejected"] = 1
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
            return {"kind": kind, "status": "rejected", "accepted": 0, "rejected": len(raw),
                    "errors": errors, "per_slice": _per_slice_status(raw, errors)}
        _ingest_slices(con, raw)
        return {"kind": kind, "status": "accepted", "accepted": len(raw), "rejected": 0, "errors": [],
                "stale": _revalidate_ingested(con, git_head=_meta_git_head(con), event="mcp")}
    if kind == "structure":
        errors = _validate_structure(con, raw)
        if errors:
            return {"kind": kind, "status": "rejected", "accepted": 0, "rejected": 1, "errors": errors}
        _ingest_structure(con, raw)
        return {"kind": kind, "status": "accepted", "accepted": 1, "rejected": 0, "errors": [],
                "stale": _revalidate_ingested(con, git_head=_meta_git_head(con), event="mcp")}
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


# ---------------------------------------------------------- curate refresh
# F4 确定性 fid 重映射写路径：drift_report 的 remap 提议（F1/F3）在此执行落库。
# fid 轮换两大根因均经 fid_ledger qn 稳定键桥接（qn 跨 etl 稳定，与 schema
# 注释同源）：content_hash 变（文件编辑，同项目 qn 正查即得）与项目名派生
# 变更（qn 剥 ledger 旧项目前缀换当前前缀再正查）。语义原样保留（模块名/
# 职责/层/slice 文案），仅 fid 指针刷新；不可唯一重映射者诚实剔除并计数，
# 空模块剔除，全灭拒做（不产出空 structure）。验证零旁路：重映射载荷走
# _validate_structure/_validate_one_slice 全量重验后才入库。

REFRESH_RECEIPT_NAME = ".vrp/curate-refresh-receipt.json"


def _write_refresh_receipt(repo: Path, receipt: dict) -> None:
    path = repo / REFRESH_RECEIPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _resolve_project(con: sqlite3.Connection) -> str | None:
    """当前项目名（可缺）：meta.cbm_project 优先，缺则 symbols 唯一 project 域兜底。

    读出面安全版（#226 drift 桥用）：不可定 → None（桥跳过），不炸只读面。
    """
    row = con.execute("SELECT value FROM meta WHERE key = 'cbm_project'").fetchone()
    if row is not None and row[0]:
        return row[0]
    rows = con.execute("SELECT DISTINCT project FROM symbols").fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def _current_project(con: sqlite3.Connection) -> str:
    """当前项目名（写路径必需版）：不可定 → SystemExit 诚实拒绝。"""
    project = _resolve_project(con)
    if project is None:
        raise SystemExit(
            "curate-refresh: cannot determine current project"
            " (meta.cbm_project missing and symbols project ambiguous - re-run etl)"
        )
    return project


def _qn_candidates(qn: str, ledger_project: str, cur_project: str) -> list[str]:
    """qn 查找候选序：原样（内容轮换根因）→ 剥 ledger 旧项目前缀换当前前缀
    （项目名派生变更根因——qn 后缀跨项目名稳定）。"""
    cands = [qn]
    if ledger_project and qn.startswith(ledger_project + "."):
        cands.append(cur_project + "." + qn[len(ledger_project) + 1:])
    return cands


def _refresh_remap_fid(con: sqlite3.Connection, fid: str, cur_project: str) -> str | None:
    """死 fid → 活 fid（唯一确定者）；不可唯一重映射 → None（诚实剔除）。

    查找序：仍活→原样；(当前项目, qn 候选序) 正查；多候选时 ledger 记录的
    file_path 唯一同文件者胜；仍歧义/无 → None。
    """
    if con.execute("SELECT 1 FROM symbols WHERE fid = ?", (fid,)).fetchone() is not None:
        return fid
    row = con.execute(
        "SELECT project, qualified_name, file_path FROM fid_ledger WHERE fid = ?", (fid,)
    ).fetchone()
    if row is None:
        return None
    ledger_project, qn, file_path = row
    for cand_qn in _qn_candidates(qn, ledger_project, cur_project):
        rows = con.execute(
            "SELECT fid, file_path FROM symbols WHERE project = ? AND qualified_name = ?",
            (cur_project, cand_qn),
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if len(rows) > 1:
            same = [r for r in rows if r[1] == file_path]
            if len(same) == 1:
                return same[0][0]
    return None


def _refresh_structure_payload(
    con: sqlite3.Connection, raw: dict
) -> tuple[dict, dict[str, str], set[str]]:
    """structure 重映射单件：模块 fid 刷新（空模块剔除/查重重映射去重），
    全量重验通过才入库；fresh（无可重映射改动）不重入库。

    返回 (收据条目, 重映射表, 剔除集)：重映射表=旧 fid → 新 fid、剔除集=
    不可重映射被剔的死 fid（两者仅库内治愈成功时非空；fresh/failed 一律
    空集）——#229 治愈写回磁盘的映射源。
    """
    cur_project = _current_project(con)
    mods_out: list[dict] = []
    remapping: dict[str, str] = {}
    dropped_fids: set[str] = set()
    stats = {"kept_live": 0, "remapped": 0, "dropped": 0, "deduped": 0, "modules_dropped": 0}
    for mod in raw.get("modules", []):
        new_fids: list[str] = []
        for fid in mod.get("symbol_fids") or []:
            new = _refresh_remap_fid(con, fid, cur_project)
            if new is None:
                stats["dropped"] += 1
                dropped_fids.add(fid)
                continue
            if new == fid:
                stats["kept_live"] += 1
            else:
                stats["remapped"] += 1
                remapping[fid] = new
            if new in new_fids:
                stats["deduped"] += 1
                continue
            new_fids.append(new)
        if not new_fids:
            stats["modules_dropped"] += 1
            continue
        fresh_mod = dict(mod)
        fresh_mod["symbol_fids"] = new_fids
        mods_out.append(fresh_mod)
    struct_item: dict = {"action": "refreshed", "project": cur_project, **stats}
    if stats["remapped"] == 0 and stats["dropped"] == 0 and stats["deduped"] == 0 \
            and stats["modules_dropped"] == 0:
        struct_item["action"] = "fresh"
    elif not mods_out:
        struct_item["action"] = "failed"
        struct_item["errors"] = ["all modules unremappable - structure left untouched"]
    else:
        payload = {"modules": mods_out, "layers": raw.get("layers"), "rules": raw.get("rules")}
        errors = _validate_structure(con, payload)
        if errors:
            struct_item["action"] = "failed"
            struct_item["errors"] = errors
        else:
            _ingest_structure(con, payload)
            return struct_item, remapping, dropped_fids
    return struct_item, {}, set()


def _sync_structure_file(path: Path, remapping: dict[str, str], dropped: set[str]) -> dict:
    """#229 治愈写回单件（structure）：指针面同步——remap 替换 + dropped
    剔除 + 空模块同构剔除（镜像库内治愈逻辑，#228 双源脱节根治）；语义面
    （模块名/职责/层/layers/rules）原样保留（最小侵入，不覆盖文件上
    未入库的其他修改）。文件缺失/解析坏/形状不符 → 诚实跳过不写。
    """
    entry: dict = {"file": f"{AI_DIR_NAME}/{path.name}", "present": path.is_file(),
                   "remapped": 0, "dropped": 0}
    if not entry["present"]:
        return entry
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entry["unparseable"] = True
        return entry
    replaced: set[str] = set()
    removed = 0
    if isinstance(raw, dict) and isinstance(raw.get("modules"), list):
        mods_out: list[object] = []
        for mod in raw["modules"]:
            if not isinstance(mod, dict) or not isinstance(mod.get("symbol_fids"), list):
                mods_out.append(mod)
                continue
            new_fids: list[object] = []
            for fid in mod["symbol_fids"]:
                if isinstance(fid, str) and fid in dropped:
                    removed += 1
                    continue
                if isinstance(fid, str) and fid in remapping:
                    replaced.add(fid)
                    new_fids.append(remapping[fid])
                else:
                    new_fids.append(fid)
            if not new_fids:
                continue  # 空模块同构剔除（镜像库内治愈）
            mod["symbol_fids"] = new_fids
            mods_out.append(mod)
        raw["modules"] = mods_out
        if replaced or removed:
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                            encoding="utf-8", newline="\n")
    entry["remapped"] = len(replaced)
    entry["dropped"] = removed
    return entry


def _sync_slices_file(path: Path, maps_by_slice: dict[str, dict[str, str]]) -> dict:
    """#229 治愈写回单件（slices）：按 slice_id 条目精确匹配——只有库内
    治愈成功的 slice 条目内的 step fid 被替换（跨 slice 共享 fid 不误伤
    skipped 条目）；库有文件无的 slice 不新建（不替 LLM 造声明）；
    文件缺失/解析坏/形状不符 → 诚实跳过不写。
    """
    entry: dict = {"file": f"{AI_DIR_NAME}/{path.name}", "present": path.is_file(), "remapped": 0}
    if not entry["present"]:
        return entry
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entry["unparseable"] = True
        return entry
    replaced: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            fid_map = maps_by_slice.get(item.get("slice_id"))
            if not fid_map:
                continue
            steps = item.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if isinstance(step, dict) and isinstance(step.get("fid"), str) and step["fid"] in fid_map:
                    replaced.add(step["fid"])
                    step["fid"] = fid_map[step["fid"]]
        if replaced:
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                            encoding="utf-8", newline="\n")
    entry["remapped"] = len(replaced)
    return entry


def curate_refresh(repo: Path, con: sqlite3.Connection) -> dict:
    """确定性 fid 重映射：structure/slices 死 fid → 活 fid，语义原样保留。

    只动 active/stale 工件（rejected 用户已裁决出列），两者独立处理
    （slices-only 仓合法：structure missing 不拦 slice 刷新）；
    structure 全模块不可重映射时拒做不落库（action=failed 诚实计数）；
    slice 逐步 fid 全可重映射且全量重验通过才刷新，否则原样保留
    （action=skipped 计数）。收据落 .vrp/curate-refresh-receipt.json；
    drift_history 记 event=curate-refresh 事件（与其他写路径同构）。

    治愈写回磁盘（#229，#228 双源脱节根因兑现）：库内治愈发生的
    remap/剔除顺手同步 .vrp/ai 声明文件（_sync_structure_file/
    _sync_slices_file 指针面同步——structure 同构剔除 dropped fid
    与空模块；slices 按 slice_id 条目精确匹配，跨 slice 共享 fid
    不误伤 skipped 条目）；语义面原样保留（最小侵入）；文件缺失/
    解析坏诚实跳过（不替 LLM 造声明）；收据 disk_sync 节记写回
    结果，fresh（无治愈发生）不写不记。
    """
    receipt: dict = {"refreshed_at": _now(), "structure": None, "slices": []}
    struct_remapping: dict[str, str] = {}
    struct_dropped: set[str] = set()
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status IN ('active', 'stale')"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    struct_item: dict
    if row is None:
        struct_item = {"action": "missing"}
    else:
        try:
            raw = json.loads(row[0])
        except json.JSONDecodeError:
            raw = None
        if not isinstance(raw, dict):
            struct_item = {"action": "failed", "errors": ["structure payload unparseable"]}
        else:
            struct_item, struct_remapping, struct_dropped = _refresh_structure_payload(con, raw)
    receipt["structure"] = struct_item
    slice_rows = con.execute(
        "SELECT slice_id, name, status, payload_json FROM slices"
        " WHERE status IN ('active', 'stale') ORDER BY slice_id"
    ).fetchall()
    # 项目名仅在有待刷新工件时求值（空仓收据路径不触发 SystemExit）
    cur_project = _current_project(con) if slice_rows else ""
    slices_payloads: list[dict] = []
    maps_by_slice: dict[str, dict[str, str]] = {}
    for slice_id, name, status, payload_json in slice_rows:
        item: dict = {"slice_id": slice_id, "status": status, "action": "skipped"}
        try:
            sraw = json.loads(payload_json)
        except json.JSONDecodeError:
            sraw = None
        if not isinstance(sraw, dict):
            item["reason"] = "payload unparseable"
        else:
            step_map: dict[str, str] = {}
            new_steps: list[dict] = []
            ok = True
            changed = False
            for step in sraw.get("steps", []):
                new = _refresh_remap_fid(con, step.get("fid", ""), cur_project)
                if new is None:
                    ok = False
                    break
                if new != step.get("fid"):
                    changed = True
                    step_map[step.get("fid", "")] = new
                fresh_step = dict(step)
                fresh_step["fid"] = new
                new_steps.append(fresh_step)
            if not ok:
                item["reason"] = "unremappable step fid"
            elif not changed:
                item["action"] = "fresh"
            else:
                fresh_slice = dict(sraw)
                fresh_slice["steps"] = new_steps
                slice_errors = _validate_one_slice(con, fresh_slice)
                if slice_errors:
                    item["reason"] = slice_errors[0].get("code", "validation error")
                else:
                    item["action"] = "refreshed"
                    slices_payloads.append(fresh_slice)
                    # 治愈成功才领写回映射：skipped/失败 slice 的条目不动
                    maps_by_slice[slice_id] = step_map
        receipt["slices"].append(item)
    if slices_payloads:
        _ingest_slices(con, slices_payloads)
    # 治愈写回磁盘（#229）：指针面同步；文件缺失/解析坏诚实跳过
    disk_sync: dict = {}
    if struct_remapping or struct_dropped:
        disk_sync["structure"] = _sync_structure_file(
            repo / AI_DIR_NAME / "structure.json", struct_remapping, struct_dropped
        )
    if maps_by_slice:
        disk_sync["slices"] = _sync_slices_file(
            repo / AI_DIR_NAME / "slices.json", maps_by_slice
        )
    if disk_sync:
        receipt["disk_sync"] = disk_sync
    _record_drift_history(
        con,
        _meta_git_head(con),
        "curate-refresh",
        {
            "structure_action": struct_item["action"],
            "structure_remapped": struct_item.get("remapped", 0),
            "structure_dropped": struct_item.get("dropped", 0),
            "slices_refreshed": sum(1 for s in receipt["slices"] if s["action"] == "refreshed"),
            "slices_fresh": sum(1 for s in receipt["slices"] if s["action"] == "fresh"),
            "slices_skipped": sum(1 for s in receipt["slices"] if s["action"] == "skipped"),
            "structure_disk_remapped": (disk_sync.get("structure") or {}).get("remapped", 0),
            "structure_disk_dropped": (disk_sync.get("structure") or {}).get("dropped", 0),
            "slices_disk_remapped": (disk_sync.get("slices") or {}).get("remapped", 0),
        },
    )
    con.commit()
    _write_refresh_receipt(repo, receipt)
    return receipt


# ------------------------------------------------------------- drift report
# F3 统一漂移查询面 + F1 读出辅助（R6' 诚实发现，构想v7 §12）：
# - 只读：不改任何 status，标记永远发生在写路径（curate/run_etl/MCP submit）
# - fid_ledger 身份史：旧 fid → (project, qualified_name, file_path) 反查，
#   再按 (project, qn) 正查现行 symbols 得唯一新 fid（remap）；
#   qn 消失（改名/删除）→ ledger 的文件域补位（cap，供 AI 人工判读）
# - 项目名派生变更桥（#226）：ledger 正查 miss 后按 _qn_candidates 候选序
#   以当前项目名再正查（与 curate-refresh 写路径同桥），remap 提议不再全盲
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
    # 项目名派生变更桥（#226）：ledger 正查 miss 后，按 qn 候选序以当前项目名
    # 再正查（与 curate-refresh 写路径同桥 _qn_candidates——读出面对项目名
    # 变更不再全盲掉进 same_file 截断；当前项目名不可定则桥诚实跳过）
    bridge_project = _resolve_project(con)
    if bridge_project is not None:
        for cand_qn in _qn_candidates(qn, project, bridge_project):
            bridged = con.execute(
                "SELECT fid, file_path FROM symbols WHERE project = ? AND qualified_name = ?",
                (bridge_project, cand_qn),
            ).fetchone()
            if bridged is not None:
                entry["candidates"] = [
                    {"fid": bridged[0], "file_path": bridged[1], "match": "qualified_name"}
                ]
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
        raw = None
    if not isinstance(raw, dict):
        # 合法 JSON 非 dict（R-X 族）：与不可解析同语义广义化
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
        raw = None
    if not isinstance(raw, dict):
        # 合法 JSON 非 dict（R-X 族）：与不可解析同语义广义化
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


def drift_history(con: sqlite3.Connection, limit: int = 50) -> dict:
    """A3 前置读出面：drift 复验历史事件（倒序最近 limit 条+总数；只读）。

    旧库无表（批④前构建、尚未经 rw 连接 ensure_schema 自愈）→ 空结果+
    note 诚实标注；payload_json 损坏（用户可写 DB，R-X 族）→ payload_raw
    原样给出不崩。

    #227 A3 到点看盘计数：nonzero_drift_events=全表非零漂移观测数
    （slices_new_stale>0 或 structure_new_stale——revalidate 形态事件专有键；
    curate-refresh 形态（remap 动作收据）无此二键天然不计数；损坏 payload
    诚实跳过）。事件计数是无信号假阳性触发器（12 事件全零实录，交接 #227），
    A3 信号条件（非零观测 ≥5 后再看）只看本值；计数全表非 limit 窗口
    ——看盘问的是积累总量。
    """
    try:
        total = con.execute("SELECT COUNT(*) FROM drift_history").fetchone()[0]
        rows = con.execute(
            "SELECT id, created_at, git_head, event, payload_json FROM drift_history"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {
            "total": 0,
            "events": [],
            "nonzero_drift_events": 0,
            "note": "drift_history 表不存在（批④前构建的 facts.db；下次 rw 连接/etl 自动建表）",
        }
    events: list[dict] = []
    for rid, created_at, git_head, event, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            payload = {"payload_raw": payload_json}
        events.append(
            {
                "id": rid,
                "created_at": created_at,
                "git_head": git_head,
                "event": event,
                "payload": payload,
            }
        )
    nonzero = 0
    for (payload_json,) in con.execute("SELECT payload_json FROM drift_history"):
        try:
            p = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(p, dict):
            continue
        n = p.get("slices_new_stale")
        if (isinstance(n, int) and n > 0) or p.get("structure_new_stale") is True:
            nonzero += 1
    return {"total": total, "events": events, "nonzero_drift_events": nonzero}


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
