"""S7 架构执法 Perl 引擎：内置源级 package/use 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 perl——纯解析器路线，零工具链依赖）：
- Perl 无内置依赖图工具（Perl::Prereqs 系需 CPAN 安装）；源级 package/use
  解析纯 stdlib 零工具链（与 rs/java/cs/kt 引擎同路线），无
  enforcer-not-found 态；语法验证仅 dogfood 走 CBM 索引。
- CBM Perl 事实（work/perl-cbm-probe 实证）：CBM 索引 Perl——Project/Branch/
  Folder/File/Module（package 声明）/Function（sub）全入表（10 符号）；
  qualified name 路径式；**CBM Perl 零 IMPORTS 边**（同 C 引擎先例——依赖边
  全部自扫，不阻塞）。
- 容器 = Perl 包（`package X;` 声明，首声明为准——Java 引擎哲学；use 语句
  的目标就是包名，容器=包免 @INC 路径猜测）。无 package 声明文件不入图
  （orphan 诚实计数；.t 测试脚本无包是常态，不计 orphan）。
- 词法（Perl 特有形态全落实）：
  * `#` 行注释（`$#` 数组尾下标 sigil 非注释守卫）；
  * POD 块（行首 `=`+字母 → `=cut` 行闭合；未闭合截断文件尾）；
  * 单双引号串（反斜杠转义、可跨行、未闭合吞到文件尾——Perl 串合法跨行，
    保守方向：多报不漏报）；引号字符保留+span 表回查原文——
    `use parent 'X'` 的引号目标是包名必须可见；
  * q/qq/qw/qr/m/s/tr/y 算符（前字符守卫防标识符误触；括号定界嵌套计数；
    转义跳过；s/tr/y 双段+尾随修饰符；qw 内容入 span 表——
    `use parent qw(Foo Bar)` 是合法常用形态）；
  * heredoc（`<<"X"`/`<<'X'`/`<<X`/`<<\\X`/`<<~X` 全形态；行末挂起队列，
    换行后逐行消费至终止行——同行多 heredoc 按序消费；`<<~` 缩进终止行）；
  * `__END__`/`__DATA__` 行首截断文件尾；
  * 裸 `/regex/` 不词法（除法歧义不可判——已知局限，正则内 `#` 可能截断
    注释、误吞到行尾：保守方向）。
- use/require 提取：行首 use/require；版本声明（`use v5.36;` `require 5.010;`）
  跳过；裸词包名；引号目标（.pm 结尾按路径解析，否则按包名）；动态目标
  （变量/拼接）不入图；`use parent/base` 语句余部全扫（引号 span+裸词，
  `-norequire` 标志跳过）；`no Module;` 反导入非依赖边不扫。
- 解析顺序：裸词目标 → 包索引（包名→首个声明文件）精确命中；引号 .pm 路径
  → lib/（最强惯例）→ 仓根 → 源文件目录 三候选。外部依赖（CPAN/core，
  索引未命中）跳过。
- 测试排除（Perl 约定式保守排除）：路径含 t/xt/test/tests **目录段**（精确
  小写——Perl t/ 目录约定），或 `.t` 后缀（TAP），或 test_*.pl/.pm、
  *_test.pl/.pm、*_tests.pl/.pm、test.pl/test.pm 裸名——排除文件的
  use/require 不入图；其 package 声明仍入图（叶包可见）。
- 违例语义 = 传递闭包（十七引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 use/require 边所在文件+行号（同包多文件满足同一边时取
  (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * 无 package 声明文件不入图不参与执法（orphan 计数；.t 不计——TAP 脚本
    无包是常态）
  * 裸 /regex/ 词法不识别（除法歧义）——正则内 # 可能误启注释（保守方向：
    可能漏报，不误报边）
  * eval 字符串内 use（运行时加载）不识别（字符串整体空白化）
  * `use if COND, 'Mod'` 条件加载的目标不提取（罕见形态）
  * blib/build/local/vendor/隐藏目录跳过（产物与 vendored 代码无架构语义）
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

PERL_SUFFIXES = (".pm", ".pl", ".t")
SKIP_DIRS = {"blib", "build", "local", "node_modules", "vendor"}
TEST_SEGMENTS = ("t", "xt", "test", "tests")

_PACKAGE_RE = re.compile(
    r"^[ \t]*package[ \t]+([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z0-9_]*)*)", re.M
)
_USE_RE = re.compile(r"^[ \t]*(use|require)[ \t]+", re.M)
_BAREWORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z0-9_]*)*")
_QOP_RE = re.compile(r"(qq|qr|qw|q|tr|s|m|y)([^A-Za-z0-9_\s])")
_HEREDOC_RE = re.compile(
    r"<<(~?)[ \t]*(?:\"([A-Za-z_]\w*)\"|'([A-Za-z_]\w*)'|\\([A-Za-z_]\w*)|([A-Za-z_]\w*))"
)
_BRACKET_PAIR = {"(": ")", "[": "]", "{": "}", "<": ">"}
_QOP_EXCLUDE_PREV = set("$@%&:)]}>'\"`*")


# ------------------------------------------------------------------ 词法底座


def _blank_range(out: list[str], a: int, b: int) -> None:
    """out[a:b) 非换行字符空白化（换行保留——行号不漂移）。"""
    for k in range(a, max(b, 0)):
        if k < len(out) and out[k] != "\n":
            out[k] = " "


def _dq_end(text: str, i: int) -> int:
    """双引号串闭引号后一位置；反斜杠转义；未闭合吞到文件尾（Perl 串可跨行）。"""
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


def _sq_end(text: str, i: int) -> int:
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        j += 1
    return n


def _qop_prev_ok(text: str, i: int) -> bool:
    """q 算符前字符守卫：标识符/sigil/闭括号/引号后不触发（防 sort/my/链式调用误触）。"""
    if i == 0:
        return True
    prev = text[i - 1]
    return not (prev.isalnum() or prev == "_" or prev in _QOP_EXCLUDE_PREV)


def _scan_delimited(text: str, dpos: int, delim: str, close: str | None) -> int:
    """定界区扫描：dpos=开定界符位置 → 闭定界符后一位置；转义跳过；括号深度计数。"""
    n = len(text)
    j = dpos + 1
    if close is None:
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == delim:
                return j + 1
            j += 1
        return n
    depth = 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == delim:
            depth += 1
        elif text[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _consume_qop(
    text: str,
    out: list[str],
    spans: dict[int, int],
    i: int,
    m: "re.Match[str]",
) -> int:
    """消费 q/qq/qw/qr/m/s/tr/y 算符：内容空白化+span 登记；返回继续位置。"""
    n = len(text)
    op = m.group(1)
    delim = m.group(2)
    close = _BRACKET_PAIR.get(delim)
    dpos = i + len(op)
    end1 = _scan_delimited(text, dpos, delim, close)
    _blank_range(out, dpos + 1, end1 - 1)
    spans[dpos] = end1
    k = end1
    if op in ("s", "tr", "y"):
        if close is not None:
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == delim:
                end2 = _scan_delimited(text, k, delim, close)
                _blank_range(out, k + 1, end2 - 1)
                spans[k] = end2
                k = end2
        else:
            end2 = _scan_delimited(text, k, delim, None)
            _blank_range(out, k, end2 - 1)
            k = end2
        while k < n and (text[k].isalpha() or text[k] == "_"):
            k += 1
    elif op in ("m", "qr"):
        while k < n and text[k].isalpha():
            k += 1
    return k


def _blank_noncode(text: str) -> tuple[str, dict[int, int]]:
    """注释/POD/字符串/q 算符/heredoc 体/__END__ → 空白（等长、换行保留）。

    引号与定界符字符保留、内容空白化；返回 (空白化文本, span 表)——
    span 表 {开定界符位置: 闭定界符后一位置}，use parent 'X' / qw(Foo Bar)
    的目标经 span 表回查原文（空白化不能丢目标——同 C include 目标区保留哲学）。
    """
    out = list(text)
    spans: dict[int, int] = {}
    i = 0
    n = len(text)
    pending: list[tuple[str, bool]] = []  # 同行多 heredoc 挂起队列：(tag, 缩进)
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            for tag, indented in pending:
                while i < n:
                    nl = text.find("\n", i)
                    end = n if nl < 0 else nl + 1
                    line = text[i : n if nl < 0 else nl]
                    matched = (line.strip() == tag) if indented else (line.rstrip() == tag)
                    _blank_range(out, i, end)
                    i = end
                    if matched or nl < 0:
                        break
            pending = []
            continue
        if c in " \t\r":
            i += 1
            continue
        at_bol = i == 0 or text[i - 1] == "\n"
        if at_bol and c == "=" and i + 1 < n and text[i + 1].isalpha():
            k = text.find("\n=cut", i)
            if k >= 0:
                e = text.find("\n", k + 1)
                end = n if e < 0 else e + 1
            else:
                end = n
            _blank_range(out, i, end)
            i = end
            continue
        if at_bol and (text.startswith("__END__", i) or text.startswith("__DATA__", i)):
            _blank_range(out, i, n)
            break
        if c == "#":
            if i > 0 and text[i - 1] == "$":
                i += 1  # $#array 数组尾下标 sigil 非注释
                continue
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank_range(out, i, j)
            i = j
            continue
        if c == '"':
            j = _dq_end(text, i)
            _blank_range(out, i + 1, j - 1)
            spans[i] = j
            i = j
            continue
        if c == "'":
            j = _sq_end(text, i)
            _blank_range(out, i + 1, j - 1)
            spans[i] = j
            i = j
            continue
        if c == "<" and text.startswith("<<", i):
            m = _HEREDOC_RE.match(text, i)
            if m:
                tag = m.group(2) or m.group(3) or m.group(4) or m.group(5)
                pending.append((tag, m.group(1) == "~"))
                i = m.end()
                continue
            i += 1
            continue
        if c in "qmtsy":
            m = _QOP_RE.match(text, i)
            if m and _qop_prev_ok(text, i):
                i = _consume_qop(text, out, spans, i, m)
                continue
            i += 1
            continue
        i += 1
    return "".join(out), spans


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_package(blanked: str) -> str | None:
    m = _PACKAGE_RE.search(blanked)
    return m.group(1) if m else None


def _iter_uses(blanked: str, spans: dict[int, int], text: str) -> list[dict]:
    """已空白化文本 → [{kind, target, line}]；kind ∈ pkg（包名）/ path（.pm 路径）。"""
    rows: list[dict] = []
    n = len(blanked)
    for m in _USE_RE.finditer(blanked):
        kw = m.group(1)
        pos = m.end()
        line = blanked.count("\n", 0, m.start()) + 1

        def add(target: str, kind: str) -> None:
            rows.append({"kind": kind, "target": target, "line": line})

        ch = blanked[pos] if pos < n else ""
        if ch.isdigit() or (ch == "v" and pos + 1 < n and blanked[pos + 1].isdigit()):
            continue  # 版本声明：use v5.36 / require 5.010
        if ch in ("'", '"'):
            end = spans.get(pos)
            if end is None:
                continue
            content = text[pos + 1 : end - 1]
            add(content, "path" if content.endswith(".pm") else "pkg")
            continue
        bm = _BAREWORD_RE.match(blanked, pos)
        if bm is None:
            continue  # 动态目标（require $mod 等）不入图
        target = bm.group(0)
        if kw == "use" and target in ("parent", "base"):
            stop = n
            semi = blanked.find(";", bm.end())
            nl = blanked.find("\n", bm.end())
            for cand in (semi, nl):
                if cand >= 0:
                    stop = min(stop, cand)
            p = bm.end()
            while p < stop:
                if p in spans:
                    end = spans[p]
                    for tok in text[p + 1 : end - 1].split():
                        add(tok, "path" if tok.endswith(".pm") else "pkg")
                    p = end
                    continue
                wm = _BAREWORD_RE.match(blanked, p)
                if wm:
                    wend = wm.end()
                    if wend in spans and wm.group(0) in (
                        "q", "qq", "qw", "qr", "m", "s", "tr", "y",
                    ):
                        p = wend
                        continue  # q 算符定界符入口——内容经 span 回查，算符名本身不是目标
                    if not (p > 0 and blanked[p - 1] == "-"):
                        add(wm.group(0), "pkg")
                    p = wend
                    continue
                p += 1
            continue
        add(target, "pkg")
    return rows


# ------------------------------------------------------------------ 路径


def _dir_of(p: str) -> str:
    slash = p.rfind("/")
    return p[:slash] if slash >= 0 else ""


def _norm(path: str) -> str:
    """词法规范化：反斜杠归一、剥 ./、.. 弹栈（越顶忽略）。"""
    parts: list[str] = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _is_test(rel: str) -> bool:
    """测试文件判定（Perl 约定）：t/xt/test/tests 目录段（精确小写——t/ 目录
    约定），或 `.t` 后缀（TAP），或 test_*.pl/.pm、*_test.pl/.pm、
    *_tests.pl/.pm 族（test.pl/test.pm 裸名亦然）。
    """
    segs = rel.split("/")
    if any(seg in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1].lower()
    if name.endswith(".t"):
        return True
    for suffix in (".pl", ".pm"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return (
                stem in ("test", "tests")
                or stem.startswith("test_")
                or stem.endswith(("_test", "_tests"))
            )
    return False


def _resolve_path(target: str, src_file: str, file_set: set[str]) -> str | None:
    """引号 .pm 目标 → 仓内文件路径；未命中（外部/系统）→ None。

    候选顺序：lib/（ExtUtils/Module::Build 最强惯例）→ 仓根 → 源文件目录
    （@INC 近似——真实 @INC 含 site 目录不可知，仓内三候选覆盖主流布局）。
    """
    if ":" in target or target.startswith(("/", "\\")):
        return None  # 绝对路径/盘符跳过
    src_dir = _dir_of(src_file)
    for cand in (
        _norm("lib/" + target),
        _norm(target),
        _norm(src_dir + "/" + target),
    ):
        if cand in file_set:
            return cand
    return None


# ------------------------------------------------------------------ 扫描


def scan_perl(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, str], list[dict], int, int, bool]:
    """repo → (file_pkg, import_rows, files_scanned, tests_excluded, timed_out)。

    file_pkg：repo 相对 posix 路径 → 包名（首 package 声明；无声明不入表）；
    import_rows：[{file, line, kind, target}]（仅测试外文件）；测试文件
    （.t/t 目录等）计入 files_scanned 与 tests_excluded，package 仍入表
    （叶包可见），use/require 不入图。
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
            if not name.lower().endswith(PERL_SUFFIXES):
                continue
            if time.monotonic() > deadline:
                return file_pkg, import_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            text = _read(repo, rel)
            blanked, spans = _blank_noncode(text)
            pkg = _parse_package(blanked)
            if pkg is not None:
                file_pkg[rel] = pkg
            if _is_test(rel):
                tests_excluded += 1
                continue
            for row in _iter_uses(blanked, spans, text):
                import_rows.append({"file": rel, **row})
    return file_pkg, import_rows, files_scanned, tests_excluded, False


