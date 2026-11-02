import json
import random
import sqlite3
import string
from pathlib import Path

import pytest

from vrp import s5_dossier, s6_transact
from tests.deep.conftest import ROOT

SCHEMA = ROOT / "vrp" / "schema" / "facts_schema.sql"

pytestmark = pytest.mark.deep


def _write(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(payload, encoding="utf-8")
    return path


VALID_BASE = {
    "id": "fz-001",
    "type": "T2",
    "summary": "s",
    "edits": [{"file": "a.py", "op": "modify", "content": "x = 1\n"}],
}


@pytest.mark.parametrize(
    "payload",
    [
        "null",
        "[]",
        '"string"',
        "42",
        "{}",
        '{"id": "", "type": "T2", "edits": [{"file": "a", "op": "delete"}]}',
        '{"id": "   ", "type": "T2", "edits": [{"file": "a", "op": "delete"}]}',
        '{"id": 7, "type": "T2", "edits": []}',
        '{"id": ["x"], "type": "T2", "edits": []}',
        '{"id": "t", "edits": [{"file": "a", "op": "delete"}]}',
        '{"id": "t", "type": "t9", "edits": [{"file": "a", "op": "delete"}]}',
        '{"id": "t", "type": null, "edits": [{"file": "a", "op": "delete"}]}',
        '{"id": "t", "type": ["T1"], "edits": [{"file": "a", "op": "delete"}]}',
        '{"id": "t", "type": "T2"}',
        '{"id": "t", "type": "T2", "edits": null}',
        '{"id": "t", "type": "T2", "edits": "x"}',
        '{"id": "t", "type": "T2", "edits": {}}',
        '{"id": "t", "type": "T2", "edits": [null]}',
        '{"id": "t", "type": "T2", "edits": ["modify"]}',
        '{"id": "t", "type": "T2", "edits": [{"op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a", "op": "explode"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a", "op": "modify"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a", "op": "create", "content": 3}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": 9, "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": null, "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "C:\\\\abs.py", "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "/abs.py", "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "../up.py", "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a/../../b.py", "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "..\\\\..\\\\x.py", "op": "delete"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a.py", "op": "delete"}], "declared_moves": "all"}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a.py", "op": "delete"}], "declared_moves": [7]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a.py", "op": "delete"}], "declared_moves": [{"from": 1, "to": 2}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a.py", "op": "delete"}], "declared_moves": [{"from": "a"}]}',
        '{"id": "t", "type": "T2", "edits": [{"file": "a.py", "op": "delete"}], "declared_moves": [{"to": "b"}]}',
    ],
)
def test_load_transaction_rejects_fuzz(tmp_path, payload):
    path = _write(tmp_path, "tx.json", payload)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        s6_transact.load_transaction(path)


@pytest.mark.parametrize(
    "tid",
    [
        "ok-id",
        "a" * 500,
        "tx id with spaces",
        "tx/with/slashes",
        'quote"inside',
        "back\\slash",
        "中文名",
        "emoji-\U0001f600",
        "tab\there",
        "newline\nhere",
        "dots...",
        "-leading-dash",
        "trailing-.",
        "CON",
        "..",
        ".",
        "ctrl\x01char",
    ],
)
def test_tid_roundtrip_and_safety(tmp_path, tid):
    tx = dict(VALID_BASE, id=tid)
    path = _write(tmp_path, "tx.json", tx)
    loaded = s6_transact.load_transaction(path)
    assert loaded["id"] == tid.strip()
    safe = s6_transact.TID_SAFE.sub("_", loaded["id"])
    assert all(c in string.ascii_letters + string.digits + "._-_" for c in safe)
    assert "/" not in safe and "\\" not in safe and "\x00" not in safe


def test_load_transaction_accepts_unicode_and_deep_paths(tmp_path):
    tx = {
        "id": "fz-uni-001",
        "type": "T1",
        "summary": "日本語サマリー with \"quotes\" and \\backslashes\\",
        "declared_moves": [{"from": "p.old.name", "to": "p.new.name"}],
        "edits": [
            {"file": "pkg/sub/deep/mod.py", "op": "modify", "content": "x = '中文'\n"},
            {"file": "pkg/sub/deep/data.json", "op": "create", "content": '{"k": "v"}\n'},
        ],
    }
    loaded = s6_transact.load_transaction(_write(tmp_path, "tx.json", tx))
    assert loaded["summary"].startswith("日本語")
    assert loaded["edits"][0]["file"] == Path("pkg/sub/deep/mod.py")
    assert loaded["edits"][1]["content"] == '{"k": "v"}\n'
    assert loaded["declared_moves"][0]["to"] == "p.new.name"


def test_load_transaction_large_edits_array(tmp_path):
    edits = [{"file": f"pkg/m{i}.py", "op": "modify", "content": f"v = {i}\n"} for i in range(2000)]
    tx = dict(VALID_BASE, id="fz-big", edits=edits)
    loaded = s6_transact.load_transaction(_write(tmp_path, "tx.json", tx))
    assert len(loaded["edits"]) == 2000
    assert loaded["edits"][1999]["content"] == "v = 1999\n"


def _random_payload(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randrange(6 if depth < 3 else 4)
    if kind == 0:
        return "s" * rng.randrange(0, 4000)
    if kind == 1:
        return rng.randrange(-10**9, 10**9)
    if kind == 2:
        return [None, True, False, 3.5][rng.randrange(4)]
    if kind == 3:
        return None
    if kind == 4:
        return {f"k{rng.randrange(20)}": _random_payload(rng, depth + 1) for _ in range(rng.randrange(1, 6))}
    return [_random_payload(rng, depth + 1) for _ in range(rng.randrange(1, 8))]


@pytest.mark.parametrize("seed", range(30))
def test_fit_terminates_on_random_payloads(seed):
    rng = random.Random(seed)
    payload = {f"section-{i}": _random_payload(rng) for i in range(rng.randrange(1, 8))}
    fitted = s5_dossier._fit(payload, cap=2000)
    assert set(fitted.keys()) == set(payload.keys())
    assert len(json.dumps(fitted, ensure_ascii=False)) >= 0


@pytest.mark.parametrize(
    "payload,cap",
    [
        ({"a": "x" * 100000}, 500),
        ({"a": ["row"] * 10000}, 500),
        ({"a": ["x" * 1000, "y" * 1000, "z" * 1000]}, 300),
        ({"a": {"nested": ["deep", "list", "of", "strings"]}, "b": "s" * 99999}, 100),
        ({"huge": "x" * 5000, "stub": [1], "pairs": [[1], [2]]}, 200),
        ({"a": "x" * 300, "b": "y" * 300}, 200),
        ({f"k{i}": [i] * 10 for i in range(50)}, 1000),
    ],
)
def test_fit_terminates_and_capped_semantics(payload, cap):
    fitted = s5_dossier._fit(payload, cap=cap)
    assert set(fitted.keys()) == set(payload.keys())
    out_len = len(json.dumps(fitted, ensure_ascii=False))
    shrinkable = any(isinstance(v, list) and len(v) > 1 for v in payload.values())
    if shrinkable:
        assert out_len <= cap or all(
            not (isinstance(v, list) and len(v) > 1) for v in fitted.values()
        )


def _mk_con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def _seed_graph(con, nodes, edges):
    for fid, qn in nodes:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, content_hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (fid, "p", qn, "Function", qn.split(".")[-1], f"{fid}.py", "h" + fid),
        )
    for src, dst in edges:
        con.execute(
            "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,'CALLS','FZ')",
            (src, dst),
        )
    con.commit()


@pytest.mark.parametrize("seed", range(10))
def test_usage_edges_matches_seeded_graph(seed):
    rng = random.Random(1000 + seed)
    nodes = [(f"f{i}", f"p.mod.n{i}") for i in range(rng.randrange(5, 30))]
    edges = [
        (f"f{rng.randrange(len(nodes))}", f"f{rng.randrange(len(nodes))}")
        for _ in range(rng.randrange(5, 60))
    ]
    con = _mk_con()
    _seed_graph(con, nodes, edges)
    got = set(s6_transact._usage_edges(con))
    expected = set()
    for src, dst in edges:
        src_qn = next(qn for fid, qn in nodes if fid == src)
        dst_qn = next(qn for fid, qn in nodes if fid == dst)
        expected.add((src_qn, "CALLS", dst_qn))
    assert got == expected
    con.close()
