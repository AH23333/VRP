"""S7 架构执法 Swift 引擎：内置源级 import 解析 + Package.swift targets 权威清单。

选型记实（2026-11-07，本机无 swift）：
- Swift 依赖边界 = module（SwiftPM target）；声明边 = import 语句。
  Package.swift 是 target 权威清单（同 Go go.mod 地位）→ 容器 = target 名。
  target 路径：path: 实参或 SwiftPM 默认（Sources/<名>；testTarget 为
  Tests/<名>）。
- 依赖边 = import <TargetName>（含 @ 属性前缀 import——@testable/
  @_exported/@_implementationOnly/@_spi(...)（Swift import 属性形态，
  任意 @ 前缀放行）与限定形态
  import class/struct/enum/protocol/func/let/var/typealias TargetName.X——
  取模块段）；未声明模块（Foundation 等系统/外部库）跳过；同 target
  自导入不成边。import 模块名是裸标识符非字符串 → 指令无目标区保留
  问题（词法比 C/Ruby/Dart 简单——canary 随注释/字符串自然空白化）。
- CBM Swift IMPORTS 边存在但 line=None（swift-cbm-probe 实测，File→
  Folder，evidence=structural）→ 锚定行号必须自扫源码。CBM 内置 Swift
  解析器（qualified name 路径式）→ 语法验证仅 dogfood 走 CBM 索引成功；
  CBM CALLS xcheck agreement 100%（code-atlas 支持 Swift 调用抽取）。
- 词法（等长空白，换行保留，前进保证处处）：行注释 //；块注释可嵌套
  （深度计数——Swift 支持嵌套注释，同 Kotlin/Scala）；单行字符串
  反斜杠转义、未闭合截断行尾；多行三引号串跨行（闭引号取引号串末
  三枚——四连引号收尾首枚属内容）；raw 字符串 # 引号串（# 计数定界，
  闭串为引号+同数 #；反斜杠井号转义跳过；井号前缀仅紧邻引号时生效）。
  插值反斜杠圆括号内嵌引号不穿透（词法局限同 kt/php/ruby/dart 家族）。
- Package.swift 启发式解析（无 Swift 编译器，纯 stdlib）：字符串/注释
  感知扫描（comment_mask/string_mask/string_starts 三掩码）；targets:
  标签（可多次出现——products 的 targets 数组不产 target）后配平方括号
  定数组区；数组顶层（深度 1）.target(/.executableTarget(/.testTarget(
  声明块配平圆括号——嵌套 dependencies: [.target(name:)] 引用不误判
  声明；块内 name:/path: 实参取圆括号深度 1 处（嵌套 .product(name:)
  不误取）且值开引号必须是真串首（大字符串内伪 label 拒绝）。诚实
  局限：计算名/变量拼接名不支持（目标不可见 → 文件 orphan 暴露）；
  .systemLibrary/.binaryTarget/.plugin 不扫（产物/系统库非源码 target）。
- 测试排除：testTarget 声明的 target 的 import 不入图（target 容器
  仍入表叶可见）；未归属任何 target 的 .swift 文件 = orphan 诚实计数
  （import 不入图——无法归属容器）。Package.swift 本身非源码不入图。
- 违例语义 = 传递闭包（十三引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 import 边 (file,line) 字典序首 + 违例文件首符号。
- 诚实局限（文档化不静默）：
  * 图键 = 全部声明 target（含空 target/test target——叶可见防假阴）
  * build/node_modules/vendor/Pods/Carthage/隐藏目录（含 .build）跳过
  * 条件编译块（#if）内 import 按文本计（保守方向）
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

SWIFT_SUFFIX = ".swift"
SKIP_DIRS = frozenset({"build", "node_modules", "vendor", "Pods", "Carthage"})  # .build 走隐藏目录排除
_IDENT_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_TARGET_LABELS = ("target", "executableTarget", "testTarget")
_KIND_WORDS = "typealias|struct|class|enum|protocol|let|var|func|indirect"
_IMPORT_RE = re.compile(
    r"\bimport\b(?:[ \t]+(?:%s))?[ \t]+([A-Za-z_][A-Za-z0-9_]*)" % _KIND_WORDS
)  # [ \t] 不跨行：裸 import 行尾不与下一行拼接成假 import
_TARGETS_LABEL_RE = re.compile(r"\btargets\s*:\s*\[")
_ARG_RES = {
    "name": re.compile(r"\bname\b\s*:\s*\"([^\"\n]*)\""),
    "path": re.compile(r"\bpath\b\s*:\s*\"([^\"\n]*)\""),
}


# ------------------------------------------------------------------ 词法底座


def _pound_run(text: str, i: int) -> int:
    """位置 i（引号）前紧邻 # 连串个数（raw 串定界计数）。"""
    j = i - 1
    k = 0
    while j >= 0 and text[j] == "#":
        k += 1
        j -= 1
    return k


