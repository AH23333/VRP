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
    node = shutil.which("node")
    if not node:
        return {"status": "skipped", "reason": "node not found"}
    db = repo / ".code-atlas" / "index.db"
    if db.exists() and not reindex:
        return {"status": "reused", "db": str(db)}
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


def _quad(sname: str, sfile: str, tname: str, tfile: str) -> tuple[str, str, str, str]:
    return (sname, _norm(sfile), tname, _norm(tfile))


def run_xcheck(repo: Path, facts_db: Path, reindex: bool = False) -> dict:
    index_result = index_with_code_atlas(repo, reindex=reindex)
    ca_db = repo / ".code-atlas" / "index.db"
    if not ca_db.exists():
        return {"index": index_result, "ledger_rows": 0}

    ca = sqlite3.connect(f"file:{ca_db}?mode=ro", uri=True)
    ca.row_factory = sqlite3.Row
    facts = sqlite3.connect(facts_db)
    try:
        ca_sym = {}
        for r in ca.execute("SELECT s.id, s.name, f.path FROM symbols s JOIN files f ON s.file_id = f.id"):
            ca_sym[r[0]] = (r[1], _norm(r[2]))
        ca_edges: dict[tuple, tuple] = {}
        for r in ca.execute("SELECT src_symbol_id, dst_symbol_id, confidence, provenance FROM edges WHERE kind='calls'"):
            s = ca_sym.get(r[0])
            t = ca_sym.get(r[1])
            if not s or not t:
                continue
            quad = _quad(s[0], s[1], t[0], t[1])
            ca_edges[quad] = (r[2], r[3])

        cbm_calls: dict[tuple, str] = {}
        cbm_usage: set[tuple] = set()
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
            quad = _quad(name_of[r[0]], file_of[r[0]], name_of[r[1]], file_of[r[1]])
            cbm_calls[quad] = r[3] or ""
        for r in facts.execute("SELECT src_fid, dst_fid FROM usage_hints"):
            if r[0] not in name_of or r[1] not in name_of:
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
            "ca_calls": len(ca_edges),
            "cbm_calls": len(cbm_calls),
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
