"""S7 架构执法 C# 引擎：内置源级 namespace/using 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，dotnet 9.0 本机）：
- roslyn 无稳定 headless CLI（dotnet build 需全量工程构建）；源级 namespace/using
  解析纯 stdlib 零构建零工具链（与 rs/java 引擎同路线），无 enforcer-not-found 态。
- 容器 = C# 命名空间（`namespace X.Y.Z;` 文件级 / `namespace X.Y.Z { ... }` 块级，
  嵌套块全名拼接）。CBM C# 符号 qualified name 是路径式（cs-cbm-probe.src.Api.Api），
  故容器一律 fid→file_path→读文件解析 namespace。
- using 指令：`using X.Y;` / `using static X.Y.Z;` / 别名 `using A = B.C.D;`
  （目标取别名右侧）/ `global using ...`；using 语句（`using var x = f();` /
  `using (x) {...}`）词形不匹配（`var`/`(` 后无 `;`/`=` 直接收尾）。
- using 归属：命名空间花括号内 → 该命名空间（嵌套取最内全名）；命名空间外
  （文件级）→ 文件全部命名空间（编译器可见性近似）；无 namespace 文件 = orphan 不入图。
- using 目标解析 = 命名空间上下文优先（`f"{ns}.{target}"`）再全局，各自对已知
  命名空间集合最长前缀（外部依赖 System.* 等无命中 → 跳过）；同命名空间不成自环。
- 测试代码排除（与 go TestImports / rs cfg(test) / java /test/java/ 对齐）：路径含
  test/tests 目录段或 *.test/*.tests 段（.NET 测试工程命名约定 Foo.Tests）的文件
  不计 using（其命名空间仍入图——叶可见，测试 fid 容器推导不受影响）；
  其余位置 *Tests.cs 不排除（保守按生产计）。
- 违例语义 = 传递闭包（五引擎同义）：BFS 最短链入 payload；锚定 = 链尾直接 using
  边所在文件+行号（同命名空间多文件满足同一边时取 (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 无 namespace 声明文件不入图不参与执法（orphan 计数）
  * #if 预处理指令不评估（全部按生产计，保守方向）
  * 插值字符串插值孔内嵌双引号（C# 11 raw string 之前的非法形态）不支持
  * 构建目录（bin/obj/node_modules/packages/target/build/.*）跳过（产物无源码语义）
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

CS_SUFFIX = ".cs"
SKIP_DIRS = {"bin", "obj", "packages", "node_modules", "target", "build"}
_TEST_SEGMENTS = {"test", "tests"}

_NS_RE = re.compile(
    r"\bnamespace\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)
_USING_RE = re.compile(
    r"(?:\bglobal\s+)?\busing\s+(?:static\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:\s*=\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*))?"
    r"\s*;"
)


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串/字符 → 空白（等长，换行保留）——代码位置与行号不变。

    C# 形态：行注释 //、块注释 /* */（不嵌套）、普通字符串（\\ 转义）、
    verbatim @"..."（"" 转义）、raw string（3+ 引号计数闭合）、
    插值前缀 $/@$/$@ 归一到对应底座；字符字面量。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        two = text[i : i + 2]
        if two == "//":
            j = i
            while j < n and text[j] != "\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text.startswith(('$@"', "@$\""), i):
            j = _verbatim_end(text, i + 3)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif two == '@"':
            j = _verbatim_end(text, i + 2)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text.startswith('$"""', i) or text.startswith("$@\"", i):
            # 插值 raw / 插值普通（$ 前缀归一：raw 从 i+1 起，普通从 i+1 起）
            if text[i + 1] == '"':
                j = _raw_end(text, i + 1)
            else:
                j = _string_end(text, i + 2)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text.startswith('"""', i):
            j = _raw_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text.startswith('$"', i):
            j = _string_end(text, i + 2)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text[i] == '"':
            j = _string_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text[i] == "'":
            j = _char_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _string_end(text: str, i: int) -> int:
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        j += 1
    return n