def build_perl_graph(
    file_pkg: dict[str, str],
    import_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """包依赖图（键 = 全部声明包——叶包无出边也入图，漏叶会假阴）+ 边明细。

    裸词目标 → 包索引精确命中（包名→字典序首声明文件）；引号 .pm 路径 →
    lib/→仓根→源目录三候选解析到文件后取其包；外部（索引/路径未命中）跳过。
    """
    packages = set(file_pkg.values())
    graph: dict[str, set[str]] = {p: set() for p in packages}
    edges: list[dict] = []
    file_set = set(file_pkg)
    for row in import_rows:
        src = file_pkg.get(row["file"])
        if src is None:
            continue
        if row["kind"] == "pkg":
            dst = row["target"] if row["target"] in packages else None
        else:
            resolved = _resolve_path(row["target"], row["file"], file_set)
            dst = file_pkg.get(resolved) if resolved else None
        if dst is None or dst == src:
            continue
        graph[src].add(dst)
        edges.append({"file": row["file"], "line": row["line"], "src": src, "dst": dst})
    return {p: sorted(t) for p, t in graph.items()}, edges


def derive_perl_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序包名集（fid→file_path→package 声明）。

    仅当 payload 符号含 .pm/.pl/.t 文件才扫描；orphan（.pm/.pl 无 package
    声明或磁盘缺失，去重路径；.t 不计——TAP 脚本无包是常态）与超时诚实
    上报——perl_files 供 run() 路由（有 .pm 但全 orphan 也要进 perl 引擎
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
    perl_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(PERL_SUFFIXES)
    }
    file_pkg: dict[str, str] = {}
    timed_out = False
    if perl_paths:
        file_pkg, _imports, _files, _tests, timed_out = scan_perl(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(PERL_SUFFIXES):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        name = file_pkg.get(p)
        if name is None:
            if not p.lower().endswith(".t"):
                orphans.add(p)
            continue
        containers[fid_to_module[fid]].add(name)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "perl_files": len(perl_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(pkg: str, prefixes: list[str]) -> bool:
    """包名 ∈ 容器集（容器可为父包——Probe 捕获 Probe::Store 子包）。"""
    return any(pkg == p or pkg.startswith(p + "::") for p in prefixes)


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


def run_perl(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Perl 执法入口：契约生成（复用层 DAG）→ 源级 use/require 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "perl-package-parser",
        "structure_status": structure_status,
    }
    file_pkg, import_rows, files_scanned, tests_excluded, timed_out = scan_perl(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"perl scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_pkg:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no perl packages found (no package declarations)",
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
    graph, edges = build_perl_graph(file_pkg, import_rows)
    summary["files"] = files_scanned
    summary["packages"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 use/require 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 use/require 边）；防御：丢弃无法锚定的违例并诚实计数
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
