"""S7 架构执法 R 引擎：内置源级 source()/包引用解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-08，本机无 R/Rscript——纯解析器路线，零工具链依赖）：
- R 依赖图工具需 R 安装（desc/namespace 系包）；源级 source()/包引用解析纯
  stdlib 零工具链（与 rs/java/cs/kt/perl/erlang 引擎同路线），无
  enforcer-not-found 态；语法验证仅 dogfood 走 CBM 索引。
- CBM R 事实（work/r-cbm-probe 实证）：CBM 索引 R——Project/Branch/Folder/
  File/Module/Function 全入表（11 符号）；Module=.R 文件（qualified name
  路径式）；Function 真行号；IMPORTS 边仅 source 文件→目标 Module
  （line=None）+ CALLS 边（line=5 heuristic）——**行号不可信，锚定自扫**
  （同 C/Perl/Erlang 先例，不阻塞）。
- 容器 = R 包目录（DESCRIPTION `Package:` 声明——最近祖先目录，含文件
  自身目录；Swift manifest 权威哲学）优先；无包归属的散 .R 文件 = 文件
  自身（repo 相对 posix 路径，Lua/Dart/Zig 文件容器哲学）。包目录键 "."
  = 仓根包。单包仓全文件归一容器（R 包内函数同命名空间无导入结构——
  层内不可执法是语言事实，诚实反映为零违例）。
- 词法（R 特有形态全落实）：
  * `#` 行注释（R 无块注释；`#'` roxygen 同为注释——`@import` 元数据
    不构成运行时依赖边）；
  * 单双引号串（反斜杠转义；R 串语法不可裸跨行——未闭合截断行尾防
    误吞文件尾）；
  * 反引号非标准标识符（内容空白化——不构成依赖语法）；
  * `source`/`library`/`require`/`loadNamespace`/`requireNamespace`
    关键字后 `(`+可选命名实参 `file=`/`package=`+字符串实参**原文保留**
    （同 C include 目标区保留哲学——实参是字符串语法不能随字符串空白
    化；实参可跨行（空白类含换行），关键字与 `(` 须同行——R 语句边界
    语义；非字符串实参（变量）正常空白化→动态调用不入图；`$`/`@` 前缀
    方法/槽访问拒绝，`::` 前缀放行——`base::source()` 语义仍是载入）。
- 依赖边提取（空白化文本）：
  * `source("path.R")`/`source(file=...)`：字符串实参解析——当前文件
    目录相对+仓根相对两候选（R 运行时 cwd 不可知——两惯例近似）；
    绝对路径/盘符/`~`/URL/反斜杠路径跳过；目标须命中 .R 文件集；
  * `library(pkg)`/`require(pkg)`/`loadNamespace`/`requireNamespace`
    （裸词/单双引号串/`package=` 命名）+ `pkg::fun`/`pkg:::fun`（R 中
    双冒号语法专用于命名空间访问）：包名 → 仓内 DESCRIPTION `Package:`
    索引命中 → 包目录容器；外部（CRAN/core，索引未命中）跳过；裸词
    变量实参按包名近似（library(var) 动态形态——保守方向，命中本地包
    才产边，实际风险极低）；
  * DESCRIPTION `Imports:`/`Depends:` 字段（DCF 续行合并；版本规格
    `pkg (>= x)` 剥离；`R` 自身跳过）：声明即依赖——R 包内代码不调
    library()，包间依赖的权威声明在 DESCRIPTION；锚定 DESCRIPTION
    字段行（无符号）；`Suggests:`/`LinkingTo:` 不产边（测试软依赖/
    编译链接）；
  * 自容器（同包/同散文件）跳过。
- 测试排除（R 约定式保守排除）：路径含 test/tests/testthat **目录段**
  （精确小写——testthat 标准布局 tests/testthat），或文件名 `test-*.R`
  （testthat 连字符标准）/`test_*.R`/`*_test.R`/`*_tests.R`/裸 `test.R`；
  排除文件的依赖边不入图；文件容器仍入图（叶可见）。
- 违例语义 = 传递闭包（十八引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接依赖边所在文件+行号（同容器多文件满足同一边时取
  (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * NAMESPACE `import()`/`importFrom()` 声明不解析（包级依赖经
    DESCRIPTION `Imports:`/`Depends:` 字段入图；importFrom 裸函数使用
    侧无 `::` 不产使用侧行号——边锚 DESCRIPTION 字段行）
  * `sys.source()`/`eval(parse(...))` 动态载入形态不入图（sys.source
    罕用；eval 动态求值不可静态解析）
  * `source()` 变量/拼接实参不入图（动态形态）
  * `library(var)` 变量实参按变量名近似包名（命中本地包才产边）
  * `source()` 反斜杠 Windows 路径形态不入图（R 惯例正斜杠）
  * roxygen `#' @import` 元数据不产边（文档元数据非运行时依赖）
  * renv/packrat 库目录跳过（包管理缓存无架构语义）
  * 重名本地包取字典序首目录（DESCRIPTION 同名冲突罕见）
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

R_SUFFIXES = (".r",)
SKIP_DIRS = {"renv", "packrat", "node_modules", "vendor"}
TEST_SEGMENTS = ("test", "tests", "testthat")
_KEYWORDS = ("source", "library", "require", "loadNamespace", "requireNamespace")

_SOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9._$])source[ \t]*\([ \t\r\n]*(?:file[ \t\r\n]*=[ \t\r\n]*)?"
    r"(?:\"([^\"]+)\"|'([^']+)'"
    r")"
)
_LIB_RE = re.compile(
    r"(?<![A-Za-z0-9._$])"
    r"(?:library|require|loadNamespace|requireNamespace)"
    r"[ \t]*\([ \t\r\n]*(?:package[ \t\r\n]*=[ \t\r\n]*)?"
    r"(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z.][A-Za-z0-9._]*))"
)
_NS_RE = re.compile(r"(?<![A-Za-z0-9._$])([A-Za-z.][A-Za-z0-9._]*)[ \t]*:{2,3}")
_DESC_RE = re.compile(r"^Package:[ \t]*(\S+)", re.M)
_DCF_FIELD_RE = re.compile(r"^(Imports|Depends):[ \t]*(.*)$")


# ------------------------------------------------------------------ 词法底座


def _blank_range(out: list[str], a: int, b: int) -> None:
    """out[a:b) 非换行字符空白化（换行保留——行号不漂移）。"""
    for k in range(a, max(b, 0)):
        if k < len(out) and out[k] != "\n":
            out[k] = " "


def _str_end(text: str, i: int) -> int:
    """引号串闭引号后一位置；反斜杠转义；换行即截断（R 串不可裸跨行——
    未闭合串自愈到行尾，不误吞文件尾）。"""
    q = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == q:
            return j + 1
        if text[j] == "\n":
            return j
        j += 1
    return n


def _bt_end(text: str, i: int) -> int:
    """反引号标识符闭反引号后一位置；反斜杠转义；换行即截断。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "`":
            return j + 1
        if text[j] == "\n":
            return j
        j += 1
    return n


