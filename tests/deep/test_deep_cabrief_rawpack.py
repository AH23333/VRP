import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, run_vrp
from vrp import s5_cabrief, s5_dossier, s5_rawpack

pytestmark = pytest.mark.deep


@pytest.fixture(scope="module")
def cr_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-cabrief-rawpack")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "test_core.py").write_text(
        "from pkg.core import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


def _dossier_views(repo: Path) -> dict:
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT view, COUNT(*) FROM dossier_views GROUP BY view").fetchall()
        return {view: count for view, count in rows}
    finally:
        con.close()


def test_dossier_views_idempotent_rerun(cr_repo):
    repo = cr_repo
    facts_db = repo / ".vrp" / "facts.db"
    for _ in range(2):
        s5_dossier.build_dossier(
            repo,
            facts_db,
            repo.name,
            budget=8000,
            task="T2",
            architecture={"stub": True},
            skip_external=True,
        )
    counts = _dossier_views(repo)
    assert counts == {section: 1 for section in s5_dossier.SECTION_ORDER}
    persisted = json.loads((repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{facts_db}?mode=ro", uri=True)
    try:
        for section in s5_dossier.SECTION_ORDER:
            rows = con.execute(
                "SELECT payload_json FROM dossier_views WHERE view = ?", (section,)
            ).fetchall()
            assert len(rows) == 1
            assert json.loads(rows[0][0]) == persisted["views"][section]
    finally:
        con.close()


def test_cabrief_real_mcp_lusiem(lusiem_repo):
    brief = s5_cabrief.repo_map_brief(lusiem_repo)
    assert brief["status"] == "ok"
    assert brief["chars"] > 1000
    assert brief["max_tokens"] == 4000
    text = brief["text"]
    assert "files" in text and "symbols" in text


def test_cabrief_big_repo_real_mcp(big_repo):
    if not (big_repo / ".code-atlas" / "index.db").exists():
        pytest.skip("big repo code-atlas index not present")
    brief = s5_cabrief.repo_map_brief(big_repo, max_tokens=2000, timeout=60)
    assert brief["status"] == "ok"
    assert brief["chars"] > 1000
    assert "python" in brief["text"]


def test_rawpack_big_repo_deterministic(big_repo):
    out = big_repo / ".vrp" / "repomix-output.xml"
    pre_sha = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
    result = s5_rawpack.build_raw_pack(big_repo)
    assert result["status"] == "ok"
    assert result["bytes"] > 30_000_000
    assert result["bytes"] == out.stat().st_size
    assert result["sha256"] == pre_sha
    assert len(result["sha256"]) == 16


def test_rawpack_big_repo_excludes_workdirs(big_repo):
    xml = (big_repo / ".vrp" / "repomix-output.xml").read_bytes()
    assert b".vrp/" not in xml
    assert xml.count(b'<file path=".code-atlas') == 0
    assert xml.count(b'<file path=".codebase-memory') == 0


def test_dossier_big_repo_full_external_t3(big_repo):
    stdout = run_vrp(["dossier", str(big_repo), "--task", "T3"], cwd=ROOT, timeout=1200)
    summary = json.loads(stdout.split("== S5 dossier ==")[1].strip())
    assert summary["task"] == "T3"
    assert summary["project"] == "tree-sitter-analyzer"
    assert summary["budget"] == 32000
    persisted = json.loads((big_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    views = persisted["views"]
    arch = views["architecture"]
    assert arch["project"] == "tree-sitter-analyzer"
    assert arch["total_nodes"] == 66176
    assert views["repo_map_brief"]["status"] == "ok"
    assert views["raw_pack"]["status"] == "ok"
    assert views["raw_pack"]["recommended_read_chars"] == 9600
    assert views["raw_pack"]["bytes"] == (big_repo / ".vrp" / "repomix-output.xml").stat().st_size
    assert views["repomap"]["files_included"] >= 1
    assert set(views["sql_views"]) == {
        "hotspots",
        "mutual_cycles",
        "strongly_connected",
        "dead_code",
        "coupling",
        "dup_clusters",
    }
    assert views["summary"]["counts"]["symbols"] > 1000
    counts = _dossier_views(big_repo)
    assert counts == {section: 1 for section in s5_dossier.SECTION_ORDER}
