"""S5 repomap view: aider repomap algorithm ported onto facts.db (v6 s7).

Adapted from Aider-AI/aider repomap (personalized PageRank over a reference
graph + budget-capped ranked map rendering). Deviation from v6's "import
source" plan: the ranking/rendering skeleton is ported as-is, but tag
extraction is replaced by facts.db symbols/edges, dropping the
tree-sitter-language-pack dependency entirely (pure stdlib constraint).
"""

from __future__ import annotations

import sqlite3

DAMPING = 0.85
TOLERANCE = 1e-6
MAX_ITERATIONS = 40
DEFINITION_LABELS = (
    "Function",
    "Method",
    "Class",
    "Interface",
    "Struct",
    "Enum",
    "Type",
    "Trait",
    "Protocol",
    "Macro",
    "Union",
    "Record",
    "Constant",
)
VARIABLE_CODE_EXTENSIONS = frozenset(
    (
        "ts", "tsx", "js", "jsx", "mjs", "cjs",
        "py", "pyi",
        "go", "rs", "java", "kt", "kts", "scala", "swift", "dart",
        "rb", "php", "cs",
        "c", "h", "cpp", "cc", "cxx", "hpp", "hh", "m", "mm",
        "lua", "ex", "exs", "erl", "hrl",
        "hs", "ml", "mli", "fs", "fsi", "fsx",
        "clj", "cljs", "cljc", "groovy",
        "pl", "pm", "r", "jl", "zig", "nim",
    )
)
MAX_SYMBOLS_PER_FILE = 10


def _file_extension(file_path: str) -> str:
    name = file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def is_definition_label(label: str, file_path: str) -> bool:
    if label in DEFINITION_LABELS:
        return True
    if label == "Variable":
        return _file_extension(file_path) in VARIABLE_CODE_EXTENSIONS
    return False


def load_graph(con: sqlite3.Connection) -> tuple[dict[str, list[str]], dict[str, dict]]:
    adjacency: dict[str, list[str]] = {}
    for src, dst in con.execute("SELECT src_fid, dst_fid FROM edges"):
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, [])
    symbols = {}
    for fid, qn, name, label, file_path, start_line, end_line in con.execute(
        "SELECT fid, qualified_name, name, label, file_path, start_line, end_line FROM symbols"
        " WHERE start_line IS NOT NULL AND file_path != ''"
        " AND file_path NOT GLOB '<*' AND file_path NOT GLOB '>*'"
    ):
        symbols[fid] = {
            "qualified_name": qn,
            "name": name,
            "label": label,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
        }
    for fid in list(adjacency.keys()):
        if fid not in symbols:
            del adjacency[fid]
    adjacency = {src: [d for d in neighbors if d in symbols] for src, neighbors in adjacency.items()}
    return adjacency, symbols


def pagerank(
    adjacency: dict[str, list[str]],
    damping: float = DAMPING,
    tolerance: float = TOLERANCE,
    max_iterations: int = MAX_ITERATIONS,
) -> dict[str, float]:
    nodes = list(adjacency.keys())
    n = len(nodes)
    if n == 0:
        return {}
    rank = {node: 1.0 / n for node in nodes}
    for _ in range(max_iterations):
        dangling = sum(rank[node] for node in nodes if not adjacency[node])
        base = (1.0 - damping) / n + damping * dangling / n
        next_rank = {node: base for node in nodes}
        for src, neighbors in adjacency.items():
            if neighbors:
                share = damping * rank[src] / len(neighbors)
                for dst in neighbors:
                    next_rank[dst] += share
        delta = sum(abs(next_rank[node] - rank[node]) for node in nodes)
        rank = next_rank
        if delta < tolerance:
            break
    return rank


def _ranked_definitions(con: sqlite3.Connection) -> list[tuple[float, dict]]:
    adjacency, symbols = load_graph(con)
    rank = pagerank(adjacency)
    ranked = []
    for fid, meta in symbols.items():
        if is_definition_label(meta["label"], meta["file_path"]):
            ranked.append((rank.get(fid, 0.0), meta))
    ranked.sort(key=lambda item: (-item[0], item[1]["qualified_name"]))
    return ranked


def render_repomap(con: sqlite3.Connection, char_budget: int = 4000) -> dict:
    ranked = _ranked_definitions(con)
    by_file: dict[str, list[tuple[float, dict]]] = {}
    for score, meta in ranked:
        by_file.setdefault(meta["file_path"], []).append((score, meta))
    file_order = sorted(by_file, key=lambda fp: (-by_file[fp][0][0], fp))
    lines: list[str] = []
    used = 0
    files_included = 0
    symbols_included = 0
    truncated = False
    for file_path in file_order:
        header = f"{file_path}:"
        if used + len(header) + 1 > char_budget:
            truncated = True
            break
        lines.append(header)
        used += len(header) + 1
        files_included += 1
        symbols_before = symbols_included
        for score, meta in by_file[file_path][:MAX_SYMBOLS_PER_FILE]:
            entry = f"  {meta['start_line']}-{meta['end_line']} {meta['name']}"
            if used + len(entry) + 1 > char_budget:
                truncated = True
                break
            lines.append(entry)
            used += len(entry) + 1
            symbols_included += 1
        if truncated:
            if symbols_included == symbols_before:
                # R-AJ: 预算在文件头后、首条目前耗尽——回滚裸 "path:" 行：
                # 零符号文件头在 aider 树形图语义里读作"该文件无定义"（实为
                # 预算耗尽），且 files_included 收据不得计入零符号文件
                # （收据即内容红线，R-AD rejected=0 同族）。
                lines.pop()
                files_included -= 1
            break
    return {
        "map": "\n".join(lines),
        "files_included": files_included,
        "symbols_included": symbols_included,
        "files_total": len(file_order),
        "symbols_total": len(ranked),
        "truncated": truncated,
    }
