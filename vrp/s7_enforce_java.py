"""S7 架构执法 Java 引擎：内置源级 package/import 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，javac 21.0.6 本机）：
- jdeps 只吃编译产物 .class（须先 mvn/gradle 全量构建——重构建依赖，本机无 gradle）；
  源级 package/import 解析纯 stdlib 零构建零工具链（与 rs 引擎同路线），
  无 enforcer-not-found 态。
- 容器 = Java 包（`package x.y.z;` 声明）。CBM Java 符号 qualified name 是路径式
  （project.src.main.java.com.probe...），故容器一律 fid→file_path→读文件解析 package。
- import 解析：单类型/静态/通配（`import a.b.C;` `import static a.b.C.m;` `import a.b.*;`）；
  Java 是自由格式语言——token 间任意空白（含换行）合法，package/import 段间点号
  两侧换行宽容（`import a.\\nb.C;` 合法，javac 21 探针实证；与 Kotlin 普通串
  禁跨行相反，勿跨语言照搬运修）；
  注释（行/块）/字符串/字符/文本块先空白化（保长度保行号）。
- import 目标解析 = 已知包集合最长前缀（`com.probe.store.Store` → com.probe.store；
  `import static com.probe.util.Log.log;` → com.probe.util；外部依赖 java.util.* 等
  无前缀命中 → 跳过）；同包 import 不成自环。
- 测试代码排除（与 go TestImports / rs cfg(test) 对齐）：路径含 /test/java/ 段
  （Maven/Gradle 测试源根约定）的文件不入 import 图（其包仍入图——叶包可见，
  测试 fid 的容器推导不受影响）；main 源根内 *Test.java 不排除（保守按生产计）。
- 违例语义 = 传递闭包（import-linter forbidden / go / rs 同义）：BFS 最短链入 payload；
  锚定 = 链尾直接 import 边所在文件+行号（同包多文件满足同一边时取 (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 无 package 声明（默认包）文件不入图不参与执法（orphan 计数）
  * 非标准测试源根（src/it/java 等）不识别（按生产计，保守方向）
  * 构建/隐藏目录（target/build/node_modules/.*）跳过（产物无源码语义）
  * 扫描超时 → enforcer-failed（部分图不可信，不静默截断）
  * `$` 标识符（JLS 3.8 合法但实践罕见）段正则不匹配（病态不修）
  * `\\uXXXX` 预处理先于词法（JLS 3.3）：行注释内 `\\u000A` 实为换行会终结
    注释使后文成活代码，引擎按注释内容处理（经典 gotcha，病态不修）
  * 未闭合文本块（WIP）空白化至 EOF（javac 同报未闭合编译错，不承诺）
  * BOM：javac 21 拒绝 U+FEFF（非法字符编译错）——有效源码无 BOM，
    引擎宽容解析不构成执法偏差（C 引擎 BOM 假阴场景在 Java 不存在）
  * 未闭合普通串/字符字面量（WIP）→ 行尾截断/回退前进（不吞后续行代码）
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

JAVA_SUFFIX = ".java"
TEST_SOURCE_ROOT = "/test/java/"
SKIP_DIRS = frozenset({"target", "build", "node_modules"})

_SEG = r"[A-Za-z_][A-Za-z0-9_]*"
_PACKAGE_RE = re.compile(r"\bpackage\s+(" + _SEG + r"(?:\s*\.\s*" + _SEG + r")*)\s*;")
_IMPORT_RE = re.compile(
    r"\bimport\s+(?:static\s+)?"
    r"(" + _SEG + r"(?:\s*\.\s*" + _SEG + r")*)"
    r"(?:\s*\.\s*\*)?\s*;"
)


def _clean_name(raw: str) -> str:
    """自由格式捕获组去空白（段间换行/缩进合法，包名本身无空白）。"""
    return re.sub(r"\s+", "", raw)


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串/字符/文本块 → 空白（等长，换行保留）——代码位置与行号不变。"""
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
            # Java 块注释不嵌套（JLS 3.7）：首个 */ 闭合
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif three == '"""' and i + 3 < n and text[i + 3] in "\r\n":
            # Java 15+ 文本块：开符后必须行终止符（JLS 3.10.6；javac 探针：缺行
            # 终止符即编译错，如 4 连引号——非块起始走串分支优雅降级不吞 EOF）
            j = _text_end(text, i)
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
            i = j if j > i else i + 1  # 非闭合字符（WIP 文件）→ 回退前进（防死循环，同 c/rs 守卫）
        else:
            i += 1
    return "".join(out)


