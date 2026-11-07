"""S7 架构执法 Erlang 引擎：内置源级 module/远程调用解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-08，本机无 erlang——纯解析器路线，零工具链依赖）：
- Erlang 依赖图工具 xref 需 OTP 安装+beam 编译产物；源级 module/远程调用解析
  纯 stdlib 零工具链（与 rs/java/cs/kt/perl 引擎同路线），无
  enforcer-not-found 态；语法验证仅 dogfood 走 CBM 索引。
- CBM Erlang 事实（work/erlang-cbm-probe 实证）：CBM 索引 Erlang——Project/
  Branch/Folder/File/Module/Function 全入表（8 符号）；qualified name 路径式
  （erlang-cbm-probe.src.api）；Module/Function 真行号；IMPORTS 边仅
  File→自身 Module（local_name，line=None）——**跨模块依赖零边**（同 C/Perl
  先例——依赖边全部自扫，不阻塞）。
- 容器 = Erlang 模块（`-module(name).` 声明，OTP 扁平命名空间；远程调用目标
  就是模块名，容器=模块免 code path 猜测——Java/Perl 包哲学）。无 -module
  声明文件不入图（orphan 诚实计数）。
- 词法（Erlang 特有形态全落实）：
  * `%` 行注释（Erlang 无块注释——语言刻意设计）；
  * 字符字面量 `$c`（`$`+转义或 `$`+单字符整体消费——`$%` 防误启注释、
    `$"` 防误开串；未闭合自愈不越行）；
  * 双引号串（反斜杠转义；Erlang 串不可跨行——未闭合自愈截断行尾防误吞
    文件尾）；
  * 带引号原子 `'...'`（转义跳过；**原文保留不入空白化**——`'my-mod':fun`
    远程调用模块名可为带引号原子）；
  * 变量大写/`_` 开头、宏 `?NAME`——远程调用动态派发（`Mod:Fun` 变量模块）
    与宏模块天然不匹配（模块侧小写原子正则）。
- 依赖边提取：远程调用 `mod:fun`（裸/带引号原子:原子；fun 侧后随 `(` 或
  `/`（`/arity`）才认定——map `:=`/typespec `::`/指令 `:-` 不误触；含
  `fun mod:fun/2` 引用与 typespec `mod:type()` 引用）；`-behaviour(mod).`/
  `-behavior(mod).` 行为模块依赖；`-import(mod, [...]).` 编译期导入（后续
  裸调用不可逐点归属，指令本身记一条依赖边）；自模块/外部模块（OTP、deps
  ——索引未命中）跳过。
- 解析：模块名 → 仓内 <name>.erl 索引精确命中（OTP 扁平命名空间全仓统一）；
  未命中（gen_server/lists 等外部）跳过。
- 测试排除（Erlang 约定式保守排除）：路径含 test/tests **目录段**（精确
  小写），或 `_test.erl`/`_tests.erl`（EUnit foo_tests 惯例），或
  `_SUITE.erl`（Common Test 套件）——排除文件的远程调用不入图；其 -module
  声明仍入图（叶模块可见）。
- 违例语义 = 传递闭包（十八引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接调用边所在文件+行号（同模块多文件满足同一边时取
  (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 无 -module 声明文件不入图不参与执法（orphan 计数）
  * `-include("x.hrl")`/`-include_lib(...)` 头文件不入图（.hrl 无模块容器）
  * `apply(mod, fun, args)` 字面原子形态不提取（动态调用形态）
  * `-compile([{parse_transform, mod}])` 编译变换不提取（罕见形态）
  * `-import` 后续裸调用不逐点归属（指令本身记一条边）
  * 带引号原子内 `:` 极端形态（`'a:b'`）不构成误边（fun 侧守卫拦截）
  * _build/deps/ebin/隐藏目录跳过（产物与 deps 无架构语义）
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

ERLANG_SUFFIXES = (".erl",)
SKIP_DIRS = {"_build", "deps", "ebin", "node_modules", "vendor"}
TEST_SEGMENTS = ("test", "tests")

_MODULE_RE = re.compile(
    r"^[ \t]*-module[ \t]*\([ \t]*(?:'([^'\n\\]*)'|([a-z][A-Za-z0-9_@]*))", re.M
)
_REMOTE_RE = re.compile(
    r"(?<![A-Za-z0-9_@'?$])"
    r"(?:'([^'\n\\]*)'|([a-z][A-Za-z0-9_@]*))"
    r"[ \t]*:[ \t]*"
    r"(?:'([^'\n\\]*)'|([A-Za-z0-9_@]*))"
    r"[ \t]*[(/]"
)
_BEHAVIOUR_RE = re.compile(
    r"^[ \t]*-behaviou?r[ \t]*\([ \t]*(?:'([^'\n\\]*)'|([a-z][A-Za-z0-9_@]*))", re.M
)
_IMPORT_DIRECTIVE_RE = re.compile(
    r"^[ \t]*-import[ \t]*\([ \t]*(?:'([^'\n\\]*)'|([a-z][A-Za-z0-9_@]*))", re.M
)


# ------------------------------------------------------------------ 词法底座


def _blank_range(out: list[str], a: int, b: int) -> None:
    """out[a:b) 非换行字符空白化（换行保留——行号不漂移）。"""
    for k in range(a, max(b, 0)):
        if k < len(out) and out[k] != "\n":
            out[k] = " "


def _dq_end(text: str, i: int) -> int:
    """双引号串闭引号后一位置；反斜杠转义；换行即截断（Erlang 串不可跨行——
    未闭合串自愈到行尾，不误吞文件尾）。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            return j + 1
        if text[j] == "\n":
            return j
        j += 1
    return n


