"""S7 架构执法真 lint-imports E2E（quick 层 mocked 覆盖见 tests/test_s7_enforce.py）。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

if s7_enforce.find_import_linter() is None:
    pytest.skip("lint-imports not found (pip install import-linter)", allow_module_level=True)

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["f1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["f2"], "layer": "interface"},
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
        " VALUES ('f1', 'p', 'pkgcore.s0.a', 'Function', 'a', 'pkgcore/s0.py', 1, 4, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'pkgiface.cli.c', 'Function', 'c', 'pkgiface/cli.py', 1, 2, 'h')"
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
    (repo / "pkgcore" / "s1.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (repo / "pkgiface").mkdir()
    (repo / "pkgiface" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkgiface" / "cli.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    return repo


def _rows(repo: Path) -> list[tuple]:
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        return con.execute(
            "SELECT pathology, kind, source, anchor_fid, file_path, start_line, payload_json"
            " FROM findings WHERE source = 's7-enforce'"
        ).fetchall()
    finally:
        con.close()


def test_enforce_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["contracts"] == 1
    assert out["broken"] == 1
    assert out["kept"] == 0
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert not (repo / ".vrp" / "importlinter.ini").exists()  # 配置运行后清理
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "f1"  # 函数内 import → 覆盖符号 a()
    assert file_path == "pkgcore/s0.py"
    assert line == 2
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "pkgcore.s0"
    assert data["imported"] == "pkgiface.cli"


def test_enforce_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["broken"] == 1
        (repo / "pkgcore" / "s0.py").write_text(
            "def a():\n    return 1\n", encoding="utf-8"
        )  # 修掉违例 import
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["broken"] == 0
    assert good["kept"] == 1
    assert good["violations"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "pkgcore" / "s0.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["broken"] == 0
    assert out["kept"] == 1
    assert out["findings_written"] == 0
    assert _rows(repo) == []
