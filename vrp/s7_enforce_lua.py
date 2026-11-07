"""S7 架构执法 Lua 引擎：内置源级 require 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 lua/luarocks）：
- Lua 依赖边界 = 模块文件（每 .lua 文件一个 chunk，require 返回模块表；
  语言无命名空间系统——模块身份 = package.path 下的文件路径）→
  容器 = 文件（repo 相对 posix 路径含 .lua 后缀，与 Dart/Zig 引擎同
  哲学——语言单元即依赖单元；fid→file_path 直推无需读文件解析声明）。
- 依赖边 = require 实参字符串（括号/无括号/单双引号四形态 + level-0
  长括号实参两形态）。解析近似：点转斜杠 + package.path 不可知 →
  repo 根 仓内候选优先（./?.lua 与 ./?/init.lua 惯例布局），相对当前
  文件目录兜底；./ ../ 前缀仅相对当前文件目录；未命中 = 外部
  （luarocks/stdlib）跳过。动态 require（变量/拼接实参）不入图。
- CBM Lua IMPORTS 边存在但 line=None（lua-cbm-probe 实测，File→Folder，
  evidence=structural）→ 锚定行号必须自扫源码。CBM 内置 Lua 解析器
  （Module/Function/Variable 符号，路径式 qname lua-cbm-probe.lua.api）；
  code-atlas 同构支持（indexed 7 files, 16 symbols——agreement 材料）。
- 词法（等长空白，换行保留，前进保证处处）：行注释 --；块注释/长字符串
  长括号 =*[ 与 ]=*] level 配对（等号数匹配，未闭合截断文件尾——
  Lua 长括号不嵌套，level n 只被 level n 闭合）；单双引号短串反斜杠
  转义（含 \\z 跳空白延续）+ 未闭合截断行尾；require 目标区整体保留
  （同 C 引擎 include、Dart 引擎 URI、Zig 引擎 @import——require 实参
  是字符串语法不能随字符串空白化）：require 标识符（双边界）后跳
  空白与左括号，紧邻字符串字面量原样保留；非字符串实参（变量/拼接）
  正常空白化（动态 require 不入图）。
- 测试排除（文件级，同 Dart/Ruby 先例）：spec/test/tests 目录段或
  *_spec.lua / *_test.lua / test_*.lua 文件名约定（busted/luaunit）——
  排除文件容器仍入表（叶可见）、require 边不入图；tests_excluded =
  排除文件计数。
- 违例语义 = 传递闭包（十五引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 require 边 (file,line) 字典序首 + 违例文件首符号。
- 诚实局限（文档化不静默）：
  * package.path 自定义布局（如 src/?.lua 前缀）不解析——未命中候选
    视为外部跳过
  * 动态 require（变量/拼接实参）不入图；level>0 长括号实参不入图
    （罕见形态）
  * require 实参字符串内嵌 require 文本的病态模块名可能误报（与
    Zig 引擎 @import 同型理论局限）
  * node_modules/vendor/隐藏目录（含 .luarocks）跳过
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

LUA_SUFFIX = ".lua"
SKIP_DIRS = {"node_modules", "vendor"}  # .luarocks 走隐藏目录排除
_TEST_DIRS = {"spec", "test", "tests"}
_WS = " \t\r\n"
_IDENT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")

_REQUIRE_RE = re.compile(r"(?<![A-Za-z0-9_])require(?![A-Za-z0-9_])")


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串 → 空白（等长，换行保留）；require 实参字符串字面量
    原样保留（require 实参是字符串语法不能随字符串空白化）。

    前进保证处处（任何回退路径至少 +1）。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("--", i):  # 行注释或块注释（-- 后长括号开）
            level = _long_open(text, i + 2)
            if level is None:
                j = text.find("\n", i)
                if j < 0:
                    j = n
            else:
                j = _long_end(text, i + 2, level)  # 未闭合 → 文件尾
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif _require_at(text, i):  # require 目标区整体保留
            j = i + len("require")
            while j < n and text[j] in _WS:
                j += 1
            if j < n and text[j] == "(":
                j += 1
                while j < n and text[j] in _WS:
                    j += 1
            if j < n and text[j] in "\"'":
                i = _string_end(text, j)  # 引号实参保留（括号/无括号同形）
                continue
            if j < n and text.startswith("[[", j):  # level-0 长括号实参保留
                i = _long_end(text, j, 0)
                continue
            i = i + len("require")  # 非字符串实参（变量/拼接）：跳过关键字
        elif text[i] == "[" and _long_open(text, i) is not None:
            level = _long_open(text, i)
            j = _long_end(text, i, level)  # 长字符串（非 require 实参）空白化
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text[i] in "\"'":
            j = _string_end(text, i)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _require_at(text: str, i: int) -> bool:
    """位置 i 的 require 是全局函数调用形态（双词法边界）。"""
    if not text.startswith("require", i):
        return False
    if i > 0 and text[i - 1] in _IDENT_CHARS:
        return False
    j = i + len("require")
    n = len(text)
    return j >= n or text[j] not in _IDENT_CHARS


def _long_open(text: str, i: int) -> int | None:
    """长括号开 level（`[` `=*` `[`）；非长括号开 → None。"""
    n = len(text)
    if i >= n or text[i] != "[":
        return None
    j = i + 1
    while j < n and text[j] == "=":
        j += 1
    if j < n and text[j] == "[":
        return j - i - 1
    return None


def _long_end(text: str, start: int, level: int) -> int:
    """长括号内容结束位置（含闭合 `]=*]`）；未闭合截断文件尾（保守前进）。

    Lua 长括号不嵌套：level n 只被 `]` + n 个 `=` + `]` 闭合。
    """
    n = len(text)
    j = start + 2 + level
    while j < n:
        if text[j] == "]":
            k = j + 1
            eq = 0
            while k < n and text[k] == "=":
                k += 1
                eq += 1
            if eq == level and k < n and text[k] == "]":
                return k + 1
        j += 1
    return n


def _string_end(text: str, i: int) -> int:
    """短字符串字面量结束位置（含闭引号）；反斜杠转义跳过（含 \\z 跳
    空白延续）；未闭合截断行尾（保守前进）。
    """
    q = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            if j + 1 < n and text[j + 1] == "z":  # \\z 跳过后续空白（含换行）
                j += 2
                while j < n and text[j] in _WS:
                    j += 1
                continue
            j += 2
            continue
        if c == q:
            return j + 1
        if c == "\n":
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


def _iter_requires(blanked: str) -> list[dict]:
    """保留区 → [{path, line}]；行号 = require 记号行。

    实参后紧随 `..`（拼接）→ 动态 require 跳过；空实参跳过。
    """
    rows: list[dict] = []
    n = len(blanked)
    for m in _REQUIRE_RE.finditer(blanked):
        j = m.end()
        while j < n and blanked[j] in _WS:
            j += 1
        if j < n and blanked[j] == "(":
            j += 1
            while j < n and blanked[j] in _WS:
                j += 1
        arg, after = _read_arg(blanked, j)
        if arg is None:
            continue
        k = after
        while k < n and blanked[k] in _WS:
            k += 1
        if blanked.startswith("..", k):  # 字符串拼接 → 动态
            continue
        rows.append({"path": arg, "line": blanked.count("\n", 0, m.start()) + 1})
    return rows


def _read_arg(text: str, j: int) -> tuple[str | None, int]:
    """位置 j 起读 require 实参 → (实参, 实参后位置)；非字符串/空串 → (None, j)。"""
    n = len(text)
    if j < n and text[j] in "\"'":
        q = text[j]
        k = j + 1
        while k < n and text[k] != q and text[k] != "\n":
            k += 1
        if k < n and text[k] == q and k > j + 1:
            return text[j + 1 : k], k + 1
        return None, j
    if text.startswith("[[", j):
        k = text.find("]]", j + 2)
        if k > j + 2 and k < n:
            return text[j + 2 : k], k + 2
        return None, j
    return None, j


# ------------------------------------------------------------------ 扫描


def _is_test(rel: str) -> bool:
    """测试文件约定：spec/test/tests 目录段或 *_spec.lua / *_test.lua /
    test_*.lua 文件名（busted/luaunit 约定）。
    """
    parts = rel.split("/")
    if any(seg in _TEST_DIRS for seg in parts[:-1]):
        return True
    name = parts[-1]
    return name.endswith(("_spec.lua", "_test.lua")) or name.startswith("test_")


def scan_lua(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[set[str], list[dict], int, int, bool]:
    """repo → (file_set, import_rows, files_scanned, tests_excluded, timed_out)。

    file_set：全部扫描的 .lua 文件（容器 = 文件，含测试文件——叶可见）；
    import_rows：[{file, line, path}]（仅测试外文件的 require）；测试文件
    （目录段或命名约定）依赖边不入图、容器仍入表；tests_excluded：排除
    测试文件计数。超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_set: set[str] = set()
    import_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(LUA_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_set, import_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            file_set.add(rel)
            blanked = _blank_noncode(_read(repo, rel))
            if _is_test(rel):
                tests_excluded += 1
                continue
            for row in _iter_requires(blanked):
                import_rows.append({**row, "file": rel})
    return file_set, import_rows, files_scanned, tests_excluded, False


# ------------------------------------------------------------------ 图


def _resolve_require(src_rel: str, name: str, file_set: set[str]) -> str | None:
    """require 实参 → 仓内 .lua 文件（未命中 = 外部包 → None）。

    package.path 不可知 → 近似：点转斜杠 + repo 根候选优先（./?.lua 与
    ./?/init.lua 惯例布局），相对当前文件目录兜底；./ ../ 前缀仅相对
    当前文件目录；.lua 结尾实参字面路径宽容候选。
    """
    if name.startswith(("./", "../")):
        base = posixpath.dirname(src_rel)
        cands = [name] if name.endswith(LUA_SUFFIX) else [name + LUA_SUFFIX, posixpath.join(name, "init.lua")]
        for c in cands:
            cand = posixpath.normpath(posixpath.join(base, c))
            if cand in file_set:
                return cand
        return None
    seg = name.replace(".", "/")
    cands = [seg + LUA_SUFFIX, posixpath.join(seg, "init.lua")]
    if name.endswith(LUA_SUFFIX):
        cands.append(name)  # 字面路径宽容（require("x.lua") 惯用形态）
    for base in ("", posixpath.dirname(src_rel)):
        for c in cands:
            cand = posixpath.normpath(posixpath.join(base, c))
            if cand in file_set:
                return cand
    return None


def build_lua_graph(
    file_set: set[str],
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键 = 全部容器——漏叶假阴）+ 边明细（含 require 溯源）。"""
    graph: dict[str, set[str]] = {f: set() for f in file_set}
    edges: list[dict] = []
    for row in import_rows:
        dst = _resolve_require(row["file"], row["path"], file_set)
        if dst is None or dst == row["file"]:
            continue
        graph[row["file"]].add(dst)
        edges.append({"file": row["file"], "line": row["line"], "src": row["file"], "dst": dst})
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_lua_containers(
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
    lua_paths = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(LUA_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        lua_paths.add(p)
        containers[fid_to_module[fid]].add(p)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "lua_files": len(lua_paths),
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


def run_lua(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Lua 执法入口：契约生成（复用层 DAG）→ 源级 require 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "lua-require-parser",
        "structure_status": structure_status,
    }
    file_set, import_rows, files_scanned, tests_excluded, timed_out = scan_lua(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"lua scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_set:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no lua files found (no .lua files on disk)",
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
    graph, edges = build_lua_graph(file_set, import_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 require 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 require 边）；防御：丢弃无法锚定的违例并诚实计数
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
