"""S7 架构执法 Dart 引擎：内置源级 import URI 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 dart）：
- Dart 依赖边界 = library（库）；声明边 = import/export 的 URI。Dart 每文件
  默认即一个 library（library 指令可选且现代风格省略）→ 容器 = 文件
  （repo 相对 posix 路径含 .dart 后缀，与 C 引擎"目录容器"同哲学——语言
  单元即依赖单元；fid→file_path 直推无需读文件解析声明）。part 文件不是
  独立 library：owner 文件 part 指令声明归属，part fid 容器 = owner 文件
  路径（owner 侧 part 指令权威；part of 侧仅作语法不计——owner 未声明
  的 part 退化为其自身路径，诚实文档化）。
- import 与 export 都算依赖边（export 转发库也是对被转发库的依赖——
  Dart 生态依赖检查同口径）；part 指令不是依赖边（part 是 library 的
  组成部分）。library 指令名忽略（可选且常省略，路径才是稳定边界）。
- CBM Dart IMPORTS 边存在但 line=None（dart-cbm-probe 实测，File→Module，
  evidence=structural）→ 锚定行号必须自扫源码。CBM 内置 Dart 解析器
  （Module 符号路径式 qname）→ 语法验证仅 dogfood 走 CBM 索引成功 +
  xcheck 记实（code-atlas Dart 调用抽取为零——支持弱同 C 引擎先例，
  不阻塞，agreement 诚实记录）。
- 词法（等长空白，换行保留，前进保证处处）：行注释 //；块注释可嵌套
  （深度计数——同 Kotlin/Scala，Dart 规范允许嵌套）；字符串单双引号
  （反斜杠转义；单行字符串不跨行——未闭合截断行尾保守前进）；raw
  字符串 r'...' r"..."（前导 r 紧邻引号且 r 前非标识符字符——raw 内
  转义不生效，与普通串分开扫描）；三引号串跨行（闭三引号序列；反斜杠
  转义跳过；raw 三引号转义不生效——review 批修正：原不分 raw 尾反斜杠
  转义吞闭引号首枚致整文件假阴性）；插值 $var / ${expr} 内嵌引号不穿透
  （词法局限同 kt/php/ruby）。Dart 无字符字面量/正则字面量/heredoc——
  词法面显著小于 Ruby。
- import/export/part 指令目标区整体保留（同 C 引擎 include、Ruby 引擎
  require 的引号目标——指令 URI 是字符串语法不能随字符串空白化）：
  关键字（前一非空白字符为标识符/引号/句点时拒绝——命名实参 import: 'x'
  与成员访问不误入）后跳空白（可跨行）、间隙注释（review 批修正——原
  间隙注释致 URI 空白化丢边假阴性；行/块注释等长空白化换行保留后继续）
  与可选 raw 前缀，紧邻字符串字面量原样保留；part of 串不保留（of
  挡住目标判定，owner 侧才计归属）。
- URI 解析：相对 URI 相对当前文件目录（percent-decode 后 posix 归一，
  dart: SDK 与未命中跳过）；package: URI 就近 pubspec.yaml 判包——
  从导入文件目录向上找首个 pubspec.yaml，name 匹配则其 lib/ 子路径
  （不匹配或无 pubspec = 外部包跳过；monorepo 跨包 import 视为外部，
  诚实文档化；pubspec name 行尾注释容忍——YAML `#` 前空白语义，
  review 批修正：原不容行尾注释致 name=None 全外部假阴性）。插值
  URI（$ 记号）动态依赖不入图。
- 测试排除：路径含 test/tests/integration_test 目录段或 *_test.dart
  文件名（Dart test/ 约定）——import/export 不入图，文件容器仍入表
  （叶可见，同各引擎哲学）。
- 违例语义 = 传递闭包（十二引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 import/export 边 (file,line) 字典序首 + 违例文件首符号。
- 诚实局限（文档化不静默）：
  * 相邻字符串拼接 URI（import 'a' 'b'; 非法 Dart 形态）只取首段
  * 条件 import（if (dart.library.x) 'b.dart'）取默认 URI，条件分支不入图
  * 插值 URI（动态依赖）不入图
  * import augment 'x.dart'（Dart 3 增强指令实验性）不入图——augment
    关键字挡住目标判定
  * build/node_modules/vendor/隐藏目录（含 .dart_tool）跳过（产物无源码语义）
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
from urllib.parse import unquote

from .s7_enforce import (
    DEFAULT_TIMEOUT,
    FINDINGS_SOURCE,
    KIND,
    PATHOLOGY,
    build_contracts,
)

DART_SUFFIX = ".dart"
SKIP_DIRS = frozenset({"build", "node_modules", "vendor"})  # .dart_tool 走隐藏目录排除
_TEST_DIRS = ("test", "tests", "integration_test")
# 指令关键字前一非空白字符 ∈ 此集合 → 非指令位置（标识符尾/成员访问/串后）
_DIRECTIVE_BLOCKERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$.'\""
)
_IDENT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$"
)

_DIRECTIVE_RE = re.compile(r"\b(import|export|part)\b[ \t\r\n]*r?(['\"])([^'\"\n]+)\2")
_PUBSPEC_NAME_RE = re.compile(
    r"^name:\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?(?:\s+#[^\n]*)?\s*$", re.M
)


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串 → 空白（等长，换行保留）；import/export/part 指令目标
    字符串字面量原样保留（指令 URI 是字符串语法不能随字符串空白化）。

    前进保证处处（任何回退路径至少 +1）。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = _block_comment_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "'\"":
            raw = i > 0 and text[i - 1] == "r" and (i < 2 or text[i - 2] not in _IDENT_CHARS)
            if text.startswith(c * 3, i):
                j = _triple_end(text, i, raw)
            else:
                j = _string_end(text, i, raw)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j if j > i else i + 1
        else:
            word = _directive_at(text, i)
            if word is None:
                i += 1
                continue
            wlen = len(word)
            k = i + wlen
            # 关键字-目标间隙：空白与注释均跳过（review 批修正——原仅空白，
            # 间隙注释被下方注释分支空白化后 URI 丢失假阴性；注释等长
            # 空白化换行保留，扫描继续）
            while True:
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k + 1 < n and text[k] == "/" and text[k + 1] == "/":
                    j = text.find("\n", k)
                    j = j if j >= 0 else n
                    for x in range(k, j):
                        out[x] = " "
                    k = j
                    continue
                if k + 1 < n and text[k] == "/" and text[k + 1] == "*":
                    j = _block_comment_end(text, k)
                    for x in range(k, min(j, n)):
                        if out[x] != "\n":
                            out[x] = " "
                    k = j
                    continue
                break
            raw = k < n and text[k] == "r" and k + 1 < n and text[k + 1] in "'\""
            if raw:
                k += 1
            if k < n and text[k] in "'\"":
                i = _string_end(text, k, raw)  # 目标区整体保留（含 raw 前缀）
            else:
                i = i + wlen  # 非字符串后继（part of 等）：跳过关键字（前进保证）
    return "".join(out)


def _directive_at(text: str, i: int) -> str | None:
    """位置 i 是指令关键字（import/export/part）→ 关键字；否则 None。

    前一非空白字符为标识符/引号/句点 → 非语句位置（命名实参/成员访问）。
    """
    for word in ("import", "export", "part"):
        if not text.startswith(word, i):
            continue
        if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
            return None
        j = i + len(word)
        if j < len(text) and (text[j].isalnum() or text[j] == "_"):
            continue
        if _prev_char(text, i) in _DIRECTIVE_BLOCKERS:
            return None
        return word
    return None


def _prev_char(text: str, i: int) -> str:
    """前一非空白字符（无则换行符——行首/文件首按语句位置处理）。"""
    j = i - 1
    while j >= 0 and text[j] in " \t\r":
        j -= 1
    return text[j] if j >= 0 else "\n"


def _string_end(text: str, i: int, raw: bool) -> int:
    """字符串字面量结束位置（含闭引号）。

    普通串反斜杠转义跳过；raw 串转义不生效（Dart 语义）；单行字符串
    未闭合截断行尾（保守前进）。
    """
    q = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        if not raw and text[j] == "\\":
            j += 2
            continue
        if text[j] == q:
            return j + 1
        if text[j] == "\n":
            break
        j += 1
    j = text.find("\n", i)
    return j if j >= 0 else n


def _triple_end(text: str, i: int, raw: bool = False) -> int:
    """三引号串结束位置（闭三引号序列；反斜杠转义跳过；未闭合文件尾）。

    raw 三引号串转义不生效（Dart 语义——闭三引号按原文扫描；review 批
    修正：原不区分 raw，尾反斜杠转义跳过吞掉闭引号首枚致整文件假阴性）。
    """
    q = text[i] * 3
    j = i + 3
    n = len(text)
    while j < n:
        if not raw and text[j] == "\\":
            j += 2
            continue
        if text.startswith(q, j):
            return j + 3
        j += 1
    return n


def _block_comment_end(text: str, i: int) -> int:
    """嵌套块注释结束位置（/* 深度 +1、*/ -1；未闭合文件尾——前进保证）。"""
    depth = 0
    j = i
    n = len(text)
    while j < n:
        if text.startswith("/*", j):
            depth += 1
            j += 2
        elif text.startswith("*/", j):
            depth -= 1
            j += 2
            if depth == 0:
                return j
        else:
            j += 1
    return n


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ 解析


def _iter_directives(blanked: str) -> list[dict]:
    """保留区 → [{kind, uri, line}]；插值 URI（$ 记号，动态依赖）跳过。"""
    rows: list[dict] = []
    for m in _DIRECTIVE_RE.finditer(blanked):
        uri = m.group(3)
        if "$" in uri:
            continue
        line = blanked.count("\n", 0, m.start()) + 1
        rows.append({"kind": m.group(1), "uri": uri, "line": line})
    return rows


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    if any(seg in _TEST_DIRS for seg in parts[:-1]):
        return True
    return parts[-1].endswith("_test.dart")


def _pubspec_name(path: Path) -> str | None:
    """pubspec.yaml → 包名（name: 行，可带引号）；缺失/无 name → None。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _PUBSPEC_NAME_RE.search(text)
    return m.group(1) if m else None


