"""S7 架构执法 Zig 引擎：内置源级 @import 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 zig）：
- Zig 依赖边界 = 文件（每 .zig 文件即一个文件级模块；build.zig 里
  b.addModule 声明的命名模块是包级依赖，V1 不解析 build.zig——程序化
  构建脚本无稳定声明形态）→ 容器 = 文件（repo 相对 posix 路径含
  .zig 后缀，与 Dart 引擎同哲学——语言单元即依赖单元；fid→file_path
  直推无需读文件解析声明）。
- 依赖边 = @import 实参以 .zig 结尾的相对路径（相对当前文件目录
  posix 归一；无 .zig 后缀的包名（std/命名模块/构建选项）→ 外部
  跳过——build.zig 程序化声明不启发式解析，诚实文档化）。动态
  import（comptime 拼接实参）不入图。
- CBM Zig IMPORTS 边存在但 line=None（zig-cbm-probe 实测，File→Module，
  evidence=structural）→ 锚定行号必须自扫源码。CBM 内置 Zig 解析器
  （Module 符号路径式 qname）；xcheck agreement 100%（code-atlas
  支持 Zig CALLS——zig-cbm-probe 实测）。
- 词法（等长空白，换行保留，前进保证处处）：Zig 只有行注释（//——
  无块注释，语言刻意排除；/// 与 //! 也是行注释）；普通字符串双引号
  反斜杠转义（未闭合截断行尾保守）；多行字符串 = 行内双反斜杠前缀
  起至行尾（连续行拼接语义，词法按行处理）；字符字面量单引号反斜杠
  转义。Zig 无块注释/raw 串/插值/heredoc——词法面为本引擎族最小。
- @import 目标区整体保留（同 C 引擎 include、Dart 引擎指令 URI——
  @import 实参是字符串语法不能随字符串空白化）：@import 标识符（@
  前非标识符字符）后跳空白与左括号，紧邻字符串字面量原样保留；
  非字符串实参（comptime 表达式）正常空白化（动态 import 不入图）。
- 测试排除：Zig 测试主形态是文件内嵌 test 块（test 后可选字符串
  标签+花括号块）——文件级排除与 Zig 测试模型不符；test 块内
  @import 不入图（refAllDecls / _ = @import 测试引用模式），块外
  依赖边照常；tests_excluded = test 块计数。独立测试文件无统一
  命名约定（诚实文档化，其依赖边保留）。
- 违例语义 = 传递闭包（十四引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 @import 边 (file,line) 字典序首 + 违例文件首符号。
- 诚实局限（文档化不静默）：
  * build.zig 程序化模块声明不解析——命名模块 import 视为外部
  * 动态 @import（comptime 拼接实参）不入图
  * zig-cache/zig-out/node_modules/vendor/隐藏目录跳过（产物无源码语义）
  * 扫描超时 → enforcer-failed（部分图不可信，不静默截断）
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sqlite3
import time
from pathlib import Path

from .s7_enforce import (
    DEFAULT_TIMEOUT,
    FINDINGS_SOURCE,
    KIND,
    PATHOLOGY,
    build_contracts,
)

ZIG_SUFFIX = ".zig"
SKIP_DIRS = {"zig-cache", "zig-out", "node_modules", "vendor"}  # .zig-cache 走隐藏目录排除
_IDENT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")

_IMPORT_RE = re.compile(r'@import[ \t\r\n]*\([ \t\r\n]*"([^"\n]+)"')
_TEST_BLOCK_RE = re.compile(r"(?<![A-Za-z0-9_.])test[ \t\r\n]*\{")


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串 → 空白（等长，换行保留）；@import 实参字符串字面量
    原样保留（@import 实参是字符串语法不能随字符串空白化）。

    前进保证处处（任何回退路径至少 +1）。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):  # 行注释（含 /// //! 文档注释）
            j = text.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
        elif text.startswith("\\\\", i):  # 多行字符串行（前缀起至行尾）
            j = text.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "'":
            j = _char_end(text, i)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j if j > i else i + 1
        elif c == '"':
            j = _string_end(text, i)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j if j > i else i + 1
        elif c == "@" and _import_at(text, i):
            j = i + len("@import")
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] == "(":
                j += 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] == '"':
                    i = _string_end(text, j)  # 目标区整体保留（实参字符串不空白化）
                    continue
            i = i + len("@import")  # 非字符串实参（comptime 表达式）：跳过关键字
        else:
            i += 1
    return "".join(out)


def _import_at(text: str, i: int) -> bool:
    """位置 i 的 @import 是 builtin 调用形态（@ 前非标识符字符）。"""
    if not text.startswith("@import", i):
        return False
    if i > 0 and text[i - 1] in _IDENT_CHARS:
        return False
    j = i + len("@import")
    n = len(text)
    return j >= n or text[j] not in _IDENT_CHARS


def _string_end(text: str, i: int) -> int:
    """字符串字面量结束位置（含闭引号）；反斜杠转义跳过；
    未闭合截断行尾（保守前进）。
    """
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        if text[j] == "\n":
            break
        j += 1
    j = text.find("\n", i)
    return j if j >= 0 else n


def _char_end(text: str, i: int) -> int:
    """字符字面量结束位置（含闭单引号）；反斜杠转义跳过；
    未闭合截断行尾（保守前进）。
    """
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        if text[j] == "\n":
            break
        j += 1
    j = text.find("\n", i)
    return j if j >= 0 else n


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ 解析


def _brace_end(text: str, open_pos: int) -> int:
    """花括号配平结束位置（含闭括号；未闭合文件尾——前进保证）。

    空白化文本上行走——字符串/注释内的花括号已随词法消失。
    """
    depth = 0
    j = open_pos
    n = len(text)
    while j < n:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _test_block_ranges(blanked: str) -> list[tuple[int, int]]:
    """test 块字节区间（test 后可选字符串标签+花括号块）。

    标签字符串已空白化（非 @import 实参）→ test 后只剩空白与花括号。
    前一字符为标识符/句点 → 非测试块（字段访问 .test / 标识符尾）。
    """
    return [
        (m.start(), _brace_end(blanked, m.end() - 1))
        for m in _TEST_BLOCK_RE.finditer(blanked)
    ]


def _iter_imports(blanked: str, test_ranges: list[tuple[int, int]]) -> tuple[list[dict], int]:
    """保留区 → ([{path, line}], suppressed)；test 块内 @import 抑制。

    suppressed = test 块内被抑制的 @import 行数（诚实计数）。
    """
    rows: list[dict] = []
    suppressed = 0
    for m in _IMPORT_RE.finditer(blanked):
        if any(s <= m.start() < e for s, e in test_ranges):
            suppressed += 1
            continue
        rows.append(
            {"path": m.group(1), "line": blanked.count("\n", 0, m.start()) + 1}
        )
    return rows, suppressed


# ------------------------------------------------------------------ 扫描


def scan_zig(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[set[str], list[dict], int, int, int, bool]:
    """repo → (file_set, import_rows, files_scanned, test_blocks, tests_excluded, timed_out)。

    file_set：全部扫描的 .zig 文件（容器 = 文件）；import_rows：
    [{file, line, path}]（仅 test 块外的 @import）；test_blocks：test
    块计数；tests_excluded：test 块内被抑制的 @import 行数。超时截断
    的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_set: set[str] = set()
    import_rows: list[dict] = []
    files_scanned = 0
    test_blocks = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(ZIG_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_set, import_rows, files_scanned, test_blocks, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            file_set.add(rel)
            blanked = _blank_noncode(_read(repo, rel))
            ranges = _test_block_ranges(blanked)
            test_blocks += len(ranges)
            rows, suppressed = _iter_imports(blanked, ranges)
            tests_excluded += suppressed
            for row in rows:
                import_rows.append({**row, "file": rel})
    return file_set, import_rows, files_scanned, test_blocks, tests_excluded, False


# ------------------------------------------------------------------ 图


def _resolve_import(src_rel: str, path: str, file_set: set[str]) -> str | None:
    """@import 实参 → 仓内 .zig 文件（未命中 = 包名/外部 → None）。"""
    if not path.endswith(ZIG_SUFFIX):
        return None  # 包名（std/命名模块/构建选项）——build.zig 声明不解析
    cand = posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), path))
    return cand if cand in file_set else None


