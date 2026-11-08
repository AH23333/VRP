"""S7 架构执法 C/C++ 引擎：内置源级 #include 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机 gcc/g++ 8.1.0 在机）：
- C/C++ 无语言级模块系统（namespace 不是依赖边界，include 才是）→ 容器 =
  目录（posix，仓相对）；依赖边 = 引号 #include 解析到仓内文件后取其目录。
  源级解析纯 stdlib 零工具链（与 rs/java/cs/kt 引擎同路线），无
  enforcer-not-found 态；编译验证仅在 dogfood 探针走 gcc。
- CBM C 事实（work/c-cbm-probe 实证）：CBM 索引 C——Module 符号=翻译单元
  （.c），函数/Macro（include guard 也是符号）/File/Folder 全入表；
  qualified name 路径式；CBM IMPORTS 边=文件级 include 边（ETL 后在
  facts.db edges 表，evidence=structural）。容器推导无需读文件——目录直接
  取自 symbols.file_path（与 ts 引擎同策略，异于 java/cs/kt 的读文件解析）。
- 词法：块注释**不可嵌套**（C/C++ 标准——首个星斜闭合，异于 Kotlin 深度
  计数）；字符串（反斜杠转义）；C++ raw string（R"delim(...)delim"，
  u8R/uR/UR/LR 前缀）；字符字面量（有界扫描防数字分隔符 1'000 误吞代码）。
- #include 提取：反斜杠续行合并为逻辑行（锚定取逻辑行首行号）；指令记号
  间隙注释（`#include /* c */ "x.h"`——C 预处理注释=空白语义，review 批
  修正：间隙注释不再丢 include 边，注释空白化后正则仍匹配）；尖括号
  <...> 系统头不入图；引号目标解析顺序近似编译器 -I 语义——源文件所在
  目录（C 标准引号查找首查）→ 仓根 → 各含头文件目录（近似 -I 根）；
  绝对路径/盘符按系统头跳过。
- 文件首 BOM（\\ufeff）空白化（review 批修正，ObjC 同型）：Windows 编辑器
  BOM 头不污染行首指令词法与 `^[ \\t]*#` 匹配（BOM+行 1 include 假阴防御）。
- 测试排除（C 无语言级测试标记，约定式保守排除）：路径含 test/tests
  **目录段**，或文件名 test_*.c / *_test.c / *_tests.c（及 .cc/.cpp 等同
  族后缀，test.c/test.cc 裸名亦然）——排除文件的 include 不入图；其目录
  仍入图（叶目录可见）。
- 违例语义 = 传递闭包（八引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 include 边所在文件+行号（同目录多文件满足同一边时取
  (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 根级文件无目录容器（与 ts 根级文件同策略）——扁平单目录 C 项目目录
    粒度退化，执法给出 not-enforceable 而非假图
  * 头文件搜索路径取近似（真实 -I 列表不可知）——同名头多目录时按解析
    顺序取首命中，误配概率低但存在
  * 条件编译（#if 0 包裹的 include）不识别——按文本计（保守方向：可能
    多报，不静默漏报）
  * 指令间隙跨行块注释（`#include /*\n*/ "x.h"`——合法 C 但注释跨行使
    逻辑行断裂，病态无实践）不识别——单行间隙注释已修（review 批）
  * .inl/.inc/.pch/.ino 等非常规后缀不扫描
  * 构建/第三方目录（build/cmake-build-*/node_modules/target/vendor/
    隐藏目录）跳过（产物与 vendored 代码无架构语义）
  * 扫描超时 → enforcer-failed（部分图不可信，不静默截断）
"""

from __future__ import annotations

import json
import os
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

C_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".c++")
C_HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx", ".h++")
C_SUFFIXES = C_SOURCE_SUFFIXES + C_HEADER_SUFFIXES
SKIP_DIRS = {
    "build",
    "cmake-build-debug",
    "cmake-build-release",
    "node_modules",
    "target",
    "vendor",
}
TEST_SEGMENTS = ("test", "tests")

_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"')


# ------------------------------------------------------------------ 词法底座


