import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from vrp import s5_dossier

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

VIEW_NAMES = {"v_hotspots", "v_cycles", "v_dead_code", "v_dup_clusters", "v_coupling"}


def _dump_facts(tmp_path: Path, con) -> Path:
    facts_db = tmp_path / "facts.db"
    dump = sqlite3.connect(facts_db)
    dump.executescript(SCHEMA.read_text(encoding="utf-8"))
    for row in con.execute("SELECT fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash FROM symbols"):
        dump.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
    for row in con.execute("SELECT src_fid, dst_fid, type, evidence FROM edges"):
        dump.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)", row)
    dump.commit()
    dump.close()
    return facts_db


@pytest.fixture
def con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fa','p','mod.a','Function','a','src/one.ts',1,2,'hash-1')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fb','p','mod.b','Function','b','src/two.ts',1,2,'hash-2')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fc','p','mod.c','Function','c','src/three.ts',1,2,'hash-1')"
    )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fb','CALLS','E2')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fb','fa','CALLS','E2')")
    con.commit()
    yield con
    con.close()


def test_views_created(con):
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    assert VIEW_NAMES <= names


def test_hotspots(con):
    rows = s5_dossier.hotspots(con)
    top = rows[0]
    assert top["qualified_name"] in ("mod.a", "mod.b")
    assert top["fan_in"] == 1
    assert top["fan_out"] == 1
    assert top["score"] == 1


def test_hotspots_fan_in_same_file(con):
    """P-3：fan_in_same_file/pct 区分同文件与跨文件 fan-in——CBM 跨文件同名
    符号合并计数假象（archify requestAnimationFrame fan-in 1000 而真实同文件
    调用 37）的辅助判读列。"""
    # fa 在 src/one.ts：已有入边来自 fb（src/two.ts，跨文件）
    # 再加同文件入边 fd（src/one.ts）→ fan_in=2，same_file=1，pct=50.0
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('fd','p','mod.d','Function','d','src/one.ts',1,2,'hash-4')"
    )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fd','fa','CALLS','E2')")
    con.commit()
    rows = s5_dossier.hotspots(con)
    top = next(r for r in rows if r["qualified_name"] == "mod.a")
    assert top["fan_in"] == 2
    assert top["fan_in_same_file"] == 1
    assert top["fan_in_same_file_pct"] == 50.0
    # 全跨文件符号：same_file=0，pct=0.0（MAX(fan_in,1) 防除零）
    other = next(r for r in rows if r["qualified_name"] == "mod.b")
    assert other["fan_in"] == 1
    assert other["fan_in_same_file"] == 0
    assert other["fan_in_same_file_pct"] == 0.0


def test_mutual_cycles(con):
    pairs = s5_dossier.mutual_cycles(con)
    assert pairs == [{"symbol_a": "mod.a", "symbol_b": "mod.b"}]


def test_strongly_connected(con):
    result = s5_dossier.strongly_connected(con)
    assert result["scc_count"] == 1
    assert result["largest"] == [{"size": 2, "members": ["mod.a", "mod.b"]}]


def test_dead_code_view(con):
    rows = s5_dossier.dead_code(con)
    assert [r["qualified_name"] for r in rows] == ["mod.c"]


def test_dead_code_entry_name_excluded(con):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
        " VALUES ('fm','p','mod.main','Function','main','src/main.ts','hash-m')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
        " VALUES ('ft','p','mod.test_helper','Function','test_helper','src/util.ts','hash-t')"
    )
    con.commit()
    names = [r["qualified_name"] for r in s5_dossier.dead_code(con)]
    assert "mod.main" not in names
    assert "mod.test_helper" not in names


def test_dup_clusters(con):
    clusters = s5_dossier.dup_clusters(con)
    assert len(clusters) == 1
    assert clusters[0]["copies"] == 2
    assert set(clusters[0]["members"].split("|")) == {"mod.a", "mod.c"}


