"""F 批次（curate 漂移治理）测试：F2 复验钩子（etl/MCP 路径）+ F3 统一漂移查询面
+ F1 fid_ledger 身份史与 remap 读出辅助（R6' 诚实发现三条，构想v7 §12）。
"""

import json
import sqlite3
from pathlib import Path

from vrp import curate as curate_mod
from vrp import etl

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")  # M4: 非 git 仓 = ''
    con.commit()
    con.close()
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _seed_symbols(repo: Path) -> list[str]:
    """a -> b 链 + 现行符号 + 身份史（ledger 记旧 fid）。"""
    con = _con(repo)
    try:
        for fid, qn in (("fid-a", "pkg.a"), ("fid-b", "pkg.b")):
            con.execute(
                "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
                " start_line, end_line, content_hash) VALUES (?, 'p', ?, 'Function', ?, 'pkg/x.py', 1, 3, 'h2')",
                (fid, qn, qn.split(".")[-1]),
            )
            con.execute(
                "INSERT INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
                " VALUES (?, 'p', ?, 'pkg/x.py', '2026-11-06T00:00:00Z')",
                (fid, qn),
            )
        # 旧世界：a 的前代 fid（内容已变）+ 已消亡符号 c（连 qn 都不在了）
        con.execute(
            "INSERT INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
            " VALUES ('fid-a-old', 'p', 'pkg.a', 'pkg/x.py', '2026-11-05T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
            " VALUES ('fid-c-gone', 'p', 'pkg.c', 'pkg/y.py', '2026-11-05T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-a', 'fid-b', 'CALLS', 'structural')"
        )
        con.commit()
    finally:
        con.close()
    return ["fid-a", "fid-b"]


def _slice(fids: list[str], slice_id: str = "test-flow", **overrides) -> dict:
    payload = {
        "slice_id": slice_id,
        "name": "测试线",
        "summary": "链路",
        "steps": [{"fid": f, "role": "process"} for f in fids],
    }
    payload.update(overrides)
    return payload


def _structure(fids: list[str]) -> dict:
    return {
        "modules": [
            {"module_id": "m1", "name": "模块一", "responsibility": "负责一",
             "symbol_fids": fids, "layer": "domain"},
        ],
        "layers": [{"name": "domain", "may_depend_on": []}],
    }


def _insert_slice(repo: Path, fids: list[str], status: str = "active", slice_id: str = "test-flow") -> None:
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO slices (slice_id, name, summary, payload_json, status)"
            " VALUES (?, 'n', 's', ?, ?)",
            (slice_id, json.dumps(_slice(fids, slice_id=slice_id), ensure_ascii=False), status),
        )
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------- F3 查询面


def test_drift_report_fresh_slice(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo)
    _insert_slice(repo, fids)
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    assert report["slices"][0]["slice_id"] == "test-flow"
    assert report["slices"][0]["fresh"] is True
    assert report["slices"][0]["missing_fids"] == []
    assert report["structure"] is None
    assert report["summary"] == {
        "slices_checked": 1, "slices_fresh": 1, "slices_drifted": 0,
        "structure_drifted": False, "active_but_drifted": [],
    }


def test_drift_report_remap_via_ledger(tmp_path):
    """F1 读出辅助：旧 fid → ledger 反查 qn → 现行 fid 唯一候选 → remap 直给。"""
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo)
    # 工件引用旧世界 fid（假 active：绕过写入路径直插模拟复验链缺位）
    _insert_slice(repo, ["fid-a-old", "fid-b"])
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    item = report["slices"][0]
    assert item["fresh"] is False
    miss = item["missing_fids"][0]
    assert miss["fid"] == "fid-a-old"
    assert miss["ledger"] is True
    assert miss["ledger_qn"] == "pkg.a"
    assert miss["candidates"] == [{"fid": "fid-a", "file_path": "pkg/x.py", "match": "qualified_name"}]
    assert item["remap"] == {"fid-a-old": "fid-a"}
    assert report["summary"]["active_but_drifted"] == ["test-flow"]


def test_drift_report_same_file_fallback_when_qn_gone(tmp_path):
    """qn 消亡（改名/删除）且同文件无现行符号：零候选诚实返回，不构 remap。"""
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo)
    _insert_slice(repo, ["fid-c-gone"])
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    miss = report["slices"][0]["missing_fids"][0]
    assert miss["ledger"] is True
    assert miss["ledger_qn"] == "pkg.c"
    assert miss["ledger_file_path"] == "pkg/y.py"
    # pkg/y.py 现行无符号 → 同文件域亦空
    assert miss["candidates"] == []
    assert "remap" not in report["slices"][0]
    assert report["slices"][0]["fresh"] is False


def test_drift_report_same_file_fallback_lists_symbols(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo)
    # 把现行符号挪进 pkg/y.py 模拟"改名后在同文件"
    con = _con(repo)
    con.execute("UPDATE symbols SET file_path = 'pkg/y.py' WHERE fid = 'fid-a'")
    con.commit()
    con.close()
    _insert_slice(repo, ["fid-c-gone"])
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    miss = report["slices"][0]["missing_fids"][0]
    assert [c["fid"] for c in miss["candidates"]] == ["fid-a"]
    assert miss["candidates"][0]["match"] == "same_file"
    assert miss["candidates"][0]["qualified_name"] == "pkg.a"