def _string_end(text: str, i: int, raw_pounds: int) -> int:
    """单行字符串结束位置（含闭引号；raw 为 # 计数，0=普通串）。

    普通串反斜杠转义跳过；raw 串仅反斜杠井号转义跳过（Swift 语义）；
    闭条件：普通串遇同引号，raw 串遇引号+同数 # 连串；未闭合截断行尾。
    """
    n = len(text)
    j = i + 1
    while j < n:
        if raw_pounds == 0 and text[j] == "\\":
            j += 2
            continue
        if raw_pounds > 0 and text[j] == "\\" and j + 1 < n and text[j + 1] == "#":
            j += 2
            continue
        if text[j] == '"':
            if raw_pounds == 0:
                return j + 1
            if text.startswith("#" * raw_pounds, j + 1):
                return j + 1 + raw_pounds
        if text[j] == "\n":
            break
        j += 1
    j = text.find("\n", i)
    return j if j >= 0 else n


def _multiline_end(text: str, i: int, raw_pounds: int) -> int:
    """多行三引号串结束位置（闭引号取引号串末三枚——四连引号收尾首枚属内容）。

    普通串反斜杠转义跳过；raw 串反斜杠井号转义跳过；闭条件：三引号
    序列 + 同数 # 连串；引号串长 >3 时取串尾三引号闭合。未闭合文件尾。
    """
    n = len(text)
    j = i + 3
    while j < n:
        if raw_pounds == 0 and text[j] == "\\":
            j += 2
            continue
        if raw_pounds > 0 and text[j] == "\\" and j + 1 < n and text[j + 1] == "#":
            j += 2
            continue
        if text[j] == '"':
            run = 0
            while j + run < n and text[j + run] == '"':
                run += 1
            if run >= 3 and text.startswith("#" * raw_pounds, j + run):
                return j + run + raw_pounds
            j += run
            continue
        j += 1
    return n


def _block_comment_end(text: str, i: int) -> int:
    """嵌套块注释结束位置（深度计数；未闭合文件尾——前进保证）。"""
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


def _scan_masks(text: str) -> tuple[list[bool], list[bool], set[int]]:
    """扫描 Swift 文本 → (comment_mask, string_mask, string_starts)。

    comment_mask/string_mask：注释/字符串字面量内（含定界符）逐字符真值；
    string_starts：字符串字面量开引号位置集（真串首判定——伪 label 拒绝）。
    """
    n = len(text)
    comment_mask = [False] * n
    string_mask = [False] * n
    string_starts: set[int] = set()
    i = 0
    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                comment_mask[k] = True
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = _block_comment_end(text, i)
            for k in range(i, min(j, n)):
                comment_mask[k] = True
            i = j
        elif c == '"':
            k = _pound_run(text, i)
            if text.startswith('"""', i):
                j = _multiline_end(text, i, k)
            else:
                j = _string_end(text, i, k)
            j = min(j, n)
            string_starts.add(i)
            for k2 in range(i, j):
                string_mask[k2] = True
            i = j if j > i else i + 1
        else:
            i += 1
    return comment_mask, string_mask, string_starts


def _blank_noncode(text: str) -> str:
    """注释/字符串 → 空白（等长，换行保留）；import 模块名是裸标识符
    无目标区保留需求（canary 随注释/字符串自然空白化）。
    """
    comment_mask, string_mask, _ = _scan_masks(text)
    return "".join(
        " " if (comment_mask[i] or string_mask[i]) and ch != "\n" else ch
        for i, ch in enumerate(text)
    )


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ 解析


def _import_statement_at(text: str, i: int) -> bool:
    """位置 i 的 import 关键字是否语句位置（成员访问/标识符尾拒绝；
    任意 @ 属性前缀放行——@testable/@_exported/@_implementationOnly/
    @_spi(...)（Swift import 属性形态；括号属性经 ) 放行路径）。
    """
    j = i - 1
    while j >= 0 and text[j] in " \t\r":
        j -= 1
    if j < 0 or text[j] == "\n":
        return True
    if text[j] in _IDENT_CHARS:
        k = j
        while k >= 0 and text[k] in _IDENT_CHARS:
            k -= 1
        return k >= 0 and text[k] == "@"  # 任意 @ 属性前缀（非裸标识符）
    return text[j] != "."


