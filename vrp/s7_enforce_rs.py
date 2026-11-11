"""S7 架构执法 Rust 引擎：内置源级 use 树解析（构想v7 §4 语言矩阵扩展）。

选型记实（2026-11-07，rustc/cargo 1.97.1 本机）：
- rustc 无稳定模块图导出 CLI（`-Zunpretty=...`/rustdoc JSON 均 nightly）；cargo-modules
  是第三方 cargo 子命令（cargo install，重外部依赖）；cargo metadata 只有 crate 级
  （工作区成员）无模块级。故 Rust 引擎 = **内置解析器（纯 stdlib，执法器即本模块）**，
  无 enforcer-not-found 态。
- mod 树构建：root=src/lib.rs（缺失时 src/main.rs——lib/main 是不同 crate 的根，
  同时入树会把两个 crate 混进同一命名空间）；`mod NAME;` 文件声明解析
  （parent=mod.rs 或 crate root → 同目录 name.rs|name/mod.rs；2018 风格非 mod.rs
  文件 → <stem>/name.rs|<stem>/name/mod.rs）；行内 mod 体不入树（树视图空白化
  行内体——防体内嵌套 `mod X;` 文件声明误按兄弟目录解析）。
- use 树解析：crate::/self::/super:: 根关键字；嵌套花括号展开
  （`use crate::api::{client::{get, post}, handler};`）；`as` 重命名/`*` glob
  （含路径后 glob `use crate::api::*;`）/多行；注释（含嵌套块注释）/字符串/字符
  字面量/原始字符串先空白化（保长度保行号）。
- **普通串跨行合法（review 批记实，rustc 探针 P1）**：Rust 普通串字面量可含裸
  换行——与 Java/C#/Kotlin 串内禁换行相反，kt/cs/java 批的「未闭合串换行截断」
  家族修法对 Rust 不适用（截断会把合法多行串后段当代码产幻影边）；未闭合串
  （rustc E0765）扫描至 EOF 保守空白化——换行截断无语法依据。
- **行内 mod use 计入（review 批修正，探针 P2/P3/P7）**：生产行内 mod 体
  （`mod helper { use crate::api; }`）是真实编译期依赖边，与函数体内局部 use
  同理计入（「编译期真实依赖边」原则）；use 视图（_read_use_view）保留行内体，
  _iter_uses_ctx 递归下钻：self/super 相对行内路径解析（super=enclosing 模块），
  边归属性=所在文件模块（行内 mod 无独立文件节点）；`use super::*` 解析回
  enclosing=自身自环跳过。旧版把树视图（行内体一刀切空白化）同时用作 use 扫描，
  生产行内 use 边被静默丢弃（假阴）。
- **macro_rules 定义体跳过（review 批修正，探针 P5）**：定义体是模板非真实边——
  _blank_macro_defs 空白化 `macro_rules! name { ... }` 体（旧版体内裸 use 被误计
  假阳、体内模板 `mod X;` 被当文件声明解析出幻影树条目）；调用点宏展开
  （如 gen_mod!() 生成 mod/use）不展开（诚实局限）。
- use 目标解析 = mod 树最长前缀（`crate::api::handle` → api::handle 非模块 → api；
  根条目 `crate::CONST` → "crate"）。self/super 链按当前文件模块路径绝对化：
  绝对段表示 crate 根 = []（空），顶层模块 = ["name"]，`crate::` 前缀天然脱落；
  cur（当前文件模块段）同表示法，crate 根文件 cur=[]。
- 违例语义 = **传递闭包**（import-linter forbidden / go list 引擎同义）：BFS 最短链
  入 payload；锚定 = 链尾直接 use 边所在文件+行号；函数体内局部 use 同样计入
  （编译期真实依赖边，比 import-linter 只看模块顶层更严）。
- 测试代码排除（与 go 引擎排除 TestImports 对齐）：
  * `#[cfg(test)]` / `#[cfg(all(..., test, ...))]` 门控的 mod 声明（文件式或行内）
    整体空白化：其子树不进 mod 树（cfg-test 文件成 orphan 诚实计数），行内体的
    use 随声明整体排除（use 视图与树视图一致排除）
  * `cfg(not(test))`/feature 条件门控不识别（按生产代码计）；生产文件内
    `#[cfg(test)] fn` 的 use 仍计入（保守方向）
- 诚实局限（文档化不静默）：
  * 无前缀 use（2015 edition 相对路径 / 2018+ extern crate）跳过——extern crate
    本不属层执法，2015 为遗留 edition
  * `#[path]` 属性非标准路径 mod 不支持（失配文件成 orphan 诚实计数）
  * orphan 文件（mod 树不可达，含 src/bin/*）不参与容器推导与执法（未编译进 crate）
  * glob 导入不展开：`use crate::api::*` 边指 glob 根 api，glob 可达的 pub 子模块/
    重导出不产传递边（假阴方向，与 import-linter 同粒度）
  * macro_rules 调用点展开生成的 mod/use 不可见（只跳过定义体——假阴方向）
  * mod 树/use 扫描超时 → enforcer-failed（部分树/图不可信，不静默截断）
  * BOM：rustc 接受 U+FEFF（探针 P4），引擎无行首假设天然免疫
"""

