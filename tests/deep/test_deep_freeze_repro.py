import hashlib
import json
import sqlite3

import pytest

from vrp import etl as etl_mod, s1_cbm, s4_freeze
from tests.deep.conftest import git

pytestmark = pytest.mark.deep


@pytest.fixture()
def freeze_repo(tmp_path):
    repo = tmp_path / "freeze-repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    s1_cbm.index_repository(repo, repo.name, reindex=True)
    etl_mod.run_etl(repo, repo.name)
    return repo


def _zst_sha(repo) -> tuple[str, int]:
    zst = repo / ".codebase-memory" / "graph.db.zst"
    assert zst.exists(), "graph.db.zst missing after freeze"
    data = zst.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def test_freeze_graph_zst_byte_reproducible(freeze_repo):
    repo = freeze_repo
    facts = repo / ".vrp" / "facts.db"
    s1 = s4_freeze.freeze(repo, facts, repo.name, skip_commands=True, graph_timeout=300)
    h1, n1 = _zst_sha(repo)
    s2 = s4_freeze.freeze(repo, facts, repo.name, skip_commands=True, graph_timeout=300)
    h2, n2 = _zst_sha(repo)
    assert (h1, n1) == (h2, n2), f"graph.db.zst not byte-reproducible: {h1[:12]} vs {h2[:12]}"


def test_freeze_summary_structure(freeze_repo):
    repo = freeze_repo
    facts = repo / ".vrp" / "facts.db"
    summary = s4_freeze.freeze(repo, facts, repo.name, skip_commands=True, graph_timeout=300)
    assert "frozen_at" in summary
    assert "git" in summary
    assert summary["frozen_at"]
    zst = repo / ".codebase-memory" / "graph.db.zst"
    assert zst.stat().st_size > 0


def test_freeze_does_not_touch_facts_owned_tables(freeze_repo):
    repo = freeze_repo
    facts = repo / ".vrp" / "facts.db"
    con = sqlite3.connect(facts)
    try:
        before = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    finally:
        con.close()
    s4_freeze.freeze(repo, facts, repo.name, skip_commands=True, graph_timeout=300)
    con = sqlite3.connect(facts)
    try:
        after = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    finally:
        con.close()
    assert before == after > 0


def test_freeze_written_artifacts_git_clean_exempt(freeze_repo):
    repo = freeze_repo
    facts = repo / ".vrp" / "facts.db"
    s4_freeze.freeze(repo, facts, repo.name, skip_commands=True, graph_timeout=300)
    status = git(repo, "status", "--porcelain")
    leftover = [
        line
        for line in status.splitlines()
        if line.strip()
        and not any(
            line[3:].startswith(p)
            for p in (".vrp/", ".codebase-memory/", ".code-atlas/")
        )
    ]
    assert leftover == [], f"freeze left non-exempt dirt: {leftover}"
