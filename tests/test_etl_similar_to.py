# -*- coding: utf-8 -*-
"""#139 覆盖面：similar_to findings 必须去规范化锚点位置（file_path/start_line/end_line）。

tsa 实测 5519/15741 findings 因 file_path NULL 永不可链——etl 插入漏填
锚点符号位置；修复 = INSERT..SELECT 与符号行同源。
"""
import sqlite3

from vrp import etl


def _mk_cbm(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO store_meta VALUES (?,?)",
                    [("db_uid", "uid-1"), ("mutation_gen", "7")])
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, properties TEXT)"
    )
    con.execute("INSERT INTO nodes VALUES (1,'proj','Function','go','p.a.go','a.py',1,3,'{}')")
    con.execute("INSERT INTO nodes VALUES (2,'proj','Function','h','p.b.h','b.py',5,6,'{}')")
    con.execute("CREATE TABLE edges (source_id INT, target_id INT, type TEXT,"
                " properties TEXT, project TEXT)")
    con.execute("INSERT INTO edges VALUES (1,2,'SIMILAR_TO','{\"jaccard\":0.8}','proj')")
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("CREATE TABLE file_hashes (project TEXT, rel_path TEXT, sha256 TEXT)")
    con.commit()
    con.close()


def test_similar_to_finding_denormalizes_anchor_location(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def go():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def h():\n    pass\n", encoding="utf-8")
    cbm = tmp_path / "proj.db"
    _mk_cbm(cbm)
    monkeypatch.setattr(etl.s1_cbm, "cbm_db_path", lambda name, repo=None: cbm)
    out_db = repo / ".vrp" / "facts.db"
    etl.run_etl(repo, "proj", out_db=out_db)
    con = sqlite3.connect(out_db)
    try:
        rows = con.execute(
            "SELECT pathology, kind, source, file_path, start_line, end_line,"
            " anchor_fid, related_fid, payload_json FROM findings").fetchall()
        assert len(rows) == 1, "SIMILAR_TO 边必须落 1 条 finding"
        r = rows[0]
        assert r[0] == "P2" and r[1] == "similar_to" and r[2] == "etl"
        assert (r[3], r[4], r[5]) == ("a.py", 1, 3), "锚点位置必须取符号行（原 None/None/None）"
        # 锚点/对端均可解析回符号且锚点即位置来源
        anchor_fp = con.execute("SELECT file_path FROM symbols WHERE fid = ?",
                                (r[6],)).fetchone()
        assert anchor_fp is not None and anchor_fp[0] == "a.py"
        assert con.execute("SELECT COUNT(*) FROM symbols WHERE fid = ?",
                           (r[7],)).fetchone()[0] == 1
        assert "jaccard" in r[8]
    finally:
        con.close()
