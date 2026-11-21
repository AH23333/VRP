"""C8 层度量与阈值（构想v10 §C8）——扇入扇出+JDepend 稳定性半面。

JDepend 判定学（D 度量公式）+deptrac 判定学（阈值语义）照学，规则仍走
structure JSON 声明（不引入 DSL 解析面）：

- fan_in（Ca 传入耦合）/fan_out（Ce 传出耦合）：**模块间去重边对**计数
  （DISTINCT (src_fid, dst_fid)——类依赖语义非调用次数；同 fid 可属多模块
  （s7 先例），模块对全组合计入；两端均在 structure 符号宇宙内——
  内建/未分配符号噪声地板排除记档）；
- instability I = Ce/(Ca+Ce)（Martin 稳定性半面）；孤立模块（Ca+Ce==0）
  I=None 诚实未定义；
- abstractness A **不可推导**（facts.db 无修饰符数据——BB2 同源边界：
  properties_json 只有 complexity/lines/is_exported 等七键无 abstract/
  interface 标志）→ D=|A+I-1| 半面记档不造数；
- thresholds：structure payload 可选顶层键（curate 校验不拒未知键——
  纯数据面），形如 {"modules": {"<module_id>": {"max_fan_in": n,
  "max_fan_out": n, "max_instability": x}}, "layers": {...}}；
- 违例 rc=1（deptrac 阈值语义 CI 可用）；无 thresholds 声明=纯度量报告
  rc=0；无 structure（未 curate）诚实提示 rc=0（度量面非门）；
- 命名约定规则面**缓议**（v10 自注"表达力按需扩，避免过度设计"——
  无真实需求案例不造轮）。
"""

from __future__ import annotations

import json
import sqlite3

VALID_THRESHOLD_KEYS = ("max_fan_in", "max_fan_out", "max_instability")


def _load_payload(con: sqlite3.Connection) -> dict | None:
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _memberships(payload: dict) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """fid → {module_id} / fid → {layer} 两映射（多属容许——s7 先例）。"""
    fid_to_modules: dict[str, set[str]] = {}
    fid_to_layers: dict[str, set[str]] = {}
    for mod in payload.get("modules", []):
        if not isinstance(mod, dict):
            continue
        mid = mod.get("module_id")
        fids = mod.get("symbol_fids")
        layer = mod.get("layer")
        if not isinstance(mid, str) or not isinstance(fids, list):
            continue
        for fid in fids:
            if isinstance(fid, str):
                fid_to_modules.setdefault(fid, set()).add(mid)
                if isinstance(layer, str):
                    fid_to_layers.setdefault(fid, set()).add(layer)
    return fid_to_modules, fid_to_layers


def compute_metrics(con: sqlite3.Connection, payload: dict) -> dict:
    """per-module/per-layer fan_in/fan_out/I——模块间去重边对。"""
    fid_to_modules, fid_to_layers = _memberships(payload)
    module_ids = sorted({m for mods in fid_to_modules.values() for m in mods})
    layer_names = sorted({l for ls in fid_to_layers.values() for l in ls})
    mod_seen: set[tuple[str, str, str, str]] = set()
    layer_seen: set[tuple[str, str, str, str]] = set()  # 与 mod_seen 分域——名字撞车不互蔽
    m_fan_in = {m: 0 for m in module_ids}
    m_fan_out = {m: 0 for m in module_ids}
    l_fan_in = {l: 0 for l in layer_names}
    l_fan_out = {l: 0 for l in layer_names}
    for src, dst in con.execute("SELECT DISTINCT src_fid, dst_fid FROM edges"):
        if src == dst:
            continue
        for sm in fid_to_modules.get(src, ()):
            for dm in fid_to_modules.get(dst, ()):
                if sm == dm or (src, dst, sm, dm) in mod_seen:
                    continue
                mod_seen.add((src, dst, sm, dm))
                m_fan_out[sm] += 1
                m_fan_in[dm] += 1
        for sl in fid_to_layers.get(src, ()):
            for dl in fid_to_layers.get(dst, ()):
                if sl == dl or (src, dst, sl, dl) in layer_seen:
                    continue
                layer_seen.add((src, dst, sl, dl))
                l_fan_out[sl] += 1
                l_fan_in[dl] += 1

    def _rows(names: list[str], fan_in: dict[str, int], fan_out: dict[str, int]) -> list[dict]:
        out = []
        for name in names:
            ca, ce = fan_in[name], fan_out[name]
            instability = round(ce / (ca + ce), 4) if (ca + ce) else None
            out.append(
                {
                    "name": name,
                    "fan_in": ca,
                    "fan_out": ce,
                    "instability": instability,
                }
            )
        return out

    return {
        "modules": _rows(module_ids, m_fan_in, m_fan_out),
        "layers": _rows(layer_names, l_fan_in, l_fan_out),
        "notes": [
            "fan_in/fan_out = inter-scope deduped edge pairs (class-dependency semantics, not call counts)",
            "abstractness not derivable (no modifier data in facts.db - BB2 boundary); D=|A+I-1| half-face: instability only",
        ],
    }


