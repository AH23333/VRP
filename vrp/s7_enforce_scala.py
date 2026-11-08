"""S7 架构执法 Scala 引擎：内置源级 package/import 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 scalac/sbt）：
- CBM Scala IMPORTS 边为 0（scala-cbm-probe 实测）→ 依赖图纯自扫；源级
  package/import 解析纯 stdlib 零构建零工具链（与 rs/java/cs/kt/php 引擎同路线），
  无 enforcer-not-found 态；编译验证仅在 dogfood 探针走 Maven scala-maven-plugin。
- 容器 = Scala 包。package 三形态：平铺 `package a.b`（无分号要求）；packaging 块
  `package a.b { ... }`；嵌套链 `package a { package b { ... } }` 取拼接首链
  （a.b——chained packaging 为 legacy 罕见形态，文档化近似）；多 package 块
  文件取首个声明。
- 词法与 Java 的关键差异：块注释**可嵌套**（Scala 语法 /* /* */ */ 合法——深度计数，
  异于 Java）；三引号字符串（3 枚连续双引号开合）终止于其后不再紧跟双引号的
  首个三引号序列（故 4 连引号收尾时第 1 枚属于内容，实证）；插值字符串
  s/f/raw 前缀不特殊处理（内嵌引号不穿透——词法局限同 kt/php）；'x' 字符字面量
  与 'symbol 符号字面量共用短距回退判别（无闭合引号则视为普通字符）。
- import 全形态统一捕获点链：单类型 `import a.b.C`；Scala 2 通配 `import a.b._`
  （随链捕获为 a.b._，前缀解析时剥落）；Scala 3 通配 `import a.b.*`（* 不入链）；
  选择器 `import a.b.{C, D}` / 重命名 `{C => D}` / 成员通配 `import a.b.C._`
  （选择器与通配均在链后截断）。import/package 目标与关键字同行
  （review 批修正：`\s+` 改 `[ \t]+`——Scala 换行即语句终止，无续行运算符，
  原 `import\ncom.probe.Foo` 残缺文本被跨行捕获为伪边/伪包）。
- package 声明排除 `package object` 子句（review 批修正：`package a { package
  object b }` 原 `(?!object)` 缺失把 object 误捕获为包名段 → a.object 错包；
  `package objectx` 合法包名不误伤——`\b` 边界精确区分）。
- import 目标解析 = 已知包集合最长前缀（`com.probe.store.Storage` → com.probe.store；
  外部依赖 scala.* / java.util.* 等无前缀命中 → 跳过）；同包 import 不成自环。
- 测试代码排除（与 java /test/java/、kt /test/kotlin/ 对齐）：路径含 /test/scala/
  段（sbt/Maven Scala 测试源根约定）的文件不入 import 图（其包仍入图——叶包
  可见，测试 fid 的容器推导不受影响）；main 源根内 *Test.scala 不排除（保守按生产计）。
- 违例语义 = 传递闭包（九引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 import 边所在文件+行号（同包多文件满足同一边时取 (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 无 package 声明（默认包）文件不入图不参与执法（orphan 计数）
  * .sc（worksheet/Ammonite 脚本）不匹配 .scala 后缀，不参与执法
  * 多 packaging 块文件仅取首链（首声明近似，文件级容器粒度不受影响）
  * XML 字面量（Scala 2 legacy，2.13 起废弃）不识别——内嵌引号/注释可能导致词法漂移
  * Scala 3 `export` 子句（export a.b.*——成员转发依赖边）不入图（诚实局限，
    import 才是本引擎的声明边口径；export 支持留待后续批）
  * raw 插值字符串内的转义序列按字面处理，误判为普通字符串词法（文档化微局限）
  * 非标准测试源根不识别（按生产计，保守方向）
  * 构建/隐藏目录（target/build/node_modules/.*）跳过（产物无源码语义）
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

SCALA_SUFFIX = ".scala"
TEST_SOURCE_ROOT = "/test/scala/"
SKIP_DIRS = {"target", "build", "node_modules"}

# package 后目标同行（Scala 换行即语句终止——\n 跨行不收，病态残缺代码不产伪包）；
# (?!object\b) 拒 package object 子句（review 批修正：原 `package a { package object b }`
# 把 object 误捕获为包名段 → a.object 错包；\b 保 package objectx 合法包名不误伤）
_PACKAGE_RE = re.compile(
    r"\bpackage[ \t]+(?!object\b)([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)
# import 后目标同行（同上——Scala 无续行运算符，跨行形态必为残缺代码）
_IMPORT_RE = re.compile(
    r"\bimport[ \t]+"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释（嵌套块）/字符串/三引号字符串/字符 → 空白（等长，换行保留）。"""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        two = text[i : i + 2]
        three = text[i : i + 3]
        if two == "//":
            j = i
            while j < n and text[j] != "\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "/*":
            # Scala 块注释可嵌套（SLS 1.3.2，异于 Java）：深度计数至配平闭合
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif text[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif three == '"""':
            # 三引号字符串：终止于其后不再紧跟双引号的首个三引号序列
            #（"""a"""" 内容为 a"——4 连引号收尾时第 1 枚属于内容，实证）
            j = i + 3
            while True:
                j = text.find('"""', j)
                if j < 0:
                    j = n
                    break
                if j + 3 < n and text[j + 3] == '"':
                    j += 1
                    continue
                j = min(j + 3, n)
                break
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
            i = j if j > i else i + 1  # 'symbol 无闭合 → 回退前进（防死循环，同 c/rs 守卫）
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


def _char_end(text: str, i: int) -> int:
    """字符字面量 'x' / '\\n' / '\\uXXXX' 结束位置；非字面量（短距无闭合）→ i。

    Scala 'symbol 符号字面量无闭合引号——短距无配对时按普通字符回退（不吞后续词法）。
    """
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


def _block_end(blanked: str, pos: int) -> int | None:
    """pos 后首个非空白字符为 '{' 时返回其配平 '}' 之后位置；否则 None。"""
    n = len(blanked)
    i = pos
    while i < n and blanked[i] in " \t\r\n":
        i += 1
    if i >= n or blanked[i] != "{":
        return None
    depth = 1
    j = i + 1
    while j < n and depth:
        if blanked[j] == "{":
            depth += 1
        elif blanked[j] == "}":
            depth -= 1
        j += 1
    return j


def _parse_package(blanked: str) -> str | None:
    """首个 package 声明 → 包名（packaging 块/嵌套链拼接首链；多块取首）。"""
    m = _PACKAGE_RE.search(blanked)
    if m is None:
        return None
    parts = [m.group(1)]
    end = _block_end(blanked, m.end())
    while end is not None:
        m2 = _PACKAGE_RE.search(blanked, m.end(), end)
        if m2 is None:
            break
        parts.append(m2.group(1))
        m = m2
        end = _block_end(blanked, m.end())
    return ".".join(parts)


def _iter_imports(blanked: str) -> list[tuple[str, int]]:
    """已空白化文本 → [(import 点分目标, 行号)]（选择器/通配在链后截断）。"""
    rows: list[tuple[str, int]] = []
    for m in _IMPORT_RE.finditer(blanked):
        line = blanked.count("\n", 0, m.start()) + 1
        rows.append((m.group(1), line))
    return rows


# ------------------------------------------------------------------ 扫描


def scan_scala(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, str], list[dict], int, int, bool]:
    """repo → (file_pkg, import_rows, files_scanned, tests_excluded, timed_out)。

    file_pkg：repo 相对 posix 路径 → 包名（无 package 声明不入表 = orphan）；
    import_rows：[{file, line, target}]（仅测试源根外文件；target 为未解析原始路径）；
    测试源根（/test/scala/ 段）文件不计 import（包仍入 file_pkg——叶包可见）。
    超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_pkg: dict[str, str] = {}
    import_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(SCALA_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_pkg, import_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            blanked = _blank_noncode(_read(repo, rel))
            pkg = _parse_package(blanked)
            if pkg is None:
                continue
            file_pkg[rel] = pkg
            if TEST_SOURCE_ROOT in "/" + rel:
                tests_excluded += 1
                continue
            for target, line in _iter_imports(blanked):
                import_rows.append({"file": rel, "line": line, "target": target})
    return file_pkg, import_rows, files_scanned, tests_excluded, False


def _resolve_import(target: str, packages: set[str]) -> str | None:
    """import 点分路径 → 已知包最长前缀；外部依赖（无前缀命中）→ None。"""
    segs = target.split(".")
    for k in range(len(segs), 0, -1):
        cand = ".".join(segs[:k])
        if cand in packages:
            return cand
    return None


def build_scala_graph(
    file_pkg: dict[str, str],
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """包依赖图（键 = 全部包——叶包无入边也入图，漏叶会假阴）+ 边明细。"""
    packages = set(file_pkg.values())
    graph: dict[str, set[str]] = {p: set() for p in packages}
    edges: list[dict] = []
    for row in import_rows:
        pkg = file_pkg.get(row["file"])
        if pkg is None:
            continue
        target = _resolve_import(row["target"], packages)
        if target is None or target == pkg:
            continue
        graph[pkg].add(target)
        edges.append({"file": row["file"], "line": row["line"], "src": pkg, "dst": target})
    return {p: sorted(t) for p, t in graph.items()}, edges


def derive_scala_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序包名集（fid→file_path→package 声明）。

    仅当 payload 符号含 .scala 文件才扫描；orphan（.scala 无 package 声明或磁盘
    缺失，去重路径）与超时诚实上报——scala_files 供 run() 路由（有 .scala 但
    全 orphan 也要进 scala 引擎给出可行动的 not-enforceable）。
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
    scala_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(SCALA_SUFFIX)
    }
    file_pkg: dict[str, str] = {}
    timed_out = False
    if scala_paths:
        file_pkg, _imports, _files, _tests, timed_out = scan_scala(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(SCALA_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        name = file_pkg.get(p)
        if name is None:
            orphans.add(p)
            continue
        containers[fid_to_module[fid]].add(name)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "scala_files": len(scala_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(pkg: str, prefixes: list[str]) -> bool:
    """包名 ∈ 容器集（容器可为父包——com.probe 捕获 com.probe.store 子包）。"""
    return any(pkg == p or pkg.startswith(p + ".") for p in prefixes)


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


def run_scala(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Scala 执法入口：契约生成（复用层 DAG）→ 源级 import 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "scala-package-parser",
        "structure_status": structure_status,
    }
    file_pkg, import_rows, files_scanned, tests_excluded, timed_out = scan_scala(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"scala scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_pkg:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no scala packages found (no package declarations)",
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
    graph, edges = build_scala_graph(file_pkg, import_rows)
    summary["files"] = files_scanned
    summary["packages"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 import 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 import 边）；防御：丢弃无法锚定的违例并诚实计数
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