def _ident_left(text: str, j: int) -> tuple[str, int]:
    """text[j]（含）向左取完整 R 标识符 → (word, word 起始下标前一位置)。"""
    start = j
    while start >= 0 and (text[start].isalnum() or text[start] in "._"):
        start -= 1
    return text[start + 1 : j + 1], start


def _preserved_quote(text: str, i: int) -> bool:
    """原始文本回看：text[i] 开引号是否 source/library 族关键字的首字符串
    实参（可直接/`file=`/`package=` 命名）——是则保留原文（同 C include
    目标区保留）。`$`/`@` 前缀（方法/槽访问）拒绝；`::` 前缀放行
    （`base::source()` 语义仍是载入）。"""
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j >= 0 and text[j] == "=":
        k = j - 1
        while k >= 0 and text[k] in " \t\r\n":
            k -= 1
        word, start = _ident_left(text, k)
        if word not in ("file", "package"):
            return False
        j = start
        while j >= 0 and text[j] in " \t\r\n":
            j -= 1
    if j < 0 or text[j] != "(":
        return False
    word, start = _ident_left(text, j - 1)
    if word not in _KEYWORDS:
        return False
    if start >= 0 and (text[start].isalnum() or text[start] in "._$@"):
        return False
    return True


def _blank_noncode(text: str) -> str:
    """注释/串（保留区除外）/反引号标识符 → 空白（等长、换行保留）。

    `source`/`library` 族首字符串实参**原文保留**（依赖目标必须可见——
    同 C include 目标区保留哲学）；其内部字符经消费跳过，防串内 `#`
    误启注释。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            continue
        if c in "\"'":
            if _preserved_quote(text, i):
                i = _str_end(text, i)
                continue
            j = _str_end(text, i)
            _blank_range(out, i + 1, j - 1)
            i = j
            continue
        if c == "`":
            j = _bt_end(text, i)
            _blank_range(out, i + 1, j - 1)
            i = j
            continue
        if c == "#":
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


def _iter_refs(blanked: str) -> list[dict]:
    """已空白化文本 → [{kind, target, line}]（kind='source'|'package'）。"""
    rows: list[dict] = []
    for m in _SOURCE_RE.finditer(blanked):
        target = m.group(1) or m.group(2)
        if target:
            rows.append(
                {"kind": "source", "target": target,
                 "line": blanked.count("\n", 0, m.start()) + 1}
            )
    for m in _LIB_RE.finditer(blanked):
        target = m.group(1) or m.group(2) or m.group(3)
        if target:
            rows.append(
                {"kind": "package", "target": target,
                 "line": blanked.count("\n", 0, m.start()) + 1}
            )
    for m in _NS_RE.finditer(blanked):
        rows.append(
            {"kind": "package", "target": m.group(1),
             "line": blanked.count("\n", 0, m.start()) + 1}
        )
    return rows


# ------------------------------------------------------------------ 路径


def _is_test(rel: str) -> bool:
    """测试文件判定（R 约定）：test/tests/testthat 目录段（精确小写——
    testthat 标准布局 tests/testthat），或文件名 `test-*.R`（testthat
    连字符标准）/`test_*.R`/`*_test.R`/`*_tests.R`/裸 `test.R`。"""
    segs = rel.split("/")
    if any(seg in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1].lower()
    if not name.endswith(".r"):
        return False
    stem = name[: -len(".r")]
    return (
        stem in ("test", "tests")
        or stem.startswith(("test-", "test_"))
        or stem.endswith(("_test", "_tests"))
    )


def _owner(rel: str, package_dirs: dict[str, str]) -> str | None:
    """文件 → 最近祖先包目录（含自身目录；根包键 "."）；无包归属 → None。"""
    segs = rel.split("/")
    for k in range(len(segs) - 1, -1, -1):
        d = "/".join(segs[:k]) if k else "."
        if d in package_dirs:
            return d
    return None


def _abs_like(target: str) -> bool:
    """绝对路径/盘符/`~`/URL/反斜杠形态——不可仓内解析。"""
    return (
        target.startswith(("/", "~", "\\"))
        or re.match(r"^[A-Za-z]:", target) is not None
        or "://" in target
        or "\\" in target
    )


def _desc_imports(text: str) -> list[dict]:
    """DESCRIPTION `Imports:`/`Depends:` 字段 → [{kind, target, line}]。

    DCF 续行（缩进行）合并；版本规格 `pkg (>= x)` 剥离；`R` 自身跳过；
    锚定字段起始行。声明即依赖——R 包内代码不调 library()，包间依赖的
    权威声明在 DESCRIPTION（NAMESPACE 亦由 R 包构建器从其生成）。
    """
    rows: list[dict] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m = _DCF_FIELD_RE.match(lines[i])
        if m is None:
            i += 1
            continue
        val = m.group(2)
        line_no = i + 1
        i += 1
        while i < n and lines[i][:1] in (" ", "\t"):
            val += " " + lines[i].strip()
            i += 1
        for part in val.split(","):
            name = re.sub(r"[ \t]*\(.*\)[ \t]*$", "", part.strip()).strip()
            if name and name != "R":
                rows.append({"kind": "package", "target": name, "line": line_no})
    return rows


# ------------------------------------------------------------------ 扫描


def scan_r(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[set[str], list[dict], dict[str, str], int, int, bool]:
    """repo → (file_set, ref_rows, package_dirs, files_scanned, tests_excluded,
    timed_out)。

    file_set：repo 相对 posix .R 路径集；ref_rows：[{file, line, kind,
    target}]（仅测试外文件）；package_dirs：目录 posix（根="."）→ 包名；
    测试文件（tests/ 目录、test-*.R 等）计入 files_scanned 与
    tests_excluded，容器仍入表（叶可见），依赖边不入图。
    超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_set: set[str] = set()
    ref_rows: list[dict] = []
    package_dirs: dict[str, str] = {}
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
            if name == "DESCRIPTION":
                if time.monotonic() > deadline:
                    return file_set, ref_rows, package_dirs, files_scanned, tests_excluded, True
                rel = (rel_dir + "/" + name) if rel_dir else name
                raw = _read(repo, rel)
                m = _DESC_RE.search(raw)
                if m and m.group(1) not in package_dirs.values():
                    package_dirs[rel_dir or "."] = m.group(1)
                    for prow in _desc_imports(raw):
                        ref_rows.append({"file": rel, **prow})
                continue
            if not name.lower().endswith(R_SUFFIXES):
                continue
            if time.monotonic() > deadline:
                return file_set, ref_rows, package_dirs, files_scanned, tests_excluded, True
            rel = (rel_dir + "/" + name) if rel_dir else name
            files_scanned += 1
            file_set.add(rel)
            if _is_test(rel):
                tests_excluded += 1
                continue
            blanked = _blank_noncode(_read(repo, rel))
            for row in _iter_refs(blanked):
                ref_rows.append({"file": rel, **row})
    return file_set, ref_rows, package_dirs, files_scanned, tests_excluded, False