def _string_end(text: str, i: int) -> int:
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        if text[j] == "\n":
            return j  # 字符串不跨行（无续行）：行尾截断防误吞下行代码
        j += 1
    return n


def _char_end(text: str, i: int) -> int:
    """字符字面量 'x' / '\\n' / '\\uXXXX' 结束位置；非字面量（短距无闭合）→ i。"""
    j = i + 1
    n = len(text)
    while j < n and j - i <= 12:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        if text[j] == "\n":
            return i
        j += 1
    return i


def _raw_prefix_end(text: str, i: int) -> int:
    """'"' 在 i → C++ raw string 前缀（R/uR/u8R/UR/LR）起点；非 raw → -1。

    标识符尾接引号（VAR"..."）不是 raw string——R 前必须是非标识符字符。
    """
    j = i - 1
    if j < 0 or text[j] != "R":
        return -1
    k = j - 1
    if k >= 0:
        c = text[k]
        if c == "8" and k - 1 >= 0 and text[k - 1] in "uU":
            return k - 1
        if c in "uUL":
            return k
        if c.isalnum() or c == "_":
            return -1
    return j


def _raw_string_end(text: str, i: int) -> int:
    """raw string（'\"' 在 i）闭引号后一位置；delim 未闭合（非法）→ 普通字符串回退。

    语法 R"delim(content)delim"：delim 紧随开引号、后接左括号——闭序列
    ")delim\""（探针首版误按第二引号取 delim，实证修正）。
    """
    k = text.find("(", i + 1)
    if k < 0:
        return _string_end(text, i)
    delim = text[i + 1 : k]
    closing = ")" + delim + '"'
    j = text.find(closing, k + 1)
    if j < 0:
        return _string_end(text, i)
    return j + len(closing)


def _include_directive_end(
    text: str, i: int
) -> tuple[int, list[tuple[int, int]]] | None:
    """行首 '#' 在 i → (引号 include 指令目标区结束位置, 间隙注释区段)；非引号 include → None。

    允许记号间反斜杠续行与注释（C 预处理注释=空白语义——review 批修正：
    间隙注释不再丢 include 边，注释区段回传调用方空白化后正则仍匹配）；
    尖括号/畸形目标 → None（按普通文本词法，不入图）。
    """
    n = len(text)
    spans: list[tuple[int, int]] = []

    def skip_gap(k: int) -> int:
        while k < n:
            c = text[k]
            if c in " \t":
                k += 1
            elif c == "\\" and k + 1 < n and text[k + 1] in "\r\n":
                k += 2
                while k < n and text[k] in "\r\n":
                    k += 1
            elif text.startswith("//", k):
                e = k
                while e < n and text[e] != "\n":
                    e += 1
                spans.append((k, e))
                k = e
            elif text.startswith("/*", k):
                e = text.find("*/", k + 2)
                e = n if e < 0 else e + 2
                spans.append((k, e))
                k = e
            else:
                break
        return k

    j = skip_gap(i + 1)
    if not text.startswith("include", j):
        return None
    j = skip_gap(j + len("include"))
    if j < n and text[j] == '"':
        k = text.find('"', j + 1)
        if k < 0 or "\n" in text[j + 1 : k]:
            return None
        return k + 1, spans
    return None


