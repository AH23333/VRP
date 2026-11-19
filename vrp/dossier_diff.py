"""A2（构想v10）：dossier diff 报告——两次 run 的 dossier 对账。

递归 dict/list 对账 → 新增/消除/变化清单——release notes 自动素材
（v0.1.2 手工做的活，构想原文）。轮子判定：**绕**（deepdiff 不引入——
dossier schema 自有，dict 递归对账 ~百行纯 stdlib；jd/json-diff 输出
形态参照）。

对账语义（比 jd 多一层——release notes 要的是"新增/消除病理"不是
"索引 3 变了"）：
- dict 逐键递归：old 独有=removed、new 独有=added、共有=递归；
- **列表身份键对齐**：dossier 的列表多为带身份键的 dict 数组
  （hotspots 的 symbol/dup_clusters 的 fid/file_path 等）——按身份键
  对齐后新增病理=added、消除病理=removed、同键递归值，**列表头插不
  产生整列假 changed**；身份键偏好序 deterministic（id/fid/name/
  symbol/path/file_path/anchor/file/qn/kind 取第一个双列表全元素具备
  的键）；无公共身份键回退逐位对齐（纯标量列的诚实形态）；
- 例外面：`generated_at`（时间戳恒变——纯噪音）按路径前缀排除。

路径记法 `$.views.summary.hotspots[3]`（JSONPath 子集，人可读可 grep）。
rc 语义：报告面恒 0（变化是信息不是错误）。确定性：纯函数零 IO 副作用
（读两份 JSON 出一份报告，不写文件）。
"""

from __future__ import annotations

import json
from pathlib import Path

# 路径前缀排除面：dossier 根的时间戳恒变，对账纯噪音
IGNORE_PATHS: tuple[tuple[str, ...], ...] = (("generated_at",),)
# 列表身份键偏好序（deterministic——不取 dict 迭代序）
_ID_KEYS = ("id", "fid", "name", "symbol", "path", "file_path", "anchor", "file", "qn", "kind")


def _ignored(path: tuple[str, ...]) -> bool:
    return any(path[: len(p)] == p for p in IGNORE_PATHS)


def _list_id_key(old: list, new: list) -> str | None:
    """双列表全元素具备且值可哈希的身份键（偏好序取首个）；无则 None。"""
    for key in _ID_KEYS:
        ok = True
        for el in old + new:
            if not isinstance(el, dict) or not isinstance(el.get(key), (str, int)):
                ok = False
                break
        if ok and old and new:
            return key
    return None


def _fmt(path: tuple[str, ...]) -> str:
    """路径记法：$.a.b[3]（JSONPath 子集）。"""
    out = "$"
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def diff_json(old: object, new: object, path: tuple = ()) -> list[dict]:
    """递归对账核心：返回变更列表（op ∈ added/removed/changed）。
    added/removed 带 value；changed 带 old/new。纯函数。"""
    if _ignored(path):
        return []
    if type(old) is not type(new):
        return [{"op": "changed", "path": _fmt(path), "old": old, "new": new}]
    if isinstance(old, dict):
        changes: list[dict] = []
        for key in sorted(old.keys() | new.keys()):
            child = (*path, key)
            if key not in old:
                changes.append({"op": "added", "path": _fmt(child), "value": new[key]})
            elif key not in new:
                changes.append({"op": "removed", "path": _fmt(child), "value": old[key]})
            else:
                changes.extend(diff_json(old[key], new[key], child))
        return changes
    if isinstance(old, list):
        id_key = _list_id_key(old, new)
        if id_key is not None:
            return _diff_list_by_key(old, new, id_key, path)
        changes = []
        for i in range(max(len(old), len(new))):
            child = (*path, i)
            if i >= len(old):
                changes.append({"op": "added", "path": _fmt(child), "value": new[i]})
            elif i >= len(new):
                changes.append({"op": "removed", "path": _fmt(child), "value": old[i]})
            else:
                changes.extend(diff_json(old[i], new[i], child))
        return changes
    if old != new:
        return [{"op": "changed", "path": _fmt(path), "old": old, "new": new}]
    return []


def _diff_list_by_key(old: list, new: list, id_key: str, path: tuple) -> list[dict]:
    """身份键对齐：new 独有键=added、old 独有键=removed、共有键递归。
    键冲突（同键多元素）取首个——dossier 列表身份键唯一是 schema 事实，
    冲突形态诚实取首不猜。"""
    old_map: dict = {}
    for el in old:
        old_map.setdefault(el[id_key], el)
    new_map: dict = {}
    for el in new:
        new_map.setdefault(el[id_key], el)
    changes: list[dict] = []
    for key in sorted(old_map.keys() | new_map.keys(), key=repr):
        child = (*path, key)
        if key not in old_map:
            changes.append({"op": "added", "path": _fmt(child), "value": new_map[key]})
        elif key not in new_map:
            changes.append({"op": "removed", "path": _fmt(child), "value": old_map[key]})
        else:
            changes.extend(diff_json(old_map[key], new_map[key], child))
    return changes


def _load(path: Path) -> dict:
    """JSON 载入（顶层须对象——dossier schema 事实）；坏文件诚实退出。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot load {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top level must be a JSON object, got {type(data).__name__}")
    return data


def run_dossier_diff(old_path: Path, new_path: Path) -> dict:
    """`vrp dossier-diff` 入口。"""
    old_path, new_path = Path(old_path), Path(new_path)
    old, new = _load(old_path), _load(new_path)
    changes = diff_json(old, new)
    counts = {"added": 0, "removed": 0, "changed": 0}
    for c in changes:
        counts[c["op"]] += 1
    return {
        "old": str(old_path),
        "new": str(new_path),
        "summary": counts,
        "changes": changes,
    }


def render_text(result: dict) -> str:
    """人读呈现：按 op 分组（release notes 素材序：新增→消除→变化）。"""
    s = result["summary"]
    lines = [
        f"vrp dossier-diff - {Path(result['old']).name} -> {Path(result['new']).name}",
        f"  {s['added']} added, {s['removed']} removed, {s['changed']} changed",
    ]
    for op, label in (("added", "ADDED"), ("removed", "REMOVED"), ("changed", "CHANGED")):
        group = [c for c in result["changes"] if c["op"] == op]
        if not group:
            continue
        lines.append(f"  {label} ({len(group)}):")
        for c in group:
            if op == "changed":
                lines.append(f"    {c['path']}: {json.dumps(c['old'], ensure_ascii=False)} -> {json.dumps(c['new'], ensure_ascii=False)}")
            else:
                v = json.dumps(c["value"], ensure_ascii=False)
                lines.append(f"    {c['path']}: {v[:120]}")
    return "\n".join(lines)
