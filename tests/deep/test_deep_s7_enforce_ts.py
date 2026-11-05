"""S7 TS/JS 执法真 depcruise E2E（quick 层 mocked 覆盖见 tests/test_s7_enforce_ts.py）。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce, s7_enforce_ts

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

if s7_enforce_ts.find_depcruise() is None:
    pytest.skip("depcruise not found (work/dep-cruise-probe sandbox)", allow_module_level=True)

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["t1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["t2"], "layer": "interface"},
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
    (repo / "package.json").write_text('{"name": "fx", "version": "0.0.0"}\n', encoding="utf-8")
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
            "SELECT pathology, kind, source, anchor_fid, file_path, start_line, payload_json"
            " FROM findings WHERE source = 's7-enforce'"
        ).fetchall()
    finally:
        con.close()


def test_enforce_ts_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "dependency-cruiser"  # 语言路由命中 TS 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["total_cruised"] == 2
    assert not (repo / ".vrp" / "dependency-cruiser.cjs").exists()  # 配置运行后清理
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "t1"  # 锚定文件首符号
    assert file_path == "src/core/a.ts"
    assert line == 1  # 行号取锚符号 start_line（depcruise 无行号）
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "src/core/a.ts"
    assert data["imported"] == "src/iface/b.ts"
    assert data["line"] is None


def test_enforce_ts_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_ts_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        (repo / "src" / "core" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")  # 修掉违例 import
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_ts_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "core" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    assert out["findings_written"] == 0
    assert out["total_cruised"] == 2
    assert _rows(repo) == []
