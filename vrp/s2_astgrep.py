from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

FINDINGS_SOURCE = "s2-astgrep"
RULES_DIR = Path(__file__).parent / "rules" / "s2"

RULE_META: dict[str, tuple[str, str]] = {
    "js-console-log": ("P1", "debug-print"),
    "ts-console-log": ("P1", "debug-print"),
    "ts-empty-catch": ("P3", "swallowed-error"),
    "ts-todo-comment": ("P3", "debris-marker"),
    "ts-prototype-name": ("P3", "prototype-name"),
    "py-print-debug": ("P1", "debug-print"),
    "py-bare-except": ("P3", "swallowed-error"),
    "py-todo-comment": ("P3", "debris-marker"),
    "py-prototype-name": ("P3", "prototype-name"),
}


def find_astgrep() -> str | None:
    env = os.environ.get("VRP_ASTGREP_BIN")
    if env and Path(env).is_file():
        return env
    sandbox = Path(__file__).parent.parent / "work" / "astgrep" / "node_modules" / "@ast-grep"
    if sandbox.is_dir():
        for pkg in sorted(sandbox.iterdir()):
            for name in ("ast-grep.exe", "ast-grep", "sg.exe", "sg"):
                cand = pkg / name
                if cand.is_file():
                    return str(cand)
    for name in ("ast-grep", "ast-grep.exe", "sg", "sg.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def rule_id_of(rule_file: Path) -> str | None:
    for line in rule_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("id:"):
            return stripped[3:].strip()
    return None


def run_rule(bin_path: str, rule_file: Path, scan_root: Path) -> list[dict]:
    proc = subprocess.run(
        [bin_path, "scan", "--rule", str(rule_file), "--json", str(scan_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ast-grep scan failed (rc={proc.returncode}): {proc.stderr[:500]}")
    return json.loads(proc.stdout)


def _anchor_symbol(con: sqlite3.Connection, rel_path: str, line: int) -> str | None:
    row = con.execute(
        "SELECT fid FROM symbols WHERE file_path = ? AND start_line IS NOT NULL"
        " AND start_line <= ? AND (end_line IS NULL OR end_line >= ?)"
        " ORDER BY start_line DESC LIMIT 1",
        (rel_path, line, line),
    ).fetchone()
    return row[0] if row else None


def scan(facts_db: sqlite3.Connection, repo: Path, bin_path: str | None = None) -> dict:
    bin_path = bin_path or find_astgrep()
    if bin_path is None:
        raise RuntimeError("ast-grep binary not found (set VRP_ASTGREP_BIN or install work/astgrep sandbox)")

    per_rule: dict[str, int] = {}
    anchored = 0
    rejected = 0
    rows: list[tuple] = []

    for rule_file in sorted(RULES_DIR.glob("*.yml")):
        rid = rule_id_of(rule_file)
        if rid is None or rid not in RULE_META:
            raise RuntimeError(f"rule file {rule_file.name} has no id or no RULE_META entry")
        pathology, kind = RULE_META[rid]
        matches = run_rule(bin_path, rule_file, repo)
        count = 0
        for m in matches:
            abs_file = Path(m["file"])
            try:
                rel = abs_file.relative_to(repo).as_posix()
            except ValueError:
                rejected += 1
                continue
            line = m["range"]["start"]["line"] + 1
            end_line = m["range"]["end"]["line"] + 1
            if not (repo / rel).is_file():
                rejected += 1
                continue
            src_line_count = len((repo / rel).read_text(encoding="utf-8", errors="replace").splitlines())
            if line > src_line_count:
                rejected += 1
                continue
            anchor = _anchor_symbol(facts_db, rel, line)
            if anchor is not None:
                anchored += 1
            payload = json.dumps(
                {
                    "rule": rid,
                    "snippet": m.get("text", ""),
                    "language": m.get("language", ""),
                    "severity": m.get("severity", ""),
                    "note": m.get("note", ""),
                },
                ensure_ascii=False,
            )
            rows.append((pathology, kind, FINDINGS_SOURCE, anchor, None, rel, line, end_line, payload))
            count += 1
        per_rule[rid] = count

    facts_db.execute("DELETE FROM findings WHERE source = ?", (FINDINGS_SOURCE,))
    facts_db.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid, file_path, start_line, end_line, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    facts_db.commit()
    return {"total": len(rows), "anchored": anchored, "rejected": rejected, "per_rule": per_rule}
