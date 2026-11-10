"""S7 架构执法 Ruby 引擎：内置源级 module/require 解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，本机无 ruby）：
- Ruby 依赖边界 = module 嵌套命名空间（Foo::Bar）；声明边 = require/require_relative
  （Ruby 生态架构工具同口径）→ 内置源级 module/require 解析纯 stdlib 零工具链
  （与 rs/java/cs/kt/c/php/scala 引擎同路线），无 enforcer-not-found 态；
  语法验证仅在 dogfood 探针走 CBM 索引成功 + xcheck agreement（CBM 内置 Ruby
  解析器——异于 PHP 自建沙箱 php -l 的路线记实）。
- CBM Ruby IMPORTS 边存在但 line=None（ruby-cbm-probe 实测，File→Module，
  evidence=structural）→ 锚定行号必须自扫源码；容器推导 fid→file_path→读文件
  解析 module 嵌套（与 java/cs/kt/php/scala 同策略）。
- 容器 = module/class 嵌套全路径（module Foo; module Bar → Foo::Bar；compact
  形态 module Foo::Bar 与 class 同构——Ruby 类也是命名空间；class Foo < Bar
  超类忽略——依赖边是 require 不是继承）。文件容器 = 文件内声明全路径的
  极大元（中间命名空间被更深层声明覆盖时排除：module Probe 遇 Probe::Api
  时 Probe 不入图——共享根坍缩噪声防护，叶容器仍可见）。
- 词法（等长空白，换行保留，前进保证处处——_char_end 死循环教训）：行注释 #；
  =begin/=end 块注释（列 0）；__END__ 行首独立行截断其后 DATA 数据节
  （review 批修正：原数据节内伪 require/module/字符串污染图——假阳方向）；
  单引号串仅单引号与反斜杠转义（异于双引号全转义）；双引号/反引号/冒号
  引号符号串全转义（插值花括号内嵌引号不穿透——词法局限同 kt/php）；
  % 字面量 %q/%Q/%w/%W/%i/%I/%s/%r/裸 % 成对或同字符定界（前一非空白
  字符为标识符/数字/右括号时按模运算跳过）；heredoc 形态 <<ID/<<-ID/
  <<~ID/带引号 ID（前一非空白字符为标识符字符时按左移/追加跳过；无终止
  行截断至文件尾——WIP 未闭合 heredoc 保守处理）；正则字面量按表达式
  位置启发式（前一非空白字符为等号/左括号/逗号/左方括号/感叹/与或/问冒
  分号/花括号/波浪号（=~ 是 Ruby 正则最高频位置——review 批修正）或
  行首/文件首，或前一非空白记号以 if/unless/when/while/until/and/or/
  not/elsif/then/do/return 结尾（review 批修正——行首条件正则）——
  除法/追加误判防护；字符类内斜杠不闭合；无闭合回退行尾）。
- require 目标区整体保留（同 C 引擎 include 引号目标——require 是字符串语法
  不能随字符串空白化）：require/require_relative 后同行紧邻（可含左括号，
  括号实参可跨行——review 批修正：require(\n'foo.rb'\n) 合法 Ruby 原丢边；
  无括号形态不跨行——require\n'x' 是两条语句非实参）的首个字符串字面量
  原样保留；成员访问 obj.require(...) 拒（review 批修正：原目标保留→
  伪边——同 Dart _DIRECTIVE_BLOCKERS 句点拒绝哲学）；目标含花括号插值
  或实参非字符串（变量/表达式）→ 动态 require 不入图（诚实文档化）。
- 块深度启发式：行首 opener 关键字（module/class/def/do/if/unless/case/
  begin/while/until/for）+1、end 词计数 -1；Ruby 3 endless def
  （def 名(参数)? = 表达式——无 end 闭合；= 两侧空白区分 setter
  def foo=(v)）不开启块（review 批修正：原 +1 永不闭合致深度漂移、
  后续 module 嵌套错挂——假阳/假阴双风险）；行中 modifier if/unless
  不开启块（常规风格 opener 均在行首）；{} 块不计深（module 声明罕见于
  brace 块内）；同行分号链逐声明 local_depth 递进（module A; module B
  同行 → A::B 嵌套正确）。
- require_relative 解析 = 相对当前文件目录 + .rb（实参自带 .rb 时原样候选）；
  require 解析 = $LOAD_PATH 近似候选根（仓根/lib/src/app）+ .rb；./ ../ 前缀
  按相对当前文件目录；未命中 → 外部 gem/stdlib 跳过（不入图）。
- 测试排除：路径含 test/tests/spec 目录段或 test_*.rb / *_test.rb / *_spec.rb
  文件名（minitest+rspec 约定）——require 不入图，module 容器仍入图（叶可见，
  同 php/scala 哲学）。
- 违例语义 = 传递闭包（十引擎统一）：BFS 最短链入 payload；
  锚定 = 链尾直接 require 边 (file,line) 字典序首 + 违例文件首符号。
- 诚实局限（文档化不静默）：
  * 无 module/class 声明的脚本文件不入图（orphan 计数）
  * 行中块开启（x = if y 形态）不计数 → 嵌套深度漂移（常规风格不出现）
  * 动态 require（插值/变量实参）不入图
  * require 行内注释间隙（require # c\n'x'）不支持——Ruby 语义 = 两条语句
    非实参（异于 Dart 批：Dart 间隙注释同行合法故跳过）
  * archaic 字符字面量（?' ?# 等）不解析（现代代码零出现，不特调）
  * 继承（< Super）不是依赖边（require 才是声明边，与各引擎同哲学）
  * vendor/node_modules/build/target/隐藏目录跳过（产物无源码语义）
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

RUBY_SUFFIX = ".rb"
SKIP_DIRS = {"vendor", "node_modules", "build", "target"}
_LOAD_PATH_ROOTS = ("", "lib/", "src/", "app/")
_PAIRED = {"(": ")", "[": "]", "{": "}", "<": ">"}
# heredoc / % 字面量的前一非空白字符为标识符/数字/右括号 → 运算符（<<追加/%模）非字面量
_OP_TAIL = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_)].")
# 正则字面量表达式位置：前一非空白字符 ∈ 此集合（或行首/文件首）
_REGEX_PREV = set("=(,[!&|?:;{}~")
# 正则字面量关键字尾：前一非空白记号以这些关键字结尾（if/unless/when/...）
# —— =~ 与行首条件是 Ruby 正则最高频位置（review 批修正：原漏 ~ 与关键字尾，
# 正则体内 module/class/require 文本泄漏为伪声明/伪边）
_REGEX_KW_TAIL = re.compile(
    r"(?:\A|[^\w.])(?:if|unless|when|while|until|and|or|not|elsif|then|do|return)[ \t]*$"
)
# Ruby 3 endless def：def 名(参数)? = 表达式——无 end 闭合（块深度漂移根源；
# Ruby 语法要求 = 两侧空白——\s+=\s 精确区分 setter def foo=(v) 与 endless）
_ENDLESS_DEF_RE = re.compile(r"^[ \t]*def\s+(self\.)?[a-z_]\w*[?!]?(\([^()]*\))?\s+=\s")

_DECL_RE = re.compile(
    r"\b(module|class)\s+([A-Z][A-Za-z0-9_]*(?:::[A-Z][A-Za-z0-9_]*)*)"
)
_OPENER_RE = re.compile(
    r"^[ \t]*(module|class|def|do|if|unless|case|begin|while|until|for)\b"
)
_END_RE = re.compile(r"\bend\b")
_HEREDOC_RE = re.compile(r"<<([-~]?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_BLOCK_COMMENT_END = re.compile(r"^=end", re.M)
_REQUIRE_RE = re.compile(
    # 括号实参可跨行（review 批修正：require(\n'foo.rb'\n) 合法 Ruby 原丢边）；
    # 无括号形态不跨行（require\n'x' 是两条语句非实参——Ruby 语句完整即终止）
    r"\b(require_relative|require)\b[ \t]*(?:\([ \t\r\n]*|)[ \t]*(['\"])([^'\"\n]+)\2"
)


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串/%字面量/heredoc/正则 → 空白（等长，换行保留）。

    require/require_relative 同行紧邻的目标字符串字面量原样保留（require 是
    字符串语法不能随字符串空白化——同 C 引擎 include 引号目标）。前进保证
    处处（任何回退路径至少 +1，_char_end 死循环教训）。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            if j < 0:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
        elif (
            c == "="
            and (i == 0 or text[i - 1] == "\n")
            and text.startswith("=begin", i)
            and (i + 6 >= n or text[i + 6] in "\r\n \t")
        ):
            m = _BLOCK_COMMENT_END.search(text, i + 6)
            j = n if m is None else text.find("\n", m.start())
            if j < 0:
                j = n
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif (
            c == "_"
            and text.startswith("__END__", i)
            and (i == 0 or text[i - 1] == "\n")
            and (i + 7 >= n or text[i + 7] in "\r\n")
        ):
            # __END__ 行首独立行：其后全为 DATA 数据节非代码（review 批修正：
            # 原数据节内伪 require/module/字符串污染图——假阳方向）
            for k in range(i, n):
                if out[k] != "\n":
                    out[k] = " "
            i = n
        elif (
            (_word_at(text, i, "require_relative") or _word_at(text, i, "require"))
            and _prev_char(text, i) != "."
        ):
            # 成员访问 obj.require(...) 拒（review 批修正：原误入目标保留→伪边）
            wlen = 16 if text.startswith("require_relative", i) else 7
            k = i + wlen
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == "(":
                k += 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
            if k < n and text[k] in "'\"":
                i = _string_end(text, k)  # 目标区整体保留（含标识符与字面量）
            else:
                i = i + wlen  # 非字符串实参：动态 require，跳过标识符（前进保证）
        elif c == "<" and text.startswith("<<", i) and _prev_char(text, i) not in _OP_TAIL:
            m = _HEREDOC_RE.match(text, i)
            if m is None:
                i += 1
                continue
            j = _heredoc_end(text, m)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "'\"`":
            j = _string_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j if j > i else i + 1
        elif c == "%" and _prev_char(text, i) not in _OP_TAIL:
            j = _percent_end(text, i)
            if j is None:
                i += 1
                continue
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "/" and (
            _prev_char(text, i) in _REGEX_PREV
            or _REGEX_KW_TAIL.search(text[max(0, i - 48) : i]) is not None
        ):
            j = _regex_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j if j > i else i + 1
        else:
            i += 1
    return "".join(out)


def _word_at(text: str, i: int, word: str) -> bool:
    if not text.startswith(word, i):
        return False
    if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        return False
    j = i + len(word)
    if j < len(text) and (text[j].isalnum() or text[j] == "_"):
        return False
    return True


def _prev_char(text: str, i: int) -> str:
    """前一非空白字符（无则换行符——行首/文件首按可开字面量处理）。"""
    j = i - 1
    while j >= 0 and text[j] in " \t\r":
        j -= 1
    return text[j] if j >= 0 else "\n"


def _string_end(text: str, i: int) -> int:
    """字符串字面量结束位置（含闭引号；转义跳过；未闭合 → 文件尾）。"""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == text[i]:
            return j + 1
        j += 1
    return n


def _heredoc_end(text: str, m: re.Match) -> int:
    """heredoc 结束位置（终止行行尾；squiggly/dash 允许前导空白）。

    无终止行 → 文件尾（WIP 未闭合 heredoc 保守截断，前进保证）。
    blank 由调用方完成；本函数只返回 stop 位置。
    """
    n = len(text)
    ident = m.group(3)
    squash = m.group(1) in ("-", "~")
    nl = text.find("\n", m.end())
    line_start = nl + 1 if nl >= 0 else n
    while line_start < n:
        line_end = text.find("\n", line_start)
        if line_end < 0:
            line_end = n
        raw = text[line_start:line_end].rstrip("\r")
        cand = raw.lstrip() if squash else raw
        if cand == ident:
            return line_end
        line_start = line_end + 1
    return n


def _percent_end(text: str, i: int) -> int | None:
    """% 字面量结束位置；非 % 字面量形态（%= 等）→ None。"""
    n = len(text)
    j = i + 1
    if j < n and text[j] in "qQwWiIsSrx":
        j += 1
    if j >= n or text[j] == "=":
        return None
    open_d = text[j]
    close_d = _PAIRED.get(open_d, open_d)
    nested = close_d != open_d
    depth = 1
    k = j + 1
    while k < n:
        if text[k] == "\\":
            k += 2
            continue
        if nested and text[k] == open_d:
            depth += 1
        elif text[k] == close_d:
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return n


def _regex_end(text: str, i: int) -> int:
    """正则字面量结束位置（字符类内斜杠不闭合；未闭合回退行尾——前进保证）。"""
    n = len(text)
    j = i + 1
    in_class = False
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            break
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and (text[j].isalpha() or text[j] == "_"):
                j += 1  # 尾缀 flags（/re/i）
            return j
        j += 1
    j = text.find("\n", i)
    return j if j >= 0 else n


def _read(repo: Path, rel: str) -> str:
    """读源文件并剥离头部 UTF-8 BOM。

    ruby parse.y parser_prepare 对任何解析入口（主脚本/require 文件）跳过
    头部 EF BB BF（BOM 对词法器完全不可见）——BOM'd .rb 是合法源码。
    =begin/__END__ 等列 0（i==0 或前置 \n）构造在 BOM 未剥时全部失明
    （=begin 块当代码、__END__ 后 DATA 节当代码、行 1 opener 漏计致嵌套
    容器路径错挂），故须在读入口统一剥离；剥 1 字符使位置统一 -1、行号不变。
    """
    try:
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[1:] if text.startswith("\ufeff") else text


# ------------------------------------------------------------------ 解析


def _parse_containers(blanked: str) -> list[str]:
    """module/class 声明 → 文件容器（嵌套全路径的极大元，排序去重）。

    块深度 = 行首 opener 关键字 +1 / end 词 -1（modifier if/unless 行中不开启）；
    同行分号链逐声明 local 递进。声明全路径的极大元入容器（module Probe 遇
    Probe::Api 时 Probe 被覆盖——共享根坍缩噪声防护）。
    """
    stack: list[tuple[int, str]] = []
    paths: list[str] = []
    depth = 0
    for line in blanked.split("\n"):
        local = depth
        for m in _DECL_RE.finditer(line):
            while stack and stack[-1][0] >= local:
                stack.pop()
            for nm in m.group(2).split("::"):
                paths.append("::".join([s[1] for s in stack] + [nm]))
                stack.append((local, nm))
            local += 1
        if _OPENER_RE.match(line) and not _ENDLESS_DEF_RE.match(line):
            depth += 1
        depth -= len(_END_RE.findall(line))
        if depth < 0:
            depth = 0
        while stack and stack[-1][0] >= depth:
            stack.pop()
    maximal = [p for p in set(paths) if not any(q != p and q.startswith(p + "::") for q in paths)]
    return sorted(maximal)


def _iter_requires(blanked: str) -> list[dict]:
    """保留区 → [{kind, arg, line}]；插值目标（动态 require）跳过。"""
    rows: list[dict] = []
    for m in _REQUIRE_RE.finditer(blanked):
        arg = m.group(3)
        if "#{" in arg:
            continue
        line = blanked.count("\n", 0, m.start()) + 1
        rows.append({"kind": m.group(1), "arg": arg, "line": line})
    return rows


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if any(seg in ("test", "tests", "spec") for seg in parts[:-1]):
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.rb")
        or name.endswith("_spec.rb")
    )


# ------------------------------------------------------------------ 扫描


def scan_ruby(
    repo: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, list[str]], list[dict], int, int, bool]:
    """repo → (file_containers, require_rows, files_scanned, tests_excluded, timed_out)。

    file_containers：repo 相对 posix 路径 → 容器列表（无声明不入表 = orphan）；
    require_rows：[{file, line, kind, arg}]（仅测试外文件；arg 未解析原始实参）；
    测试文件（目录段或命名约定）require 不入图，容器仍入表（叶可见）。
    超时截断的部分扫描不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    file_containers: dict[str, list[str]] = {}
    require_rows: list[dict] = []
    files_scanned = 0
    tests_excluded = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.lower().endswith(RUBY_SUFFIX):
                continue
            if time.monotonic() > deadline:
                return file_containers, require_rows, files_scanned, tests_excluded, True
            rel = (Path(dirpath) / name).relative_to(repo).as_posix()
            files_scanned += 1
            blanked = _blank_noncode(_read(repo, rel))
            containers = _parse_containers(blanked)
            if not containers:
                continue
            file_containers[rel] = containers
            if _is_test(rel):
                tests_excluded += 1
                continue
            for row in _iter_requires(blanked):
                row["file"] = rel
                require_rows.append(row)
    return file_containers, require_rows, files_scanned, tests_excluded, False


