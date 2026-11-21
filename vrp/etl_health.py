"""BB4 ETL 健康面（构想v13 §BB4）——不变式清单查询式断言。

codegraph IndexHealth（issue #2336：内容哈希相等≠产物完好——
needsReindex/parseErrors 分类）与 CGC repair_missing_contains_links
（E2E BUG-005：删除留 9 个孤儿节点）判定学：健康字段与不变量清单照学；
自愈**不**照抄——VRP ETL owned 表是重建派生面（同 facts.db 字节级
同产物红线），原地补写违反确定性；修复=重跑 etl，本模块只检测+建议
（响亮报错：违例 rc=1，CI 可用；健康 rc=0）。

不变式清单镜像 tests/deep/test_deep_ref_integrity.py 断言集（六向零
孤儿/零自环/fid 唯一/8 元组全行去重）——deep 面是测试时守卫，本模块
是运行时纵深防御层（任何 facts.db 可随时体检，不依赖测试设施）。

symbols_stale_files 是 CGC BUG-005 的 VRP 对偶：符号声称的文件不在
etl_file_state（J8 增量快照）——文件已删或 CBM 索引滞后工作树（D2），
facts.db 陈旧信号（"哈希没变但符号没了/文件没了但符号还在"类漂移）。
豁免面（dogfood 实测钉死，fresh run 36 例全合法非文件实体）：
- Folder/Project/Branch label——目录树/项目/分支节点结构性非文件
  （CBM 索引目录节点，etl_file_state 只快照文件）；
- file_path LIKE '<%'——合成命名空间（<python-builtins>，v_hotspots
  过滤面同源）；空 file_path——无文件符号合法。

schema 陈旧（表缺失）不炸——诚实报告 schema_stale 后跳过依赖检查
（dogfood 实录：J8 前 facts.db 无 etl_file_state 表，健康面应报
"重跑 vrp run"而非 traceback）。
"""

from __future__ import annotations

import sqlite3

REQUIRED_TABLES = ("symbols", "edges", "usage_hints", "test_linkage", "etl_file_state")

# (key, 依赖表, sql, hint)——count 期望恒 0
CHECKS: list[tuple[str, tuple[str, ...], str, str]] = [
    (
        "symbols_fid_unique",
        ("symbols",),
        "SELECT COUNT(*) - COUNT(DISTINCT fid) FROM symbols",
        "symbols.fid 存在重复（重建 etl）",
    ),
    (
        "edges_orphan_src",
        ("edges", "symbols"),
        "SELECT COUNT(*) FROM edges WHERE src_fid NOT IN (SELECT fid FROM symbols)",
        "edges.src_fid 悬垂——边写入未对齐 symbols（重建 etl）",
    ),
    (
        "edges_orphan_dst",
        ("edges", "symbols"),
        "SELECT COUNT(*) FROM edges WHERE dst_fid NOT IN (SELECT fid FROM symbols)",
        "edges.dst_fid 悬垂——边写入未对齐 symbols（重建 etl）",
    ),
    (
        "edges_self_loop",
        ("edges",),
        "SELECT COUNT(*) FROM edges WHERE src_fid = dst_fid",
        "自环边（语义无意义——重建 etl）",
    ),
    (
        "usage_hints_orphans",
        ("usage_hints", "symbols"),
        "SELECT (SELECT COUNT(*) FROM usage_hints WHERE src_fid NOT IN (SELECT fid FROM symbols))"
        " + (SELECT COUNT(*) FROM usage_hints WHERE dst_fid NOT IN (SELECT fid FROM symbols))",
        "usage_hints 悬垂引用（重建 etl）",
    ),
    (
        "test_linkage_orphans",
        ("test_linkage", "symbols"),
        "SELECT (SELECT COUNT(*) FROM test_linkage WHERE test_fid NOT IN (SELECT fid FROM symbols))"
        " + (SELECT COUNT(*) FROM test_linkage WHERE target_fid NOT IN (SELECT fid FROM symbols))",
        "test_linkage 悬垂引用（重建 etl）",
    ),
    (
        "edges_full_row_dupes",
        ("edges",),
        "SELECT (SELECT COUNT(*) FROM edges) - (SELECT COUNT(*) FROM (SELECT DISTINCT src_fid, dst_fid,"
        " type, evidence, strategy, confidence, line, properties_json FROM edges))",
        "全行重复边（冗余 ETL 行——重建 etl）",
    ),
    (
        "symbols_stale_files",
        ("symbols", "etl_file_state"),
        "SELECT COUNT(*) FROM symbols WHERE file_path != ''"
        " AND file_path NOT LIKE '<%'"
        " AND label NOT IN ('Folder','Project','Branch')"
        " AND file_path NOT IN (SELECT file_path FROM etl_file_state)",
        "符号声称的文件不在 ETL 快照——文件已删或 CBM 索引滞后（重跑 vrp run 刷新索引+ETL）",
    ),
]


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row and row[0])


def run_health(con: sqlite3.Connection) -> dict:
    """查询式不变式断言清单。健康={'healthy': True}；违例如实逐条报数。"""
    missing = [t for t in REQUIRED_TABLES if not _table_exists(con, t)]
    checks: list[dict] = []
    for key, deps, sql, hint in CHECKS:
        if any(d in missing for d in deps):
            checks.append(
                {
                    "check": key,
                    "ok": False,
                    "count": None,
                    "hint": f"依赖表缺失（{'/'.join(d for d in deps if d in missing)}）——schema 陈旧，重跑 vrp run",
                }
            )
            continue
        count = con.execute(sql).fetchone()[0]
        checks.append({"check": key, "ok": count == 0, "count": int(count), "hint": hint})
    if missing:
        checks.append(
            {
                "check": "schema_stale",
                "ok": False,
                "count": len(missing),
                "hint": f"缺表 {'/'.join(missing)}——facts.db 由旧版生成，重跑 vrp run",
            }
        )
    healthy = all(c["ok"] for c in checks)
    return {
        "healthy": healthy,
        "violations": sum(1 for c in checks if not c["ok"]),
        "checks": checks,
        "hint": "healthy" if healthy else "违例如上——修复面=重跑 vrp run（owned 表重建派生，无原地自愈）",
    }


def render_text(result: dict) -> str:
    """text 报告面：逐检查 OK/FAIL 行 + 修复总述。"""
    lines = ["== etl-health =="]
    for c in result["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        count = c["count"]
        lines.append(f"[{mark}] {c['check']}: {count}")
        if not c["ok"]:
            lines.append(f"       -> {c['hint']}")
    lines.append(f"healthy: {str(result['healthy']).lower()} ({result['violations']} violation(s))")
    lines.append(f"hint: {result['hint']}")
    return "\n".join(lines)
