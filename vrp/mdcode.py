"""N20（构想v11 §N20）：Markdown 代码块提取——文档示例代码与 facts.db 对账。

doctest 哲学（文档示例必须可信）；litprog 思想（文档即代码）——**学**哲学
**造**实现：fenced block 语言标注 → 对应提取器（py=stdlib ast 真解析；
ts/js=正则轻量提取，树库不引边界记档）→ 符号（name+类别）与 symbols 表
（name+label）比对 → 漂移候选报告。

漂移两类：doc-only（文档声明符号在 facts.db 无同名——改名/删除/虚构 API 嫌疑）
与 kind-mismatch（py 清晰面：doc def vs db Class（或反向）；Method≈Function 同
族不误报；ts 边界=CBM ts label 词表未钉，name-only 匹配不做类别比对——诚实
边界）。

报告非门（langmatrix 先例）：rc 恒 0——漂移是发现不是命令失败。只读面：
facts.db 只读连接，md 文件只读扫描（排除目录复用 watch.EXCLUDE_DIRS 单源）。

诚实边界记档：
- name 级比对（参数级签名不在 facts.db——content_hash 是摘要非签名）；
- 同名碰撞（文档 def go 与库里任意 go 同名即判 matched）不消歧——报告注明；
- 未闭合 fence 照提取（视觉上仍是代码块）但 closed=False 计数暴露；
- 无语言标注/未认知语言块不路由（plain 计数，不猜语言）。
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

from .watch import EXCLUDE_DIRS

# 语言标注 → 提取器路由（js 归 ts——langmatrix 归并先例）
_LANG_ROUTE: dict[str, str] = {
    "python": "py",
    "py": "py",
    "python3": "py",
    "ts": "ts",
    "typescript": "ts",
    "js": "ts",
    "javascript": "ts",
}

# py 清晰面类别族：doc def 与 db Method/Function 同族（CBM 方法标注词表宽——不误报）
_PY_FUNC_LABELS = frozenset({"Function", "Method"})
_PY_CLASS_LABELS = frozenset({"Class"})

_TS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Function", re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)")),
    ("Class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")),
    ("Interface", re.compile(r"\binterface\s+([A-Za-z_$][\w$]*)")),
    ("Type", re.compile(r"\btype\s+([A-Za-z_$][\w$]*)\s*=")),
    (
        "Function",
        re.compile(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
            r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
        ),
    ),
)

DRIFT_CAP = 50  # 渲染面样本上限（数据面全量）


def iter_md_files(repo: Path) -> list[Path]:
    """md 文件枚举（确定性序；排除目录复用 watch.EXCLUDE_DIRS 单源）。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in _walk(repo):
        for fname in filenames:
            if fname.lower().endswith(".md"):
                out.append(Path(dirpath) / fname)
    return out


def _walk(repo: Path):
    """os.walk 包装（py3.11 无 Path.walk）：排除目录剪枝+双侧排序保确定性。"""
    import os

    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in sorted(dirnames) if d not in EXCLUDE_DIRS]
        yield dirpath, dirnames, sorted(filenames)


def extract_blocks(text: str) -> list[dict]:
    """fence 解析：```lang … ```（未闭合照提取，closed=False 暴露）。"""
    lines = text.splitlines()
    blocks: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].lstrip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            j = i + 1
            while j < n and not lines[j].lstrip().startswith("```"):
                j += 1
            closed = j < n
            blocks.append(
                {
                    "lang": lang,
                    "code": "\n".join(lines[i + 1 : j]),
                    "fence_line": i + 1,  # 1-based fence 行
                    "closed": closed,
                }
            )
            i = j + 1 if closed else n
        else:
            i += 1
    return blocks