def test_drift_report_missing_edge_both_fids_alive(tmp_path):
    """两端 fid 存活但边消亡：missing_edges 单列。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo)
    _insert_slice(repo, fids)
    con = _con(repo)
    con.execute("DELETE FROM edges")
    con.commit()
    con.close()
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    item = report["slices"][0]
    assert item["fresh"] is False
    assert item["missing_fids"] == []
    assert item["missing_edges"] == [{"step": 0, "from_fid": "fid-a", "to_fid": "fid-b"}]


def test_drift_report_fids_recovered_on_stale(tmp_path):
    """stale 但 fid 已复活（内容回退）：fids_recovered=true，原载荷可原样重提。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo)
    _insert_slice(repo, fids, status="stale")
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    item = report["slices"][0]
    assert item["status"] == "stale"
    assert item["fresh"] is True
    assert item["fids_recovered"] is True
    assert report["summary"]["slices_drifted"] == 0


def test_drift_report_rejected_excluded_and_no_ledger_row(tmp_path):
    """rejected 不入查询面；ledger 无记录的 missing fid 诚实零候选。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo)
    _insert_slice(repo, fids, status="rejected")
    _insert_slice(repo, ["fid-never-seen"], slice_id="ghost-flow")
    con = _con(repo)
    try:
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    assert [s["slice_id"] for s in report["slices"]] == ["ghost-flow"]
    miss = report["slices"][0]["missing_fids"][0]
    assert miss["ledger"] is False
    assert miss["candidates"] == []


def test_drift_report_structure_drift_and_remap(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo)
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
            (json.dumps(_structure(["fid-a-old", "fid-b"]), ensure_ascii=False),),
        )
        con.commit()
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    st = report["structure"]
    assert st["fresh"] is False
    assert st["remap"] == {"fid-a-old": "fid-a"}
    assert st["modules"] == [{"module_id": "m1", "missing": 1}]
    assert report["summary"]["structure_drifted"] is True
    assert report["summary"]["active_but_drifted"] == ["structure"]


def test_drift_report_old_schema_without_ledger_table(tmp_path):
    """pre-F facts.db（无 fid_ledger 表）：查询面不炸，零候选诚实降级。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo)
    _insert_slice(repo, ["fid-a-old", fids[1]])
    con = _con(repo)
    try:
        con.execute("DROP TABLE fid_ledger")
        con.commit()
        report = curate_mod.drift_report(con)
    finally:
        con.close()
    item = report["slices"][0]
    assert item["fresh"] is False
    miss = item["missing_fids"][0]
    assert miss["ledger"] is False
    assert miss["candidates"] == []
    assert "remap" not in item
    assert report["summary"]["active_but_drifted"] == ["test-flow"]


# ------------------------------------------------- F2 MCP 路径补验


