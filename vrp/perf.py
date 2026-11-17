"""J10 管道性能回归追踪（构想v11 甜点，rustc perf tracker 思想 + hyperfine 形态参照）。

设计三要点（#170）：
- 零额外命令落档：`vrp run` 四阶段（precheck/cbm_index/etl/xcheck）monotonic
  计时，run 结束自动追加一行 JSONL 到 `.vrp/perf.jsonl`——记录不靠人想起
  （rustc perf tracker：每次运行都是样本）。
- 批次对账：`vrp perf` 读 JSONL，last vs previous 逐阶段 delta 百分比；
  回归旗标双门 = 相对门（+REGRESSION_PCT）且绝对门（+REGRESSION_ABS_S）——
  小基数噪声（0.2s→0.3s=+50%）不误报。
- 诚实边界：单样本对比非 N 次中位（hyperfine 会跑 N 轮）——noise_note 明示；
  rc 恒 0（报告面不是门面，回归是信号不是错误）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REGRESSION_PCT = 25.0
REGRESSION_ABS_S = 1.0

STAGE_ORDER = ("precheck", "cbm_index", "etl", "xcheck", "total")

NOISE_NOTE = (
    "single-sample comparison - timings are noisy (hyperfine would take"
    " medians over N runs); treat small deltas as noise"
)


def perf_path(repo: Path) -> Path:
    return repo / ".vrp" / "perf.jsonl"


def record_run(repo: Path, stages: dict[str, float], git_head: str | None = None, meta: dict | None = None) -> dict:
    """追加一行 JSONL（时间戳+git_head+各阶段秒+meta）；返回落档条目。"""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": git_head,
        "stages": {k: round(v, 3) for k, v in stages.items()},
    }
    if meta:
        entry["meta"] = meta
    path = perf_path(repo)
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_entries(repo: Path) -> tuple[list[dict], int]:
    """读 JSONL；坏行诚实跳过并计数（不静默吞、不裸崩）。"""
    path = perf_path(repo)
    if not path.exists():
        return [], 0
    entries: list[dict] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("stages"), dict):
            bad += 1
            continue
        entries.append(raw)
    return entries, bad


def _delta_pct(latest: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return round((latest - previous) / previous * 100, 1)


def _is_regression(latest: float, previous: float) -> bool:
    pct = _delta_pct(latest, previous)
    if pct is None:
        return False
    return pct > REGRESSION_PCT and (latest - previous) > REGRESSION_ABS_S


def run_perf(repo: Path) -> dict:
    entries, bad = load_entries(repo)
    if not entries:
        raise SystemExit(
            f"no perf history - run 'vrp run' first (records to {perf_path(repo)})"
        )
    latest = entries[-1]
    result: dict = {
        "entries": len(entries),
        "bad_lines": bad,
        "latest": latest,
        "noise_note": NOISE_NOTE,
        "stages": [],
        "regressions": [],
    }
    if len(entries) < 2:
        result["previous"] = None
        result["note"] = "single entry - no previous run to compare"
        return result
    previous = entries[-2]
    result["previous"] = previous
    stage_names = sorted(set(latest["stages"]) | set(previous["stages"]))
    for stage in stage_names:
        lv = float(latest["stages"].get(stage, 0.0) or 0.0)
        pv = float(previous["stages"].get(stage, 0.0) or 0.0)
        item = {
            "stage": stage,
            "latest_s": round(lv, 3),
            "previous_s": round(pv, 3),
            "delta_pct": _delta_pct(lv, pv),
        }
        item["regression"] = _is_regression(lv, pv)
        if item["regression"]:
            result["regressions"].append(stage)
        result["stages"].append(item)
    return result


def render_text(result: dict) -> str:
    latest = result["latest"]
    lines = [
        "vrp perf - 管道性能批次对账",
        f"  entries: {result['entries']}  bad_lines: {result['bad_lines']}",
        f"  latest : {latest.get('ts')}  git_head: {(latest.get('git_head') or '-')[:12]}",
    ]
    if result.get("previous") is None:
        lines.append(f"  note   : {result['note']}")
        for stage, sec in sorted(latest["stages"].items()):
            lines.append(f"  {stage:<12} {sec:>10.3f}s")
        return "\n".join(lines)
    prev = result["previous"]
    lines.append(f"  prev   : {prev.get('ts')}  git_head: {(prev.get('git_head') or '-')[:12]}")
    for item in result["stages"]:
        pct = item["delta_pct"]
        pct_txt = f"{pct:+.1f}%" if pct is not None else "n/a"
        flag = "  [regression]" if item["regression"] else ""
        lines.append(
            f"  {item['stage']:<12} {item['latest_s']:>10.3f}s  (prev {item['previous_s']:>9.3f}s, {pct_txt}){flag}"
        )
    if result["regressions"]:
        lines.append(f"  regressions: {', '.join(result['regressions'])}")
    else:
        lines.append("  regressions: -")
    lines.append(f"  note: {result['noise_note']}")
    return "\n".join(lines)
