r"""S7 架构执法 PHP 引擎：内置源级 namespace/use 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 php，编译验证走钉版沙箱 PHP 8.5.12-1）：
- PHP 依赖边界 = namespace（PSR-4 事实标准），声明边 = `use`（deptrac 等 PHP
  架构工具同口径）；require/include 是运行时文件加载（bootstrap/legacy），
  现代 autoload 代码不承载架构语义 → 首版不解析，诚实文档化。
- 源级 namespace/use 解析纯 stdlib 零构建零工具链（与 rs/java/cs/kt 引擎同
  路线），无 enforcer-not-found 态；语法验证仅在 dogfood 探针走沙箱 php -l。
- 前置探针（work/php-cbm-probe，2026-11-07）：CBM 索引 PHP 全链通（xcheck
  agreement 100%）——qualified name **路径式**（php-cbm-probe.src.Api）、
  File/Module/Class/Method 符号、IMPORTS 边存在（line=None）→ 容器一律
  fid→file_path→读文件解析 namespace（与 java/cs/kt 同策略）。
- PHP 词法要点（引擎全落实）：PHP 代码只在 `<?php`/`<?=`/`<?` 岛内（岛外
  HTML 空白化）；`//` 与 `#` 行注释但 `#[` 是 **attribute 非注释**（深度
  扫描 `[`/`]` 后整体空白化——attribute 不含 namespace/use）；块注释不可
  嵌套；单引号串仅 `\'`/`\\` 转义（异于双引号全转义）；双引号串支持
  `\$var`/`{$...}` 插值——插值内嵌引号不穿透（与 Kotlin `${}` 同型局限）；
  heredoc/nowdoc（`<<<EOT`/`<<<'EOT'`，PHP 7.3+ 缩进闭合容错）；反引号
  shell-exec 按双引号串处理。
- use 形态（全落实）：`use A\B\C;`、别名 `as D`（取左侧目标）、
  `use function f;`/`use const X;`（kind 不影响命名空间边）、逗号多目标
  `use A\B, C\D;`、**分组** `use A\{B, C as D};`；闭包捕获 `use ($x)`
  词形不匹配（括号守卫拒绝）。目标命名空间 = FQN 去尾段（use 导入的是
  命名项，其所在命名空间是父级——`use Probe\Api\Api;` → Probe\Api），
  再对已知命名空间最长前缀匹配（外部依赖无命中 → 跳过）。
- 测试排除（约定式保守）：路径 test/tests 目录段或 `*Test.php`（PHPUnit
  命名约定，大小写敏感——`greatest.php` 不是测试）；排除文件的 use 不入图，
  命名空间仍入图（叶可见）。
- 违例语义 = 传递闭包（九引擎统一）：BFS 最短链入 payload；锚定 = 链尾
  直接 use 边所在文件+行号（同命名空间多文件满足同一边时取 (file,line)
  字典序首）。
- 诚实局限（文档化不静默）：
  * 无 namespace 声明（全局命名空间）文件不入图不参与执法（orphan 计数）
  * 多 namespace 块文件取首个声明（PSR-1 一文件一命名空间；违例布局不在
    执法范围）
  * inline FQN 类引用（无 use 声明的 `\Full\Qualified\Name` 使用）不入图
    ——首版面向 use 声明规范代码（PSR-12 要求 use 声明）
  * require/include 表达式（`__DIR__ . '/x.php'` 等）不解析（现代 autoload
    代码无架构语义；legacy require 链不覆盖）
  * 双引号/插值内嵌引号（`"{$arr["k"]}"`）词法不穿透——字符串在插值内
    首个裸引号处提前截断，残余按代码扫描
  * 行注释内 `?>` 短闭合不识别（PHP 实际会闭合岛——残余按注释空白化，
    影响面极窄）
  * 构建/第三方目录（vendor/node_modules/build/target/隐藏目录）跳过
    （产物与 Composer 第三方无架构语义）
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

PHP_SUFFIX = ".php"
TEST_SEGMENTS = ("test", "tests")
SKIP_DIRS = {"vendor", "node_modules", "build", "target"}

_NAMESPACE_RE = re.compile(
    r"\bnamespace\s+([A-Za-z_][A-Za-z0-9_]*(?:\\[A-Za-z_][A-Za-z0-9_]*)*)\s*[;{]"
)
_USE_RE = re.compile(r"\buse\s+(function\s+|const\s+)?")


# ------------------------------------------------------------------ 词法底座


def _sq_end(text: str, i: int) -> int:
    """单引号串结束位置：仅 `\\'` 与 `\\\\` 转义（异于双引号全转义）。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\" and j + 1 < n and text[j + 1] in "'\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        if text[j] == "\n":
            return j  # 防御：单引号串不跨行，行尾截断防误吞下行代码
        j += 1
    return n