def _verbatim_end(text: str, quote_pos: int) -> int:
    """verbatim 字符串（@" 开头后从开引号位起）："" 转义，单 " 闭合。"""
    j = quote_pos + 1
    n = len(text)
    while j < n:
        if text[j] == '"':
            if text[j + 1 : j + 2] == '"':
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _raw_end(text: str, i: int) -> int:
    """raw string（i 指向首个引号）：开引号连续计数 k，首个 k 连引号闭合。"""
    n = len(text)
    k = 0
    while i + k < n and text[i + k] == '"':
        k += 1
    if k < 3:
        # 两引号是空串/误入，单引号走普通字符串——防御，不吞代码
        return _string_end(text, i)
    close = text.find('"' * k, i + k)
    return n if close < 0 else close + k


def _char_end(text: str, i: int) -> int:
    """字符字面量 'x' / '\\n' / '\\uXXXX' 结束位置；非字面量（超界）→ i。"""
    j = i + 1
    n = len(text)
    while j < n and j - i <= 12:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        j += 1
    return i


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ namespace/using 解析


def _match_brace(blanked: str, open_pos: int) -> int:
    depth = 0
    for k in range(open_pos, len(blanked)):
        if blanked[k] == "{":
            depth += 1
        elif blanked[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(blanked)


def _parse_ns_spans(blanked: str) -> list[dict]:
    """namespace 声明 → 区间表（块级配花括号 / 文件级到 EOF），嵌套全名拼接。"""
    spans: list[dict] = []
    for m in _NS_RE.finditer(blanked):
        j = m.end()
        n = len(blanked)
        while j < n and blanked[j] in " \t\r\n":
            j += 1
        if j < n and blanked[j] == "{":
            spans.append(
                {"pos": m.start(), "name": m.group(1), "open": j, "kind": "block"}
            )
        elif j < n and blanked[j] == ";":
            spans.append(
                {"pos": m.start(), "name": m.group(1), "open": j, "kind": "file"}
            )
    full: dict[int, str] = {}
    for s in spans:
        if s["kind"] == "block":
            s["close"] = _match_brace(blanked, s["open"])
        else:
            s["close"] = len(blanked)
    for s in spans:
        parents = [
            t
            for t in spans
            if t is not s
            and t["kind"] == "block"
            and t["open"] < s["pos"]
            and s["close"] <= t["close"]
        ]
        if parents:
            inner = max(parents, key=lambda t: t["open"])
            full[id(s)] = full[id(inner)] + "." + s["name"]
        else:
            full[id(s)] = s["name"]
    for s in spans:
        s["full"] = full[id(s)]
    return spans


def _iter_usings(blanked: str, spans: list[dict]) -> list[dict]:
    """using 指令 → [{line, ns, target}]；ns=None 表示文件级（文件全部命名空间）。"""
    rows: list[dict] = []
    for m in _USING_RE.finditer(blanked):
        target = m.group(2) if m.group(2) else m.group(1)
        line = blanked.count("\n", 0, m.start()) + 1
        inner = None
        for s in spans:
            if s["open"] < m.start() < s["close"]:
                if inner is None or s["open"] > inner["open"]:
                    inner = s
        rows.append({"line": line, "ns": inner["full"] if inner else None, "target": target})
    return rows


def _is_test_path(rel: str) -> bool:
    for seg in rel.split("/"):
        low = seg.lower()
        if low in _TEST_SEGMENTS or low.endswith(".test") or low.endswith(".tests"):
            return True
    return False


# ------------------------------------------------------------------ 扫描


def scan_cs(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, list[str]], list[dict], int, int, bool]:
    """repo → (file_ns, import_rows, files_scanned, tests_excluded, timed_out)。

    file_ns：repo 相对 posix 路径 → 命名空间全名列表（无 namespace 声明不入表
    = orphan）；import_rows：[{file, line, ns, target}]（仅测试目录外文件；
    target 为未解析原始点分路径）；测试目录段文件不计 using（命名空间仍入
    file_ns——叶可见）。超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_ns: dict[str, list[str]] = {}
    import_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(CS_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_ns, import_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            blanked = _blank_noncode(_read(repo, rel))
            spans = _parse_ns_spans(blanked)
            if not spans:
                continue
            file_ns[rel] = sorted({s["full"] for s in spans})
            if _is_test_path(rel):
                tests_excluded += 1
                continue
            for row in _iter_usings(blanked, spans):
                import_rows.append({"file": rel, **row})
    return file_ns, import_rows, files_scanned, tests_excluded, False


def _longest_prefix(path: str, namespaces: set[str]) -> str | None:
    segs = path.split(".")
    for k in range(len(segs), 0, -1):
        prefix = ".".join(segs[:k])
        if prefix in namespaces:
            return prefix
    return None


def _resolve(target: str, ns: str | None, namespaces: set[str]) -> str | None:
    """using 点分路径 → 已知命名空间最长前缀；命名空间上下文优先再全局。

    上下文候选 f"{ns}.{target}" 只接受 ns 严格子空间命中（startswith ns+"."）——
    最长前缀回落到 ns 自身是伪命中（using 指向 ns 内部，无执法语义）。
    外部依赖（System.* 等无前缀命中）→ None 跳过。
    """
    if ns:
        res = _longest_prefix(f"{ns}.{target}", namespaces)
        if res and res.startswith(ns + "."):
            return res
    return _longest_prefix(target, namespaces)


def build_cs_graph(
    file_ns: dict[str, list[str]],
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """命名空间依赖图（键 = 全部命名空间——叶无入边也入图，漏叶会假阴）+ 边明细。"""
    namespaces: set[str] = set()
    for lst in file_ns.values():
        namespaces.update(lst)
    graph: dict[str, set[str]] = {ns: set() for ns in namespaces}
    edges: list[dict] = []
    for row in import_rows:
        resolved = _resolve(row["target"], row["ns"], namespaces)
        if resolved is None:
            continue
        sources = [row["ns"]] if row["ns"] else list(file_ns.get(row["file"], []))
        for src in sources:
            if src == resolved:
                continue
            graph[src].add(resolved)
            edges.append({"file": row["file"], "line": row["line"], "src": src, "dst": resolved})
    return {ns: sorted(t) for ns, t in graph.items()}, edges


def derive_cs_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序命名空间集（fid→file_path→namespace 声明）。

    仅当 payload 符号含 .cs 文件才扫描；orphan（.cs 无 namespace 声明或磁盘
    缺失，去重路径）与超时诚实上报——cs_files 供 run() 路由（有 .cs 但
    全 orphan 也要进 cs 引擎给出可行动的 not-enforceable）。
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
    cs_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(CS_SUFFIX)
    }
    file_ns: dict[str, list[str]] = {}
    timed_out = False
    if cs_paths:
        file_ns, _imports, _files, _tests, timed_out = scan_cs(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(CS_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        names = file_ns.get(p)
        if not names:
            orphans.add(p)
            continue
        containers[fid_to_module[fid]].update(names)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "cs_files": len(cs_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(ns: str, prefixes: list[str]) -> bool:
    """命名空间 ∈ 容器集（容器可为父命名空间——Probe 捕获 Probe.Store 子空间）。"""
    return any(ns == p or ns.startswith(p + ".") for p in prefixes)


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


def run_cs(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """C# 执法入口：契约生成（复用层 DAG）→ 源级 using 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "cs-namespace-parser",
        "structure_status": structure_status,
    }
    file_ns, import_rows, files_scanned, tests_excluded, timed_out = scan_cs(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"c# scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_ns:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no c# namespaces found (no namespace declarations)",
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
    graph, edges = build_cs_graph(file_ns, import_rows)
    summary["files"] = files_scanned
    summary["namespaces"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 using 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 using 边）；防御：丢弃无法锚定的违例并诚实计数
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