def _blank_noncode(text: str) -> str:
    """注释（// 与不可嵌套块注释）/字符串/raw string/字符 → 空白（等长，换行保留）。

    #include 的引号目标整体保留——include 是引号语法，不能随字符串空白化；
    注释/字符串内的伪 include 因词法先耗尽不会误保留（块注释整体跳过）。
    """
    out = list(text)
    i = 0
    n = len(text)
    bol = True
    if text.startswith("\ufeff"):
        out[0] = " "  # 文件首 BOM 空白化：bol 追踪/行首指令词法不受污染（BOM+行 1 include 假阴防御——ObjC 同型修正）
        i = 1
    while i < n:
        c = text[i]
        if c == "\n":
            bol = True
            i += 1
            continue
        if bol and c == "#":
            res = _include_directive_end(text, i)
            if res is not None:
                end, spans = res
                for s, e in spans:
                    for k in range(s, min(e, n)):
                        if out[k] != "\n":
                            out[k] = " "
                i = end
                bol = False
                continue
        if c in " \t":
            i += 1
            continue
        bol = False
        two = text[i : i + 2]
        if two == "//":
            j = i
            while j < n and text[j] != "\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "/*":
            # C/C++ 块注释不可嵌套（异于 Kotlin）：首个星斜闭合
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == '"':
            p = _raw_prefix_end(text, i)
            j = _raw_string_end(text, i) if p >= 0 else _string_end(text, i)
            start = p if p >= 0 else i
            for k in range(start, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "'":
            j = _char_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j if j > i else i + 1
        else:
            i += 1
    return "".join(out)


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _iter_includes(blanked: str) -> list[tuple[str, int]]:
    """已空白化文本 → [(include 引号目标, 行号)]；反斜杠续行合并为逻辑行。"""
    rows: list[tuple[str, int]] = []
    lines = [l.rstrip("\r") for l in blanked.split("\n")]
    i = 0
    n = len(lines)
    while i < n:
        logical = lines[i]
        start = i
        while logical.endswith("\\") and i + 1 < n:
            i += 1
            logical = logical[:-1] + " " + lines[i]
        m = _INCLUDE_RE.search(logical)
        if m:
            rows.append((m.group(1), start + 1))
        i += 1
    return rows


# ------------------------------------------------------------------ 路径


def _dir_of(p: str) -> str:
    slash = p.rfind("/")
    return p[:slash] if slash >= 0 else ""


def _norm(path: str) -> str:
    """词法规范化：反斜杠归一、剥 ./、.. 弹栈（越顶忽略）。"""
    parts: list[str] = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _is_test(rel: str) -> bool:
    """测试文件判定：test/tests 目录段，或 test_*.c / *_test.c / *_tests.c 族文件名。"""
    segs = rel.split("/")
    if any(seg in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1].lower()
    for suffix in C_SUFFIXES:
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return (
                stem == "test"
                or stem == "tests"
                or stem.startswith("test_")
                or stem.endswith(("_test", "_tests"))
            )
    return False


def _resolve_include(
    target: str,
    src_dir: str,
    file_set: set[str],
    header_dirs: list[str],
) -> str | None:
    """引号 include → 仓内文件路径；未命中（系统头/外部）→ None。

    解析顺序近似编译器 -I 语义：源文件所在目录（C 标准引号查找首查）→
    仓根 → 各含头文件目录（近似 -I 根；同名头歧义取字典序首）。
    """
    if ":" in target or target.startswith(("/", "\\")):
        return None  # 绝对路径/盘符：按系统头跳过
    seen: set[str] = set()
    for cand_dir in (src_dir, "", *header_dirs):
        if cand_dir in seen:
            continue
        seen.add(cand_dir)
        cand = _norm(cand_dir + "/" + target)
        if cand in file_set:
            return cand
    return None


# ------------------------------------------------------------------ 扫描


def scan_c(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], list[dict], int, int, bool]:
    """repo → (c_files, include_rows, files_scanned, tests_excluded, timed_out)。

    c_files：repo 相对 posix 路径排序清单（.c/.cc/.cpp/.cxx/.c++ 与头文件）；
    include_rows：[{file, line, target}]（仅测试外文件；target 为引号原始串）；
    测试文件计入 files_scanned 与 tests_excluded（诚实计数）。
    超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    c_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(C_SUFFIXES):
                continue
            if time.monotonic() > deadline:
                return c_files, [], 0, 0, True
            c_files.append((Path(dirpath) / name).relative_to(repo).as_posix())
    c_files.sort()
    include_rows: list[dict] = []
    tests_excluded = 0
    for rel in c_files:
        if time.monotonic() > deadline:
            return c_files, include_rows, len(c_files), tests_excluded, True
        if _is_test(rel):
            tests_excluded += 1
            continue
        blanked = _blank_noncode(_read(repo, rel))
        for target, line in _iter_includes(blanked):
            include_rows.append({"file": rel, "line": line, "target": target})
    return c_files, include_rows, len(c_files), tests_excluded, False


def build_c_graph(
    c_files: list[str],
    include_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """目录依赖图（键 = 全部含 C 文件目录——叶目录无出边也入图，漏叶会假阴）+ 边明细。"""
    file_set = set(c_files)
    header_dirs = sorted(
        {_dir_of(p) for p in c_files if p.lower().endswith(C_HEADER_SUFFIXES)}
    )
    graph: dict[str, set[str]] = {_dir_of(p): set() for p in c_files}
    edges: list[dict] = []
    for row in include_rows:
        src_dir = _dir_of(row["file"])
        resolved = _resolve_include(row["target"], src_dir, file_set, header_dirs)
        if resolved is None:
            continue
        dst_dir = _dir_of(resolved)
        if dst_dir == src_dir:
            continue  # 同目录 include：无自环
        graph[src_dir].add(dst_dir)
        edges.append({"file": row["file"], "line": row["line"], "src": src_dir, "dst": dst_dir})
    return {d: sorted(t) for d, t in graph.items()}, edges


def derive_c_containers(con: sqlite3.Connection, payload: dict) -> dict:
    """module_id → 排序目录容器（fid→file_path→posix 目录；根级文件无容器）。

    目录直接取自 symbols.file_path（无源码扫描——容器即路径，异于
    java/cs/kt 的读文件解析）；c_files 供 run() 路由（有 C 文件但全部
    根级/无容器也进 C 引擎给出可行动的 not-enforceable）；c_source_files
    仅计 C 源文件（.c/.cc/.cpp/.cxx/.c++，不含 .h——.h 与 objc 引擎歧义，
    run() 路由归属判定用：.m/.mm 在仓且无 C 源文件时 .h 归 objc 引擎）。
    """
    fids_by_module: dict[str, list[str]] = {}
    for mod in payload.get("modules", []):
        if isinstance(mod, dict) and isinstance(mod.get("module_id"), str):
            fids_by_module[mod["module_id"]] = [
                f for f in mod.get("symbol_fids", []) if isinstance(f, str)
            ]
    fid_to_module = {f: mid for mid, fids in fids_by_module.items() for f in fids}
    containers: dict[str, set[str]] = {mid: set() for mid in fids_by_module}
    extensions: set[str] = set()
    c_paths: set[str] = set()
    c_source = 0
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
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(C_SUFFIXES):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        c_paths.add(p)
        if p.lower().endswith(C_SOURCE_SUFFIXES):
            c_source += 1
        d = _dir_of(p)
        if d == "":
            continue  # 根级文件无目录容器（与 ts 根级文件同策略）
        containers[fid_to_module[fid]].add(d)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "c_files": len(c_paths),
        "c_source_files": c_source,
    }


# ------------------------------------------------------------------ 违例


def _belongs(d: str, prefixes: list[str]) -> bool:
    """目录 ∈ 容器集（容器可为父目录——src 捕获 src/store 子目录）。"""
    return any(d == p or d.startswith(p + "/") for p in prefixes)


def _find_violations(graph: dict[str, list[str]], contracts: list[dict]) -> list[dict]:
    violations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for contract in contracts:
        sources = [p for p in graph if _belongs(p, contract["source_modules"])]
        forbidden = {p for p in graph if _belongs(p, contract["forbidden_modules"])}
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


def run_c(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """C/C++ 执法入口：契约生成（复用层 DAG）→ 源级 include 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "c-include-parser",
        "structure_status": structure_status,
    }
    c_files, include_rows, files_scanned, tests_excluded, timed_out = scan_c(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"c/c++ scan exceeded {timeout}s (partial graph discarded)",
        }
    if not c_files:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no c/c++ sources found on disk (structure refers to deleted paths?)",
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
    graph, edges = build_c_graph(c_files, include_rows)
    summary["files"] = files_scanned
    summary["dirs"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 include 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 include 边）；防御：丢弃无法锚定的违例并诚实计数
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