def test_coupling(con):
    rows = s5_dossier.coupling(con)
    assert len(rows) == 2
    pair = {(r["file_a"], r["file_b"]) for r in rows}
    assert pair == {("src/one.ts", "src/two.ts"), ("src/two.ts", "src/one.ts")}
    assert all(r["edge_count"] == 1 for r in rows)


def test_fit_trims_longest_list():
    payload = {"small": "x", "big": [f"row-{i}" for i in range(500)]}
    fitted = s5_dossier._fit(payload, cap=2000)
    assert len(json.dumps(fitted, ensure_ascii=False)) <= 2000
    assert fitted["small"] == "x"
    assert len(fitted["big"]) < 500


def test_fit_terminates_when_unshrinkable():
    payload = {"huge": "x" * 5000, "stub": [1]}
    fitted = s5_dossier._fit(payload, cap=200)
    assert fitted["huge"] == "x" * 5000
    assert fitted["stub"] == [1]


def test_fit_noop_when_small():
    payload = {"a": [1, 2, 3]}
    assert s5_dossier._fit(payload, cap=1000) is payload


def test_build_dossier(tmp_path, con):
    facts_db = _dump_facts(tmp_path, con)
    dossier = s5_dossier.build_dossier(
        tmp_path,
        facts_db,
        "p",
        budget=32000,
        architecture={"modules": ["one", "two"]},
        skip_external=True,
    )
    assert dossier["project"] == "p"
    assert dossier["task"] == "T4"
    assert dossier["views"]["architecture"] == {"modules": ["one", "two"]}
    assert dossier["views"]["sql_views"]["hotspots"][0]["qualified_name"].startswith("mod.")
    assert dossier["views"]["summary"]["counts"]["symbols"] == 3
    assert dossier["views"]["repo_map_brief"]["status"] == "skipped"
    assert dossier["views"]["raw_pack"]["status"] == "skipped"
    assert dossier["views"]["repomap"]["files_included"] >= 1
    assert dossier["out"].endswith("dossier.json")
    persisted = json.loads((tmp_path / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert persisted["project"] == "p"
    check = sqlite3.connect(facts_db)
    rows = check.execute("SELECT view FROM dossier_views ORDER BY rowid").fetchall()
    check.close()
    assert [r[0] for r in rows] == list(s5_dossier.SECTION_ORDER)


def test_build_dossier_unknown_task(tmp_path):
    with pytest.raises(ValueError, match="unknown task"):
        s5_dossier.build_dossier(tmp_path, tmp_path / "facts.db", "p", task="T9")


def test_task_profiles_complete_and_balanced():
    for task, profile in s5_dossier.TASK_PROFILES.items():
        assert sum(profile.values()) == pytest.approx(1.0), task
        assert set(profile) == set(s5_dossier.SECTION_ORDER), task
        assert all(share > 0 for share in profile.values()), task


def test_cap_brief_truncates():
    brief = {"status": "ok", "text": "x" * 3000, "chars": 3000}
    capped = s5_dossier._cap_brief(brief, 100)
    assert capped["text"].endswith("...[truncated]")
    assert len(capped["text"]) < 130
    assert capped["truncated"] is True
    assert capped["chars"] == 3000


def test_cap_brief_noop():
    brief = {"status": "ok", "text": "short", "chars": 5}
    assert s5_dossier._cap_brief(brief, 100) is brief
    failed = {"status": "failed", "reason": "x"}
    assert s5_dossier._cap_brief(failed, 100) is failed


def test_build_dossier_t3_raw_pack_pointer(tmp_path, con, monkeypatch):
    facts_db = _dump_facts(tmp_path, con)
    monkeypatch.setattr("vrp.s5_dossier.get_architecture", lambda name: {"modules": []})
    monkeypatch.setattr(
        "vrp.s5_dossier.s5_cabrief.repo_map_brief",
        lambda repo, max_tokens=4000, timeout=120: {"status": "ok", "text": "map text", "chars": 8, "max_tokens": max_tokens},
    )
    monkeypatch.setattr(
        "vrp.s5_dossier.s5_rawpack.build_raw_pack",
        lambda repo, timeout=600: {"status": "ok", "path": str(repo / ".vrp" / "repomix-output.xml"), "bytes": 1024, "sha256": "ab12"},
    )
    dossier = s5_dossier.build_dossier(tmp_path, facts_db, "p", budget=32000, task="T3")
    raw = dossier["views"]["raw_pack"]
    assert raw["status"] == "ok"
    assert raw["recommended_read_chars"] == max(1, int(32000 * s5_dossier.TASK_PROFILES["T3"]["raw_pack"]))
    brief = dossier["views"]["repo_map_brief"]
    assert brief["status"] == "ok"
    assert brief["max_tokens"] == max(200, min(20000, int(32000 * s5_dossier.TASK_PROFILES["T3"]["repo_map_brief"]) // 4))


def _no_arch(monkeypatch):
    monkeypatch.setattr("vrp.s5_dossier.get_architecture", lambda name: {"modules": []})


def test_build_dossier_arch_failure_degrades(tmp_path, con, monkeypatch):
    """v6 degradation contract: CBM get_architecture failure downgrades the
    architecture section to {"status": "failed"}; the DB-derived sections
    still assemble ("failed" is never conflated with "skipped")."""
    facts_db = _dump_facts(tmp_path, con)

    def boom(name):
        raise RuntimeError("cbm daemon down")

    monkeypatch.setattr("vrp.s5_dossier.get_architecture", boom)
    dossier = s5_dossier.build_dossier(tmp_path, facts_db, "p", skip_external=False)
    arch = dossier["views"]["architecture"]
    assert arch["status"] == "failed"
    assert "cbm daemon down" in arch["reason"]
    assert dossier["views"]["sql_views"]["hotspots"][0]["qualified_name"] == "mod.a"
    assert dossier["views"]["repomap"]["files_included"] >= 1


def test_build_dossier_raw_pack_timeout_degrades(tmp_path, con, monkeypatch):
    """repomix TimeoutExpired re-raised by s5_rawpack must not kill the dossier."""
    facts_db = _dump_facts(tmp_path, con)
    _no_arch(monkeypatch)

    def boom(repo, timeout=600):
        raise subprocess.TimeoutExpired(cmd="repomix", timeout=timeout)

    monkeypatch.setattr("vrp.s5_dossier.s5_rawpack.build_raw_pack", boom)
    dossier = s5_dossier.build_dossier(tmp_path, facts_db, "p", skip_external=False)
    raw = dossier["views"]["raw_pack"]
    assert raw["status"] == "failed"
    assert "repomix" in raw["reason"]
    assert dossier["views"]["sql_views"]["hotspots"][0]["qualified_name"] == "mod.a"
    assert (tmp_path / ".vrp" / "dossier.json").is_file()


def test_build_dossier_raw_pack_oserror_degrades(tmp_path, con, monkeypatch):
    facts_db = _dump_facts(tmp_path, con)
    _no_arch(monkeypatch)

    def boom(repo, timeout=600):
        raise OSError("repomix bin disappeared")

    monkeypatch.setattr("vrp.s5_dossier.s5_rawpack.build_raw_pack", boom)
    dossier = s5_dossier.build_dossier(tmp_path, facts_db, "p", skip_external=False)
    raw = dossier["views"]["raw_pack"]
    assert raw["status"] == "failed"
    assert "disappeared" in raw["reason"]


def test_build_dossier_cabrief_oserror_degrades(tmp_path, con, monkeypatch):
    facts_db = _dump_facts(tmp_path, con)
    _no_arch(monkeypatch)

    def boom(repo, max_tokens=4000, timeout=120):
        raise OSError("node spawn failed")

    monkeypatch.setattr("vrp.s5_dossier.s5_cabrief.repo_map_brief", boom)
    dossier = s5_dossier.build_dossier(tmp_path, facts_db, "p", skip_external=False)
    brief = dossier["views"]["repo_map_brief"]
    assert brief["status"] == "failed"
    assert "node spawn failed" in brief["reason"]
