"""S2 P2 recall backstop: jscpd token-clone adapter.

Role per 构想v6 §12.5: CBM SIMILAR_TO (ETL -> findings P2, source='etl') is the
main-precision signal - function-level MinHash with a ~0.95 emission floor, so
near-identical pairs only. jscpd is the recall backstop: token-level clones
(minTokens 50 default) catch verbatim/partial-function blocks below or outside
that floor. Disagreements land in the discrepancy ledger (category
'p2-cross-check-jscpd'), registered never adjudicated (§3).

jscpd honours .gitignore by default (probed on code-atlas-lusiem: zero clones
from .vrp/), matching the CBM working-tree universe closely enough; residual
differences are ledger content by design.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from .s2_astgrep import _anchor_symbol

LEDGER_CATEGORY = "p2-cross-check-jscpd"
SOURCE = "s2-jscpd"

_EXCLUDED_PREFIXES = (".vrp/", ".code-atlas/", ".codebase-memory/", "node_modules/")


def find_jscpd() -> str | None:
    from os import environ
    from shutil import which

    env = environ.get("VRP_JSCPD_BIN")
    if env and Path(env).is_file():
        return env
    shim = Path(__file__).parent.parent / "work" / "jscpd" / "node_modules" / ".bin" / "jscpd.cmd"
    if shim.is_file():
        return str(shim)
    return which("jscpd")


def _norm_rel(p: str, repo: Path) -> str:
    p = p.replace("\\", "/").strip()
    if not p:
        return ""
    prefix = str(repo).replace("\\", "/").rstrip("/") + "/"
    if p.lower().startswith(prefix.lower()):
        p = p[len(prefix):]
    return p.lstrip("/")


def parse_clones(data: dict, repo: Path) -> list[dict]:
    rows: list[dict] = []
    for dup in data.get("duplicates", []):
        first = dup.get("firstFile") or {}
        second = dup.get("secondFile") or {}
        f1 = _norm_rel(str(first.get("name", "")), repo)
        f2 = _norm_rel(str(second.get("name", "")), repo)
        if not f1 or not f2:
            continue
        rows.append(
            {
                "first_file": f1,
                "first_line": (first.get("startLoc") or {}).get("line"),
                "first_end": (first.get("endLoc") or {}).get("line"),
                "second_file": f2,
                "second_line": (second.get("startLoc") or {}).get("line"),
                "second_end": (second.get("endLoc") or {}).get("line"),
                "lines": dup.get("lines"),
                "tokens": dup.get("tokens"),
                "format": dup.get("format"),
            }
        )
    return rows


def run_jscpd(bin_path: str, repo: Path) -> list[dict] | None:
    with tempfile.TemporaryDirectory(prefix="vrp-jscpd-") as out:
        proc = subprocess.run(
            [bin_path, str(repo), "--reporters", "json", "--output", out, "--silent"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if proc.returncode != 0:
            return None
        report = Path(out) / "jscpd-report.json"
        if not report.is_file():
            return None
        data = json.loads(report.read_text(encoding="utf-8"))
    return parse_clones(data, repo)


def _ingest_filter(row: dict) -> bool:
    return not (row["first_file"].startswith(_EXCLUDED_PREFIXES) or row["second_file"].startswith(_EXCLUDED_PREFIXES))


def cbm_p2_file_pairs(con: sqlite3.Connection) -> set:
    rows = con.execute(
        "SELECT sa.file_path, sb.file_path FROM findings f"
        " JOIN symbols sa ON sa.fid = f.anchor_fid"
        " JOIN symbols sb ON sb.fid = f.related_fid"
        " WHERE f.pathology = 'P2' AND f.source = 'etl'"
    ).fetchall()
    pairs = set()
    for a, b in rows:
        if a and b:
            pairs.add(frozenset((a, b)))
    return pairs


def _pair_key(pair: frozenset) -> str:
    return "|".join(sorted(pair))


def cross_check(clones: list[dict], cbm_pairs: set) -> list[tuple]:
    ledger: list[tuple] = []
    covered: set = set()
    for c in clones:
        key = frozenset((c["first_file"], c["second_file"]))
        src_key = f"jscpd:{c['first_file']}:{c['first_line']}<->{c['second_file']}:{c['second_line']}"
        if key in cbm_pairs:
            covered.add(key)
            ledger.append(
                (LEDGER_CATEGORY, src_key, f"cbm-p2:{_pair_key(key)}",
                 json.dumps({"tool": "jscpd", "agreed": True, "lines": c["lines"]}))
            )
        else:
            ledger.append(
                (LEDGER_CATEGORY, src_key, None,
                 json.dumps({"tool": "jscpd", "agreed": False, "side": "jscpd-only", "lines": c["lines"]}))
            )
    for key in cbm_pairs - covered:
        ledger.append(
            (LEDGER_CATEGORY, f"cbm-p2:{_pair_key(key)}", "jscpd:none",
             json.dumps({"tool": "jscpd", "agreed": False, "side": "cbm-only"}))
        )
    return ledger


def _write_findings(con: sqlite3.Connection, clones: list[dict]) -> None:
    out = []
    for c in clones:
        anchor = _anchor_symbol(con, c["first_file"], c["first_line"]) if c["first_line"] is not None else None
        related = _anchor_symbol(con, c["second_file"], c["second_line"]) if c["second_line"] is not None else None
        payload = json.dumps(
            {
                "second_file": c["second_file"],
                "second_line": c["second_line"],
                "second_end": c["second_end"],
                "lines": c["lines"],
                "tokens": c["tokens"],
                "format": c["format"],
            },
            ensure_ascii=False,
        )
        out.append(("P2", "token-clone", SOURCE, anchor, related, c["first_file"], c["first_line"], c["first_end"], payload))
    con.execute("DELETE FROM findings WHERE source = ?", (SOURCE,))
    con.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        out,
    )
    con.commit()


def _write_ledger(con: sqlite3.Connection, rows: list[tuple]) -> None:
    con.execute("DELETE FROM discrepancy_ledger WHERE category = ?", (LEDGER_CATEGORY,))
    con.executemany(
        "INSERT INTO discrepancy_ledger (category, src_key, dst_key, detail_json) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()


def scan(facts_db: sqlite3.Connection, repo: Path) -> dict:
    stats: dict = {"jscpd": "skipped", "ledger_rows": 0}
    bin_path = find_jscpd()
    if not bin_path:
        return stats
    clones = run_jscpd(bin_path, repo)
    if clones is None:
        return stats
    clones = [c for c in clones if _ingest_filter(c)]
    _write_findings(facts_db, clones)
    ledger = cross_check(clones, cbm_p2_file_pairs(facts_db))
    _write_ledger(facts_db, ledger)
    stats["jscpd"] = len(clones)
    stats["ledger_rows"] = len(ledger)
    return stats