def _qa_end(text: str, i: int) -> int:
    """带引号原子闭引号后一位置；反斜杠转义；换行即截断（原子不可跨行）。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        if text[j] == "\n":
            return j
        j += 1
    return n


def _blank_noncode(text: str) -> str:
    """注释/双引号串/字符字面量 → 空白（等长、换行保留）。

    带引号原子**原文保留**（`'my-mod':fun` 远程调用的模块名必须可见——
    同 C include 目标区保留哲学）；其内部字符经消费跳过，防 `'` 内 `%`/`"`
    误启注释/串。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            continue
        if c == '"':
            j = _dq_end(text, i)
            _blank_range(out, i + 1, j - 1)
            i = j
            continue
        if c == "'":
            i = _qa_end(text, i)  # 原子原文保留，仅消费防误触
            continue
        if c == "$":
            # 字符字面量：$+转义（3 字符近似）或 $+单字符；$%/$" 先行消费
            end = min(i + (3 if i + 1 < n and text[i + 1] == "\\" else 2), n)
            _blank_range(out, i, end)
            i = end
            continue
        if c == "%":
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank_range(out, i, j)
            i = j
            continue
        i += 1
    return "".join(out)


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_module(blanked: str) -> str | None:
    m = _MODULE_RE.search(blanked)
    return (m.group(1) or m.group(2)) if m else None


def _iter_module_refs(blanked: str) -> list[dict]:
    """已空白化文本 → [{target, line}]（远程调用+behaviour+import 指令）。"""
    rows: list[dict] = []
    for m in _REMOTE_RE.finditer(blanked):
        target = m.group(1) or m.group(2)
        if target:
            rows.append(
                {"target": target, "line": blanked.count("\n", 0, m.start()) + 1}
            )
    for rex in (_BEHAVIOUR_RE, _IMPORT_DIRECTIVE_RE):
        for m in rex.finditer(blanked):
            target = m.group(1) or m.group(2)
            if target:
                rows.append(
                    {"target": target, "line": blanked.count("\n", 0, m.start()) + 1}
                )
    return rows


# ------------------------------------------------------------------ 路径


