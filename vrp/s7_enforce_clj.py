"""S7 架构执法 Clojure 引擎：内置源级 ns/require 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-08，本机无 clojure/lein/boot/bb——纯解析器路线，零工具
链依赖）：
- Clojure 依赖图工具需 JVM/Clojure 安装；源级 ns/require 解析纯 stdlib
  零工具链（与 rs/java/cs/kt/perl/erlang/r 引擎同路线），无
  enforcer-not-found 态；语法验证仅 dogfood 走 CBM 索引。
- CBM Clojure 事实（work/clojure-cbm-probe 实证）：CBM 索引 Clojure——
  Project/Branch/Folder/File/Module/Function 全入表；Module=.clj 文件
  （qualified name 路径式）；Function 真行号；IMPORTS 边 File→File
  （payload 含 local_name，line=None）——**行号不可信，锚定自扫**
  （同 C/Perl/Erlang/R 先例，不阻塞）。
- 容器 = Clojure 命名空间（首 `(ns name)` 声明——require 目标即命名空
  间，免 classpath 猜测，Erlang 模块/Perl 包哲学）；无 ns 声明文件 =
  文件自身（repo 相对 posix 路径，Lua/Dart/Zig/R 散文件容器哲学——可
  出边不可入边，叶可见）。
- 词法（Clojure 特有形态全落实）：
  * `;` 行注释 + `#!` 行注释（shebang 同形）；
  * `"..."` 串（`\\` 转义、**可跨行**——Clojure 串合法跨行，未闭合吞
    EOF 保守方向，同 Perl；内容空白化但记录 (offset→内容) 供 load 实
    参提取）；
  * `#"..."` 正则字面量（引号不转义——单独闭引号扫描，整体空白化）；
  * `\\c` 字符字面量（`\\newline`/`\\space` 命名、`\\uXXXX`、`\\oNNN`、任
    意单字符——`\\\\"` 防误开串，同 Erlang `$"` 哲学）；
  * `#_` 丢弃形态仅空白化两个字符（其后形态不丢弃——诚实局限）。
- 提取（空白化文本 → 微型 tokenizer+form reader——require 子句是嵌套
  向量，正则不可靠）：
  * 括号/向量/花括号单字符 token+原子 token（行号随 token）；
  * 递归遍历全部 list 形态，头部 ns/require/use/load/load-file 处理
    （`/` 成员访问守卫——`alias/require`/Java 方法不误触）；
   * ns 形态：reader 元数据前缀（`^`/`^:kw`/`#^` 及其后元数据值形态）
     跳过后取命名空间名（symbol/串）；`:require`/`:use`/
     `:require-macros` 子句——条目判别镜像 clojure.core `libspec?`
     （向量第二元素 keyword → 单 libspec；否则前缀列表）：单条目取向量
     头部命名空间（`:refer`/`:as`/`:exclude` 参数不误取）；前缀列表
     `[p sub1 [sub2 :as x]]` 逐项 load-lib 语义——裸 symbol 拼接
     p.sub1（不载入 p）、向量后缀递归拼接 prefix.sub；
  * require/use 函数形态：`'sym`/`'[vec]`/串实参+`:reload` 族 flag 跳过；
  * reader 条件 `#?`/`#?@`（.cljc 双平台形态）：形树级展开——分支不
    区分全分支按序（`#?` 插入分支形态/`#?@` 拼接分支形态项 Splicing），
    分支形内嵌套条件递归展开；
  * load/load-file：串实参——load 按 classpath 根（仓根/src/test 三根
    ×带/不带扩展九候选）解析；load-file 按仓根+文件目录两候选解析；
  * 外部命名空间（clojure.string 等，索引未命中）跳过。
- 测试排除（Clojure 约定式保守排除）：路径含 test/tests **目录段**（精
  确小写——test/ 标准布局），或文件名 `*_test.clj`/`*_test.cljc`（ns
  my-app.core-test ↔ 文件 my_app/core_test.clj 惯例）/裸 `test.clj`；
  排除文件的依赖边不入图；文件容器仍入图（叶可见）。
- 违例语义 = 传递闭包（十九引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接依赖边所在文件+行号（同容器多文件满足同一边时取
  (file,line) 字典序首）。
- 诚实局限（文档化不静默）：
  * `#_` 丢弃形态后的形态不丢弃（罕见形态，多报方向保守）
  * reader 条件分支表内直接嵌套条件（奇偶位错位形态）不展开（罕见
    ——分支形内嵌套已递归展开）
  * `(in-ns ...)` 中途切换命名空间不识别（文件容器取首 ns）
  * 同名命名空间多文件取字典序首（gen-class 生成物/多根布局罕见）
  * `:import`（Java 类）不产边（Java 类非仓内命名空间）
  * `(require)` 变量/拼接实参不入图（动态形态）
  * shadowed `require` 局部函数名误触风险（罕见，保守方向）
  * target/构建产物与 .cpcache 跳过
  * 扫描超时 → enforcer-failed（部分图不可信，不静默截断）
"""