def _iter_imports(blanked: str) -> list[dict]:
    """空白化文本 → [{module, line}]；伪 import（字符串/注释）已随词法消失。"""
    rows: list[dict] = []
    for m in _IMPORT_RE.finditer(blanked):
        if not _import_statement_at(blanked, m.start()):
            continue
        rows.append(
            {"module": m.group(1), "line": blanked.count("\n", 0, m.start()) + 1}
        )
    return rows


# ------------------------------------------------------------------ Package.swift


def _walk_balanced(
    text: str,
    comment_mask: list[bool],
    string_mask: list[bool],
    open_pos: int,
    open_ch: str,
    close_ch: str,
) -> int:
    """从开定界符位置走配平（跳过注释/字符串）→ 闭定界符位置（含）。"""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        if comment_mask[i] or string_mask[i]:
            i += 1
            continue
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _paren_depth_at(
    text: str,
    comment_mask: list[bool],
    string_mask: list[bool],
    start: int,
    end: int,
) -> int:
    """[start, end) 区间圆括号深度增量（跳过注释/字符串）。"""
    depth = 0
    for i in range(start, min(end, len(text))):
        if comment_mask[i] or string_mask[i]:
            continue
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
    return depth


def _iter_target_decls(
    text: str,
    comment_mask: list[bool],
    string_mask: list[bool],
    arr_open: int,
    arr_close: int,
):
    """targets 数组顶层（深度 1）.target(/.executableTarget(/.testTarget( 声明
    → 逐一 yield (label, paren_open)；嵌套 dependencies 引用不误判。
    未闭合数组截断文本尾（arr_close==len 时 i<n 守卫——畸形清单不越界）。
    """
    n = len(text)
    depth = 0
    i = arr_open
    while i <= arr_close and i < n:
        if comment_mask[i] or string_mask[i]:
            i += 1
            continue
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == "." and depth == 1:
            for label in _TARGET_LABELS:
                if text.startswith(label + "(", i + 1):
                    yield label, i + 1 + len(label)
                    i += 1 + len(label)
                    break
        i += 1


def _label_arg(
    text: str,
    comment_mask: list[bool],
    string_mask: list[bool],
    string_starts: set[int],
    block_open: int,
    block_close: int,
    label: str,
) -> str | None:
    """声明块内 paren-深度-1 处的 label: "值" 实参（大字符串伪 label/嵌套
    .product(name:) 拒绝）；无 → None。
    """
    res = _ARG_RES[label]
    for m in res.finditer(text, block_open, block_close):
        abs_q = m.start(1) - 1  # 值开引号
        if abs_q not in string_starts:
            continue
        if _paren_depth_at(text, comment_mask, string_mask, block_open, abs_q) != 1:
            continue
        return m.group(1)
    return None


def parse_package_swift(repo: Path) -> dict:
    """Package.swift → {"found": bool, "targets": {名: {"path": str, "test": bool}}}。

    启发式解析（纯 stdlib，无 Swift 编译器）：targets: 数组顶层声明块；
    name:/path: 深度校验取值；products 的 targets 数组不产 target（无
    声明块）；注释/字符串内伪声明与伪 label 拒绝；计算名（无名字面量）
    目标不可见（文件 orphan 暴露，诚实文档化）。
    """
    path = Path(repo) / "Package.swift"
    if not path.is_file():
        return {"found": False, "targets": {}}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"found": False, "targets": {}}
    comment_mask, string_mask, string_starts = _scan_masks(text)
    targets: dict[str, dict] = {}
    for m in _TARGETS_LABEL_RE.finditer(text):
        bracket = m.end() - 1  # "[" 位置
        if comment_mask[bracket] or string_mask[bracket]:
            continue
        arr_close = _walk_balanced(text, comment_mask, string_mask, bracket, "[", "]")
        for label, paren_open in _iter_target_decls(
            text, comment_mask, string_mask, bracket, arr_close
        ):
            paren_close = _walk_balanced(
                text, comment_mask, string_mask, paren_open, "(", ")"
            )
            name = _label_arg(
                text, comment_mask, string_mask, string_starts, paren_open, paren_close, "name"
            )
            if not name:
                continue
            tpath = _label_arg(
                text, comment_mask, string_mask, string_starts, paren_open, paren_close, "path"
            )
            if tpath is None:
                tpath = ("Tests/" if label == "testTarget" else "Sources/") + name
            else:
                tpath = posixpath.normpath(tpath.replace("\\", "/"))
            targets[name] = {"path": tpath, "test": label == "testTarget"}
    return {"found": True, "targets": targets}