# ------------------------------------------------------------------ 扫描


def scan_dart(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[set[str], dict[str, str], list[dict], int, int, bool]:
    """repo → (file_set, part_map, import_rows, files_scanned, tests_excluded, timed_out)。

    file_set：全部扫描的 .dart 文件（容器 = 文件，含测试文件——叶可见）；
    part_map：part 文件 → owner 文件（owner 侧 part 指令权威，目标未扫描
    则不入表）；import_rows：[{file, line, kind, uri}]（仅测试外文件的
    import/export；kind 保留 import/export）；测试文件（目录段或命名约定）
    依赖边不入图、容器仍入表。超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_set: set[str] = set()
    part_raw: dict[str, str] = {}
    import_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(DART_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_set, {}, import_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            file_set.add(rel)
            blanked = _blank_noncode(_read(repo, rel))
            is_test = _is_test(rel)
            if is_test:
                tests_excluded += 1
            for row in _iter_directives(blanked):
                if row["kind"] == "part":
                    target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(rel), row["uri"])
                    )
                    part_raw[target] = rel
                    continue
                if is_test:
                    continue
                import_rows.append({**row, "file": rel})
    part_map = {t: o for t, o in part_raw.items() if t in file_set}
    return file_set, part_map, import_rows, files_scanned, tests_excluded, False


def _resolve_uri(
    repo: Path,
    src_rel: str,
    uri: str,
    file_set: set[str],
) -> str | None:
    """import/export URI → 仓内 .dart 文件（未命中 = 外部/SDK → None）。

    package: URI 就近 pubspec.yaml 判包（name 匹配 → lib/ 子路径；不匹配
    或无 pubspec → 外部包跳过——monorepo 跨包 import 视为外部文档化）。
    """
    if uri.startswith("dart:"):
        return None
    if uri.startswith("package:"):
        rest = uri[len("package:"):]
        pkg, sep, sub = rest.partition("/")
        if not sep or not sub:
            return None
        owner_dir = posixpath.dirname(src_rel)
        while True:
            if _pubspec_name(repo / owner_dir / "pubspec.yaml") == pkg:
                cand = posixpath.normpath(
                    posixpath.join(owner_dir, "lib", unquote(sub))
                )
                return cand if cand in file_set else None
            if not owner_dir:
                return None
            owner_dir = posixpath.dirname(owner_dir)
    cand = posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), unquote(uri)))
    return cand if cand in file_set else None


def build_dart_graph(
    repo: Path,
    file_set: set[str],
    part_map: dict[str, str],
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键 = 全部容器——漏叶假阴）+ 边明细（含 import/export 溯源）。"""
    graph: dict[str, set[str]] = {
        part_map.get(f, f): set() for f in file_set
    }
    edges: list[dict] = []
    for row in import_rows:
        dst_file = _resolve_uri(repo, row["file"], row["uri"], file_set)
        if dst_file is None:
            continue
        src = part_map.get(row["file"], row["file"])
        dst = part_map.get(dst_file, dst_file)
        if src == dst:
            continue
        graph[src].add(dst)
        edges.append({"file": row["file"], "line": row["line"], "src": src, "dst": dst})
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_dart_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序容器集（fid→file_path→文件容器，part 归并 owner）。

    仅当 payload 符号含 .dart 文件才扫描；非 .dart 扩展名观测；超时诚实
    上报——dart_files 供 run() 路由（有 .dart 但全被删也要进 dart 引擎
    给出可行动的 not-enforceable）。
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
    dart_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(DART_SUFFIX)
    }
    part_map: dict[str, str] = {}
    timed_out = False
    if dart_paths:
        _files, part_map, _rows, _tests, _excl, timed_out = scan_dart(repo, timeout)
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(DART_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        containers[fid_to_module[fid]].add(part_map.get(p, p))
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "dart_files": len(dart_paths),
        "timed_out": timed_out,
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


def run_dart(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Dart 执法入口：契约生成（复用层 DAG）→ 源级 import URI 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "dart-import-parser",
        "structure_status": structure_status,
    }
    file_set, part_map, import_rows, files_scanned, tests_excluded, timed_out = scan_dart(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"dart scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_set:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no dart libraries found (no .dart files on disk)",
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
    graph, edges = build_dart_graph(repo, file_set, part_map, import_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 import/export 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 import/export 边）；防御：丢弃无法锚定的违例并诚实计数
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
