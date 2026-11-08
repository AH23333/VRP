"""S7 架构执法 Kotlin 引擎：内置源级 package/import 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 kotlinc/gradle）：
- 源级 package/import 解析纯 stdlib 零构建零工具链（与 rs/java/cs 引擎同路线），
  无 enforcer-not-found 态；编译验证仅在 dogfood 探针走 Maven kotlin-maven-plugin。
- 容器 = Kotlin 包（`package x.y.z` 声明，分号可选）。CBM Kotlin 符号 qualified name
  是路径式（kt-cbm-probe.src.main.kotlin.com.probe...），故容器一律 fid→file_path→
  读文件解析 package（与 java/cs 同策略）。
- 词法与 Java 的关键差异：块注释**可嵌套**（Kotlin 语法 /* /* */ */ 合法——深度计数）；
  raw string（三引号字符串）；package/import **分号可选**（行尾或 EOF 均终止——
  文件末行无换行不丢声明）；import 别名 `import a.b.C as D`（取左侧目标）；
  import 通配 `import a.b.*`；关键字与目标间仅空格/Tab（Kotlin 语法
  packageHeader/importHeader 产生式在该处无 NL 记号——`import⏎com.x` 非法，
  正则按 `[ \t]+` 严格匹配）；BOM 免疫（\ufeff 非词字符，\b 边界成立）。
- import 目标解析 = 已知包集合最长前缀（`com.probe.store.Storage` → com.probe.store；
  外部依赖 kotlin.* / java.util.* 等无前缀命中 → 跳过）；同包 import 不成自环。
- 测试代码排除（与 java /test/java/ 对齐）：路径含 /test/kotlin/ 段
  （Maven/Gradle Kotlin 测试源根约定）的文件不入 import 图（其包仍入图——叶包
  可见，测试 fid 的容器推导不受影响）；main 源根内 *Test.kt 不排除（保守按生产计）。
- 违例语义 = 传递闭包（六引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 import 边所在文件+行号（同包多文件满足同一边时取 (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 无 package 声明（默认包）文件不入图不参与执法（orphan 计数）
  * .kts 脚本（build.gradle.kts 等）不入 .kt 后缀匹配，不参与执法（构建脚本非生产源码）
  * 字符串模板 ${} 内嵌引号（如 "${map["k"]}"）词法不穿透——普通字符串在模板内
    首个裸引号处提前截断，残余按代码扫描；package/import 位于该残余的概率极低
  * 未闭合普通字符串（WIP 文件）换行即截断（Kotlin 普通串不得含字面换行，跨行
    须 raw string）——不吞后续行的真 package/import
  * backtick 标识符（如 val `import`、含空格的测试函数名）词法不识别——病态不修
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

KT_SUFFIX = ".kt"
TEST_SOURCE_ROOT = "/test/kotlin/"
SKIP_DIRS = {"target", "build", "node_modules"}

# 终结符 [;\r\n]|$：分号可选且行尾/EOF 均终止（文件末行无换行不丢声明）；
# 关键字与目标间仅 [ \t]+：Kotlin 语法 packageHeader/importHeader 产生式在
# 'package'/'import' 与 identifier 之间无 NL 记号（import⏎com.x 非法）。
_PACKAGE_RE = re.compile(
    r"\bpackage[ \t]+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"[ \t]*(?:[;\r\n]|$)"
)
_IMPORT_RE = re.compile(
    r"\bimport[ \t]+"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:\.\*)?"
    r"(?:[ \t]+as[ \t]+[A-Za-z_][A-Za-z0-9_]*)?"
    r"[ \t]*(?:[;\r\n]|$)"
)


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释（嵌套块）/字符串/raw string/字符 → 空白（等长，换行保留）。"""
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
            # Kotlin 块注释可嵌套（异于 Java）：深度计数至配平闭合
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
            # raw string：首个 """ 闭合（内部 "" 与转义均不终止）
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
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
    """普通字符串结束位置；换行即截断（Kotlin 普通串不得含字面换行，跨行须 raw）。

    未闭合串（WIP 文件）在行尾截断——不吞后续行的真 package/import；
    行尾反斜杠不越过换行（反斜杠+换行非合法转义，视同未闭合）。
    """
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\n":
            return j
        if text[j] == "\\" and text[j + 1 : j + 2] != "\n":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
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
        j += 1
    return i


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_package(blanked: str) -> str | None:
    m = _PACKAGE_RE.search(blanked)
    return m.group(1) if m else None


def _iter_imports(blanked: str) -> list[tuple[str, int]]:
    """已空白化文本 → [(import 点分目标, 行号)]（别名/通配统一剥壳为目标路径）。"""
    rows: list[tuple[str, int]] = []
    for m in _IMPORT_RE.finditer(blanked):
        line = blanked.count("\n", 0, m.start()) + 1
        rows.append((m.group(1), line))
    return rows


# ------------------------------------------------------------------ 扫描


def scan_kt(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, str], list[dict], int, int, bool]:
    """repo → (file_pkg, import_rows, files_scanned, tests_excluded, timed_out)。

    file_pkg：repo 相对 posix 路径 → 包名（无 package 声明不入表 = orphan）；
    import_rows：[{file, line, target}]（仅测试源根外文件；target 为未解析原始路径）；
    测试源根（/test/kotlin/ 段）文件不计 import（包仍入 file_pkg——叶包可见）。
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
            if not name.lower().endswith(KT_SUFFIX):
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


def build_kt_graph(
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


def derive_kt_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序包名集（fid→file_path→package 声明）。

    仅当 payload 符号含 .kt 文件才扫描；orphan（.kt 无 package 声明或磁盘
    缺失，去重路径）与超时诚实上报——kt_files 供 run() 路由（有 .kt 但
    全 orphan 也要进 kt 引擎给出可行动的 not-enforceable）。
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
    kt_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(KT_SUFFIX)
    }
    file_pkg: dict[str, str] = {}
    timed_out = False
    if kt_paths:
        file_pkg, _imports, _files, _tests, timed_out = scan_kt(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(KT_SUFFIX):
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
        "kt_files": len(kt_paths),
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


def run_kt(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Kotlin 执法入口：契约生成（复用层 DAG）→ 源级 import 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "kt-package-parser",
        "structure_status": structure_status,
    }
    file_pkg, import_rows, files_scanned, tests_excluded, timed_out = scan_kt(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"kotlin scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_pkg:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no kotlin packages found (no package declarations)",
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
    graph, edges = build_kt_graph(file_pkg, import_rows)
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
