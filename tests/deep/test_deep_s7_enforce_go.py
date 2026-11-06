"""S7 Go 执法真 go list E2E（quick 层 mocked 覆盖见 tests/test_s7_enforce_go.py）。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce, s7_enforce_go

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

if s7_enforce_go.find_go() is None:
    pytest.skip("go toolchain not found (install Go, or set VRP_GO_BIN)", allow_module_level=True)

MODULE_PATH = "example.com/deep"

STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "核心", "responsibility": "算", "symbol_fids": ["g1"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g2"], "layer": "interface"},
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
        " VALUES ('g1', 'p', 'deep/store.Get', 'Function', 'Get', 'store/repo.go', 5, 7, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'deep/api.Handle', 'Function', 'Handle', 'api/handler.go', 3, 5, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "go.mod").write_text(f"module {MODULE_PATH}\n\ngo 1.26\n", encoding="utf-8")
    (repo / "api").mkdir()
    (repo / "api" / "handler.go").write_text(
        "package api\n\nfunc Handle() int { return 1 }\n", encoding="utf-8"
    )
    (repo / "store").mkdir()
    # 违例：core 直 import interface（第 3 行引号字面量锚定）
    (repo / "store" / "repo.go").write_text(
        f"package store\n\nimport \"{MODULE_PATH}/api\"\n\nfunc Get() int {{ return api.Handle() }}\n",
        encoding="utf-8",
    )
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


def test_enforce_go_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "go-list"  # 语言路由命中 Go 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["packages"] == 2
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g1"  # 锚定违例 import 文件首符号
    assert file_path == "store/repo.go"
    assert line == 3  # 真 go list 图 + 文件扫描定位违例 import 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "store"
    assert data["imported"] == "api"
    assert data["chain"] == ["store", "api"]
    assert data["line"] == 3


def test_enforce_go_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_go_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 import（保包可编译）
        (repo / "store" / "repo.go").write_text(
            "package store\n\nfunc Get() int { return 1 }\n", encoding="utf-8"
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_go_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "store" / "repo.go").write_text(
        "package store\n\nfunc Get() int { return 1 }\n", encoding="utf-8"
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    assert out["findings_written"] == 0
    assert out["packages"] == 2
    assert _rows(repo) == []
