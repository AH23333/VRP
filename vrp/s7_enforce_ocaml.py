"""S7 架构执法 OCaml 引擎：内置源级 module/open 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-08，本机无 ocaml/dune/opam——纯解析器路线，零工具链
依赖）：
- OCaml 依赖图工具需 ocaml 工具链（dune/ocamldep）；源级 module/open
  解析纯 stdlib 零工具链（与 rs/java/cs/kt/perl/erlang/r/clj 引擎同路
  线），无 enforcer-not-found 态；语法验证仅 dogfood 走 CBM 索引。
- CBM OCaml 事实（work/ocaml-cbm-probe 实证）：CBM 索引 OCaml——
  Project/Branch/File/Module/Function 入表；Module=.ml 编译单元（真行
  号）；Function 真行号；**零 IMPORTS 边**（同 C/Perl/Erlang/R 先例，
  不阻塞）——依赖边全自扫。
- 容器 = OCaml 编译单元文件（repo 相对 posix 路径 .ml/.mli，Lua/Dart/
  Zig 文件容器哲学；fid→file_path 直推）。模块名=文件名词干首字母大写
  （foo.ml→Foo——OCaml 编译单元事实）；依赖解析：引用路径头 → 模块名
  索引（词干小写键，.ml 优先于 .mli、同名字典序首）→ 命中文件容器。
- 词法（OCaml 特有形态全落实）：
  * `(* ... *)` 块注释——**可嵌套**（depth 计数，同 Kotlin/Swift；未
    闭合截断文件尾）；**注释内字符串字面量整体跳过**（`*)` 在注释内串
    里不闭注释——OCaml 词法事实；注释内 quoted string 同跳过）；
  * `"..."` 串（`\\` 转义——`\\\\"`/`\\\\`/`\\n`/`\\t`/`\\ddd` 十进制/
    `\\xHH`/`\\` 续行；**可跨行**——OCaml 串合法跨行，未闭合吞 EOF 保
    守方向，同 Perl/Clojure）；
  * `{id| ... |id}` quoted string（花括号 raw 串，定界 id 可空、**同
    id 可嵌套**；未闭合吞 EOF 保守）；
  * `'c'` 字符字面量（`\\'`/`\\\\`/`\\n`/`\\ddd`/`\\xHH` 转义；**类型
    变量歧义消解**——`'a'` 闭引号=字符字面量、`'a` 无闭引号=类型变量
    不吞——`(x : 'a)`/`('a, 'b) t` 类型注解不误吞）；
  * `#!` shebang 首行。
- 依赖边提取（空白化文本正则——OCaml 依赖形态无嵌套子句）：
  * `open [!]Mod`/`let open [!]Mod in`/`include Mod`（前一字符标识符
    尾/`.`/`'` 拒绝——`x.open`/`A.open` 成员不误触）；
  * `module M = Mod` 模块别名 RHS（`module type`/`struct`/`functor`
    不误取——模块类型≠编译单元不产边）；
  * `Mod.` 大写路径访问（`M.x` 值引用/`M.(expr)` 局部 open/`M.{...}`
    记录/`M.Bar.baz` 子模块路径——路径头解析，路径尾段不重复提取）；
  * 构造器误触天然免疫：构造器应用 `Some x`/`Ok 1` 无点不匹配；外部
    模块（Stdlib/List/Printf 等索引未命中）跳过；同词干 .mli↔.ml 自
    引用跳过（同一编译单元非依赖）。
- 测试排除（OCaml 约定式保守排除）：路径含 test/tests 目录段（精确小
  写——dune test/ 标准布局），或 `*_test.ml`/`*_test.mli`（alcotest
  foo_test.ml 惯例）/裸 `test.ml`/`test.mli`；排除文件的依赖边不入
  图；文件容器仍入图（叶可见）。
- 违例语义 = 传递闭包（二十引擎统一）：BFS 最短链入 payload；锚定 =
  链尾直接依赖边所在文件+行号（同容器多边满足同一边时取 (file,line)
  字典序首）。
- 诚实局限（文档化不静默）：
  * `#load`/`#use` toplevel 指令（脚本形态）不入图
  * `(module M)` 一等模块打包/解包形态不入图（`let module M = F in`
    经别名正则捕获——前一字符为空格放行）
  * `module M : S = F` 署名别名不提取（罕见形态）
  * 局部子模块遮蔽编译单元名（`module Sub = struct ... end` 后 `Sub.x`
    恰撞仓内 sub.ml）误入图（罕见，保守方向）
  * CBM .mli 符号（若索引）容器独立——同词干 .ml 边不归属
  * `_build`（dune 构建产物）与 `_opam` 跳过
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

OCAML_SUFFIXES = (".ml", ".mli")
SKIP_DIRS = {"_build", "_opam", "node_modules", "vendor"}
TEST_SEGMENTS = ("test", "tests")


# ------------------------------------------------------------------ 词法底座


def _blank_range(out: list[str], a: int, b: int) -> None:
    """out[a:b) 非换行字符空白化（换行保留——行号不漂移）。"""
    for k in range(a, max(b, 0)):
        if k < len(out) and out[k] != "\n":
            out[k] = " "


def _str_end(text: str, i: int) -> int:
    """闭引号后一位置；`\\` 转义；OCaml 串可跨行——未闭合吞 EOF（保守）。"""
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


def _char_lit_end(text: str, i: int) -> int | None:
    """`'c'` 字符字面量结束位置；类型变量（`'a` 无闭引号）→ None 不吞。"""
    n = len(text)
    j = i + 1
    if j >= n:
        return None
    if text[j] == "\\":
        j += 1
        if j >= n:
            return None
        c = text[j]
        if c in "xX":
            j += 1
            cnt = 0
            while j < n and cnt < 2 and text[j] in "0123456789abcdefABCDEF":
                j += 1
                cnt += 1
            if cnt == 0:
                return None
        elif c in "0123456789":
            cnt = 0
            while j < n and cnt < 3 and text[j] in "0123456789":
                j += 1
                cnt += 1
        else:
            j += 1  # \n \t \' \" \\ \b \r 等单字符转义
        if j < n and text[j] == "'":
            return j + 1
        return None
    if text[j] == "'" or text[j] == "\n":
        return None
    if j + 1 < n and text[j + 1] == "'":
        return j + 2
    return None


_QUOTED_OPEN = re.compile(r"\{([a-z_]*)\|")


def _quoted_end(text: str, i: int) -> int | None:
    """`{id| ... |id}` quoted string 结束位置（同 id 可嵌套；未闭合吞
    EOF 保守）；非 quoted string 开头（`{x=1}` 记录等）→ None。"""
    m = _QUOTED_OPEN.match(text, i)
    if m is None:
        return None
    tag = m.group(1)
    opener = "{" + tag + "|"
    closer = "|" + tag + "}"
    depth = 1
    j = m.end()
    n = len(text)
    while j < n:
        if text.startswith(opener, j):
            depth += 1
            j += len(opener)
            continue
        if text.startswith(closer, j):
            depth -= 1
            j += len(closer)
            if depth == 0:
                return j
            continue
        j += 1
    return n


def _comment_end(text: str, i: int) -> int:
    """`(* ... *)` 嵌套注释结束位置（注释内串字面量整体跳过——`*)` 在注
    释内串里不闭注释；未闭合截断文件尾）。"""
    n = len(text)
    depth = 1
    j = i + 2
    while j < n:
        c = text[j]
        if c == "(" and text.startswith("(*", j):
            depth += 1
            j += 2
            continue
        if c == "*" and text.startswith("*)", j):
            depth -= 1
            j += 2
            if depth == 0:
                return j
            continue
        if c == '"':
            j = _str_end(text, j)
            continue
        if c == "'":
            e = _char_lit_end(text, j)
            j = e if e is not None else j + 1
            continue
        if c == "{":
            e = _quoted_end(text, j)
            if e is not None:
                j = e
                continue
        j += 1
    return n


def _blank_noncode(text: str) -> str:
    """注释/串内容/quoted string/字符字面量/shebang → 空白（等长、换行
    保留——行号不漂移）；类型变量（`'a`）保留不吞。"""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(" and text.startswith("(*", i):
            j = _comment_end(text, i)
            _blank_range(out, i, j)
            i = j
            continue
        if c == '"':
            j = _str_end(text, i)
            closed = j - 1 > i and j - 1 < n and text[j - 1] == '"'
            _blank_range(out, i + 1, (j - 1) if closed else n)
            i = j
            continue
        if c == "'":
            e = _char_lit_end(text, i)
            if e is not None:
                _blank_range(out, i, e)
                i = e
                continue
            i += 1
            continue
        if c == "{":
            e = _quoted_end(text, i)
            if e is not None:
                _blank_range(out, i, e)
                i = e
                continue
            i += 1
            continue
        if c == "#" and i == 0 and text.startswith("#!", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank_range(out, i, j)
            i = j
            continue
        i += 1
    return "".join(out)


# ------------------------------------------------------------------ 提取


_OPEN_RE = re.compile(r"(?<![\w.'])open!?\s+([A-Z][A-Za-z0-9_'.]*)")
_INCLUDE_RE = re.compile(r"(?<![\w.'])include\s+([A-Z][A-Za-z0-9_'.]*)")
_MODULE_ALIAS_RE = re.compile(
    r"(?<![\w.'])(module\s+(?:rec\s+)?[A-Z][A-Za-z0-9_']*\s*)=\s*([A-Z][A-Za-z0-9_'.]*)"
)
_MODULE_REF_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_']*)(?:\.[A-Z][A-Za-z0-9_']*)*\."
)


def _iter_refs(blanked: str) -> list[dict]:
    """open/include/模块别名/大写路径访问 → [{kind, target, line}]。

    open 含 `open!` 与 `let open ... in`（前一字符守卫放行空格）；
    别名 RHS 取大写路径头（struct/functor/module type 不误取）；
    大写路径访问取路径头（尾段是子模块/值——不重复提取）。
    """
    rows: list[dict] = []
    for kind, regex, group in (
        ("open", _OPEN_RE, 1),
        ("include", _INCLUDE_RE, 1),
        ("alias", _MODULE_ALIAS_RE, 2),
        ("ref", _MODULE_REF_RE, 1),
    ):
        for m in regex.finditer(blanked):
            rows.append(
                {
                    "kind": kind,
                    "target": m.group(group),
                    "line": blanked.count("\n", 0, m.start()) + 1,
                }
            )
    return rows


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ 路径


def _is_test(rel: str) -> bool:
    """测试文件判定（OCaml 约定）：test/tests 目录段（精确小写——dune
    test/ 标准布局），或 `*_test.ml`/`*_test.mli`（alcotest foo_test.ml
    惯例）/裸 `test.ml`/`test.mli`。"""
    segs = rel.split("/")
    if any(seg in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1].lower()
    for suf in OCAML_SUFFIXES:
        if name.endswith(suf):
            stem = name[: -len(suf)]
            return stem == "test" or stem.endswith("_test")
    return False


# ------------------------------------------------------------------ 扫描


def scan_ocaml(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[set[str], list[dict], int, int, bool]:
    """repo → (file_set, ref_rows, files_scanned, tests_excluded, timed_out)。

    file_set：repo 相对 posix .ml/.mli 路径集；ref_rows：[{file, line,
    kind, target}]（仅测试外文件；kind='open'|'include'|'alias'|'ref'）；
    测试文件（test/ 目录、*_test.ml）计入 files_scanned 与
    tests_excluded，容器仍入 file_set（叶可见），依赖边不入图。超时截断
    的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_set: set[str] = set()
    ref_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        rel_dir = Path(dirpath).relative_to(repo).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        for name in sorted(filenames):
            if not name.lower().endswith(OCAML_SUFFIXES):
                continue
            if time.monotonic() > deadline:
                return file_set, ref_rows, files_scanned, tests_excluded, True
            rel = (rel_dir + "/" + name) if rel_dir else name
            files_scanned += 1
            file_set.add(rel)
            blanked = _blank_noncode(_read(repo, rel))
            if _is_test(rel):
                tests_excluded += 1
                continue
            for row in _iter_refs(blanked):
                ref_rows.append({"file": rel, **row})
    return file_set, ref_rows, files_scanned, tests_excluded, False