def test_curate_payload_revalidates_other_artifacts(tmp_path):
    """MCP curate_submit 路径：提交新工件时复验其余 active 工件，收据带 stale。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo)
    con = _con(repo)
    try:
        # 先入库一条含边链的 slice
        first = curate_mod.curate_payload(con, "slices", json.dumps([_slice(fids)], ensure_ascii=False))
        assert first["status"] == "accepted"
        # 模拟漂移：直删边（绕过写路径，复现 F2 发现的缺口场景）
        con.execute("DELETE FROM edges")
        con.commit()
        second = curate_mod.curate_payload(
            con, "structure", json.dumps(_structure(fids), ensure_ascii=False)
        )
        assert second["status"] == "accepted"
        assert second["stale"]["slices"] == ["test-flow"]
        status = con.execute("SELECT status FROM slices WHERE slice_id = 'test-flow'").fetchone()[0]
        assert status == "stale"
        # 被拒路径不触发复验（无入库即无漂移窗口），也不带 stale 节
        bad = curate_mod.curate_payload(con, "slices", json.dumps([_slice(["fid-ghost"])]))
        assert bad["status"] == "rejected"
        assert "stale" not in bad
    finally:
        con.close()


# --------------------------------------------- F2 etl 钩子 + F1 全周期 E2E


def _mk_cbm(db, node_qn: str = "p.a.old", label: str = "Function"):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)", [("db_uid", "uid-1"), ("mutation_gen", "7")])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'proj',?, 'old', ?, 'a.py', 1, 2, '{}')", (label, node_qn))
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT, properties TEXT, project TEXT)")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("INSERT INTO index_coverage VALUES ('proj','a.py','ok','')")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()


def test_etl_hook_marks_drift_and_ledger_remaps_full_cycle(tmp_path, monkeypatch):
    """F 批次全周期：etl → curate → 内容漂移 → re-etl 自动标 stale（F2）→
    drift_report 给 remap（F1/F3）→ 按 remap 重提 → active 复活。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    out_db = repo / ".vrp" / "facts.db"

    stats = etl.run_etl(repo, "proj", out_db=out_db)
    assert stats["curate_stale_slices"] == 0
    assert stats["curate_stale_structure"] == 0
    con = sqlite3.connect(out_db)
    fid_old = con.execute("SELECT fid FROM symbols").fetchone()[0]
    ledger_n = con.execute("SELECT COUNT(*) FROM fid_ledger").fetchone()[0]
    con.close()
    assert ledger_n == 1

    # 梳理工件入库（单步 slice + structure 引用现行 fid）
    con = sqlite3.connect(out_db)
    ok_slice = curate_mod.curate_payload(
        con, "slices", json.dumps([_slice([fid_old], name="old 线")], ensure_ascii=False)
    )
    assert ok_slice["status"] == "accepted"
    assert ok_slice["stale"] == {"slices": [], "structure": False}
    ok_st = curate_mod.curate_payload(
        con, "structure", json.dumps(_structure([fid_old]), ensure_ascii=False)
    )
    assert ok_st["status"] == "accepted"
    con.close()

    # 内容漂移：函数体改动 → content_hash 轮换 → fid 轮换
    (repo / "a.py").write_text("def old():\n    return 2\n", encoding="utf-8")
    stats2 = etl.run_etl(repo, "proj", out_db=out_db)
    # F2：etl 钩子自动标 stale（此前只有 CLI curate 会标——假 active 窗口）
    assert stats2["curate_stale_slices"] == 1
    assert stats2["curate_stale_structure"] == 1

    con = sqlite3.connect(out_db)
    fid_new = con.execute("SELECT fid FROM symbols").fetchone()[0]
    assert fid_new != fid_old
    assert con.execute("SELECT status FROM slices").fetchone()[0] == "stale"
    assert con.execute("SELECT status FROM structure").fetchone()[0] == "stale"
    # 身份史：新旧 fid 并存，first_seen_at 各自首次出现时刻
    ledger_fids = {r[0] for r in con.execute("SELECT fid FROM fid_ledger")}
    assert ledger_fids == {fid_old, fid_new}
    # meta 计数（对账面）
    assert con.execute("SELECT value FROM meta WHERE key = 'count_curate_stale_slices'").fetchone()[0] == "1"
    assert con.execute("SELECT value FROM meta WHERE key = 'count_curate_stale_structure'").fetchone()[0] == "1"
    # F3/F1：统一查询面 + remap
    report = curate_mod.drift_report(con)
    con.close()
    sl = report["slices"][0]
    assert sl["status"] == "stale"
    assert sl["fresh"] is False
    assert sl["remap"] == {fid_old: fid_new}
    st = report["structure"]
    assert st["remap"] == {fid_old: fid_new}
    assert report["summary"] == {
        "slices_checked": 1, "slices_fresh": 0, "slices_drifted": 1,
        "structure_drifted": True, "active_but_drifted": [],
    }

    # 读出辅助闭环：按 remap 机械重提 → active 复活
    con = sqlite3.connect(out_db)
    ok = curate_mod.curate_payload(
        con, "slices", json.dumps([_slice([fid_new], name="old 线")], ensure_ascii=False)
    )
    assert ok["status"] == "accepted"
    ok_st = curate_mod.curate_payload(
        con, "structure", json.dumps(_structure([fid_new]), ensure_ascii=False)
    )
    assert ok_st["status"] == "accepted"
    con.close()

    # 内容不变再跑 etl：ledger 幂等（INSERT OR IGNORE），工件保持 active（复验无漂移）
    stats3 = etl.run_etl(repo, "proj", out_db=out_db)
    assert stats3["curate_stale_slices"] == 0
    assert stats3["curate_stale_structure"] == 0
    con = sqlite3.connect(out_db)
    assert {r[0] for r in con.execute("SELECT fid FROM fid_ledger")} == {fid_old, fid_new}
    assert con.execute("SELECT status FROM slices").fetchone()[0] == "active"
    assert con.execute("SELECT status FROM structure").fetchone()[0] == "active"
    report = curate_mod.drift_report(con)
    con.close()
    assert report["summary"]["active_but_drifted"] == []
    assert report["slices"][0]["fresh"] is True


def test_etl_hook_keeps_fresh_artifacts_active(tmp_path, monkeypatch):
    """无漂移 etl：active 工件原地保持 active（钩子零误伤）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name: cbm)
    out_db = repo / ".vrp" / "facts.db"

    etl.run_etl(repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    fid = con.execute("SELECT fid FROM symbols").fetchone()[0]
    con.close()
    con = sqlite3.connect(out_db)
    curate_mod.curate_payload(con, "slices", json.dumps([_slice([fid])], ensure_ascii=False))
    con.close()

    stats = etl.run_etl(repo, "proj", out_db=out_db)
    assert stats["curate_stale_slices"] == 0
    con = sqlite3.connect(out_db)
    assert con.execute("SELECT status FROM slices").fetchone()[0] == "active"
    con.close()