from __future__ import annotations

import json
import os
import posixpath
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

CLJ_SUFFIXES = (".clj", ".cljc")
SKIP_DIRS = {"target", ".cpcache", "node_modules", "vendor"}
TEST_SEGMENTS = ("test", "tests")
_NS_CLAUSE_KEYS = (":require", ":use", ":require-macros")
_HEADS = {"ns", "require", "use", "load", "load-file"}


# ------------------------------------------------------------------ 词法底座


def _blank_range(out: list[str], a: int, b: int) -> None:
    """out[a:b) 非换行字符空白化（换行保留——行号不漂移）。"""
    for k in range(a, max(b, 0)):
        if k < len(out) and out[k] != "\n":
            out[k] = " "


def _str_end(text: str, i: int) -> int:
    r"""闭引号后一位置；`\` 转义；Clojure 串可跨行——未闭合吞 EOF（保守）。"""
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


def _regex_end(text: str, i: int) -> int:
    """`#"..."` 正则字面量闭引号后一位置（内部引号无需转义——独立扫描）。"""
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


def _char_lit_end(text: str, i: int) -> int:
    r"""`\c` 字符字面量结束位置：`\newline` 命名/`\uXXXX`/`\oNNN`/任意单字符
    （`\\"` 防误开串——同 Erlang `$"` 哲学）。"""
    j = i + 1
    n = len(text)
    if j >= n:
        return n
    c = text[j]
    if c == "u":
        k = j + 1
        while k < n and k <= j + 4 and text[k] in "0123456789abcdefABCDEF":
            k += 1
        return k
    if c == "o":
        k = j + 1
        cnt = 0
        while k < n and cnt < 3 and text[k] in "01234567":
            k += 1
            cnt += 1
        return k
    if c.isalpha():
        k = j + 1
        while k < n and text[k].isalpha():
            k += 1
        return k
    return j + 1


def _blank_noncode(text: str) -> tuple[str, dict[int, str]]:
    """注释/正则字面量/串内容/字符字面量 → 空白（等长、换行保留）。

    返回 (空白化文本, {串起始 offset → 串内容})——串内容供 load 实参提
    取（同 C include 目标区保留哲学的等价物：记录而非原位保留）。
    """
    out = list(text)
    strings: dict[int, str] = {}
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == ";":
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank_range(out, i, j)
            i = j
            continue
        if c == '"' and i > 0 and text[i - 1] == "#":
            j = _regex_end(text, i)
            _blank_range(out, i - 1, j)
            i = j
            continue
        if c == '"':
            j = _str_end(text, i)
            closed = j - 1 > i and j - 1 < n and text[j - 1] == '"'
            content = text[i + 1 : j - 1] if closed else text[i + 1 : n]
            strings[i] = content
            _blank_range(out, i + 1, (j - 1) if closed else n)
            i = j
            continue
        if c == "\\":
            j = _char_lit_end(text, i)
            _blank_range(out, i, j)
            i = j
            continue
        if c == "#" and text[i : i + 2] == "#_":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            continue
        if c == "#" and text[i : i + 2] == "#!":
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank_range(out, i, j)
            i = j
            continue
        i += 1
    return "".join(out), strings


# ------------------------------------------------- tokenizer + form reader