def _resolve_require(src_rel: str, row: dict, file_set: set[str]) -> str | None:
    """require 实参 → 仓内 .rb 文件（未命中 = 外部 gem/stdlib → None）。"""
    arg = row["arg"]
    if row["kind"] == "require_relative" or arg.startswith(("./", "../")):
        base = posixpath.dirname(src_rel)
        cand = posixpath.normpath(posixpath.join(base, arg))
        candidates = [cand + ".rb"] + ([cand] if arg.endswith(".rb") else [])
    else:
        candidates = [posixpath.normpath(root + arg) + ".rb" for root in _LOAD_PATH_ROOTS]
        if arg.endswith(".rb"):
            candidates.insert(0, posixpath.normpath(arg))
    for c in candidates:
        if c in file_set and not c.startswith("../"):
            return c
    return None


def build_ruby_graph(
    file_containers: dict[str, list[str]],
    require_rows: list[dict],
) -> tuple[dict[str, list[str]], list[dict]]:
    """容器依赖图（键 = 全部容器——漏叶假阴）+ 边明细（含 require 溯源）。"""
    graph: dict[str, set[str]] = {
        c: set() for cs in file_containers.values() for c in cs
    }
    edges: list[dict] = []
    file_set = set(file_containers)
    for row in require_rows:
        dst_file = _resolve_require(row["file"], row, file_set)
        if dst_file is None:
            continue
        for src in file_containers.get(row["file"], []):
            for dst in file_containers.get(dst_file, []):
                if src == dst:
                    continue
                graph[src].add(dst)
                edges.append(
                    {"file": row["file"], "line": row["line"], "src": src, "dst": dst}
                )
    return {c: sorted(t) for c, t in graph.items()}, edges