def build_zig_graph(
    file_set: set[str],
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键 = 全部容器——漏叶假阴）+ 边明细（含 @import 溯源）。"""
    graph: dict[str, set[str]] = {f: set() for f in file_set}
    edges: list[dict] = []
    for row in import_rows:
        dst = _resolve_import(row["file"], row["path"], file_set)
        if dst is None or dst == row["file"]:
            continue
        graph[row["file"]].add(dst)
        edges.append({"file": row["file"], "line": row["line"], "src": row["file"], "dst": dst})
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_zig_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
) -> dict:
    """module_id → 排序容器集（fid→file_path→文件容器，无需扫描）。"""
    fids_by_module: dict[str, list[str]] = {}
    for mod in payload.get("modules", []):
        if isinstance(mod, dict) and isinstance(mod.get("module_id"), str):
            fids_by_module[mod["module_id"]] = [
                f for f in mod.get("symbol_fids", []) if isinstance(f, str)
            ]
    fid_to_module = {f: mid for mid, fids in fids_by_module.items() for f in fids}
    containers: dict[str, set[str]] = {mid: set() for mid in fids_by_module}
    extensions: set[str] = set()
    rows: list[tuple[str, str]] = []
    all_fids = list(fid_to_module)
    for i in range(0, len(all_fids), 500):
        chunk = all_fids[i : i + 500]
        rows.extend(
            con.execute(
                "SELECT fid, file_path FROM symbols WHERE fid IN (%s)"
                % ",".join("?" * len(chunk)),
                chunk,
            ).fetchall()
        )
    zig_paths = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(ZIG_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        zig_paths.add(p)
        containers[fid_to_module[fid]].add(p)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "zig_files": len(zig_paths),
    }


# ------------------------------------------------------------------ 违例


def _belongs(container: str, prefixes: list[str]) -> bool:
    """容器 ∈ 容器集（文件容器精确匹配——路径不嵌套无前缀语义）。"""
    return container in prefixes


def _find_violations(graph: dict[str, list[str]], contracts: list[dict]) -> list[dict]:
    violations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for contract in contracts:
        sources = [c for c in graph if _belongs(c, contract["source_modules"])]
        forbidden = {c for c in graph if _belongs(c, contract["forbidden_modules"])}
        for start in sources:
            prev: dict[str, str | None] = {start: None}
            queue = [start]
            hit = None
            while queue and hit is None:
                cur = queue.pop(0)
                for nxt in graph.get(cur, []):
                    if nxt in prev:
                        continue
                    prev[nxt] = cur
                    if nxt in forbidden:
                        hit = nxt
                        break
                    queue.append(nxt)
            if hit is None:
                continue
            chain: list[str] = []
            node: str | None = hit
            while node is not None:
                chain.append(node)
                node = prev[node]
            chain.reverse()
            key = (contract["name"], start, hit)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "contract": contract["name"],
                    "source_layer": contract.get("source_layer"),
                    "forbidden_layer": contract.get("forbidden_layer"),
                    "importer": start,
                    "imported": hit,
                    "chain": chain,
                }
            )
    return violations


def _write_findings(
    con: sqlite3.Connection,
    violations: list[dict],
    structure_status: str,
) -> dict:
    con.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    rows = []
    anchored = 0
    for v in violations:
        row = con.execute(
            "SELECT fid, start_line FROM symbols WHERE file_path = ? ORDER BY start_line LIMIT 1",
            (v["file_path"],),
        ).fetchone()
        anchor, sym_line = (row[0], row[1]) if row else (None, None)
        if anchor:
            anchored += 1
        line = v["line"] if v.get("line") is not None else sym_line
        payload = json.dumps(
            {
                "contract": v["contract"],
                "source_layer": v.get("source_layer"),
                "forbidden_layer": v.get("forbidden_layer"),
                "importer": v["importer"],
                "imported": v["imported"],
                "chain": v["chain"],
                "line": v.get("line"),
                "structure_status": structure_status,
            },
            ensure_ascii=False,
        )
        rows.append(
            (
                PATHOLOGY,
                KIND,
                FINDINGS_SOURCE,
                anchor,
                None,
                v["file_path"],
                line,
                line,
                payload,
            )
        )
    con.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    return {"findings_written": len(rows), "anchored": anchored}


def run_zig(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Zig 执法入口：契约生成（复用层 DAG）→ 源级 @import 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "zig-import-parser",
        "structure_status": structure_status,
    }
    file_set, import_rows, files_scanned, test_blocks, tests_excluded, timed_out = scan_zig(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"zig scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_set:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no zig files found (no .zig files on disk)",
        }
    built = build_contracts(payload, containers)
    contracts = built["contracts"]
    summary = {
        **base,
        "contracts": len(contracts),
        "modules_with_containers": sum(1 for v in containers.values() if v),
        "unenforced_rules": built["unenforced_rules"],
        "unenforced_layers": built["unenforced_layers"],
    }
    if not contracts:
        return {
            **summary,
            "status": "not-enforceable",
            "reason": "no derivable contracts (single layer, fully-connected DAG, or layers without containers)",
        }
    graph, edges = build_zig_graph(file_set, import_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["test_blocks"] = test_blocks
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 @import 边（chain[-2] → chain[-1]）的文件与行号
    edges_by_pair: dict[tuple[str, str], list[dict]] = {}
    for e in edges:
        edges_by_pair.setdefault((e["src"], e["dst"]), []).append(e)
    for v in violations:
        last = (
            edges_by_pair.get((v["chain"][-2], v["chain"][-1]))
            if len(v["chain"]) >= 2
            else None
        )
        if last:
            e = sorted(last, key=lambda x: (x["file"], x["line"]))[0]
            v["file_path"] = e["file"]
            v["line"] = e["line"]
        else:
            v["file_path"] = None
            v["line"] = None
    if any(v["file_path"] is None for v in violations):
        # 理论不可达（链边必来自 @import 边）；防御：丢弃无法锚定的违例并诚实计数
        dropped = sum(1 for v in violations if v["file_path"] is None)
        violations = [v for v in violations if v["file_path"] is not None]
        summary["unanchored_dropped"] = dropped
    written = _write_findings(con, violations, structure_status)
    return {
        **summary,
        "status": "ok",
        "violations": len(violations),
        **written,
    }