def _tokenize(blanked: str, strings: dict[int, str]) -> list[dict]:
    """空白化文本 → token 流（括号/串/关键字/symbol，行号随 token）。"""
    tokens: list[dict] = []
    i = 0
    n = len(blanked)
    line = 1
    while i < n:
        c = blanked[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if c in "()[]{}":
            tokens.append({"t": "bracket", "v": c, "line": line})
            i += 1
            continue
        if c == '"':
            j = blanked.find('"', i + 1)
            j = n if j < 0 else j + 1
            tokens.append(
                {"t": "str", "v": strings.get(i, ""), "line": line}
            )
            line += blanked.count("\n", i, j)
            i = j
            continue
        j = i
        while j < n and not blanked[j].isspace() and blanked[j] not in "()[]{}":
            j += 1
        word = blanked[i:j]
        kind = "kw" if word[0] == ":" else "sym"
        tokens.append({"t": kind, "v": word, "line": line})
        i = j
    return tokens


def _parse_forms(tokens: list[dict]) -> list[dict]:
    """token 流 → 嵌套形态树（seq/sym/kw/str；不平衡括号宽容跳过）。"""
    root: list[dict] = []
    stack: list[list[dict]] = [root]
    for tok in tokens:
        if tok["t"] == "bracket":
            if tok["v"] in "([{":
                seq: dict = {"k": "seq", "items": [], "line": tok["line"]}
                stack[-1].append(seq)
                stack.append(seq["items"])
            else:
                if len(stack) > 1:
                    stack.pop()
        else:
            stack[-1].append({"k": tok["t"], "v": tok["v"], "line": tok["line"]})
    return root


def _splice_conds(items: list[dict]) -> list[dict]:
    """reader 条件 `#?`/`#?@` 形树级展开（分支不区分——全分支按序拼接）。

    `#?` 插入分支形态本身；`#?@` 拼接分支形态项（Splicing）；分支形内
    嵌套条件递归展开；分支表内直接嵌套条件（奇偶位错位）不展开（罕见）。
    """
    out: list[dict] = []
    i = 0
    n = len(items)
    while i < n:
        it = items[i]
        if it["k"] == "seq":
            out.append({**it, "items": _splice_conds(it["items"])})
            i += 1
            continue
        if (
            it["k"] == "sym"
            and it["v"] in ("#?", "#?@")
            and i + 1 < n
            and items[i + 1]["k"] == "seq"
        ):
            branches = items[i + 1]["items"]
            forms = [
                branches[k] if branches[k]["k"] != "seq"
                else {**branches[k], "items": _splice_conds(branches[k]["items"])}
                for k in range(1, len(branches), 2)
            ]
            if it["v"] == "#?":
                out.extend(forms)
            else:
                for f in forms:
                    if f["k"] == "seq":
                        out.extend(f["items"])
            i += 2
            continue
        out.append(it)
        i += 1
    return out


def _strip_reader_macros(word: str) -> str:
    """去头部 reader 宏字符（quote/syntax-quote/unquote/deref/meta/var）。"""
    return word.lstrip("'`~@^#")


def _entry_head(entry: dict) -> str | None:
    """条目头部词（symbol/串）；关键字/括号 → None。"""
    if entry["k"] == "sym":
        w = _strip_reader_macros(entry["v"])
        if w and not w.startswith(":"):
            return w
        return None
    if entry["k"] == "str":
        return entry["v"]
    return None


def _handle_prefixed(entry: dict, prefix: str, out_rows: list[dict]) -> None:
    """前缀列表子条目：`[sub :as x]` → prefix.sub（嵌套前缀递归）。"""
    if entry["k"] != "seq" or not entry["items"]:
        return
    items = entry["items"]
    head = _entry_head(items[0])
    if head is None:
        return
    full = f"{prefix}.{head}"
    if len(items) >= 2 and items[1]["k"] == "seq":
        for sub in items[1:]:
            _handle_prefixed(sub, full, out_rows)
        return
    out_rows.append({"kind": "ns", "target": full, "line": items[0]["line"]})


def _handle_entry(entry: dict, out_rows: list[dict]) -> None:
    """require/use 条目 → 依赖行（kind='ns'）。

    形态：symbol（裸命名空间）/串/向量。向量判别镜像 clojure.core
    `libspec?`（core.clj:6122）反判定：第二元素为 keyword（`:as` 选项
    起始）→ 单 libspec `[my.ns :as x :refer [a]]`（取头部，`:refer`/
    `:as` 参数不误取）；否则 → **前缀列表** `[p sub1 sub2 [sub2b :as y]]`
    ——逐项 `load-lib p`（core.clj:6244-6247）：裸 symbol 拼接 p.sub1/
    p.sub2（官方 libs 文档 `(require '(clojure.contrib def except sql))`
    三符号形态——**不载入 p 本身**），向量后缀 `_handle_prefixed` 递归；
    keyword/串项跳过（`[p x :as y]` 真实 Clojure 抛异常——宽容跳过）。
    """
    if entry["k"] in ("sym", "str"):
        head = _entry_head(entry)
        if head and "/" not in head and not head.startswith("."):
            out_rows.append({"kind": "ns", "target": head, "line": entry["line"]})
        return
    if entry["k"] != "seq" or not entry["items"]:
        return
    items = entry["items"]
    head = _entry_head(items[0])
    if head is None:
        return
    if len(items) >= 2 and items[1]["k"] != "kw":
        for sub in items[1:]:
            if sub["k"] == "sym":
                w = _strip_reader_macros(sub["v"])
                if (
                    w
                    and not w.startswith(":")
                    and "/" not in w
                    and not w.startswith(".")
                ):
                    out_rows.append(
                        {"kind": "ns", "target": f"{head}.{w}", "line": sub["line"]}
                    )
            elif sub["k"] == "seq":
                _handle_prefixed(sub, head, out_rows)
        return
    if "/" not in head and not head.startswith("."):
        out_rows.append({"kind": "ns", "target": head, "line": items[0]["line"]})


def _handle_ns(items: list[dict], out_rows: list[dict], ns_name: dict) -> None:
    """ns 形态：reader 元数据前缀（`^`/`^:kw`/`#^`+元数据值）跳过后取名；
    `:require`/`:use`/`:require-macros` 子句条目提取。"""
    if ns_name.get("name") is None:
        i = 1
        while i < len(items):
            it = items[i]
            if it["k"] == "sym" and (
                it["v"].startswith("^") or it["v"].startswith("#^")
            ):
                i += 1
                if it["v"] in ("^", "#^"):
                    i += 1  # 裸 ^ 后随元数据值形态
                continue
            if it["k"] == "sym":
                w = _strip_reader_macros(it["v"])
                if w and "/" not in w and not w.startswith("."):
                    ns_name["name"] = w
            elif it["k"] == "str":
                ns_name["name"] = it["v"]
            break
    for clause in items[2:]:
        if clause["k"] != "seq" or not clause["items"]:
            continue
        first = clause["items"][0]
        if first["k"] == "kw" and first["v"] in _NS_CLAUSE_KEYS:
            for entry in clause["items"][1:]:
                _handle_entry(entry, out_rows)


def _walk(
    forms: list[dict], out_rows: list[dict], ns_name: dict
) -> None:
    """递归遍历形态树：头部 ns/require/use/load/load-file 的 list 处理。"""
    for form in forms:
        if form["k"] != "seq":
            continue
        items = form["items"]
        if not items:
            continue
        head = items[0]
        head_word = None
        if head["k"] == "sym":
            w = head["v"]
            if "/" not in w:
                head_word = _strip_reader_macros(w)
        if head_word == "ns":
            _handle_ns(items, out_rows, ns_name)
            continue
        if head_word in ("require", "use"):
            for entry in items[1:]:
                _handle_entry(entry, out_rows)
            continue
        if head_word == "load":
            for entry in items[1:]:
                if entry["k"] == "str":
                    out_rows.append(
                        {"kind": "load", "target": entry["v"], "line": entry["line"]}
                    )
            continue
        if head_word == "load-file":
            for entry in items[1:]:
                if entry["k"] == "str":
                    out_rows.append(
                        {"kind": "load-file", "target": entry["v"], "line": entry["line"]}
                    )
            continue
        _walk(items, out_rows, ns_name)


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ 路径


def _is_test(rel: str) -> bool:
    """测试文件判定（Clojure 约定）：test/tests 目录段（精确小写——test/
    标准布局），或文件名 `*_test.clj`/`*_test.cljc`（ns my-app.core-test ↔
    文件 my_app/core_test.clj 惯例）/裸 `test.clj`。"""
    segs = rel.split("/")
    if any(seg in TEST_SEGMENTS for seg in segs[:-1]):
        return True
    name = segs[-1].lower()
    for suf in CLJ_SUFFIXES:
        if name.endswith(suf):
            stem = name[: -len(suf)]
            return stem == "test" or stem.endswith("_test")
    return False


def _abs_like(target: str) -> bool:
    """绝对路径/盘符/`~`/URL——不可仓内解析（反斜杠归一 `/` 后可解析）。"""
    return (
        target.startswith(("/", "~"))
        or (len(target) > 1 and target[1] == ":")
        or "://" in target
    )


# ------------------------------------------------------------------ 扫描


def scan_clj(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[set[str], list[dict], dict[str, str], int, int, bool]:
    """repo → (file_set, ref_rows, ns_by_file, files_scanned, tests_excluded,
    timed_out)。

    file_set：repo 相对 posix .clj/.cljc 路径集；ref_rows：[{file, line,
    kind, target}]（仅测试外文件；kind='ns'|'load'|'load-file'）；
    ns_by_file：文件 → 首命名空间名（含测试文件——容器仍入表叶可见）；
    测试文件（test/ 目录、*_test.clj）计入 files_scanned 与
    tests_excluded，依赖边不入图。超时截断的部分扫描不可信，调用方须
    拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_set: set[str] = set()
    ref_rows: list[dict] = []
    ns_by_file: dict[str, str] = {}
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
            if not name.lower().endswith(CLJ_SUFFIXES):
                continue
            if time.monotonic() > deadline:
                return (
                    file_set,
                    ref_rows,
                    ns_by_file,
                    files_scanned,
                    tests_excluded,
                    True,
                )
            rel = (rel_dir + "/" + name) if rel_dir else name
            files_scanned += 1
            file_set.add(rel)
            blanked, strings = _blank_noncode(_read(repo, rel))
            rows: list[dict] = []
            ns_name: dict = {"name": None}
            _walk(
                _splice_conds(
                    _parse_forms(_tokenize(blanked, strings))
                ),
                rows,
                ns_name,
            )
            if ns_name["name"] is not None:
                ns_by_file[rel] = ns_name["name"]
            if _is_test(rel):
                tests_excluded += 1
                continue
            for row in rows:
                ref_rows.append({"file": rel, **row})
    return file_set, ref_rows, ns_by_file, files_scanned, tests_excluded, False


def build_clj_graph(
    file_set: set[str],
    ref_rows: list[dict],
    ns_by_file: dict[str, str],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键 = 全部容器——命名空间+散文件，漏叶会假阴）+ 边明细。

    容器 = 首命名空间名（ns 权威）或散文件自身；ns 边经命名空间索引命中
    （字典序首文件）；load 边按 classpath 根（仓根/src/test ×带/不带扩展）
    候选；load-file 边按仓根+文件目录候选；自容器/外部（索引未命中）/
    不可解析路径跳过。
    """
    container_of: dict[str, str] = {}
    for rel in file_set:
        ns = ns_by_file.get(rel)
        container_of[rel] = ns if ns else rel
    ns_index: dict[str, str] = {}
    for rel in sorted(file_set):
        ns = ns_by_file.get(rel)
        if ns and ns not in ns_index:
            ns_index[ns] = rel
    containers: set[str] = set(container_of.values())
    graph: dict[str, set[str]] = {c: set() for c in containers}
    edges: list[dict] = []
    for row in ref_rows:
        src = container_of.get(row["file"])
        if src is None:
            continue
        dst: str | None = None
        target = row["target"]
        if row["kind"] == "ns":
            target_file = ns_index.get(target)
            if target_file:
                dst = container_of[target_file]
        elif not _abs_like(target):
            t = target.replace("\\", "/")
            if row["kind"] == "load":
                for cand in (
                    t,
                    f"{t}.clj",
                    f"{t}.cljc",
                    f"src/{t}",
                    f"src/{t}.clj",
                    f"src/{t}.cljc",
                    f"test/{t}",
                    f"test/{t}.clj",
                    f"test/{t}.cljc",
                ):
                    if cand in file_set:
                        dst = container_of[cand]
                        break
            else:  # load-file
                base_dir = posixpath.dirname(row["file"])
                for cand in (
                    posixpath.normpath(t),
                    posixpath.normpath(posixpath.join(base_dir, t)),
                ):
                    if cand in file_set:
                        dst = container_of[cand]
                        break
        if dst is None or dst == src or dst not in containers:
            continue
        graph[src].add(dst)
        edges.append(
            {"file": row["file"], "line": row["line"], "src": src, "dst": dst}
        )
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_clj_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序容器集（fid→file_path→命名空间或文件自身）。

    仅当 payload 符号含 .clj/.cljc 文件才扫描；超时诚实上报——clj_files
    供 run() 路由（有 .clj/.cljc 即进 Clojure 引擎给出可行动结论）。
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
    clj_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(CLJ_SUFFIXES)
    }
    ns_by_file: dict[str, str] = {}
    timed_out = False
    if clj_paths:
        _fs, _r, ns_by_file, _files, _tests, timed_out = scan_clj(repo, timeout)
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(CLJ_SUFFIXES):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        ns = ns_by_file.get(p)
        containers[fid_to_module[fid]].add(ns if ns else p)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "clj_files": len(clj_paths),
        "namespaces": len(set(ns_by_file.values())),
        "loose_files": sum(
            1 for c in containers for m in containers[c] if m in set(clj_paths)
        ),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(container: str, prefixes: list[str]) -> bool:
    """容器 ∈ 容器集（命名空间/散文件路径唯一键——精确匹配）。"""
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


def run_clj(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Clojure 执法入口：契约生成（复用层 DAG）→ 源级 ns/require 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "clojure-namespace-parser",
        "structure_status": structure_status,
    }
    file_set, ref_rows, ns_by_file, files_scanned, tests_excluded, timed_out = scan_clj(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"clojure scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_set:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no clojure files found (no .clj/.cljc sources)",
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
    graph, edges = build_clj_graph(file_set, ref_rows, ns_by_file)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["namespaces"] = len(set(ns_by_file.values()))
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
