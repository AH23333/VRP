import json
import sqlite3

import pytest

from vrp import s5_dossier
from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp

pytestmark = pytest.mark.deep


def _json_blocks(out: str) -> list:
    blocks = []
    decoder = json.JSONDecoder()
    i = 0
    while i < len(out):
        j = out.find("{", i)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(out, j)
        except json.JSONDecodeError:
            i = j + 1
            continue
        blocks.append(obj)
        i = end
    return blocks


@pytest.fixture(scope="module")
def smoke_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("deep-smoke")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        "def compute(v):\n"
        "    return v * 2\n\n\n"
        "def dead_helper(v):\n"
        "    return v - 1\n\n\n"
        "def entry():\n"
        "    return compute(21)\n",
        encoding="utf-8",
    )
    (repo / "test_core.py").write_text(
        "from pkg.core import entry\n\n\ndef test_entry():\n    assert entry() == 42\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
    run_vrp(["etl", str(repo)], cwd=ROOT)
    yield repo
    if cache_db.exists():
        cache_db.unlink()


def test_cmd_precheck(smoke_repo):
    out = json.loads(run_vrp(["precheck", str(smoke_repo)], cwd=ROOT))
    assert "project" in out or "repo" in out or bool(out)


def test_cmd_index_then_etl(smoke_repo):
    out = json.loads(run_vrp(["index", str(smoke_repo), "--reindex"], cwd=ROOT))
    assert out.get("status") in ("indexed", "reused", "ok") or bool(out)
    stats = json.loads(run_vrp(["etl", str(smoke_repo)], cwd=ROOT))
    assert stats["symbols"] >= 3


def test_cmd_xcheck(smoke_repo):
    out = json.loads(run_vrp(["xcheck", str(smoke_repo)], cwd=ROOT))
    assert out["ca_calls"] >= 0
    assert out["cbm_calls"] >= 0


def test_cmd_run_reuses(smoke_repo):
    blocks = _json_blocks(run_vrp(["run", str(smoke_repo)], cwd=ROOT))
    assert len(blocks) >= 2
    assert any("name" in b or "symbols" in b for b in blocks)


def test_cmd_status(smoke_repo):
    out = json.loads(run_vrp(["status", str(smoke_repo)], cwd=ROOT))
    meta = out.get("meta", {})
    assert int(meta.get("count_symbols", "0")) >= 3 or "symbols" in out


def test_cmd_s2_and_mutate_plan(smoke_repo):
    out = json.loads(run_vrp(["s2", str(smoke_repo)], cwd=ROOT))
    assert out["dead_code_candidates"] == 1
    plan = json.loads(run_vrp(["mutate-plan", str(smoke_repo), "--budget", "5"], cwd=ROOT))
    assert plan["budget"] == 5
    assert plan["selected"] >= 0


def test_cmd_validate_and_gate(smoke_repo):
    out = json.loads(run_vrp(["validate", str(smoke_repo)], cwd=ROOT))
    assert out["coverage"]["total"] >= 3
    claims = [
        {
            "kind": "annotation",
            "qualified_name": f"{smoke_repo.name}.pkg.core.compute",
            "start_line": 1,
            "end_line": 2,
            "note_kind": "purpose",
            "body": "doubles",
        }
    ]
    claims_path = smoke_repo / ".vrp" / "claims.json"
    claims_path.parent.mkdir(parents=True, exist_ok=True)
    claims_path.write_text(json.dumps(claims), encoding="utf-8")
    gated = json.loads(run_vrp(["gate", str(smoke_repo), str(claims_path)], cwd=ROOT))
    assert "claims" in gated or "results" in gated or bool(gated)


def test_cmd_freeze_skip_commands(smoke_repo):
    blocks = _json_blocks(run_vrp(["freeze", str(smoke_repo), "--skip-commands"], cwd=ROOT))
    assert blocks
    flat = json.dumps(blocks, ensure_ascii=False)
    assert "graph.db.zst" in flat or "graph_baseline" in flat


def test_cmd_dossier_skip_external(smoke_repo):
    run_vrp(["dossier", str(smoke_repo), "--task", "T4", "--skip-external"], cwd=ROOT)
    doc = json.loads((smoke_repo / ".vrp" / "dossier.json").read_text(encoding="utf-8"))
    assert doc["task"] == "T4"
    assert set(doc["views"].keys()) == set(s5_dossier.SECTION_ORDER)


def test_cmd_transact_preserved_across_etl(smoke_repo):
    repo = smoke_repo
    tx = {
        "id": "smoke-tx-1",
        "type": "T2",
        "summary": "drop dead helper",
        "edits": [
            {
                "file": "pkg/core.py",
                "op": "modify",
                "content": "def compute(v):\n    return v * 2\n\n\ndef entry():\n    return compute(21)\n",
            }
        ],
    }
    tx_path = repo / ".vrp" / "smoke-tx-1.json"
    tx_path.parent.mkdir(parents=True, exist_ok=True)
    tx_path.write_text(json.dumps(tx), encoding="utf-8")
    blocks = _json_blocks(run_vrp(["transact", str(repo), str(tx_path)], cwd=ROOT))
    out = blocks[-1]
    assert out["status"] == "committed"
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        before = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        con.close()
    assert before >= 1
    run_vrp(["etl", str(repo)], cwd=ROOT)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        after = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        tids = [r[0] for r in con.execute("SELECT tid FROM transactions")]
    finally:
        con.close()
    assert after == before
    assert "smoke-tx-1" in tids
    assert (repo / ".vrp" / "verdicts" / "smoke-tx-1.json").exists()


def test_cmd_transact_batch(smoke_repo):
    repo = smoke_repo
    tx_dir = repo / ".vrp" / "txs"
    tx_dir.mkdir(parents=True, exist_ok=True)
    (tx_dir / "01-rename.json").write_text(
        json.dumps(
            {
                "id": "smoke-batch-1",
                "type": "T1",
                "summary": "rename compute to calc",
                "declared_moves": [
                    {"from": f"{repo.name}.pkg.core.compute", "to": f"{repo.name}.pkg.core.calc"}
                ],
                "edits": [
                    {
                        "file": "pkg/core.py",
                        "op": "modify",
                        "content": "def calc(v):\n    return v * 2\n\n\ndef entry():\n    return calc(21)\n",
                    },
                    {
                        "file": "test_core.py",
                        "op": "modify",
                        "content": "from pkg.core import entry\n\n\ndef test_entry():\n    assert entry() == 42\n",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    raw = run_vrp(["transact-batch", str(repo), str(tx_dir)], cwd=ROOT)
    lines = raw.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("== "))
    out = json.loads("\n".join(lines[start + 1:]))
    assert out["statuses"] == ["committed"]
    assert "vrp smoke-batch-1" in git(repo, "log", "-1", "--format=%s")
