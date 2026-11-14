"""S2 P4: per-language dead-code adapters (knip JS/TS, vulture Python) + dead-candidate cross-check into ledger."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

from . import s2_deadcode, testsig
from .s2_astgrep import _anchor_symbol

LEDGER_CATEGORY = "p4-cross-check"


def ledger_category(tool: str) -> str:
    return f"{LEDGER_CATEGORY}-{tool}"

KNIP_KIND = {
    "exports": "unused-export",
    "namespaceMembers": "unused-export",
    "enumMembers": "unused-enum-member",
    "types": "unused-type",
    "files": "unused-file",
    "dependencies": "unused-dependency",
    "devDependencies": "unused-dependency",
    "optionalPeerDependencies": "unused-dependency",
    "catalog": "unused-dependency",
    "catalogReferences": "unused-dependency",
    "unlisted": "unlisted-dependency",
    "unresolved": "unresolved-import",
    "binaries": "unused-binary",
    "duplicates": "duplicate-export",
}

_VULTURE_RE = re.compile(r"^(.+?):(\d+): unused ([a-z]+) '([^']*)' \((\d+)% confidence\)$")


def find_knip() -> str | None:
    from os import environ
    from shutil import which

    env = environ.get("VRP_KNIP_BIN")
    if env and Path(env).is_file():
        return env
    shim = Path(__file__).parent.parent / "work" / "knip" / "node_modules" / ".bin" / "knip.cmd"
    if shim.is_file():
        return str(shim)
    return which("knip")


def find_vulture() -> str | None:
    from shutil import which

    return which("vulture")


def run_knip(bin_path: str, repo: Path) -> list[dict] | None:
    proc = subprocess.run(
        [bin_path, "--reporter", "json"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if proc.returncode not in (0, 2) or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        # 上游 knip 输出形状漂移（非 JSON 对象）：与不可解析同 best-effort 降级
        return None
    return knip_rows(data.get("issues", []))


def knip_rows(issues: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for issue in issues:
        file_path = issue.get("file", "")
        for category, items in issue.items():
            if category in ("file",) or category not in KNIP_KIND or not items:
                continue
            for item in items:
                if category == "files":
                    raw = item.get("name", "") if isinstance(item, dict) else str(item)
                    rows.append({"kind": KNIP_KIND[category], "file": _norm_path(raw), "name": "", "line": None})
                    continue
                name = item.get("name", "") if isinstance(item, dict) else str(item)
                line = item.get("line") if isinstance(item, dict) else None
                rows.append(
                    {
                        "kind": KNIP_KIND[category],
                        "file": _norm_path(file_path),
                        "name": name,
                        "line": line,
                    }
                )
    return rows


def parse_vulture_line(line: str) -> dict | None:
    m = _VULTURE_RE.match(line.strip())
    if not m:
        return None
    return {
        "kind": f"unused-{m.group(3)}",
        "file": _norm_path(m.group(1)),
        "line": int(m.group(2)),
        "name": m.group(4),
    }


def _has_py(repo: Path) -> bool:
    # inner any() consumes each glob generator (Path objects are truthy);
    # a bare any(gen for gen in globs) would test generator objects themselves
    # - always truthy - and never detect an empty repo (probed by R-E repro F4)
    return any(any(repo.glob(p)) for p in ("*.py", "*/*.py", "*/*/*.py"))


def run_vulture(bin_path: str, repo: Path) -> list[dict] | None:
    if not _has_py(repo):
        return None
    proc = subprocess.run(
        [bin_path, ".", "--min-confidence", "60"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if proc.returncode not in (0, 1, 2, 3):
        return None
    rows = []
    for raw in proc.stdout.splitlines():
        parsed = parse_vulture_line(raw)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _norm_path(p: str) -> str:
    return p.replace("\\", "/")


def _ingest_filter(row: dict) -> bool:
    fp = row["file"]
    if not fp or testsig.is_synthetic_path(fp):
        return False
    if row["kind"] in ("unused-import", "unused-variable") and testsig.is_test_path(fp):
        return False
    return True


def _row_key(row: dict) -> str:
    return f"{row['name']}@{row['file']}"


def cross_check(candidates: list[tuple], tool_rows: list[dict], tool: str) -> list[tuple]:
    """candidates: (fid, qualified_name, name, file_path, start_line, end_line, label) from s2_deadcode."""
    category = ledger_category(tool)
    ledger: list[tuple] = []
    covered: set[str] = set()
    cand_by_key: dict[str, tuple] = {}
    for cand in candidates:
        cand_by_key[f"{cand[2]}@{cand[3]}"] = cand
    for row in tool_rows:
        key = _row_key(row)
        cand = cand_by_key.get(key)
        if cand is None and row.get("line") is not None:
            for ck, c in cand_by_key.items():
                if c[3] == row["file"] and c[4] is not None and c[5] is not None and c[4] <= row["line"] <= c[5]:
                    cand = c
                    break
        if cand is not None:
            covered.add(f"{cand[2]}@{cand[3]}")
            ledger.append((category, f"{tool}:{key}", f"deadcode:{cand[1]}",
                           json.dumps({"tool": tool, "agreed": True}, ensure_ascii=False)))
        else:
            ledger.append((category, f"{tool}:{key}", None,
                           json.dumps({"tool": tool, "agreed": False}, ensure_ascii=False)))
    for key, cand in cand_by_key.items():
        if key not in covered:
            ledger.append((category, f"deadcode:{cand[1]}", f"{tool}:none",
                           json.dumps({"tool": tool, "agreed": False, "side": "candidate-only"}, ensure_ascii=False)))
    return ledger


def scan(facts_db: sqlite3.Connection, repo: Path) -> dict:
    stats: dict = {"knip": "skipped", "vulture": "skipped", "ledger_rows": 0}
    candidates = s2_deadcode.dead_code_candidates(facts_db)

    knip_bin = find_knip()
    if knip_bin and (repo / "package.json").is_file():
        rows = run_knip(knip_bin, repo)
        if rows is None:
            # bin present + package.json present but the run failed (rc out of
            # range, empty stdout, corrupt JSON) - must not masquerade as
            # "skipped"; stale knip findings/ledger stay, stats say "failed"
            stats["knip"] = "failed"
        else:
            rows = [r for r in rows if _ingest_filter(r)]
            _write_tool_findings(facts_db, "s2-knip", "P4", rows)
            ledger = cross_check(candidates, rows, "knip")
            _write_ledger(facts_db, ledger_category("knip"), ledger)
            stats["knip"] = len(rows)
            stats["ledger_rows"] += len(ledger)

    vulture_bin = find_vulture()
    if vulture_bin:
        rows = run_vulture(vulture_bin, repo)
        if rows is None:
            # distinguish "not applicable" (no python files) from a failed
            # run (rc out of range) - both return None from run_vulture
            stats["vulture"] = "failed" if _has_py(repo) else "skipped"
        else:
            rows = [r for r in rows if _ingest_filter(r)]
            _write_tool_findings(facts_db, "s2-vulture", "P4", rows)
            ledger = cross_check(candidates, rows, "vulture")
            _write_ledger(facts_db, ledger_category("vulture"), ledger)
            stats["vulture"] = len(rows)
            stats["ledger_rows"] += len(ledger)

    return stats


def _write_tool_findings(con: sqlite3.Connection, source: str, pathology: str, rows: list[dict]) -> None:
    out = []
    for r in rows:
        anchor = None
        if r.get("line") is not None:
            anchor = _anchor_symbol(con, r["file"], r["line"])
        payload = json.dumps({"name": r["name"], "kind": r["kind"]}, ensure_ascii=False)
        out.append((pathology, r["kind"], source, anchor, None, r["file"], r.get("line"), r.get("line"), payload))
    con.execute("DELETE FROM findings WHERE source = ?", (source,))
    con.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        out,
    )
    con.commit()


def _write_ledger(con: sqlite3.Connection, category: str, rows: list[tuple]) -> None:
    con.execute("DELETE FROM discrepancy_ledger WHERE category = ?", (category,))
    con.executemany(
        "INSERT INTO discrepancy_ledger (category, src_key, dst_key, detail_json) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
