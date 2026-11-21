"""R2（构想v12）：co-change 隐式耦合边——git log 共现挖掘入一等公民表。

学 code-maat/Tornhill temporal coupling，公式全开放（S6"可解释"哲学）：
    support(a,b)     = a、b 同 commit 共现次数（--no-merges：merge 的文件
                       集是冲突消解产物，非真实共变信号）
    changes(a)       = a 出现过的 commit 数
    confidence(a->b) = support(a,b) / changes(a)——"改 a 时 b 也改了"的条件概率

- 输入 `git log --no-merges --name-only --pretty=format:%x1f%H%x1f%aI`
  （US 分隔避免路径/哈希歧义；只 HEAD 历史——复现性红线：同 HEAD 同产物）。
- 行规范 file_a < file_b 字典序（无序对唯一行）；文件路径键而非 fid
  （路径跨 etl 稳定——runtime_calls qn 键 / fid_ledger 同哲学）。
- 无时间戳列：数据自带 commit 时间（first/last_co_change）——同 facts.db
  字节级同产物。
- 幂等 = DELETE+INSERT 全量替换（#62 教训：当前状态语义，重跑零膨胀）。
- 挖掘=写路径（CLI 层 _open_facts_rw，freshness M4 硬拒照旧）；呈现=读路径
  （阈值只过滤展示面，表存全量对——阈值改动零重挖）。
- 诚实局限：不做 rename 追踪（--follow 是单文件 API 不适用全量对）；纯共现
  非因果（诊断信号非裁决依据——v_hotspots fan_in_same_file_pct 同定位）；
  bulk commit 抬高全仓 support，由 --min-support/--min-confidence 调。
- 叙事定位：CALLS/CALL_REFERENCE 是显式依赖，co_changes 是隐式耦合——
  两图分歧即发现（"无调用边却总是同改"= 隐藏耦合；"有调用边从不共变" =
  稳定接口）。dossier/slices 消费面接入各自立项（设计稿 §5）。
"""

from __future__ import annotations

import sqlite3
from itertools import combinations
from pathlib import Path

from . import procs
from . import timeline as timeline_mod

DEFAULT_MIN_SUPPORT = 3
DEFAULT_MIN_CONFIDENCE = 0.3
DEFAULT_LIMIT = 50
GIT_LOG_TIMEOUT = 60
GIT_LOG_FORMAT = "%x1f%H%x1f%aI"


def parse_log(raw: str) -> list[dict]:
    """git log 原始输出 → commit 列表（git log 序：新→旧）。

    每元素 {"date": ISO 时间, "files": [去重+规范化+排序后的路径]}。
    解析单源 = timeline.parse_history（同 US 格式同 GIT_LOG_FORMAT——
    #214 深度审查统一 jaccard 0.953 重复面）；cochange 面只取
    date+files（sha 不入共现语义）。
    """
    return [
        {"date": c["date"], "files": c["files"]} for c in timeline_mod.parse_history(raw)
    ]


def mine(raw: str) -> dict:
    """共现挖掘：返回 {"commits", "changes", "pairs"}（公式见模块 docstring）。

    确定性：per-commit 文件排序 → combinations 键恒 (a, b) 且 a < b；
    first/last_co_change 按 git log 遍历序取值（新→旧：首见即最新 =
    last_co_change，持续覆写成最旧 = first_co_change）——序取值而非字符串
    比较，对 %aI 混合时区偏移（+08:00 vs +02:00 字典序不可比）稳健。
    """
    commits = parse_log(raw)
    changes: dict[str, int] = {}
    support: dict[tuple[str, str], int] = {}
    first_seen: dict[tuple[str, str], str] = {}
    last_seen: dict[tuple[str, str], str] = {}
    for c in commits:
        for f in c["files"]:
            changes[f] = changes.get(f, 0) + 1
        for a, b in combinations(c["files"], 2):
            key = (a, b)
            support[key] = support.get(key, 0) + 1
            if key not in last_seen:
                last_seen[key] = c["date"]
            first_seen[key] = c["date"]
    pairs: dict[tuple[str, str], dict] = {}
    for key in sorted(support):
        a, b = key
        s = support[key]
        pairs[key] = {
            "support": s,
            "changes_a": changes[a],
            "changes_b": changes[b],
            "confidence_ab": s / changes[a],
            "confidence_ba": s / changes[b],
            "first_co_change": first_seen[key],
            "last_co_change": last_seen[key],
        }
    return {"commits": len(commits), "changes": changes, "pairs": pairs}