# ------------------------------------------------------------------ 扫描


def _owning_target(rel: str, target_map: dict) -> str | None:
    """文件 → 所属 target（最长路径前缀命中；无 → None=orphan）。"""
    best: str | None = None
    best_len = -1
    for tname, info in target_map.items():
        p = info["path"]
        if rel == p or rel.startswith(p + "/"):
            if len(p) > best_len:
                best, best_len = tname, len(p)
    return best


def scan_swift(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict, list[dict], int, int, int, bool]:
    """repo → (target_map, import_rows, files_scanned, tests_excluded, orphan_files, timed_out)。

    import_rows：[{file, line, module, src_target}]（仅生产 target 文件；
    测试 target 文件与 orphan 文件 import 不入图）。超时截断的部分扫描
    不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    target_map = parse_package_swift(repo)["targets"]
    deadline = time.monotonic() + timeout
    import_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    orphan_files = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(SWIFT_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return target_map, import_rows, files_scanned, tests_excluded, orphan_files, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            if rel == "Package.swift":
                continue  # 根清单文件非源码（后缀撞 .swift——SwiftPM 命名陷阱）
            files_scanned += 1
            owner = _owning_target(rel, target_map)
            if owner is None:
                orphan_files += 1
                continue
            if target_map[owner]["test"]:
                tests_excluded += 1
                continue
            blanked = _blank_noncode(_read(repo, rel))
            for row in _iter_imports(blanked):
                import_rows.append({**row, "file": rel, "src_target": owner})
    return target_map, import_rows, files_scanned, tests_excluded, orphan_files, False


# ------------------------------------------------------------------ 图


def build_swift_graph(
    target_map: dict,
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键=全部声明 target——含空/test target，叶可见防假阴）
    + 边明细（含 import 溯源）；未声明模块（Foundation 等）与自环跳过。
    """
    graph: dict[str, set[str]] = {name: set() for name in target_map}
    edges: list[dict] = []
    for row in import_rows:
        if row["module"] not in target_map:
            continue
        src = row["src_target"]
        if src == row["module"]:
            continue
        graph[src].add(row["module"])
        edges.append(
            {"file": row["file"], "line": row["line"], "src": src, "dst": row["module"]}
        )
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_swift_containers(repo: Path, con: sqlite3.Connection, payload: dict) -> dict:
    """module_id → 排序容器集（fid→file_path→所属 SwiftPM target）。

    非 .swift 扩展名观测；无 Package.swift/未归属 target 的 .swift 不入
    容器（orphan 由 run 阶段扫描计数）；swift_files 供 run() 路由。
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
    target_map = parse_package_swift(Path(repo))["targets"]
    swift_files = 0
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(SWIFT_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        swift_files += 1
        owner = _owning_target(p, target_map)
        if owner is not None:
            containers[fid_to_module[fid]].add(owner)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "swift_files": swift_files,
    }


# ------------------------------------------------------------------ 违例


def _belongs(container: str, names: list[str]) -> bool:
    """容器 ∈ 容器集（target 名精确匹配——名字不嵌套无前缀语义）。"""
    return container in names


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


def run_swift(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Swift 执法入口：契约生成（复用层 DAG）→ Package.swift 权威 target 图
    → BFS 违例 → P9（幂等）。
    """
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "swift-target-parser",
        "structure_status": structure_status,
    }
    if not (repo / "Package.swift").is_file():
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no Package.swift found (SwiftPM manifest is the target authority)",
        }
    target_map, import_rows, files_scanned, tests_excluded, orphan_files, timed_out = scan_swift(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"swift scan exceeded {timeout}s (partial graph discarded)",
        }
    if not target_map:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no targets declared in Package.swift (computed/variable names unsupported)",
        }
    if files_scanned == 0:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no swift sources found (no .swift files on disk)",
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
    graph, edges = build_swift_graph(target_map, import_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    if orphan_files:
        summary["orphan_swift_files"] = orphan_files
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