def derive_ruby_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序容器集（fid→file_path→module/class 嵌套全路径）。

    仅当 payload 符号含 .rb 文件才扫描；orphan（.rb 无 module/class 声明或
    磁盘缺失，去重路径）与超时诚实上报——ruby_files 供 run() 路由（有 .rb
    但全 orphan 也要进 ruby 引擎给出可行动的 not-enforceable）。
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
    ruby_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(RUBY_SUFFIX)
    }
    file_containers: dict[str, list[str]] = {}
    timed_out = False
    if ruby_paths:
        file_containers, _requires, _files, _tests, timed_out = scan_ruby(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(RUBY_SUFFIX):
            ext = Path(p).suffix
            if ext:
                extensions.add(ext)
            continue
        names = file_containers.get(p)
        if not names:
            orphans.add(p)
            continue
        containers[fid_to_module[fid]].update(names)
    return {
        "containers": {mid: sorted(names) for mid, names in containers.items()},
        "extensions": sorted(extensions),
        "ruby_files": len(ruby_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _belongs(container: str, prefixes: list[str]) -> bool:
    """容器 ∈ 容器集（容器可为父命名空间——Probe 捕获 Probe::Storage 子路径）。"""
    return any(container == p or container.startswith(p + "::") for p in prefixes)


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


def run_ruby(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Ruby 执法入口：契约生成（复用层 DAG）→ 源级 require 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "ruby-module-parser",
        "structure_status": structure_status,
    }
    file_containers, require_rows, files_scanned, tests_excluded, timed_out = scan_ruby(
        repo, timeout
    )
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"ruby scan exceeded {timeout}s (partial graph discarded)",
        }
    if not file_containers:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no ruby modules found (no module/class declarations)",
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
    graph, edges = build_ruby_graph(file_containers, require_rows)
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    summary["tests_excluded"] = tests_excluded
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 require 边（chain[-2] → chain[-1]）的文件与行号
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
        # 理论不可达（链边必来自 require 边）；防御：丢弃无法锚定的违例并诚实计数
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