def _dq_end(text: str, i: int, quote: str) -> int:
    """双引号串/反引号 shell-exec 结束位置：全转义+插值不穿透（文档化局限）。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if text[j] == "\n":
            return j  # 防御：不跨行
        j += 1
    return n


def _heredoc_end(text: str, i: int) -> int | None:
    """`<<<` 在 i → heredoc/nowdoc 闭合标记后位置；非 heredoc → None。

    `<<<EOT` / `<<<"EOT"`（heredoc）/ `<<<'EOT'`（nowdoc）；PHP 7.3+ 允许
    闭合行前导空白。闭合行内容（如 `EOT;` 的分号）保留为代码。
    """
    n = len(text)
    j = i + 3
    while j < n and text[j] in " \t":
        j += 1
    quote = ""
    if j < n and text[j] in "'\"":
        quote = text[j]
        j += 1
    start = j
    while j < n and (text[j].isalnum() or text[j] == "_"):
        j += 1
    marker = text[start:j]
    if not marker:
        return None
    if quote and j < n and text[j] == quote:
        j += 1
    while j < n and text[j] != "\n":
        j += 1
    if j >= n:
        return n
    j += 1
    # 逐行找闭合标记：行首可选空白 + marker + 非标识符续尾
    while j <= n:
        eol = text.find("\n", j)
        stop = n if eol < 0 else eol
        line = text[j:stop]
        stripped = line.lstrip(" \t")
        if stripped.startswith(marker):
            after = stripped[len(marker) : len(marker) + 1]
            if not after or not (after.isalnum() or after == "_"):
                return j + (len(line) - len(stripped)) + len(marker)
        if eol < 0:
            return n
        j = eol + 1
    return n


def _attribute_end(text: str, i: int) -> int:
    """`#[` 在 i → attribute 块 `]` 后位置（嵌套深度计数）。"""
    n = len(text)
    depth = 0
    j = i
    while j < n:
        c = text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j + 1
        elif c == "\n":
            pass
        j += 1
    return n


def _blank_noncode(text: str) -> str:
    """岛外 HTML/注释/字符串/heredoc/attribute → 空白（等长，换行保留）。

    PHP 代码只在 `<?php`/`<?=`/`<?` 岛内；`?>` 闭合回 HTML 模式。
    """
    out = list(text)
    i = 0
    n = len(text)
    in_php = False
    while i < n:
        if not in_php:
            j = text.find("<?", i)
            if j < 0:
                for k in range(i, n):
                    if out[k] != "\n":
                        out[k] = " "
                break
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            if text.startswith("<?php", j):
                i = j + 5
            elif text.startswith("<?=", j):
                i = j + 3
            else:
                i = j + 2
            in_php = True
            continue
        c = text[i]
        if text.startswith("?>", i):
            out[i] = " "
            out[i + 1] = " "
            i += 2
            in_php = False
            continue
        if c == "\n":
            i += 1
            continue
        two = text[i : i + 2]
        if two == "//" or (c == "#" and two != "#["):
            j = i
            while j < n and text[j] != "\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "#[":
            # PHP 8 attribute：非注释（`#` 行注释规则的例外）；嵌套深度扫描
            j = _attribute_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif two == "/*":
            # PHP 块注释不可嵌套：首个 */ 闭合
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif text[i : i + 3] == "<<<":
            j = _heredoc_end(text, i)
            if j is None:
                i += 3
                continue
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == '"':
            j = _dq_end(text, i, '"')
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "`":
            j = _dq_end(text, i, "`")
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "'":
            j = _sq_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ 解析


def _parse_namespace(blanked: str) -> str | None:
    """首个 namespace 声明（PSR-1 一文件一命名空间；全局 `namespace {` 不匹配）。"""
    m = _NAMESPACE_RE.search(blanked)
    return m.group(1) if m else None


def _use_items(stmt: str) -> list[str]:
    """use 语句体 → FQN 目标清单（分组展开/逗号多目标/别名剥壳）。"""
    stmt = stmt.strip()
    if "{" in stmt and "}" in stmt:
        brace = stmt.index("{")
        close = stmt.rindex("}")
        prefix = stmt[:brace].strip().rstrip("\\")
        items = stmt[brace + 1 : close].split(",")
        out = []
        for item in items:
            item = item.strip()
            if not item:
                continue
            out.append((prefix + "\\" + item) if prefix else item)
        return out
    return [x.strip() for x in stmt.split(",") if x.strip()]


def _target_ns(item: str) -> str | None:
    """use 项 FQN → 目标命名空间（去尾段；别名/`::` 成员/顶层名剥除）。"""
    name = item.split()[0]  # 丢 `as D` 别名
    name = name.lstrip("\\")
    if "::" in name:
        name = name.split("::", 1)[0]
    segs = name.split("\\")
    if len(segs) < 2 or any(not seg for seg in segs):
        return None  # 顶层名（use Exception; 等）无命名空间语义
    return "\\".join(segs[:-1])


def _iter_uses(blanked: str) -> list[tuple[str, int]]:
    """已空白化文本 → [(use 目标命名空间, 行号)]。

    闭包捕获 `use ($x)` 词形被括号守卫拒绝（非 use 导入）。
    """
    rows: list[tuple[str, int]] = []
    n = len(blanked)
    for m in _USE_RE.finditer(blanked):
        semi = blanked.find(";", m.end())
        if semi < 0:
            continue
        stmt = blanked[m.end() : semi]
        s = stmt.strip()
        if not s or not (s[0] == "\\" or s[0].isalpha() or s[0] == "_"):
            continue  # 闭包捕获 use ( ... 或畸形体
        line = blanked.count("\n", 0, m.start()) + 1
        for item in _use_items(s):
            ns = _target_ns(item)
            if ns:
                rows.append((ns, line))
    return rows


# ------------------------------------------------------------------ 扫描


def _is_test(rel: str) -> bool:
    """测试文件判定：test/tests 目录段，或 `*Test.php`/`Test.php`（PHPUnit
    命名约定，大小写敏感——greatest.php 不是测试）。"""
    segs = rel.split("/")
    if any(seg.lower() in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1]
    if not name.lower().endswith(PHP_SUFFIX):
        return False
    stem = name[: -len(PHP_SUFFIX)]
    return stem == "Test" or stem.endswith("Test")


def scan_php(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, str], list[dict], int, int, bool]:
    """repo → (file_ns, use_rows, files_scanned, tests_excluded, timed_out)。

    file_ns：repo 相对 posix 路径 → 命名空间（无 namespace 声明不入表 =
    orphan；纯 HTML 文件同）；use_rows：[{file, line, target}]（仅测试外
    文件；target 为已去尾段的命名空间）。超时截断的部分扫描不可信，调用
    方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_ns: dict[str, str] = {}
    use_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(PHP_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_ns, use_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            blanked = _blank_noncode(_read(repo, rel))
            ns = _parse_namespace(blanked)
            if ns is None:
                continue
            file_ns[rel] = ns
            if _is_test(rel):
                tests_excluded += 1
                continue
            for target, line in _iter_uses(blanked):
                use_rows.append({"file": rel, "line": line, "target": target})
    return file_ns, use_rows, files_scanned, tests_excluded, False


def _resolve_ns(target: str, namespaces: set[str]) -> str | None:
    """use 目标命名空间 → 已知命名空间最长前缀；外部依赖（无命中）→ None。"""
    segs = target.split("\\")
    for k in range(len(segs), 0, -1):
        cand = "\\".join(segs[:k])
        if cand in namespaces:
            return cand
    return None


def build_php_graph(
    file_ns: dict[str, str],
    use_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """命名空间依赖图（键 = 全部命名空间——叶无出边也入图，漏叶会假阴）+ 边明细。"""
    namespaces = set(file_ns.values())
    graph: dict[str, set[str]] = {p: set() for p in namespaces}
    edges: list[dict] = []
    for row in use_rows:
        ns = file_ns.get(row["file"])
        if ns is None:
            continue
        target = _resolve_ns(row["target"], namespaces)
        if target is None or target == ns:
            continue
        graph[ns].add(target)
        edges.append({"file": row["file"], "line": row["line"], "src": ns, "dst": target})
    return {p: sorted(t) for p, t in graph.items()}, edges


def derive_php_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序命名空间集（fid→file_path→namespace 声明）。

    仅当 payload 符号含 .php 文件才扫描；orphan（.php 无 namespace 声明
    或磁盘缺失，去重路径）与超时诚实上报——php_files 供 run() 路由（有
    .php 但全 orphan 也要进 php 引擎给出可行动的 not-enforceable）。
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
    php_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(PHP_SUFFIX)
    }
    file_ns: dict[str, str] = {}
    timed_out = False
    if php_paths:
        file_ns, _uses, _files, _tests, timed_out = scan_php(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(PHP_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        name = file_ns.get(p)
        if name is None:
            orphans.add(p)
            continue
        containers[fid_to_module[fid]].add(name)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "php_files": len(php_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(ns: str, prefixes: list[str]) -> bool:
    r"""命名空间 ∈ 容器集（容器可为父命名空间——Probe 捕获 Probe\Store 子空间）。"""
    return any(ns == p or ns.startswith(p + "\\") for p in prefixes)


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


def run_php(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """PHP 执法入口：契约生成（复用层 DAG）→ 源级 use 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "php-namespace-parser",
        "structure_status": structure_status,
    }
    file_ns, use_rows, files_scanned, tests_excluded, timed_out = scan_php(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"php scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_ns:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no php namespaces found (no namespace declarations)",
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
    graph, edges = build_php_graph(file_ns, use_rows)
    summary["files"] = files_scanned
    summary["namespaces"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 use 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 use 边）；防御：丢弃无法锚定的违例并诚实计数
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