def _is_test(rel: str) -> bool:
    """测试文件判定（Erlang 约定）：test/tests 目录段（精确小写），或
    `_test.erl`/`_tests.erl`（EUnit foo_tests 惯例），或 `_SUITE.erl`
    （Common Test 套件）。
    """
    segs = rel.split("/")
    if any(seg in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1].lower()
    if not name.endswith(".erl"):
        return False
    stem = name[: -len(".erl")]
    return stem in ("test", "tests") or stem.endswith(("_test", "_tests", "_suite"))


# ------------------------------------------------------------------ 扫描


def scan_erlang(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, str], list[dict], int, int, bool]:
    """repo → (file_mod, ref_rows, files_scanned, tests_excluded, timed_out)。

    file_mod：repo 相对 posix 路径 → 模块名（首 -module 声明；无声明不入表）；
    ref_rows：[{file, line, target}]（仅测试外文件）；测试文件（test/ 目录、
    _tests/_SUITE 等）计入 files_scanned 与 tests_excluded，-module 声明仍入表
    （叶模块可见），远程调用不入图。
    超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_mod: dict[str, str] = {}
    ref_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(ERLANG_SUFFIXES):
                continue
            if time.monotonic() > deadline:
                return file_mod, ref_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            text = _read(repo, rel)
            blanked = _blank_noncode(text)
            mod = _parse_module(blanked)
            if mod is not None:
                file_mod[rel] = mod
            if _is_test(rel):
                tests_excluded += 1
                continue
            for row in _iter_module_refs(blanked):
                ref_rows.append({"file": rel, **row})
    return file_mod, ref_rows, files_scanned, tests_excluded, False


def build_erlang_graph(
    file_mod: dict[str, str],
    ref_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """模块依赖图（键 = 全部声明模块——叶模块无出边也入图，漏叶会假阴）+ 边明细。

    目标 → 模块索引精确命中（OTP 扁平命名空间）；外部（OTP/deps，索引未
    命中）与自模块跳过。
    """
    modules = set(file_mod.values())
    graph: dict[str, set[str]] = {m: set() for m in modules}
    edges: list[dict] = []
    for row in ref_rows:
        src = file_mod.get(row["file"])
        dst = row["target"] if row["target"] in modules else None
        if dst is None or dst == src:
            continue
        graph[src].add(dst)
        edges.append({"file": row["file"], "line": row["line"], "src": src, "dst": dst})
    return {m: sorted(t) for m, t in graph.items()}, edges


def derive_erlang_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序模块名集（fid→file_path→-module 声明）。

    仅当 payload 符号含 .erl 文件才扫描；orphan（.erl 无 -module 声明或
    磁盘缺失，去重路径）与超时诚实上报——erlang_files 供 run() 路由
    （有 .erl 但全 orphan 也要进 erlang 引擎给出可行动的 not-enforceable）。
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
    erlang_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(ERLANG_SUFFIXES)
    }
    file_mod: dict[str, str] = {}
    timed_out = False
    if erlang_paths:
        file_mod, _refs, _files, _tests, timed_out = scan_erlang(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(ERLANG_SUFFIXES):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        name = file_mod.get(p)
        if name is None:
            orphans.add(p)
            continue
        containers[fid_to_module[fid]].add(name)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "erlang_files": len(erlang_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(mod: str, prefixes: list[str]) -> bool:
    """模块名 ∈ 容器集（Erlang 扁平命名空间——精确匹配）。"""
    return mod in prefixes


def _find_violations(graph: dict[str, list[str]], contracts: list[dict]) -> list[dict]:
    violations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for contract in contracts:
        sources = [m for m in graph if _belongs(m, contract["source_modules"])]
        forbidden = {m for m in graph if _belongs(m, contract["forbidden_modules"])}
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


def run_erlang(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Erlang 执法入口：契约生成（复用层 DAG）→ 源级远程调用图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "erlang-module-parser",
        "structure_status": structure_status,
    }
    file_mod, ref_rows, files_scanned, tests_excluded, timed_out = scan_erlang(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"erlang scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_mod:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no erlang modules found (no -module declarations)",
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
    graph, edges = build_erlang_graph(file_mod, ref_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接远程调用边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自远程调用边）；防御：丢弃无法锚定的违例并诚实计数
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