def build_ocaml_graph(
    file_set: set[str],
    ref_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键=全部 .ml/.mli 文件——漏叶会假阴）+ 边明细。

    模块名索引：词干小写键，.ml 优先于 .mli（实现权威）、同名取字典序
    首；引用路径头解析 → 索引命中文件容器；未命中=外部（Stdlib/第三方）
    跳过；同词干 .mli↔.ml 自引用跳过（同一编译单元非依赖）。
    """
    index: dict[str, str] = {}
    for rel in sorted(file_set):
        base = posixpath.basename(rel)
        stem = base[: base.rfind(".")].lower()
        cur = index.get(stem)
        if cur is None or (cur.endswith(".mli") and rel.endswith(".ml")):
            index[stem] = rel
    graph: dict[str, set[str]] = {rel: set() for rel in file_set}
    edges: list[dict] = []
    for row in ref_rows:
        src = row["file"]
        head = row["target"].split(".")[0].lower()
        dst = index.get(head)
        if not dst or dst == src:
            continue
        src_base = posixpath.basename(src)
        src_stem = src_base[: src_base.rfind(".")].lower()
        if head == src_stem:
            continue
        graph[src].add(dst)
        edges.append(
            {"file": src, "line": row["line"], "src": src, "dst": dst}
        )
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_ocaml_containers(
    con: sqlite3.Connection,
    payload: dict,
) -> dict:
    """module_id → 排序容器集（fid→file_path 直推——文件容器，无扫描）。

    仅 .ml/.mli 符号入容器；其他扩展名计入 extensions 观测；
    ocaml_files 供 run() 路由（有 .ml/.mli 即进 OCaml 引擎给出可行动
    结论）。
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
    ocaml_paths: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if p.lower().endswith(OCAML_SUFFIXES):
            ocaml_paths.add(p)
            containers[fid_to_module[fid]].add(p)
        elif Path(p).suffix:
            extensions.add(Path(p).suffix)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "ocaml_files": len(ocaml_paths),
    }


# ------------------------------------------------------------------ 违例


def _belongs(container: str, prefixes: list[str]) -> bool:
    """容器 ∈ 容器集（编译单元文件路径唯一键——精确匹配）。"""
    return container in prefixes


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


def run_ocaml(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """OCaml 执法入口：契约生成（复用层 DAG）→ 源级 module/open 图 →
    BFS 违例 → P9 findings（幂等 replace-all；超时/失败不误删既有）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "ocaml-module-parser",
        "structure_status": structure_status,
    }
    file_set, ref_rows, files_scanned, tests_excluded, timed_out = scan_ocaml(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"ocaml scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_set:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no ocaml files found (no .ml/.mli sources)",
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
    graph, edges = build_ocaml_graph(file_set, ref_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接依赖边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自依赖边）；防御：丢弃无法锚定的违例并诚实计数
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
