"""#144 B 件：运行时调用证据 vrp traces（学 CBM ingest_traces）。

CBM ingest_traces（src/mcp/mcp.c :860）记录**观测到的运行时调用**以区分
observed vs merely possible paths：trace = qn 调用路径（相邻 = caller→callee）
或裸 caller/callee 对 + count；per run label 存储、qn 键（reindex-proof——
fid 随 content_hash 轮换，qn 跨 etl 稳定 = fid_ledger 同哲学）；只存两端都
能解析到现行符号的对，未解析名诚实上报（cap 10）不静默丢。

VRP 落地：
- 输入 traces JSON：{"traces": [{"path": [qn, ...]} | {"caller": qn,
  "callee": qn, "count": n}], "label": "..."}——生产者外部（pytest 插件/
  profiler/agent 手记；**采集器不在本批**——VRP 只记录外部观测，与 freeze
  记录命令输出同哲学）。
- 存 runtime_calls 表（qn 键）；per label 整批替换（同 label 重导入 =
  DELETE 后 INSERT，幂等——CBM "whole runs age out together" 对应）；
  跨 etl 经 OWNED_TABLES 快照回灌存续（qn 键 reindex-proof）。
- 对账三分类（导入收据附 agreement 面）：agreed（观测对且静态
  SLICE_EDGE_TYPES 有边）/ observed_only（观测但静态无边——动态路径
  静态分析漏了，xcheck ca_only 同型信号）/ static_only（静态有边从未
  观测——理论可达未发生，dead-code/coverage 佐证）。

红线：观测证据独立表，**不并入静态 facts**（CALLS 边/视图零动——静态
分析本体红线）；纯 stdlib；写入经 CLI `_open_facts_rw`（M4 新鲜度硬闸
同族——facts 陈旧时拒绝写入，先重跑 etl）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .curate import SLICE_EDGE_TYPES

UNRESOLVED_SAMPLE_CAP = 10
AGREEMENT_SAMPLE_CAP = 5
TRACE_PATH_MIN = 2
TRACE_PATH_MAX = 256
DEFAULT_LABEL = "unlabeled"


def _load_traces_file(file_path: Path) -> dict:
    """读 traces JSON 文件；形状守卫（traces 数组 + 可选 label）。"""
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"traces: {file_path} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise SystemExit(f"traces: {file_path} must be a JSON object with a 'traces' array")
    traces = raw.get("traces")
    if not isinstance(traces, list):
        raise SystemExit(f"traces: {file_path} 'traces' must be an array")
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise SystemExit(f"traces: {file_path} 'label' must be a string")
    return {"traces": traces, "label": label}


def _pair_counts(traces: list) -> dict[tuple[str, str], int]:
    """traces 数组 → (caller_qn, callee_qn) 计数：路径展开相邻对+裸对累计。

    形状违例整文件拒绝（诚实失败优于部分采信——curate 原子性同哲学）。
    """
    counts: dict[tuple[str, str], int] = {}
    for i, item in enumerate(traces):
        if not isinstance(item, dict):
            raise SystemExit(f"traces[{i}]: must be an object (path or caller/callee)")
        path = item.get("path")
        if path is not None:
            if not isinstance(path, list) or not (
                TRACE_PATH_MIN <= len(path) <= TRACE_PATH_MAX
            ):
                raise SystemExit(
                    f"traces[{i}]: 'path' must be an array of {TRACE_PATH_MIN}..{TRACE_PATH_MAX} qualified names"
                )
            for qn in path:
                if not isinstance(qn, str) or not qn:
                    raise SystemExit(f"traces[{i}]: path entries must be non-empty strings")
            for a, b in zip(path, path[1:]):
                if a == b:
                    continue  # 自环跳过（静态边零自环口径一致）
                key = (a, b)
                counts[key] = counts.get(key, 0) + 1
            continue
        caller = item.get("caller")
        callee = item.get("callee")
        count = item.get("count", 1)
        if not isinstance(caller, str) or not isinstance(callee, str) or not caller or not callee:
            raise SystemExit(
                f"traces[{i}]: needs 'path' or non-empty 'caller'+'callee' strings"
            )
        if not isinstance(count, int) or count < 1:
            raise SystemExit(f"traces[{i}]: 'count' must be a positive integer")
        if caller == callee:
            continue
        counts[(caller, callee)] = counts.get((caller, callee), 0) + count
    return counts


def ingest_traces(con: sqlite3.Connection, file_path: Path, label: str | None = None) -> dict:
    """导入 traces 文件 → runtime_calls（per label 整批替换）+ 对账三分类收据。

    label 优先级：CLI 参数 > 文件 'label' 字段 > "unlabeled"（CBM 同默认）。
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        raise SystemExit(f"traces: file not found: {file_path}")
    payload = _load_traces_file(file_path)
    run_label = label or payload["label"] or DEFAULT_LABEL
    counts = _pair_counts(payload["traces"])
    # qn → fid 单遍解析（qn 键 reindex-proof；fid 仅供对账，存储恒 qn）
    fid_of: dict[str, str] = {}
    unresolved: list[str] = []
    qns = sorted({qn for pair in counts for qn in pair})
    for qn in qns:
        row = con.execute(
            "SELECT fid FROM symbols WHERE qualified_name = ? ORDER BY fid LIMIT 1",
            (qn,),
        ).fetchone()
        if row is None:
            unresolved.append(qn)
        else:
            fid_of[qn] = row[0]
    resolved = {
        (caller, callee): count
        for (caller, callee), count in counts.items()
        if caller in fid_of and callee in fid_of
    }
    # per label 整批替换（幂等）
    con.execute("DELETE FROM runtime_calls WHERE label = ?", (run_label,))
    con.executemany(
        "INSERT INTO runtime_calls (label, caller_qn, callee_qn, count) VALUES (?,?,?,?)",
        [
            (run_label, caller, callee, count)
            for (caller, callee), count in sorted(resolved.items())
        ],
    )
    con.commit()
    # 对账三分类：观测（qn 对 → fid 对）vs 静态 SLICE_EDGE_TYPES 边
    marks = ",".join("?" * len(SLICE_EDGE_TYPES))
    static_pairs = {
        (src, dst)
        for src, dst in con.execute(
            f"SELECT DISTINCT src_fid, dst_fid FROM edges WHERE type IN ({marks})",
            SLICE_EDGE_TYPES,
        )
    }
    observed_pairs = {(fid_of[c], fid_of[d]) for (c, d) in resolved}
    agreed = observed_pairs & static_pairs
    observed_only = observed_pairs - static_pairs
    static_only = static_pairs - observed_pairs
    # observed_only 样本：动态路径静态漏了——可操作信号（qn 对回显，消费方免 join）
    observed_only_sample = sorted(
        f"{c} -> {d}" for (c, d) in (_qn_pair_of(con, cf, df) for (cf, df) in observed_only)
    )[:AGREEMENT_SAMPLE_CAP]
    return {
        "ok": True,
        "label": run_label,
        "file": str(file_path),
        "trace_items": len(payload["traces"]),
        "distinct_pairs": len(counts),
        "stored_pairs": len(resolved),
        "skipped_unresolved": len(counts) - len(resolved),
        "unresolved_sample": sorted(unresolved)[:UNRESOLVED_SAMPLE_CAP],
        "agreement": {
            "agreed": len(agreed),
            "observed_only": len(observed_only),
            "static_only": len(static_only),
            "static_pairs_total": len(static_pairs),
            "observed_only_sample": observed_only_sample,
        },
        "note": "runtime call evidence ingested (qn-keyed, per-label whole-run replace, "
        "reindex-proof across etl); agreement: observed vs static "
        f"{'/'.join(SLICE_EDGE_TYPES)} edges - observed_only = dynamic paths static "
        "analysis missed (xcheck ca_only-type signal); static_only = statically "
        "possible never observed (dead-code/coverage corroboration)",
    }


def _qn_pair_of(con: sqlite3.Connection, caller_fid: str, callee_fid: str) -> tuple[str, str]:
    names: dict[str, str] = {}
    for fid in (caller_fid, callee_fid):
        if fid not in names:
            row = con.execute(
                "SELECT qualified_name FROM symbols WHERE fid = ?", (fid,)
            ).fetchone()
            names[fid] = row[0] if row else fid
    return names[caller_fid], names[callee_fid]