def persist(con: sqlite3.Connection, mined: dict) -> int:
    """幂等落库：DELETE + INSERT 全量替换（键序写入——字节级复现）。

    返回写入行数；空挖掘同样落库（清残——表内容恒与当前 HEAD 历史一致）。
    """
    con.execute("DELETE FROM co_changes")
    rows = [
        (
            a,
            b,
            p["support"],
            p["changes_a"],
            p["changes_b"],
            p["confidence_ab"],
            p["confidence_ba"],
            p["first_co_change"],
            p["last_co_change"],
        )
        for (a, b), p in sorted(mined["pairs"].items())
    ]
    con.executemany(
        "INSERT INTO co_changes (file_a, file_b, support, changes_a, changes_b,"
        " confidence_ab, confidence_ba, first_co_change, last_co_change)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    return len(rows)


def query_pairs(
    con: sqlite3.Connection,
    min_support: int = DEFAULT_MIN_SUPPORT,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], int]:
    """呈现=读路径：阈值过滤（>= 边界含等）→ 排序 → 截断。

    过滤/排序用原始值（confidence_max = max(ab, ba)，Tornhill 语义：
    任一方向够强即耦合）；浮点仅展示（round 4，#159 同哲学）。
    排序 (support DESC, confidence_max DESC, file_a, file_b)——整数键
    优先，浮点只作次级，字符串键收尾保证确定性。
    返回 (展示列表, 阈值后幸存数)——幸存数在截断前计（诚实计数）。
    """
    filtered: list[dict] = []
    for a, b, s, ca, cb, cab, cba, first, last in con.execute(
        "SELECT file_a, file_b, support, changes_a, changes_b, confidence_ab,"
        " confidence_ba, first_co_change, last_co_change FROM co_changes"
    ).fetchall():
        cmax = max(cab, cba)
        if s < min_support or cmax < min_confidence:
            continue
        filtered.append(
            {
                "file_a": a,
                "file_b": b,
                "support": s,
                "changes_a": ca,
                "changes_b": cb,
                "confidence_ab": cab,
                "confidence_ba": cba,
                "confidence_max": cmax,
                "first_co_change": first,
                "last_co_change": last,
            }
        )
    filtered.sort(
        key=lambda e: (-e["support"], -e["confidence_max"], e["file_a"], e["file_b"])
    )
    surviving = len(filtered)
    shown = [
        {
            **e,
            "confidence_ab": round(e["confidence_ab"], 4),
            "confidence_ba": round(e["confidence_ba"], 4),
            "confidence_max": round(e["confidence_max"], 4),
        }
        for e in filtered[: max(limit, 0)]
    ]
    return shown, surviving


def run_cochange(
    repo: Path,
    con: sqlite3.Connection,
    min_support: int = DEFAULT_MIN_SUPPORT,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """`vrp cochange` 入口：挖掘（写）→ 呈现（读）一体，幂等可重跑。

    git log 超时响亮 RuntimeError（测量失败不是零耦合——静默空结果是谎言）；
    非 git 仓/空历史（rc!=0）→ 诚实空结果（commits=0，仍清残写空表）。
    """
    repo = Path(repo).resolve()
    res = procs.run_tree(
        ["git", "log", "--no-merges", "--name-only", f"--pretty=format:{GIT_LOG_FORMAT}"],
        cwd=repo,
        timeout=GIT_LOG_TIMEOUT,
    )
    if res["timed_out"]:
        raise RuntimeError(
            f"git log timed out after {GIT_LOG_TIMEOUT}s (tree killed): {res['stderr'][:300]}"
        )
    mined = (
        mine(res["stdout"])
        if res["returncode"] == 0
        else {"commits": 0, "changes": {}, "pairs": {}}
    )
    stored = persist(con, mined)
    shown, surviving = query_pairs(con, min_support, min_confidence, limit)
    summary = {
        "commits": mined["commits"],
        "files": len(mined["changes"]),
        "pairs": stored,
        "surviving_pairs": surviving,
        "min_support": min_support,
        "min_confidence": min_confidence,
    }
    return {
        "repo": str(repo),
        "summary": summary,
        "limit": limit,
        "shown": len(shown),
        "pairs": shown,
    }


def render_text(result: dict) -> str:
    """人读呈现（chrome 英文，惯例同报告链）。"""
    s = result["summary"]
    lines = [f"vrp cochange - {Path(result['repo']).name}"]
    lines.append(f"  commits     : {s['commits']} scanned (no merges)")
    lines.append(f"  files       : {s['files']} distinct")
    lines.append(f"  pairs       : {s['pairs']} stored")
    lines.append(
        f"  thresholds  : min-support {s['min_support']},"
        f" min-confidence {s['min_confidence']}"
        f" -> {s['surviving_pairs']} surviving, showing {result['shown']}"
    )
    for e in result["pairs"]:
        lines.append(
            f"  {e['support']:>5}  conf {e['confidence_ab']:.4f}/{e['confidence_ba']:.4f}"
            f"  {e['file_a']} <-> {e['file_b']}"
            f"  [{e['first_co_change'][:10]} .. {e['last_co_change'][:10]}]"
        )
    return "\n".join(lines)