def build_r_graph(
    file_set: set[str],
    ref_rows: list[dict],
    package_dirs: dict[str, str],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键 = 全部容器——包目录+散文件，漏叶会假阴）+ 边明细。

    容器 = 最近祖先包目录（DESCRIPTION 权威）或散文件自身；
    source 边解析当前文件目录相对+仓根相对两候选；package 边经包名索引
    命中；自容器/外部（未命中索引）/空包目录（非图节点）跳过。
    """
    container_of: dict[str, str] = {}
    for rel in file_set:
        owner = _owner(rel, package_dirs)
        container_of[rel] = owner if owner is not None else rel
    containers: set[str] = set(container_of.values())
    graph: dict[str, set[str]] = {c: set() for c in containers}
    edges: list[dict] = []
    for row in ref_rows:
        src = container_of.get(row["file"])
        if src is None:
            # DESCRIPTION Imports:/Depends: 声明行（非 .R 文件）——归属其
            # 包目录；无 .R 文件的包非图节点（无代码即无契约链）跳过。
            d = posixpath.dirname(row["file"]) or "."
            if not row["file"].endswith("DESCRIPTION") or d not in containers:
                continue
            src = d
        dst: str | None = None
        if row["kind"] == "source":
            target = row["target"]
            if not _abs_like(target):
                base_dir = posixpath.dirname(row["file"])
                for cand in (
                    posixpath.normpath(posixpath.join(base_dir, target)),
                    posixpath.normpath(target),
                ):
                    if cand in file_set:
                        dst = container_of[cand]
                        break
        else:
            for d, name in package_dirs.items():
                if name == row["target"]:
                    dst = d
                    break
        if dst is None or dst == src or dst not in containers:
            continue
        graph[src].add(dst)
        edges.append(
            {"file": row["file"], "line": row["line"], "src": src, "dst": dst}
        )
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_r_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序容器集（fid→file_path→包目录或文件自身）。

    仅当 payload 符号含 .R 文件才扫描；超时诚实上报——r_files 供 run()
    路由（有 .R 即进 R 引擎给出可行动结论）。
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
    r_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(R_SUFFIXES)
    }
    file_set: set[str] = set()
    package_dirs: dict[str, str] = {}
    timed_out = False
    if r_paths:
        file_set, _refs, package_dirs, _files, _tests, timed_out = scan_r(
            repo, timeout
        )
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(R_SUFFIXES):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        owner = _owner(p, package_dirs)
        containers[fid_to_module[fid]].add(owner if owner is not None else p)
    loose = {
        c
        for names in containers.values()
        for c in names
        if c not in package_dirs
    }
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "r_files": len(r_paths),
        "packages": len(package_dirs),
        "loose_files": len(loose),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(container: str, prefixes: list[str]) -> bool:
    """容器 ∈ 容器集（包目录/散文件路径唯一键——精确匹配）。"""
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


def run_r(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """R 执法入口：契约生成（复用层 DAG）→ 源级 source/包引用图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "r-source-package-parser",
        "structure_status": structure_status,
    }
    file_set, ref_rows, package_dirs, files_scanned, tests_excluded, timed_out = scan_r(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"r scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_set:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no r files found (no .R sources)",
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
    graph, edges = build_r_graph(file_set, ref_rows, package_dirs)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["packages"] = len(package_dirs)
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