from __future__ import annotations

import json
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

RS_SUFFIX = ".rs"
ROOT_MODULE = "crate"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ------------------------------------------------------------------ 词法底座


def _blank_noncode(text: str) -> str:
    """注释/字符串/字符/原始字符串 → 空白（等长，换行保留）——代码位置与行号不变。"""
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        two = text[i : i + 2]
        if two == "//":
            j = i
            while j < n and text[j] != "\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "/*":
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
        elif ch == '"' or (ch in ("r", "b") and _raw_string_ahead(text, i)):
            j = _raw_string_end(text, i) if ch != '"' else _string_end(text, i)
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif ch == "'":
            # 字符字面量 'x' / '\n' / '\u{1F600}'；寿命标注 'a 不吃下一个 token：
            # ' 后是 \ 或闭合 ' 前内容≤~10 才算字符字面量（寿命标注形如 'a 后无 '）
            j = _char_end(text, i)
            if j > i:
                for k in range(i, j):
                    out[k] = " "
                i = j
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def _raw_string_ahead(text: str, i: int) -> bool:
    """r"..."/r#"..."#（可 b 前缀）：寿命标签误判防护——r 后必须紧跟 \" 或 #+\"。"""
    j = i
    if text[j] == "b":
        j += 1
    if j < len(text) and text[j] == "r":
        j += 1
        while j < len(text) and text[j] == "#":
            j += 1
        return j < len(text) and text[j] == '"'
    return False


def _string_end(text: str, i: int) -> int:
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


def _raw_string_end(text: str, i: int) -> int:
    j = i
    n = len(text)
    while j < n and text[j] in ("b", "r"):
        j += 1
    hashes = 0
    while j < n and text[j] == "#":
        hashes += 1
        j += 1
    j += 1  # opening quote
    closer = '"' + "#" * hashes
    while j < n:
        if text.startswith(closer, j):
            return j + len(closer)
        j += 1
    return n


def _char_end(text: str, i: int) -> int:
    """'x'/'\\n'/'\\u{...}' → 结束位置；形如寿命标注（'a 无闭合）→ -1。"""
    n = len(text)
    j = i + 1
    if j >= n:
        return -1
    if text[j] == "\\":
        j += 1
        if j < n and text[j] == "u" and j + 1 < n and text[j + 1] == "{":
            while j < n and text[j] != "}":
                j += 1
            return j + 2 if j < n and j + 1 < n and text[j + 1] == "'" else -1
        j += 1
        return j + 1 if j < n and text[j] == "'" else -1
    if j + 1 < n and text[j + 1] == "'":
        return j + 2
    return -1


def _find_word(text: str, word: str, start: int, end: int | None = None) -> int:
    n = len(text) if end is None else min(end, len(text))
    i = start
    while i < n:
        i = text.find(word, i, n)
        if i < 0:
            return -1
        before_ok = i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
        after = i + len(word)
        after_ok = after >= n or not (text[after].isalnum() or text[after] == "_")
        if before_ok and after_ok:
            return i
        i += len(word)
    return -1


def _read_ident(text: str, i: int):
    m = _IDENT.match(text, i)
    if not m:
        return None, i
    return m.group(0), m.end()


def _skip_ws(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _peek_word(text: str, i: int, word: str) -> bool:
    if not text.startswith(word, i):
        return False
    after = i + len(word)
    if after < len(text) and (text[after].isalnum() or text[after] == "_"):
        return False
    return True


# ------------------------------------------------------------------ use 解析


def _parse_use_target(text: str, i: int):
    """i=use 关键字后。→ (路径段列表 or None, 结束位置)。仅 crate/self/super 根。"""
    j = _skip_ws(text, i)
    root, j = _read_ident(text, j)
    if root not in ("crate", "self", "super"):
        return None, j
    j2 = _skip_ws(text, j)
    if not text.startswith("::", j2):
        return None, j2  # 裸 use crate;——无效/无意义
    j = _skip_ws(text, j2 + 2)
    n = len(text)
    if j < n and text[j] == "*":
        return [[root]], j + 1
    if j < n and text[j] == "{":
        rel, j = _parse_group(text, j)
        if rel is None:
            return None, j
        return [[root] + p for p in rel], j
    item, j = _parse_group_item(text, j)
    if item is None:
        return None, j
    return [[root] + p for p in item], j


def _parse_group(text: str, i: int):
    """i='{'. → (展开路径列表 or None, next_i)；路径相对组前缀。"""
    j = i + 1
    paths: list[list[str]] = []
    n = len(text)
    while True:
        j = _skip_ws(text, j)
        if j >= n:
            return None, j
        if text[j] == "}":
            return paths, j + 1
        if text[j] == "*":
            paths.append([])
            j += 1
        else:
            item, j = _parse_group_item(text, j)
            if item is None:
                return None, j
            paths.extend(item)
        j = _skip_ws(text, j)
        if j < n and text[j] == ",":
            j += 1
        elif j < n and text[j] == "}":
            return paths, j + 1
        else:
            return None, j


def _parse_group_item(text: str, i: int):
    """组内一项（IDENT 链/嵌套组/as 重命名/路径后 glob）→ (路径列表 or None, next_i)。"""
    j = i
    segs: list[str] = []
    n = len(text)
    while True:
        name, j = _read_ident(text, j)
        if name is None:
            return None, j
        segs.append(name)
        j2 = _skip_ws(text, j)
        if _peek_word(text, j2, "as"):
            j = _skip_ws(text, j2 + 2)
            _, j = _read_ident(text, j)
            return [segs], j
        if text.startswith("::", j2):
            j = _skip_ws(text, j2 + 2)
            if j < n and text[j] == "{":
                sub, j = _parse_group(text, j)
                if sub is None:
                    return None, j
                return [segs + p for p in sub], j
            if j < n and text[j] == "*":
                return [segs], j + 1  # use crate::api::*——路径后 glob
            continue
        return [segs], j


def _iter_uses(text: str):
    """空白化文本 → [(use 关键字位置, [路径段列表])]（扁平视图，cur=[] 兼容包装）。"""
    return [(pos, paths) for pos, paths, _cur in _iter_uses_ctx(text, [])]


def _iter_mod_decls(text: str):
    """空白化文本 → [(name, is_inline)]（`mod NAME;` vs `mod NAME {`）。"""
    out = []
    i = 0
    while True:
        i = _find_word(text, "mod", i)
        if i < 0:
            break
        j = _skip_ws(text, i + 3)
        name, j = _read_ident(text, j)
        if name:
            j2 = _skip_ws(text, j)
            if j2 < len(text) and text[j2] in (";", "{"):
                out.append((name, text[j2] == "{"))
                i = j2 + 1
                continue
        i += 3
    return out


def _match_brace(text: str, open_pos: int, end: int) -> int:
    """i='{' → 配平 '}' 位置（含）；失配/越界 → end（保守吞余文，调用方循环终止）。"""
    depth = 0
    j = open_pos
    n = min(end, len(text))
    while j < n:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return end


def _blank_macro_defs(text: str) -> str:
    """`macro_rules! name { ... }` 定义体 → 空白（换行保留）：模板非真实边。

    rustc 1.97.1 探针 P5 实证：定义体可含模板 mod/use，非宏调用不生效；旧版体内
    裸 use 被误计（假阳）、体内模板 `mod X;` 被当文件声明解析出幻影树条目。
    调用点宏展开（如 gen_mod!() 生成 mod/use）不识别（诚实局限，见模块 docstring）。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        j = _find_word(text, "macro_rules", i)
        if j < 0:
            break
        k = _skip_ws(text, j + len("macro_rules"))
        if k < n and text[k] == "!":
            k = _skip_ws(text, k + 1)
            _name, k = _read_ident(text, k)
            k = _skip_ws(text, k)
            if k < n and text[k] == "{":
                body_end = _match_brace(text, k, n)
                for p in range(k + 1, min(body_end, n)):
                    if out[p] != "\n":
                        out[p] = " "
                i = body_end + 1
                continue
        i = j + len("macro_rules")
    return "".join(out)


def _iter_uses_ctx(text: str, base_cur: list[str]):
    """上下文感知 use 迭代 → [(use 位置, [路径段列表], 行内 mod 上下文 cur 段)]。

    生产行内 mod 体递归下钻（探针 P2/P3/P7）：self/super 相对行内路径解析
    （super=enclosing 模块），边归属性由调用方定为文件模块；文件式 `mod X;`
    声明跳过（其文件另行扫描）；cfg(test) mod 已被 _blank_test_mods 整体空白化、
    macro_rules 定义体已被 _blank_macro_defs 空白化，此处均不可见。
    """
    out: list[tuple[int, list[list[str]], list[str]]] = []
    _walk_inline_mods(text, 0, len(text), list(base_cur), out)
    return out


def _walk_inline_mods(
    text: str,
    start: int,
    end: int,
    cur: list[str],
    out: list[tuple[int, list[list[str]], list[str]]],
) -> None:
    i = start
    n = min(end, len(text))
    while i < n:
        u = _find_word(text, "use", i, n)
        m = _find_word(text, "mod", i, n)
        if u < 0 and m < 0:
            return
        if u >= 0 and (m < 0 or u < m):
            paths, e = _parse_use_target(text, u + 3)
            if paths:
                out.append((u, paths, list(cur)))
                i = e
            else:
                i = u + 3
            continue
        j = _skip_ws(text, m + 3)
        name, j = _read_ident(text, j)
        if name is None:
            i = m + 3
            continue
        j2 = _skip_ws(text, j)
        if j2 < n and text[j2] == ";":
            i = j2 + 1
            continue
        if j2 < n and text[j2] == "{":
            body_end = _match_brace(text, j2, n)
            _walk_inline_mods(text, j2 + 1, body_end, cur + [name], out)
            i = body_end + 1
            continue
        i = m + 3


def _blank_inline_mods(text: str) -> str:
    """行内 mod 块体 → 空白（换行保留）：块内 use 与嵌套 mod 声明不可见。"""
    out = list(text)
    i = 0
    n = len(text)
    while True:
        i = _find_word(text, "mod", i)
        if i < 0:
            break
        j = _skip_ws(text, i + 3)
        name, j = _read_ident(text, j)
        if not name:
            i += 3
            continue
        j2 = _skip_ws(text, j)
        if j2 >= n or text[j2] != "{":
            i += 3
            continue
        depth = 0
        k = j2
        while k < n:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        for m in range(j2 + 1, min(k, n)):
            if out[m] != "\n":
                out[m] = " "
        i = k + 1
    return "".join(out)


# ------------------------------------------------- cfg(test) 门控 mod 排除


def _attr_end(text: str, i: int) -> int | None:
    """i='#[' 的 '[' → 配平 ']' 位置（含）；失配 → None。"""
    depth = 0
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


_CFG_HEAD_RE = re.compile(r"^\s*cfg\s*\(")
_ALL_HEAD_RE = re.compile(r"^all\s*\(")


def _is_test_cfg(inner: str) -> bool:
    """cfg 属性内容 → 是否测试门控：cfg(test) / cfg(all(..., test, ...))。

    顶层谓词精确匹配（not(test)/feature 条件不算——按生产代码计，保守方向）。

    语义与旧单正则（^\\s*cfg\\s*\\(\\s*(.*?)\\s*\\)\\s*$ re.S + ^all\\s*\\((.*)\\)$
    re.S）逐字保留：中段=首 ( 后至末 )（剥首尾空白）、闭括号必须是末个非空白
    字符、all 参数深度感知逗号切分。

    R-Q：旧惰性组+\\s* 形状在多 ')' 后缀垃圾的近失配上 O(M²)（惰性组逐位
    前进 → \\s* 贪吃 M 空格 → \\s*+\\)$ 失配 → \\s* 逐位退回 → 重扫；possessive
    只消除退回重试不消除逐位重扫）——前缀正则（线性）+ 字符串结构判定等价替换。
    """
    m = _CFG_HEAD_RE.match(inner)
    if not m:
        return False
    stripped = inner[m.end():].rstrip()
    if not stripped.endswith(")"):
        return False
    cond = stripped[:-1].strip()
    if cond == "test":
        return True
    m2 = _ALL_HEAD_RE.match(cond)
    if not m2 or not cond.endswith(")"):
        return False
    args_src = cond[m2.end():-1]
    depth = 0
    args: list[str] = []
    cur: list[str] = []
    for ch in args_src:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return any(a.strip() == "test" for a in args)


def _skip_mod_prefix(text: str, i: int) -> int:
    """cfg-test 属性后 → 跳过后续属性串与可选 pub(+作用域)，指向 mod 声明位。"""
    n = len(text)
    j = _skip_ws(text, i)
    while j < n and text[j] == "#" and j + 1 < n and text[j + 1] == "[":
        e = _attr_end(text, j)
        if e is None:
            return j
        j = _skip_ws(text, e + 1)
    if _peek_word(text, j, "pub"):
        j = _skip_ws(text, j + 3)
        if j < n and text[j] == "(":
            depth = 0
            while j < n:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            j = _skip_ws(text, j + 1)
    return j


def _mod_decl_end(text: str, i: int) -> int | None:
    """i=mod 关键字位置 → 声明结尾（';' 后或行内 '}' 配平后）；失配 → None。"""
    n = len(text)
    j = _skip_ws(text, i + 3)
    name, j = _read_ident(text, j)
    if name is None:
        return None
    j = _skip_ws(text, j)
    if j < n and text[j] == ";":
        return j + 1
    if j < n and text[j] == "{":
        depth = 0
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    return None


def _blank_test_mods(text: str) -> str:
    """`#[cfg(test)] mod NAME;`/`{...}` 声明整体 → 空白：测试子树不进 mod 树
    （与 go 引擎排除 TestImports 对齐；行内块体已被 _blank_inline_mods 空白化，
    此处连属性+声明头一并抹除）。
    """
    out = list(text)
    i = 0
    n = len(text)
    while True:
        j = text.find("#[", i)
        if j < 0:
            break
        end_attr = _attr_end(text, j)
        if end_attr is None:
            break
        if _is_test_cfg(text[j + 2 : end_attr]):
            k = _skip_mod_prefix(text, end_attr + 1)
            if _peek_word(text, k, "mod"):
                decl_end = _mod_decl_end(text, k)
                if decl_end is not None:
                    for p in range(j, min(decl_end, n)):
                        if out[p] != "\n":
                            out[p] = " "
                    i = decl_end
                    continue
        i = end_attr + 1
    return "".join(out)


# ------------------------------------------------------------------ mod 树


def _read(repo: Path, rel: str) -> str:
    """读文件 → mod 树视图空白化（非代码 → macro 定义体 → 行内 mod 体 → cfg(test) mod 声明）。"""
    try:
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _blank_test_mods(_blank_inline_mods(_blank_macro_defs(_blank_noncode(text))))


def _read_use_view(repo: Path, rel: str) -> str:
    """读文件 → use 扫描视图：行内 mod 体保留（_iter_uses_ctx 下钻计入生产行内
    use 边，探针 P2），macro_rules 定义体与 cfg(test) mod 声明整体空白化。"""
    try:
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _blank_test_mods(_blank_macro_defs(_blank_noncode(text)))


def build_mod_tree(repo: Path, timeout: int = DEFAULT_TIMEOUT) -> tuple[dict[str, str], bool]:
    """repo 相对 posix 文件路径 → 模块路径（crate 根="crate"；orphan 不入表）。

    返回 (file_mod, timed_out)：超时截断的部分树不可信，调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    root: str | None = None
    for cand in ("src/lib.rs", "src/main.rs"):
        if (repo / cand).is_file():
            root = cand
            break  # lib.rs 优先：lib/main 是不同 crate 的根，不同时入树
    if root is None:
        return {}, False
    file_mod: dict[str, str] = {}
    timed_out = False
    stack: list[tuple[str, str]] = [(root, ROOT_MODULE)]
    while stack:
        rel, mod = stack.pop()
        if rel in file_mod:
            continue
        file_mod[rel] = mod
        if time.monotonic() > deadline:
            timed_out = True
            break
        text = _read(repo, rel)
        p = Path(rel)
        if p.name == "mod.rs" or (p.parent.name == "src" and p.name in ("lib.rs", "main.rs")):
            base_dir = p.parent
        else:
            base_dir = p.parent / p.stem
        for name, is_inline in _iter_mod_decls(text):
            if is_inline:
                continue
            child_mod = name if mod == ROOT_MODULE else f"{mod}::{name}"
            cands = [
                (base_dir / f"{name}.rs").as_posix(),
                (base_dir / name / "mod.rs").as_posix(),
            ]
            for c in cands:
                if c not in file_mod and (repo / c).is_file():
                    stack.append((c, child_mod))
                    break
    return file_mod, timed_out


def derive_rs_containers(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """module_id → 排序模块路径集（fid→file_path→mod 树）。

    仅当 payload 符号含 .rs 文件才建树；orphan（.rs 文件不在 mod 树，去重路径）
    与超时诚实上报——rs_files 供 run() 路由（有 .rs 但无 crate root 也要进 rs 引擎
    给出可行动的 not-enforceable，而非泛化 unsupported）。
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
    rs_paths = {
        (file_path or "").replace("\\", "/")
        for _fid, file_path in rows
        if (file_path or "").replace("\\", "/").lower().endswith(RS_SUFFIX)
    }
    file_mod: dict[str, str] = {}
    timed_out = False
    if rs_paths:
        file_mod, timed_out = build_mod_tree(repo, timeout)
    orphans: set[str] = set()
    for fid, file_path in rows:
        p = (file_path or "").replace("\\", "/")
        if not p.lower().endswith(RS_SUFFIX):
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
        "rs_files": len(rs_paths),
        "orphan_files": len(orphans),
        "timed_out": timed_out,
    }


# ------------------------------------------------------------------ 违例


def _absolutize(segs: list[str], cur: list[str]) -> list[str] | None:
    """use 路径段（首段 crate/self/super）→ 绝对模块段列表；非法 → None。

    绝对段表示：crate 根 = []，顶层模块 = ["name"]（`crate::` 前缀天然脱落）；
    cur 同表示法（crate 根文件 cur=[]）。super 链弹栈越过 crate 根 → None（非法）。
    """
    if not segs:
        return None
    root = segs[0]
    rest = segs[1:]
    if root == "crate":
        base: list[str] = []
    elif root == "self":
        base = list(cur)
    elif root == "super":
        supers = 1
        while rest and rest[0] == "super":
            supers += 1
            rest = rest[1:]
        if supers > len(cur):
            return None
        base = cur[: len(cur) - supers]
    else:
        return None
    if not rest:
        return base or [ROOT_MODULE]
    return base + rest


def _resolve_target(abs_segs: list[str], module_set: set[str]) -> str:
    """绝对段 → mod 树最长前缀模块；无匹配（根条目）→ "crate"。"""
    while abs_segs:
        name = "::".join(abs_segs)
        if name in module_set:
            return name
        abs_segs = abs_segs[:-1]
    return ROOT_MODULE


def _belongs(mod: str, prefixes: list[str]) -> bool:
    return any(mod == p or mod.startswith(p + "::") for p in prefixes)


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


def scan_use_edges(
    repo: Path,
    file_mod: dict[str, str],
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, list[str]], list[dict], int, bool]:
    """mod 树 → (模块依赖图, use 边明细[{file,line,src,dst}], 文件数, timed_out)。

    图以全部树模块为键（叶模块无出边也入图——_find_violations 的 forbidden 集
    须见全部模块，漏叶会假阴）；扫描用 use 视图（_read_use_view——行内 mod 体
    保留，_iter_uses_ctx 下钻计入生产行内 use 边）；超时截断的部分图不可信，
    调用方须拒绝执法。
    """
    repo = Path(repo)
    deadline = time.monotonic() + timeout
    module_set = set(file_mod.values())
    graph: dict[str, set[str]] = {m: set() for m in module_set}
    edge_rows: list[dict] = []
    files_scanned = 0
    timed_out = False
    for rel, mod in sorted(file_mod.items()):
        if time.monotonic() > deadline:
            timed_out = True
            break
        files_scanned += 1
        text = _read_use_view(repo, rel)
        if not text:
            continue
        cur = [] if mod == ROOT_MODULE else mod.split("::")
        for pos, paths, use_cur in _iter_uses_ctx(text, cur):
            line = text.count("\n", 0, pos) + 1
            for segs in paths:
                abs_segs = _absolutize(segs, use_cur)
                if abs_segs is None:
                    continue
                target = _resolve_target(abs_segs, module_set)
                if target == mod:
                    continue
                graph[mod].add(target)
                edge_rows.append(
                    {"file": rel, "line": line, "src": mod, "dst": target}
                )
    return (
        {m: sorted(t) for m, t in graph.items()},
        edge_rows,
        files_scanned,
        timed_out,
    )


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


def run_rs(
    repo: Path,
    con: sqlite3.Connection,
    payload: dict,
    structure_status: str,
    containers: dict[str, list[str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Rust 执法入口：契约生成（复用层 DAG）→ 源级 use 图 → BFS 违例 → P9（幂等）。"""
    repo = Path(repo)
    base = {
        "step": "s7-enforce",
        "engine": "rs-use-parser",
        "structure_status": structure_status,
    }
    file_mod, timed_out = build_mod_tree(repo, timeout)
    if timed_out:
        return {
            **base,
            "status": "enforcer-failed",
            "reason": f"mod tree build exceeded {timeout}s (partial tree discarded)",
        }
    if not file_mod:
        return {
            **base,
            "status": "not-enforceable",
            "reason": "no crate root (src/lib.rs or src/main.rs) found",
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
    graph, edge_rows, files_scanned, timed_out = scan_use_edges(repo, file_mod, timeout)
    if timed_out:
        return {
            **summary,
            "status": "enforcer-failed",
            "reason": f"use scan exceeded {timeout}s (partial graph discarded)",
        }
    summary["files"] = files_scanned
    summary["modules"] = len(graph)
    violations = _find_violations(graph, contracts)
    # 锚定：违例链最后一条直接 use 边（chain[-2] → chain[-1]）的文件与行号
    edges_by_pair: dict[tuple[str, str], list[dict]] = {}
    for e in edge_rows:
        edges_by_pair.setdefault((e["src"], e["dst"]), []).append(e)
    for v in violations:
        last = edges_by_pair.get((v["chain"][-2], v["chain"][-1])) if len(v["chain"]) >= 2 else None
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