def check_thresholds(metrics: dict, payload: dict) -> tuple[list[dict], bool]:
    """structure 可选 thresholds 键 → 违例清单（deptrac 阈值语义）。

    返回 (violations, thresholds_declared)。声明缺省/形状不合=未声明
    （不猜不造——宽容面：非法声明行跳过）。
    """
    raw = payload.get("thresholds")
    if not isinstance(raw, dict):
        return [], False
    violations: list[dict] = []
    for scope, rows in (("module", metrics["modules"]), ("layer", metrics["layers"])):
        # payload 键 "modules"/"layers"；scope 单数只用于报告字段
        key = "modules" if scope == "module" else "layers"
        declared = raw.get(key)
        if not isinstance(declared, dict):
            continue
        for row in rows:
            limits = declared.get(row["name"])
            if not isinstance(limits, dict):
                continue
            for metric_key, attr in (
                ("max_fan_in", "fan_in"),
                ("max_fan_out", "fan_out"),
                ("max_instability", "instability"),
            ):
                limit = limits.get(metric_key)
                if not isinstance(limit, (int, float)) or isinstance(limit, bool):
                    continue
                value = row[attr]
                if value is None:  # 孤立 scope 的 instability——无值不判
                    continue
                if value > limit:
                    violations.append(
                        {
                            "scope": scope,
                            "name": row["name"],
                            "metric": attr,
                            "value": value,
                            "limit": limit,
                        }
                    )
    violations.sort(key=lambda v: (v["scope"], v["name"], v["metric"]))
    return violations, True


def run_layer_metrics(con: sqlite3.Connection) -> dict:
    payload = _load_payload(con)
    if payload is None:
        # s7 同语义只消费 active；区分"从未 curate"与"已陈旧"（symbols 漂移后失效）
        row = con.execute("SELECT status FROM structure ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0] != "active":
            note = f"structure 已陈旧（status={row[0]}，symbols 漂移后失效）——重跑 'vrp curate-auto' 刷新结构（度量面非门）"
        else:
            note = "structure 未 curate——先跑 'vrp curate-auto'（度量面非门）"
        return {
            "structure": False,
            "modules": [],
            "layers": [],
            "violations": [],
            "thresholds_declared": False,
            "notes": [note],
        }
    metrics = compute_metrics(con, payload)
    violations, declared = check_thresholds(metrics, payload)
    return {
        "structure": True,
        **metrics,
        "violations": violations,
        "thresholds_declared": declared,
    }


def render_text(result: dict) -> str:
    lines = ["== layermetrics =="]
    if not result["structure"]:
        lines.append(result["notes"][0])
        return "\n".join(lines)
    for scope, rows in (("module", result["modules"]), ("layer", result["layers"])):
        if not rows:
            continue
        lines.append(f"-- {scope}s --")
        for row in rows:
            inst = row["instability"]
            inst_s = f"{inst:.4f}" if inst is not None else "-"
            lines.append(f"{row['name']}: fan_in={row['fan_in']} fan_out={row['fan_out']} I={inst_s}")
    if result["violations"]:
        lines.append("-- threshold violations --")
        for v in result["violations"]:
            lines.append(
                f"[FAIL] {v['scope']} {v['name']}: {v['metric']}={v['value']} exceeds limit {v['limit']}"
            )
    else:
        note = "thresholds declared: no violations" if result["thresholds_declared"] else "no thresholds declared (metrics report only)"
        lines.append(f"thresholds: {note}")
    return "\n".join(lines)
