"""Deep eval: SIMILAR_TO (P2) calibration on an AI-generated-code-style repo.

Closes open question #2 (§11.2 / §12.5): measure CBM SIMILAR_TO behaviour on
planted ground truth written in the style of LLM-generated Python.

Planted pairs and expected verdicts (measured 2026-11-03):
- PAIR-1 verbatim duplicate (renamed function, byte-identical body)
  -> detected, jaccard == 1.0
- PAIR-2 near-duplicate (renamed identifiers, same structure)
  -> detected, jaccard >= 0.95 (measured 1.0)
- PAIR-3 heavy rewrite (same purpose, different structure)
  -> below the ~0.95 emission floor, NOT detected (jscpd 补召回, §12.5)
- controls (unrelated helpers) -> no P2 findings at all
"""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp

pytestmark = pytest.mark.deep

_PARSER_BODY = '''"""Load application settings from disk."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def {fn}(path: str) -> dict[str, Any]:
    """Parse settings from a JSON file with defaults applied."""
    defaults: dict[str, Any] = {{
        "theme": "light",
        "language": "en",
        "retries": 3,
        "timeout": 30,
        "verbose": False,
    }}
    file = Path(path)
    if not file.exists():
        return defaults
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return defaults
    if not isinstance(raw, dict):
        return defaults
    for key, value in raw.items():
        if key in defaults and isinstance(value, type(defaults[key])):
            defaults[key] = value
    return defaults
'''

_REPORT_BODY = '''"""{doc}"""
from __future__ import annotations


def {fn}({arg_records}: list[dict], {arg_title}: str) -> str:
    """Render a human-readable report from activity records."""
    lines: list[str] = [f"# {{{arg_title}}}", ""]
    {var_total} = 0
    for {var_record} in {arg_records}:
        {var_name} = {var_record}.get("name", "unknown")
        {var_count} = {var_record}.get("count", 0)
        {var_total} += {var_count}
        status = "high" if {var_count} > 100 else "normal"
        lines.append(f"- {{{var_name}}}: {{{var_count}}} events ({{status}})")
    lines.append("")
    lines.append(f"Total events: {{{var_total}}}")
    if not {arg_records}:
        lines.append("No activity recorded this week.")
    return chr(10).join(lines)
'''

_INVENTORY_BODY = '''"""Track warehouse inventory levels."""
from __future__ import annotations


def track_inventory(stocks: dict[str, int], orders: list[tuple[str, int]]) -> dict[str, int]:
    """Apply orders to stock levels and report remaining inventory."""
    remaining = dict(stocks)
    for sku, qty in orders:
        if sku not in remaining:
            raise KeyError(f"unknown sku: {sku}")
        remaining[sku] -= qty
        if remaining[sku] < 0:
            raise ValueError(f"negative stock for {sku}")
    return remaining
'''

_STOCK_BODY = '''"""Manage product stock across warehouses."""
from __future__ import annotations


def manage_stock(
    stock: dict[str, int],
    requested: list[dict],
) -> dict[str, int]:
    """Consume stock for each requested item, validating availability."""
    updated: dict[str, int] = {}
    for entry in requested:
        item_id = entry["sku"]
        needed = int(entry["quantity"])
        available = stock.get(item_id, 0)
        if needed > available:
            raise RuntimeError(f"insufficient stock: {item_id}")
        updated[item_id] = available - needed
    merged = dict(stock)
    merged.update(updated)
    return merged
'''

_DATE_BODY = '''"""Small date helpers."""
from __future__ import annotations

import datetime as _dt


def format_date(value: _dt.date, style: str = "iso") -> str:
    """Format a date as iso or human text."""
    if style == "iso":
        return value.isoformat()
    if style == "human":
        return value.strftime("%B %d, %Y")
    raise ValueError(f"unknown style: {style}")
'''

_CHUNK_BODY = '''"""Small list helpers."""
from __future__ import annotations

from typing import Any


def chunk_list(items: list[Any], size: int) -> list[list[Any]]:
    """Split items into fixed-size chunks."""
    if size <= 0:
        raise ValueError("size must be positive")
    out: list[list[Any]] = []
    for i in range(0, len(items), size):
        out.append(items[i : i + size])
    return out
'''

