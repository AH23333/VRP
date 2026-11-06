"""S7 Rust 执法真解析 E2E（quick 层紧凑 fixture 见 tests/test_s7_enforce_rs.py）。

引擎内置解析器无外部工具链：deep 层覆盖真实 crate 形态——mod.rs/2018 双风格
嵌套模块、传递链、cfg(test) 文件模块排除；真仓 dogfood 另见 work/rs-enforce-probe。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import s7_enforce

pytestmark = pytest.mark.deep

SCHEMA = Path(__file__).parent.parent.parent / "vrp" / "schema" / "facts_schema.sql"

# 两层：core(store/mod.rs + store/db.rs 嵌套) ← interface(api/mod.rs + api/client.rs)
STRUCTURE = json.dumps(
    {
        "modules": [
            {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g1", "g2"], "layer": "core"},
            {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g3"], "layer": "interface"},
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
        " VALUES ('g1', 'p', 'deep::store', 'Module', 'store', 'src/store/mod.rs', 1, 1, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g2', 'p', 'deep::store::db::get', 'Function', 'get', 'src/store/db.rs', 3, 3, 'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g3', 'p', 'deep::api::client::handle', 'Function', 'handle', 'src/api/client.rs', 1, 1, 'h')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?, 'active', 't')",
        (STRUCTURE,),
    )
    con.commit()
    con.close()
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "deep"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8"
    )
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("mod api;\nmod store;\n", encoding="utf-8")
    (repo / "src" / "api").mkdir()
    (repo / "src" / "api" / "mod.rs").write_text("pub mod client;\n", encoding="utf-8")
    (repo / "src" / "api" / "client.rs").write_text(
        "pub fn handle() -> u32 { 1 }\n", encoding="utf-8"
    )
    (repo / "src" / "store").mkdir()
    (repo / "src" / "store" / "mod.rs").write_text("pub mod db;\n", encoding="utf-8")
    # 违例：嵌套模块 store::db 上向 use crate::api::client（第 1 行）
    (repo / "src" / "store" / "db.rs").write_text(
        "use crate::api::client::handle;\n\npub fn get() -> u32 { handle() }\n", encoding="utf-8"
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


def test_enforce_rs_e2e_violation_to_finding(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == "rs-use-parser"  # 语言路由命中 Rust 引擎
    assert out["contracts"] == 1
    assert out["violations"] == 1
    assert out["findings_written"] == 1
    assert out["anchored"] == 1
    assert out["files"] == 5
    assert out["modules"] == 5  # crate/api/api::client/store/store::db
    rows = _rows(repo)
    assert len(rows) == 1
    pathology, kind, source, anchor, file_path, line, payload = rows[0]
    assert (pathology, kind, source) == ("P9", "layer-violation", "s7-enforce")
    assert anchor == "g2"  # 锚定违例 use 文件首符号
    assert file_path == "src/store/db.rs"
    assert line == 1  # 违例 use 行
    data = json.loads(payload)
    assert data["source_layer"] == "core"
    assert data["forbidden_layer"] == "interface"
    assert data["importer"] == "store::db"  # 容器 = mod 树模块路径
    assert data["imported"] == "api::client"
    assert data["chain"] == ["store::db", "api::client"]
    assert data["line"] == 1


def test_enforce_rs_e2e_transitive_and_cfg_test_excluded(tmp_path):
    """三层链 store::db → queue → api::client + cfg(test) 文件模块不掺入。"""
    repo = _mk_repo(tmp_path)
    (repo / "src" / "lib.rs").write_text(
        "mod api;\nmod queue;\nmod store;\n#[cfg(test)]\nmod integration;\n", encoding="utf-8"
    )
    (repo / "src" / "queue.rs").write_text(
        "use crate::api::client::handle;\n\npub fn work() -> u32 { handle() }\n", encoding="utf-8"
    )
    # db 补上向 use queue（core→app 边）
    (repo / "src" / "store" / "db.rs").write_text(
        "use crate::queue::work;\nuse crate::api::client::handle;\n\npub fn get() -> u32 { work() + handle() }\n",
        encoding="utf-8",
    )
    # cfg(test) 文件模块：若泄漏会多出 interface 层违例
    (repo / "src" / "integration.rs").write_text(
        "use crate::api::client::handle;\nuse crate::store::db;\n", encoding="utf-8"
    )
    structure = json.dumps(
        {
            "modules": [
                {"module_id": "core-mod", "name": "存储", "responsibility": "存", "symbol_fids": ["g1", "g2"], "layer": "core"},
                {"module_id": "app-mod", "name": "队列", "responsibility": "派", "symbol_fids": ["g4"], "layer": "app"},
                {"module_id": "iface-mod", "name": "接口", "responsibility": "面", "symbol_fids": ["g3"], "layer": "interface"},
            ],
            "layers": [
                {"name": "core", "may_depend_on": []},
                {"name": "app", "may_depend_on": ["core"]},
                {"name": "interface", "may_depend_on": ["app", "core"]},
            ],
        },
        ensure_ascii=False,
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('g4', 'p', 'deep::queue::work', 'Function', 'work', 'src/queue.rs', 3, 3, 'h')"
    )
    con.execute("UPDATE structure SET payload_json = ?", (structure,))
    con.commit()
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["files"] == 6  # integration.rs 被 cfg(test) 排除在树外
    assert out["violations"] == 3  # core→app / core→interface / app→interface 各一
    rows = _rows(repo)
    by_pair = {(json.loads(r[6])["importer"], json.loads(r[6])["imported"]): json.loads(r[6]) for r in rows}
    assert by_pair[("store::db", "queue")]["chain"] == ["store::db", "queue"]
    assert by_pair[("queue", "api::client")]["chain"] == ["queue", "api::client"]
    assert by_pair[("store::db", "api::client")]["chain"] == ["store::db", "api::client"]
    assert all("integration" not in json.loads(r[6])["importer"] for r in rows)


def test_enforce_rs_e2e_idempotent_rerun(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        first = s7_enforce.run(repo, con, timeout=180)
        second = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert first["findings_written"] == second["findings_written"] == 1
    assert len(_rows(repo)) == 1  # replace-all：重跑不膨胀


def test_enforce_rs_e2e_fix_clears_findings(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        bad = s7_enforce.run(repo, con, timeout=180)
        assert bad["violations"] == 1
        # 修掉违例 use（保模块可编译）
        (repo / "src" / "store" / "db.rs").write_text(
            "pub fn get() -> u32 { 0 }\n", encoding="utf-8"
        )
        good = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert good["status"] == "ok"
    assert good["violations"] == 0
    assert good["findings_written"] == 0
    assert _rows(repo) == []  # 合规重跑清掉旧违例


def test_enforce_rs_e2e_compliant_repo_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "src" / "store" / "db.rs").write_text(
        "pub fn get() -> u32 { 0 }\n", encoding="utf-8"
    )
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["violations"] == 0
    assert out["findings_written"] == 0
    assert _rows(repo) == []
