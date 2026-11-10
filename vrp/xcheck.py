"""S1 cross-validation: code-atlas calls vs facts.db edges -> discrepancy_ledger (v6 12.6)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import etl as etl_mod
from . import testsig

BUILTINISH = frozenset(
    {
        "number",
        "string",
        "boolean",
        "any",
        "unknown",
        "never",
        "void",
        "null",
        "undefined",
        "name",
        "error",
        "value",
        "text",
        "length",
        "Object",
        "Array",
        "Promise",
        "Error",
        "String",
        "Number",
        "Boolean",
        "Symbol",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "JSON",
        "Math",
        "Date",
        "RegExp",
        "console",
    }
)


def ca_js_path() -> Path | None:
    env = os.environ.get("VRP_CODE_ATLAS_JS")
    if env and Path(env).exists():
        return Path(env)
    local = (
        Path(__file__).resolve().parent.parent
        / "work"
        / "code-atlas-npx"
        / "node_modules"
        / "@lusiem"
        / "code-atlas"
        / "dist"
        / "index.js"
    )
    if local.exists():
        return local
    return None


_ca_js_path = ca_js_path


def index_with_code_atlas(repo: Path, reindex: bool = False, timeout: int = 900) -> dict:
    """恒跑 code-atlas index（R6' 病根五同修：index.db 存在即 reused 的短路取消——
    陈旧交叉索引会让 agreement% 失真。reindex 保留 API 兼容，两者等价）。"""
    node = shutil.which("node")
    if not node:
        return {"status": "skipped", "reason": "node not found"}
    db = repo / ".code-atlas" / "index.db"
    js = _ca_js_path()
    if js is not None:
        proc = subprocess.run(
            [node, str(js), "index", "--root", str(repo)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if proc.returncode != 0 or not db.exists():
            return {"status": "failed", "reason": (proc.stderr or proc.stdout)[-500:]}
        return {"status": "indexed", "db": str(db), "output": proc.stdout.strip()}
    return {"status": "skipped", "reason": "code-atlas dist not found (set VRP_CODE_ATLAS_JS)"}


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").removeprefix("./")


# 第三方收编代码路径段（R7' 诚实发现：CBM 默认不索引 vendor/，CA 全量索引——
# vrp 自仓 xcheck agreement 27.7% 即 vrp/vendor/archify 的 4744 条 CA calls
# 单侧污染所致）。双仓对称过滤后再比对，排除计数入 summary 公开，不静默。
EXCLUDED_SEGMENTS = frozenset({"vendor", "node_modules", "site-packages"})


def _excluded_path(rel: str) -> bool:
    return any(seg in EXCLUDED_SEGMENTS for seg in _norm(rel).split("/"))


# P-1b（跨项目实测）：压缩/生成 JS 的调用提取两侧天然分歧（minified 代码
# 的解析丢边制造 ca_only 噪音）。扩展名对称排除 .min.js/.min.css/.min.mjs，
# 计数公开入 summary，不静默。单行超长检测需逐文件读内容——成本不成比
# 例，不做（诚实边界）。
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".min.mjs")


def _minified_path(rel: str) -> bool:
    return _norm(rel).lower().endswith(MINIFIED_SUFFIXES)


def _quad(sname: str, sfile: str, tname: str, tfile: str) -> tuple[str, str, str, str]:
    return (sname, _norm(sfile), tname, _norm(tfile))


def run_xcheck(repo: Path, facts_db: Path, reindex: bool = False) -> dict:
    """CA calls vs facts.db edges → discrepancy_ledger。

    index 非 indexed（node/CA 缺失或失败）时不比对陈旧 ca_db——
    R6' 病根五残留收口（陈旧交叉索引使 agreement% 失真）。
    """
    index_result = index_with_code_atlas(repo, reindex=reindex)
    if index_result.get("status") != "indexed":
        return {
            "index": index_result,
            "ledger_rows": 0,
            "reason": "ca index not fresh (skipped/failed) - stale db not compared",
        }
    ca_db = repo / ".code-atlas" / "index.db"
    if not ca_db.exists():
        return {"index": index_result, "ledger_rows": 0}

    ca = sqlite3.connect(f"file:{ca_db}?mode=ro", uri=True)
    ca.row_factory = sqlite3.Row
    facts = sqlite3.connect(facts_db)
    try:
        ca_sym = {}
        ca_excluded_ids: set = set()
        ca_excluded_symbols = 0
        ca_minified_ids: set = set()
        ca_minified_symbols = 0
        for r in ca.execute("SELECT s.id, s.name, f.path FROM symbols s JOIN files f ON s.file_id = f.id"):
            if _excluded_path(r[2]):
                ca_excluded_ids.add(r[0])
                ca_excluded_symbols += 1
                continue
            if _minified_path(r[2]):
                ca_minified_ids.add(r[0])
                ca_minified_symbols += 1
                continue
            ca_sym[r[0]] = (r[1], _norm(r[2]))
        ca_excluded_files = sum(1 for r in ca.execute("SELECT path FROM files") if _excluded_path(r[0]))
        ca_edges: dict[tuple, tuple] = {}
        ca_excluded_calls = 0
        ca_minified_calls = 0
        for r in ca.execute("SELECT src_symbol_id, dst_symbol_id, confidence, provenance FROM edges WHERE kind='calls'"):
            if r[0] in ca_excluded_ids or r[1] in ca_excluded_ids:
                ca_excluded_calls += 1
                continue
            if r[0] in ca_minified_ids or r[1] in ca_minified_ids:
                ca_minified_calls += 1
                continue
            s = ca_sym.get(r[0])
            t = ca_sym.get(r[1])
            if not s or not t:
                continue
            quad = _quad(s[0], s[1], t[0], t[1])
            ca_edges[quad] = (r[2], r[3])

        cbm_calls: dict[tuple, str] = {}
        cbm_usage: set[tuple] = set()
        cbm_excluded_calls = 0
        cbm_minified_calls = 0
        cbm_minified_usage = 0
        name_of: dict[str, str] = {}
        file_of: dict[str, str] = {}
        for fid, name, file_path in facts.execute("SELECT fid, name, file_path FROM symbols"):
            name_of[fid] = name
            file_of[fid] = file_path
        for r in facts.execute(
            "SELECT e.src_fid, e.dst_fid, e.type, e.strategy FROM edges e"
            " WHERE e.type IN ('CALLS','CALL_REFERENCE')"
        ):
            if r[0] not in name_of or r[1] not in name_of:
                continue
            if _excluded_path(file_of[r[0]]) or _excluded_path(file_of[r[1]]):
                cbm_excluded_calls += 1
                continue
            if _minified_path(file_of[r[0]]) or _minified_path(file_of[r[1]]):
                cbm_minified_calls += 1
                continue
            quad = _quad(name_of[r[0]], file_of[r[0]], name_of[r[1]], file_of[r[1]])
            cbm_calls[quad] = r[3] or ""
        for r in facts.execute("SELECT src_fid, dst_fid FROM usage_hints"):
            if r[0] not in name_of or r[1] not in name_of:
                continue
            if _excluded_path(file_of[r[0]]) or _excluded_path(file_of[r[1]]):
                continue
            if _minified_path(file_of[r[0]]) or _minified_path(file_of[r[1]]):
                cbm_minified_usage += 1
                continue
            cbm_usage.add(_quad(name_of[r[0]], file_of[r[0]], name_of[r[1]], file_of[r[1]]))

        ledger = []
        agreement = 0
        for quad, (conf, prov) in ca_edges.items():
            if quad in cbm_calls or quad in cbm_usage:
                agreement += 1
                continue
            src_name, src_file, dst_name, dst_file = quad
            detail = json.dumps({"src_name": src_name, "src_file": src_file, "dst_name": dst_name, "dst_file": dst_file})
            if dst_name in BUILTINISH:
                ledger.append(("ca_only_builtin_target", f"{src_name}@{src_file}", f"{dst_name}@{dst_file}", None, conf, prov, detail))
            else:
                ledger.append(("ca_only", f"{src_name}@{src_file}", f"{dst_name}@{dst_file}", None, conf, prov, detail))

        cbm_only_test = 0
        for quad, strategy in cbm_calls.items():
            if quad in ca_edges:
                continue
            src_name, src_file, dst_name, dst_file = quad
            detail = json.dumps({"src_name": src_name, "src_file": src_file, "dst_name": dst_name, "dst_file": dst_file, "strategy": strategy})
            if testsig.is_test_path(src_file):
                cbm_only_test += 1
                ledger.append(("cbm_only_test_source", f"{src_name}@{src_file}", f"{dst_name}@{dst_file}", "CALLS", None, None, detail))
            else:
                ledger.append(("cbm_only", f"{src_name}@{src_file}", f"{dst_name}@{dst_file}", "CALLS", None, None, detail))

        facts.execute(
            "DELETE FROM discrepancy_ledger WHERE category IN ('ca_only','ca_only_builtin_target','cbm_only','cbm_only_test_source')"
        )
        facts.executemany(
            "INSERT INTO discrepancy_ledger (category, src_key, dst_key, cbm_edge_type, ca_confidence, ca_provenance, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            ledger,
        )
        summary = {
            "index": index_result,
            "excluded_segments": sorted(EXCLUDED_SEGMENTS),
            "ca_excluded_files": ca_excluded_files,
            "ca_excluded_symbols": ca_excluded_symbols,
            "ca_excluded_calls": ca_excluded_calls,
            "ca_calls": len(ca_edges),
            "cbm_calls": len(cbm_calls),
            "cbm_excluded_calls": cbm_excluded_calls,
            "ca_minified_symbols": ca_minified_symbols,
            "ca_minified_calls": ca_minified_calls,
            "cbm_minified_calls": cbm_minified_calls,
            "cbm_minified_usage": cbm_minified_usage,
            "agreement": agreement,
            "agreement_pct": round(100.0 * agreement / len(ca_edges), 1) if ca_edges else None,
            "ca_only": sum(1 for r in ledger if r[0] == "ca_only"),
            "ca_only_builtin_target": sum(1 for r in ledger if r[0] == "ca_only_builtin_target"),
            "cbm_only": sum(1 for r in ledger if r[0] == "cbm_only"),
            "cbm_only_test_source": cbm_only_test,
            "ledger_rows": len(ledger),
        }
        meta_rows = [(f"xcheck_{k}", str(v)) for k, v in summary.items() if k != "index"]
        facts.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", meta_rows)
        facts.commit()

        out = repo / ".vrp" / "xcheck.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    finally:
        ca.close()
        facts.close()