def extract_py_symbols(code: str) -> tuple[list[tuple[str, str, int]], bool]:
    """stdlib ast 真解析 → (name, kind, lineno-in-block)；语法错误=( [], False)。

    ast.walk 是 BFS——按 (lineno, name) 排序恢复文档序（报告确定性）。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [], False
    out: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, "Function", node.lineno))
        elif isinstance(node, ast.ClassDef):
            out.append((node.name, "Class", node.lineno))
    out.sort(key=lambda t: (t[2], t[0]))
    return out, True


def extract_ts_symbols(code: str) -> list[tuple[str, str, int]]:
    """ts/js 正则轻量提取（同块同键去重保首见序——确定性）。"""
    out: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in _TS_PATTERNS:
        for m in pattern.finditer(code):
            key = (m.group(1), kind)
            if key in seen:
                continue
            seen.add(key)
            line = code.count("\n", 0, m.start()) + 1
            out.append((m.group(1), kind, line))
    return out


def _match_symbol(con: sqlite3.Connection, name: str, doc_kind: str) -> tuple[bool, str | None, str | None]:
    """返回 (matched, db_label, reason)；doc-only/kind-mismatch 语义见模块 docstring。"""
    row = con.execute(
        "SELECT label FROM symbols WHERE name = ? ORDER BY fid LIMIT 1", (name,)
    ).fetchone()
    if row is None:
        return False, None, "doc-only"
    label = row[0]
    if doc_kind == "Function" and label in _PY_FUNC_LABELS:
        return True, label, None
    if doc_kind == "Class" and label in _PY_CLASS_LABELS:
        return True, label, None
    if doc_kind in ("Function", "Class"):
        return False, label, "kind-mismatch"
    # ts 面（Interface/Type 等词表未钉）：name-only 匹配，不做类别比对
    return True, label, None


def run_mdcode(facts_db: sqlite3.Connection, repo: Path) -> dict:
    """扫描 md → 提取 → 对账。只读；确定性（文件序/块序/符号首见序全定）。"""
    repo = Path(repo)
    md_files = iter_md_files(repo)
    blocks_total = 0
    plain_blocks = 0
    unclosed = 0
    parse_errors = 0
    extracted = 0
    matched = 0
    per_lang: dict[str, int] = {"py": 0, "ts": 0}
    drift: list[dict] = []
    for md_file in md_files:
        rel = md_file.relative_to(repo).as_posix()
        text = md_file.read_text(encoding="utf-8", errors="replace")
        for block in extract_blocks(text):
            blocks_total += 1
            if not block["closed"]:
                unclosed += 1
            route = _LANG_ROUTE.get(block["lang"])
            if route is None:
                plain_blocks += 1
                continue
            per_lang[route] += 1
            if route == "py":
                syms, ok = extract_py_symbols(block["code"])
                if not ok:
                    parse_errors += 1
                    continue
            else:
                syms = extract_ts_symbols(block["code"])
            for name, doc_kind, lineno in syms:
                extracted += 1
                is_match, db_label, reason = _match_symbol(facts_db, name, doc_kind)
                if is_match:
                    matched += 1
                    continue
                drift.append(
                    {
                        "md_file": rel,
                        "line": block["fence_line"] + lineno,
                        "lang": route,
                        "name": name,
                        "doc_kind": doc_kind,
                        "db_label": db_label,
                        "reason": reason,
                    }
                )
    return {
        "md_files": len(md_files),
        "blocks": blocks_total,
        "plain_blocks": plain_blocks,
        "unclosed_fences": unclosed,
        "parse_errors": parse_errors,
        "extracted": extracted,
        "matched": matched,
        "drift": drift,
        "per_lang": per_lang,
    }


def render_text(result: dict) -> str:
    lines = [
        "vrp mdcode - markdown code-block drift report (N20)",
        f"  md files scanned : {result['md_files']}",
        f"  fenced blocks    : {result['blocks']}"
        f" (py {result['per_lang']['py']} / ts {result['per_lang']['ts']}"
        f" / plain {result['plain_blocks']}, unclosed {result['unclosed_fences']})",
        f"  symbols extracted: {result['extracted']}"
        f" (parse errors {result['parse_errors']})",
        f"  matched          : {result['matched']}",
        f"  drift candidates : {len(result['drift'])}",
    ]
    for d in result["drift"][:DRIFT_CAP]:
        label = d["db_label"] if d["db_label"] is not None else "-"
        lines.append(
            f"  [{d['reason']}] {d['md_file']}:{d['line']} {d['name']}"
            f" (doc {d['doc_kind']} vs db {label}, {d['lang']})"
        )
    if len(result["drift"]) > DRIFT_CAP:
        lines.append(f"  ... and {len(result['drift']) - DRIFT_CAP} more (use --json)")
    if result["drift"]:
        lines.append(
            "  note: name-level match - common names may collide;"
            " doc-only = renamed/removed/fictional API candidate"
        )
    else:
        lines.append("  no drift - all documented symbols resolve in facts.db")
    return "\n".join(lines)
