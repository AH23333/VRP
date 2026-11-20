"""S7 混仓分治 dogfood：py(import-linter) + ts(depcruise) 真实双引擎 E2E。

quick 层 mocked 覆盖见 tests/test_s7_mixed.py；本件验证分治全链——
真实 lint-imports 与真实 depcruise 在同一 facts.db 上各自落件并存
（C7 核心交付：混仓 findings 写面并存，跨语言边上报）。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce, s7_enforce_ts

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

if s7_enforce.find_import_linter() is None:
    pytest.skip("lint-imports not found (pip install import-linter)", allow_module_level=True)
if s7_enforce_ts.find_depcruise() is None:
    pytest.skip("depcruise not found (work/dep-cruise-probe sandbox)", allow_module_level=True)

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "py-core", "name": "py核心", "responsibility": "算", "symbol_fids": ["p1"], "layer": "core"},
            {"module_id": "py-iface", "name": "py接口", "responsibility": "面", "symbol_fids": ["p2"], "layer": "interface"},
            {"module_id": "ts-core", "name": "ts核心", "responsibility": "算", "symbol_fids": ["t1"], "layer": "core"},
            {"module_id": "ts-iface", "name": "ts接口", "responsibility": "面", "symbol_fids": ["t2"], "layer": "interface"},
        ],
        "layers": [
            {"name": "core", "may_depend_on": []},
            {"name": "interface", "may_depend_on": ["core"]},
        ],
    },
    ensure_ascii=False,
)


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('p1', 'p', 'pkgcore.s0.a', 'Function', 'a', 'pkgcore/s0.py', 1, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('p2', 'p', 'pkgiface.cli.c', 'Function', 'c', 'pkgiface/cli.py', 1, 2, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t1', 'p', 'src.core.a', 'Function', 'a', 'src/core/a.ts', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('t2', 'p', 'src.iface.b', 'Function', 'b', 'src/iface/b.ts', 1, 2, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "pkgcore").mkdir()
    (repo / "pkgcore" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkgcore" / "s0.py").write_text(
        "def a():\n    import pkgiface.cli as _cli\n\n    return 1\n", encoding="utf-8"
    )
    (repo / "pkgiface").mkdir()
    (repo / "pkgiface" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkgiface" / "cli.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    (repo / "src" / "core").mkdir(parents=True)
    (repo / "src" / "core" / "a.ts").write_text(
        'import { b } from "../iface/b";\nexport const a = b + 1;\n', encoding="utf-8"
    )
    (repo / "src" / "iface").mkdir(parents=True)
    (repo / "src" / "iface" / "b.ts").write_text("export const b = 41;\n", encoding="utf-8")
    return repo


def _rows(repo: Path) -> list[tuple]:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        return con.execute(
            "SELECT pathology, kind, source, anchor_fid, file_path, payload_json"
            " FROM findings WHERE source = 's7-enforce'"
        ).fetchall()
    finally:
        con.close()


def test_deep_mixed_py_ts_real_e2e(tmp_path):
    """真实双引擎分治 E2E：py+ts 违例同 run 各自落件并存。"""
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=600)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert list(out["languages"]) == ["py", "ts"]
    py = out["languages"]["py"]
    ts = out["languages"]["ts"]
    assert py["status"] == "ok"
    assert (py["violations"], py["findings_written"], py["anchored"]) == (1, 1, 1)
    assert (py["kept"], py["broken"], py["contracts"]) == (0, 1, 1)
    assert ts["status"] == "ok"
    assert (ts["violations"], ts["findings_written"], ts["anchored"]) == (1, 1, 1)
    assert ts["engine"] == "dependency-cruiser"
    assert (out["violations"], out["findings_written"], out["anchored"]) == (2, 2, 2)
    assert out["cross_language_edges"] == []
    assert out["cross_language_edge_groups_total"] == 0
    assert "per-engine timeout 600s each" in out["timeout_note"]
    assert not (repo / ".vrp" / "importlinter.ini").exists()
    assert not (repo / ".vrp" / "dependency-cruiser.cjs").exists()
    rows = _rows(repo)
    assert len(rows) == 2  # 真实双引擎写面并存——C7 核心交付锚
    by_anchor = {r[3]: r for r in rows}
    py_row = by_anchor["p1"]
    ts_row = by_anchor["t1"]
    assert (py_row[0], py_row[1], py_row[2]) == ("P9", "layer-violation", "s7-enforce")
    assert py_row[4] == "pkgcore/s0.py"
    py_payload = json.loads(py_row[5])
    assert py_payload["importer"] == "pkgcore.s0"
    assert py_payload["imported"] == "pkgiface.cli"
    assert (ts_row[0], ts_row[1], ts_row[2]) == ("P9", "layer-violation", "s7-enforce")
    assert ts_row[4] == "src/core/a.ts"
    ts_payload = json.loads(ts_row[5])
    assert ts_payload["importer"] == "src/core/a.ts"
    assert ts_payload["imported"] == "src/iface/b.ts"


def test_deep_mixed_py_ts_real_idempotent_rerun(tmp_path):
    """真实双引擎分治幂等：重跑 findings 不膨胀（单次清场 + 只插不删）。"""
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=600)
        second = s7_enforce.run(repo, con, timeout=600)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 2
    assert len(_rows(repo)) == 2
