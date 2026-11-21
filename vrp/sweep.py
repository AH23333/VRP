"""S8 阈值敏感度扫描（构想v12 §S8）——阈值×计数曲线+悬崖点。

scikit-learn validation curve 判定学（超参敏感度曲线形态——只学不引）：
同仓多阈值 finding 计数曲线，帮用户选 jscpd minTokens 等阈值。

离线纪律：**不重跑引擎**——扫描基于 findings 表已落盘 payload 值域重切
（纯读侧直方图）：每阈值 t 计数=# findings 值≥t（单调不增）。
可扫面（dogfood 实录锚定）：s2-jscpd payload tokens（P2，1018 条——
本仓"选阈值难"真实案例攒够）；lines 同理。不可扫面诚实记档：
bare-except 等规则面布尔无值域；s2-deadcode/s2-vulture payload 无数值
阈值字段（vulture confidence 未落盘）。

悬崖点=相邻网格点最大绝对落差点（阈值选点参考：悬崖右侧=噪音快速
衰减区）。jscpd minTokens 默认 50（s2_jscpd 单源注释）在 tokens 曲线
标 current。rc 恒 0（分析面非门）。
"""

from __future__ import annotations

import json
import sqlite3

TOKEN_GRID: tuple[int, ...] = tuple(range(20, 201, 10))
LINE_GRID: tuple[int, ...] = tuple(range(5, 51, 5))
JSCPD_MIN_TOKENS_DEFAULT = 50

NON_SWEEPABLE_NOTES = (
    "s2-astgrep rule findings are boolean matches (no numeric threshold field in payload)",
    "s2-deadcode / s2-vulture payloads carry no numeric threshold (vulture confidence not persisted)",
)


def _payload_values(con: sqlite3.Connection, source: str, key: str) -> list[int]:
    """findings payload 值收集（宽容解析：非 dict/缺键/非 int 行跳过）。"""
    out: list[int] = []
    for (raw,) in con.execute(
        "SELECT payload_json FROM findings WHERE source = ?", (source,)
    ):
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out.append(value)
    return out


def sweep(values: list[int], grid: tuple[int, ...]) -> dict:
    """阈值×计数曲线（计数=# 值≥t 单调不增）+悬崖点。"""
    counts = [sum(1 for v in values if v >= t) for t in grid]
    cliff = None
    max_drop = 0
    for i in range(1, len(grid)):
        drop = counts[i - 1] - counts[i]
        if drop > max_drop:
            max_drop = drop
            cliff = {"threshold": grid[i], "drop": drop}
    return {
        "findings": len(values),
        "thresholds": list(grid),
        "counts": counts,
        "cliff": cliff,
    }


def run_sweep(con: sqlite3.Connection) -> dict:
    tokens = _payload_values(con, "s2-jscpd", "tokens")
    lines = _payload_values(con, "s2-jscpd", "lines")
    return {
        "jscpd_min_tokens": {
            **sweep(tokens, TOKEN_GRID),
            "current": JSCPD_MIN_TOKENS_DEFAULT,
        },
        "jscpd_min_lines": sweep(lines, LINE_GRID),
        "notes": [
            "counts = findings with payload value >= threshold (monotone non-increasing; offline re-cut of stored payloads, no engine re-run)",
            *NON_SWEEPABLE_NOTES,
        ],
    }


def render_text(result: dict) -> str:
    lines = ["== sweep =="]
    for title, key, unit in (
        ("jscpd minTokens", "jscpd_min_tokens", "tokens"),
        ("jscpd minLines", "jscpd_min_lines", "lines"),
    ):
        s = result[key]
        lines.append(f"-- {title} ({s['findings']} findings) --")
        peak = max(s["counts"]) if s["counts"] else 0
        current = s.get("current")
        for t, c in zip(s["thresholds"], s["counts"]):
            bar = "#" * int(40 * c / peak) if peak else ""
            mark = " <- current" if current is not None and t == current else ""
            lines.append(f"{t:>4} {unit:<7} {c:>5}  {bar}{mark}")
        if s["cliff"]:
            lines.append(
                f"cliff: biggest drop at threshold {s['cliff']['threshold']} (-{s['cliff']['drop']} findings)"
            )
        else:
            lines.append("cliff: none on grid (flat or empty)")
    lines.append("notes: " + "; ".join(result["notes"]))
    return "\n".join(lines)
