"""M16 curate 工件三方合并（设计稿-M16 批准实施，D1-D6 全采纳）。

两代理各自梳理同一仓 → slices/structure 冲突检测+合并（学 git merge
base/ours/theirs 语义权威；OT 冲突分类参照：非冲突自动合，冲突留人工）。

三方：ours=当前库 active 面；theirs=外来产物（.vrp/ai 形态目录或单文件，
复用 curate 输入面）；base=curate_base 覆盖前快照表（curate._snapshot_base
在 ingest 覆盖前自动维护——D1 表载体 / D2 每次覆盖刷新；非严格 git LCA，
误报只偏保守方向，永不静默丢变更）。

决策（设计稿 §3）：
- slices 按 slice_id 六分支：theirs-only 采 / ours==base 采 theirs /
  theirs==base 保 ours / 同值不动 / 双动且异冲突保 ours；
- structure 字段级三方（D4）：modules by module_id / layers by name
  列表项级并集，其余顶层字段整字段三方；合并结果全量校验兜底
  （_validate_structure 不过整体降级冲突，不落半成品）；
- 冲突=decisions 留痕（kind=curate_merge，reject 先例零 schema 改动）
  +库保 ours 不写（D3 零静默丢核心保证）；同冲突重跑不重复留痕（幂等）；
- 相等判定=规范化 JSON 比较（D5：parsed 对象 ==，键序不构成冲突）；
- 单事务原子：坏载荷先全量校验零写入即拒（R-B 同语义）；
  采纳+留痕一起 commit；
- 合并后 F2 复验照跑（_revalidate_ingested）；
- v1 合并面只 slices+structure（D6）；decisions/dossier 不属合并面。

纯标准库；写 facts.db 受 M4 freshness 硬拒（调用方用 _open_facts_rw）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import curate as curate_mod

# 字段缺失哨兵：区别于 JSON null（null 是合法字段值，缺失=键不在）
_MISSING = object()

CONFLICT_KIND = "curate_merge"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read/parse theirs file {path}: {exc}") from exc


def _parse_dict_or_none(payload_json: str) -> dict | None:
    try:
        raw = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


# ------------------------------------------------------------- theirs 输入面


def load_theirs(path: Path) -> dict:
    """读 theirs 产物：目录（slices.json/structure.json）或单文件（形状识别）。"""
    path = Path(path)
    if path.is_dir():
        slices: object = None
        structure: object = None
        found = False
        for filename in ("slices.json", "structure.json"):
            fp = path / filename
            if fp.is_file():
                found = True
                if filename == "slices.json":
                    slices = _read_json(fp)
                else:
                    structure = _read_json(fp)
        if not found:
            raise SystemExit(f"theirs directory has no slices.json/structure.json: {path}")
        return {"slices": slices, "structure": structure, "source": str(path)}
    if path.is_file():
        raw = _read_json(path)
        if isinstance(raw, list):
            return {"slices": raw, "structure": None, "source": str(path)}
        if isinstance(raw, dict):
            return {"slices": None, "structure": raw, "source": str(path)}
        raise SystemExit(f"theirs file must be a JSON array (slices) or object (structure): {path}")
    raise SystemExit(f"theirs path not found: {path}")


def _validate_theirs(con: sqlite3.Connection, theirs: dict) -> None:
    """合并前全量校验 theirs（零写入即拒——R-B 整批语义，坏载荷不进决策）。"""
    slices = theirs.get("slices")
    if slices is not None:
        if not isinstance(slices, list):
            raise SystemExit("theirs slices payload must be a JSON array")
        errors = curate_mod._validate_slice_array(con, slices)
        if errors:
            _reject_theirs(errors)
    structure = theirs.get("structure")
    if structure is not None:
        errors = curate_mod._validate_structure(con, structure)
        if errors:
            _reject_theirs(errors)


def _reject_theirs(errors: list[dict]) -> None:
    lines = ["theirs payload rejected (zero writes):"]
    for e in errors[:10]:
        lines.append(f"  {e.get('code')}: {e.get('detail')}")
    raise SystemExit("\n".join(lines))


# ------------------------------------------------------------ 三方读面


def _active_slices(con: sqlite3.Connection) -> dict[str, tuple[dict, str]]:
    """slice_id -> (parsed payload, raw payload_json)，只收 active 面。

    坏载荷行跳过（诚实：不炸不吞——视同无 ours，their 对该 key 走采纳）。
    """
    out: dict[str, tuple[dict, str]] = {}
    for sid, payload_json in con.execute(
        "SELECT slice_id, payload_json FROM slices WHERE status = 'active'"
    ).fetchall():
        raw = _parse_dict_or_none(payload_json)
        if raw is None:
            continue
        out[sid] = (raw, payload_json)
    return out


def _base_payload(con: sqlite3.Connection, key: str) -> object:
    """curate_base 读面：无行=_MISSING（无祖先知识）；坏载荷同 _MISSING（保守）。"""
    row = con.execute("SELECT payload_json FROM curate_base WHERE key = ?", (key,)).fetchone()
    if row is None:
        return _MISSING
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return _MISSING


# ------------------------------------------------------------ 决策面


def _decide(theirs_obj: object, ours_obj: object, base: object) -> str:
    """六分支三方决策：'adopt' | 'keep' | 'unchanged' | 'conflict'（D5 规范化 ==）。"""
    if ours_obj is _MISSING:
        return "adopt"
    if theirs_obj == ours_obj:
        return "unchanged"
    if base is not _MISSING and theirs_obj == base:
        return "keep"
    if base is not _MISSING and ours_obj == base:
        return "adopt"
    return "conflict"


def _slice_conflict_note(ours_obj: dict, theirs_obj: dict) -> str:
    o_steps = ours_obj.get("steps", [])
    t_steps = theirs_obj.get("steps", [])
    return (
        f"ours: name={ours_obj.get('name', '')!r} steps={len(o_steps)}"
        f" summary={str(ours_obj.get('summary', ''))[:60]!r} |"
        f" theirs: name={theirs_obj.get('name', '')!r} steps={len(t_steps)}"
        f" summary={str(theirs_obj.get('summary', ''))[:60]!r}"
    )


def _structure_conflict_note(conflict_keys: list[str]) -> str:
    return f"field-level merge conflicts: {', '.join(conflict_keys)}"


def _merge_keyed_list(ours: list, theirs: list, base: object, id_key: str) -> tuple[list, list[str]]:
    """modules by module_id / layers by name 的列表项级三方并集。

    保序确定性：ours 序在前（保留/被采项），theirs-only 项按 theirs 序追加。
    冲突项保 ours（若上层整体降级冲突则不落库）。
    """
    base_map: dict = {}
    if isinstance(base, list):
        for item in base:
            if isinstance(item, dict) and isinstance(item.get(id_key), str):
                base_map[item[id_key]] = item
    ours_map: dict = {item[id_key]: item for item in ours}
    theirs_map: dict = {item[id_key]: item for item in theirs}
    ordered = [item[id_key] for item in ours] + [k for k in theirs_map if k not in ours_map]
    merged: list = []
    conflicts: list[str] = []
    for key in ordered:
        o = ours_map.get(key)
        t = theirs_map.get(key)
        if t is None:
            merged.append(o)
            continue
        if o is None:
            merged.append(t)
            continue
        if o == t:
            merged.append(o)
            continue
        b = base_map.get(key)
        if b is not None:
            if t == b:
                merged.append(o)
                continue
            if o == b:
                merged.append(t)
                continue
        conflicts.append(key)
        merged.append(o)
    return merged, conflicts


def _merge_structure(ours: dict, theirs: dict, base: object) -> tuple[dict | None, list[str]]:
    """structure 字段级三方（D4）：modules/layers 列表项级，其余顶层整字段三方。

    返回 (merged, conflict_keys)——conflict 非空时 merged=None（上层降级冲突）。
    """
    base_dict = base if isinstance(base, dict) else _MISSING
    merged: dict = {}
    conflicts: list[str] = []
    ordered_keys = list(ours.keys()) + [k for k in theirs.keys() if k not in ours]
    for key in ordered_keys:
        o = ours.get(key, _MISSING)
        t = theirs.get(key, _MISSING)
        if key in ("modules", "layers"):
            id_key = "module_id" if key == "modules" else "name"
            o_list = o if isinstance(o, list) else []
            t_list = t if isinstance(t, list) else []
            if o is _MISSING:
                merged[key] = t_list
                continue
            if t is _MISSING:
                merged[key] = o_list
                continue
            b = base_dict.get(key, _MISSING) if base_dict is not _MISSING else _MISSING
            list_merged, list_conflicts = _merge_keyed_list(o_list, t_list, b, id_key)
            conflicts.extend(f"{key}:{c}" for c in list_conflicts)
            merged[key] = list_merged
            continue
        if o is _MISSING:
            merged[key] = t
            continue
        if t is _MISSING:
            merged[key] = o
            continue
        if o == t:
            merged[key] = o
            continue
        b = base_dict.get(key, _MISSING) if base_dict is not _MISSING else _MISSING
        if b is not _MISSING:
            if t == b:
                merged[key] = o
                continue
            if o == b:
                merged[key] = t
                continue
        conflicts.append(key)
        merged[key] = o
    if conflicts:
        return None, conflicts
    return merged, []


# ------------------------------------------------------------ 写面（事务内）


def _adopt_slice(con: sqlite3.Connection, raw: dict, prev_payload_json: str | None) -> None:
    """采纳即覆盖：base 刷新（D2）+DELETE+INSERT active——不 commit（merge 控事务）。"""
    if prev_payload_json is not None:
        curate_mod._snapshot_base(con, f"slice:{raw['slice_id']}", prev_payload_json)
    con.execute("DELETE FROM slices WHERE slice_id = ?", (raw["slice_id"],))
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status, created_at)"
        " VALUES (?, ?, ?, ?, 'active', ?)",
        (raw["slice_id"], raw["name"], raw["summary"], json.dumps(raw, ensure_ascii=False), _now()),
    )


def _adopt_structure(con: sqlite3.Connection, raw: dict, prev_payload_json: str | None) -> None:
    if prev_payload_json is not None:
        curate_mod._snapshot_base(con, "structure", prev_payload_json)
    con.execute("DELETE FROM structure")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', ?)",
        (json.dumps(raw, ensure_ascii=False), _now()),
    )


def _record_conflict(con: sqlite3.Connection, target_id: str, note: str) -> bool:
    """冲突留痕（D3）：同冲突重跑不重复留痕（幂等——最新同 note 即跳过）。"""
    last = con.execute(
        "SELECT note FROM decisions WHERE kind = ? AND target_id = ? AND decision = 'conflict'"
        " ORDER BY id DESC LIMIT 1",
        (CONFLICT_KIND, target_id),
    ).fetchone()
    if last is not None and last[0] == note:
        return False
    con.execute(
        "INSERT INTO decisions (kind, target_id, decision, note) VALUES (?, ?, 'conflict', ?)",
        (CONFLICT_KIND, target_id, note),
    )
    return True


# ------------------------------------------------------------ 入口


def run_curate_merge(repo: Path, con: sqlite3.Connection, theirs_path: Path) -> dict:
    """`vrp curate-merge` 入口：校验（零写入）→ 决策 → 单事务写 → F2 复验。"""
    theirs = load_theirs(theirs_path)
    _validate_theirs(con, theirs)

    # ---- slices 三方（六分支）
    ours_slices = _active_slices(con)
    theirs_slice_map: dict[str, dict] = {}
    theirs_slices = theirs.get("slices")
    if theirs_slices:
        for raw in theirs_slices:
            theirs_slice_map[raw["slice_id"]] = raw
    adopted: list[str] = []
    kept: list[str] = []
    unchanged: list[str] = []
    conflict: list[str] = []
    slice_ops: list[tuple[dict, str | None]] = []
    conflict_notes: list[tuple[str, str]] = []
    for sid in sorted(theirs_slice_map):
        t_obj = theirs_slice_map[sid]
        if sid in ours_slices:
            o_obj, o_json = ours_slices[sid]
            action = _decide(t_obj, o_obj, _base_payload(con, f"slice:{sid}"))
            prev_json: str | None = o_json
        else:
            action = "adopt"
            prev_json = None
        if action == "adopt":
            adopted.append(sid)
            slice_ops.append((t_obj, prev_json))
        elif action == "keep":
            kept.append(sid)
        elif action == "unchanged":
            unchanged.append(sid)
        else:
            conflict.append(sid)
            conflict_notes.append((sid, _slice_conflict_note(ours_slices[sid][0], t_obj)))
    kept.extend(sid for sid in ours_slices if sid not in theirs_slice_map)
    kept.sort()

    # ---- structure 三方（字段级 D4 + 校验兜底）
    st_action = "absent"
    st_conflict_keys: list[str] = []
    structure_op: tuple[dict, str | None] | None = None
    theirs_st = theirs.get("structure")
    ours_st_row = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if theirs_st is not None:
        if ours_st_row is None:
            st_action = "adopted"
            structure_op = (theirs_st, None)
        else:
            ours_st_raw = _parse_dict_or_none(ours_st_row[0])
            if ours_st_raw is None:
                st_action = "adopted"
                structure_op = (theirs_st, ours_st_row[0])
            else:
                base_st = _base_payload(con, "structure")
                action = _decide(theirs_st, ours_st_raw, base_st)
                if action == "unchanged":
                    st_action = "unchanged"
                elif action == "keep":
                    st_action = "kept"
                elif action == "adopt":
                    st_action = "adopted"
                    structure_op = (theirs_st, ours_st_row[0])
                else:
                    merged, conflict_keys = _merge_structure(ours_st_raw, theirs_st, base_st)
                    if merged is None:
                        st_action = "conflict"
                        st_conflict_keys = conflict_keys
                        conflict_notes.append(("structure", _structure_conflict_note(conflict_keys)))
                    else:
                        errors = curate_mod._validate_structure(con, merged)
                        if errors:
                            st_action = "conflict"
                            st_conflict_keys = [f"validation:{e.get('code')}" for e in errors[:5]]
                            conflict_notes.append(
                                ("structure", "merged validation failed: " + ",".join(sorted({str(e.get("code")) for e in errors})))
                            )
                        else:
                            st_action = "merged"
                            structure_op = (merged, ours_st_row[0])

    # ---- 写阶段：单事务（采纳+留痕一起 commit；决策阶段零写入）
    for raw, prev_json in slice_ops:
        _adopt_slice(con, raw, prev_json)
    if structure_op is not None:
        _adopt_structure(con, structure_op[0], structure_op[1])
    recorded = 0
    for target_id, note in conflict_notes:
        if _record_conflict(con, target_id, note):
            recorded += 1
    con.commit()

    # 批④ A3 前置：event=curate-merge（三方合并复验时刻同入历史事件流）
    stale = curate_mod._revalidate_ingested(
        con, git_head=curate_mod._meta_git_head(con), event="curate-merge"
    )

    return {
        "merged_at": _now(),
        "theirs": {
            "source": theirs["source"],
            "slices": len(theirs_slice_map),
            "structure": theirs_st is not None,
        },
        "slices": {"adopted": adopted, "kept": kept, "unchanged": unchanged, "conflict": conflict},
        "structure": {"action": st_action, "conflict_keys": st_conflict_keys},
        "conflicts_recorded": recorded,
        "stale": stale,
    }


def render_text(result: dict) -> str:
    lines = [f"curate-merge - {result['theirs']['source']}", ""]
    s = result["slices"]
    lines.append(
        f"slices: adopted {len(s['adopted'])} | kept {len(s['kept'])}"
        f" | unchanged {len(s['unchanged'])} | conflict {len(s['conflict'])}"
    )
    for label in ("adopted", "kept", "unchanged", "conflict"):
        for sid in s[label]:
            lines.append(f"  {label}: {sid}")
    st = result["structure"]
    line = f"structure: {st['action']}"
    if st.get("conflict_keys"):
        line += f" ({', '.join(st['conflict_keys'])})"
    lines.append(line)
    lines.append(f"conflicts recorded in decisions: {result['conflicts_recorded']}")
    if result["slices"]["conflict"] or st["action"] == "conflict":
        lines.append("conflicts kept ours (edit theirs and re-run, or reject ours first)")
    return "\n".join(lines)