def _string_end(text: str, i: int) -> int:
    """普通串结束位置；未闭合（WIP/病态）→ 行尾截断（JLS 3.10.5 串内禁换行，
    javac 探针实证未闭合串编译错——换行后是代码不是串，防吞码假阴，kt/cs 同家族修）。"""
    j = i + 1
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if c == "\n":
            return j  # 未闭合串：行截断（换行符本身保留）
        j += 1
    return n


def _text_end(text: str, i: int) -> int:
    """文本块闭合位置（转义感知）：反斜杠转义对跳过——转义引号不计入闭合
    （JLS 3.10.6：3+ 连引号须至少一个转义；javac/jshell 21 探针实证单引号
    转义技巧产出裸 3 引号内容且不闭合块——裸扫描会提前闭合产幻影代码）；
    未闭合（WIP）→ EOF（编译错误文件不承诺，docstring 诚实局限）。"""
    j = i + 3
    n = len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if text[j : j + 3] == '"""':
            return j + 3
        j += 1
    return n


def _char_end(text: str, i: int) -> int:
    """字符字面量 'x' / '\\n' / '\\uXXXX' 结束位置；非字面量（12 字符内无闭合
    或遇换行，均 WIP 文件）→ i（调用方回退前进，不吞下一行代码）。"""
    j = i + 1
    n = len(text)
    while j < n and j - i <= 12:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            return j + 1
        if c == "\n":
            return i
        j += 1
    return i


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_package(blanked: str) -> str | None:
    m = _PACKAGE_RE.search(blanked)
    return _clean_name(m.group(1)) if m else None


def _iter_imports(blanked: str) -> list[tuple[str, int]]:
    """已空白化文本 → [(import 点分目标, 行号)]（static/通配统一剥壳为目标路径；
    自由格式段间空白去净）。"""
    rows: list[tuple[str, int]] = []
    for m in _IMPORT_RE.finditer(blanked):
        line = blanked.count("\n", 0, m.start()) + 1
        rows.append((_clean_name(m.group(1)), line))
    return rows


# ------------------------------------------------------------------ 扫描


def scan_java(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, str], list[dict], int, int, bool]:
    """repo → (file_pkg, import_rows, files_scanned, tests_excluded, timed_out)。

    file_pkg：repo 相对 posix 路径 → 包名（无 package 声明不入表 = orphan）；
    import_rows：[{file, line, target}]（仅测试源根外文件；target 为未解析原始路径）；
    测试源根（/test/java/ 段）文件不计 import（包仍入 file_pkg——叶包可见）。
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
            if not name.lower().endswith(JAVA_SUFFIX):
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


def build_java_graph(
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


def derive_java_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序包名集（fid→file_path→package 声明）。

    仅当 payload 符号含 .java 文件才扫描；orphan（.java 无 package 声明或磁盘
    缺失，去重路径）与超时诚实上报——java_files 供 run() 路由（有 .java 但
    全 orphan 也要进 java 引擎给出可行动的 not-enforceable）。
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
    java_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(JAVA_SUFFIX)
    }
    file_pkg: dict[str, str] = {}
    timed_out = False
    if java_paths:
        file_pkg, _imports, _files, _tests, timed_out = scan_java(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(JAVA_SUFFIX):
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
        "java_files": len(java_paths),
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


def run_java(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Java 执法入口：契约生成（复用层 DAG）→ 源级 import 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "java-import-parser",
        "structure_status": structure_status,
    }
    file_pkg, import_rows, files_scanned, tests_excluded, timed_out = scan_java(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"java scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_pkg:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no java packages found (no package declarations)",
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
    graph, edges = build_java_graph(file_pkg, import_rows)
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