_MAIN_BODY = '''"""Entry point exercising the app helpers."""
from app.config_loader import parse_settings
from app.report_render import render_report
from app.inventory import track_inventory
from app.date_utils import format_date
from app.list_utils import chunk_list


def main() -> None:
    settings = parse_settings("settings.json")
    print(render_report([{"name": "deploy", "count": 12}], "Weekly"))
    print(track_inventory({"widget": 5}, [("widget", 2)]))
    print(format_date(__import__("datetime").date(2026, 11, 3)))
    print(chunk_list(list(range(7)), 3), settings["theme"])


if __name__ == "__main__":
    main()
'''

_TEST_BODY = '''from app.config_loader import parse_settings
from app.list_utils import chunk_list


def test_settings_defaults():
    assert parse_settings("nope.json")["theme"] == "light"


def test_chunk():
    assert chunk_list([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
'''


def _write_eval_repo(repo: Path) -> None:
    app = repo / "app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    # PAIR-1: verbatim duplicate, renamed function only.
    (app / "config_loader.py").write_text(_PARSER_BODY.format(fn="parse_settings"), encoding="utf-8")
    (app / "settings_reader.py").write_text(_PARSER_BODY.format(fn="load_settings"), encoding="utf-8")
    # PAIR-2: near-duplicate, renamed identifiers, same structure.
    (app / "report_render.py").write_text(
        _REPORT_BODY.format(
            doc="Render the weekly activity report.",
            fn="render_report",
            arg_records="records",
            arg_title="title",
            var_total="total",
            var_record="record",
            var_name="name",
            var_count="count",
        ),
        encoding="utf-8",
    )
    (app / "report_format.py").write_text(
        _REPORT_BODY.format(
            doc="Format the weekly activity report.",
            fn="format_report",
            arg_records="items",
            arg_title="heading",
            var_total="sum_total",
            var_record="item",
            var_name="label",
            var_count="amount",
        ),
        encoding="utf-8",
    )
    # PAIR-3: same purpose, heavy rewrite.
    (app / "inventory.py").write_text(_INVENTORY_BODY, encoding="utf-8")
    (app / "stock_manager.py").write_text(_STOCK_BODY, encoding="utf-8")
    # Controls: unrelated helpers.
    (app / "date_utils.py").write_text(_DATE_BODY, encoding="utf-8")
    (app / "list_utils.py").write_text(_CHUNK_BODY, encoding="utf-8")
    (repo / "main.py").write_text(_MAIN_BODY, encoding="utf-8")
    (repo / "test_app.py").write_text(_TEST_BODY, encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")


def _p2_pairs(repo: Path) -> dict:
    facts = repo / ".vrp" / "facts.db"
    con = sqlite3.connect(f"file:{facts}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT sa.qualified_name, sb.qualified_name, f.payload_json"
            " FROM findings f JOIN symbols sa ON sa.fid = f.anchor_fid"
            " JOIN symbols sb ON sb.fid = f.related_fid"
            " WHERE f.pathology = 'P2'"
        ).fetchall()
    finally:
        con.close()
    pairs = {}
    for qa, qb, payload in rows:
        a = qa.rsplit(".", 1)[-1]
        b = qb.rsplit(".", 1)[-1]
        pairs[frozenset((a, b))] = json.loads(payload)
    return pairs


@pytest.fixture(scope="session")
def dup_eval(tmp_path_factory):
    repo = tmp_path_factory.mktemp("vibe-dup-eval")
    _write_eval_repo(repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


def test_verbatim_duplicate_detected(dup_eval):
    pairs = _p2_pairs(dup_eval)
    key = frozenset(("parse_settings", "load_settings"))
    assert key in pairs
    assert pairs[key]["jaccard"] == 1.0
    assert pairs[key]["same_file"] is False


def test_near_duplicate_detected(dup_eval):
    pairs = _p2_pairs(dup_eval)
    key = frozenset(("render_report", "format_report"))
    assert key in pairs
    assert pairs[key]["jaccard"] >= 0.95


def test_heavy_rewrite_below_floor_not_detected(dup_eval):
    pairs = _p2_pairs(dup_eval)
    assert frozenset(("track_inventory", "manage_stock")) not in pairs


def test_no_control_noise(dup_eval):
    pairs = _p2_pairs(dup_eval)
    expected = {frozenset(("parse_settings", "load_settings")), frozenset(("render_report", "format_report"))}
    assert set(pairs.keys()) == expected
    for key in (frozenset(("format_date", "chunk_list")), frozenset(("format_date", "format_report"))):
        assert key not in pairs
