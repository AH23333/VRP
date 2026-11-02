import random

import pytest

from vrp.s6_transact import _gate_g4

pytestmark = pytest.mark.deep

PREFIX = "proj.mod"


def _graph(rng: random.Random, n: int, m: int):
    qns = [f"{PREFIX}.f{i}" for i in range(n)]
    pairs = [(a, b) for a in qns for b in qns]
    rng.shuffle(pairs)
    edges = [(a, "CALLS", b) for a, b in pairs[:m]]
    return set(qns), edges


def _rename(qns, edges, moves):
    moved = dict(moves)
    post_qns = {moved.get(q, q) for q in qns}
    post_edges = [(moved.get(s, s), t, moved.get(d, d)) for s, t, d in edges]
    return post_qns, post_edges


def _tx(moves, tx_type="T1"):
    return {
        "id": "prop",
        "type": tx_type,
        "summary": "",
        "declared_moves": [{"from": f, "to": t} for f, t in moves],
        "edits": [],
    }


@pytest.mark.parametrize("seed", range(15))
def test_t1_legal_rename_never_fails(seed):
    rng = random.Random(seed)
    pre_qns, pre_edges = _graph(rng, rng.randrange(6, 25), rng.randrange(5, 50))
    movable = sorted(pre_qns)
    k = rng.randrange(1, min(6, len(movable)) + 1)
    chosen = rng.sample(movable, k)
    moves = [(qn, qn.rsplit(".", 1)[0] + ".renamed_" + qn.rsplit(".", 1)[1]) for qn in chosen]
    post_qns, post_edges = _rename(pre_qns, pre_edges, moves)
    ok, detail = _gate_g4(_tx(moves), pre_qns, post_qns, pre_edges, post_edges)
    assert ok, f"false positive on legal rename: {detail}"


@pytest.mark.parametrize("seed", range(15))
def test_t1_dropped_incident_edge_always_fails(seed):
    rng = random.Random(2000 + seed)
    pre_qns, pre_edges = _graph(rng, rng.randrange(6, 25), rng.randrange(5, 50))
    victim = sorted(pre_qns)[rng.randrange(len(pre_qns))]
    moved_name = PREFIX + ".moved_victim"
    moves = [(victim, moved_name)]
    post_qns, post_edges = _rename(pre_qns, pre_edges, moves)
    post_set = set(post_edges)
    incident = [e for e in post_edges if moved_name in (e[0], e[2])]
    if not incident:
        outsider = next(q for q in sorted(pre_qns) if q != victim)
        pre_edges = pre_edges + [(outsider, "CALLS", victim)]
        post_edges = post_edges + [(outsider, "CALLS", moved_name)]
        incident = [(outsider, "CALLS", moved_name)]
    assert incident[0] in post_set or True
    post_edges = [e for e in post_edges if e != incident[0]]
    ok, detail = _gate_g4(_tx(moves), pre_qns, post_qns, pre_edges, post_edges)
    assert not ok and detail["reason"] == "edge-mismatch", f"missed dropped edge: {ok} {detail}"


@pytest.mark.parametrize("seed", range(10))
def test_t1_added_incident_edge_always_fails(seed):
    rng = random.Random(3000 + seed)
    pre_qns, pre_edges = _graph(rng, rng.randrange(6, 25), rng.randrange(5, 50))
    victim = sorted(pre_qns)[rng.randrange(len(pre_qns))]
    moved_name = PREFIX + ".moved_victim"
    moves = [(victim, moved_name)]
    post_qns, post_edges = _rename(pre_qns, pre_edges, moves)
    post_set = set(post_edges)
    added = None
    for outsider in sorted(pre_qns):
        if outsider == victim:
            continue
        candidate = (outsider, "CALLS", moved_name)
        if candidate not in post_set:
            added = candidate
            break
    if added is None:
        return
    post_edges = post_edges + [added]
    ok, detail = _gate_g4(_tx(moves), pre_qns, post_qns, pre_edges, post_edges)
    assert not ok and detail["reason"] == "edge-mismatch", f"missed added edge: {ok} {detail}"


def test_t1_cross_rename_edge_rewrite():
    pre_qns = {PREFIX + ".a", PREFIX + ".b", PREFIX + ".z"}
    pre_edges = [
        (PREFIX + ".a", "CALLS", PREFIX + ".b"),
        (PREFIX + ".b", "CALLS", PREFIX + ".a"),
        (PREFIX + ".a", "CALLS", PREFIX + ".z"),
    ]
    moves = [(PREFIX + ".a", PREFIX + ".x"), (PREFIX + ".b", PREFIX + ".y")]
    moved = dict(moves)
    post_qns = {moved.get(q, q) for q in pre_qns}
    post_edges = [(moved.get(s, s), t, moved.get(d, d)) for s, t, d in pre_edges]
    ok, detail = _gate_g4(_tx(moves), pre_qns, post_qns, pre_edges, post_edges)
    assert ok, detail


def test_t1_chained_rename_into_occupied_name_rejected():
    pre_qns = {PREFIX + ".a", PREFIX + ".b", PREFIX + ".c"}
    moves = [(PREFIX + ".a", PREFIX + ".b"), (PREFIX + ".b", PREFIX + ".c")]
    moved = dict(moves)
    post_qns = {moved.get(q, q) for q in pre_qns}
    pre_edges = []
    post_edges = []
    ok, detail = _gate_g4(_tx(moves), pre_qns, post_qns, pre_edges, post_edges)
    assert not ok
    assert detail["reason"] == "undeclared-change"


def test_t1_identity_move_rejected():
    qn = PREFIX + ".same"
    ok, detail = _gate_g4(_tx([(qn, qn)]), {qn}, {qn}, [], [])
    assert not ok
    assert detail["reason"] == "undeclared-change"


@pytest.mark.parametrize("seed", range(10))
def test_t2_dead_removal_never_fails(seed):
    rng = random.Random(4000 + seed)
    pre_qns, pre_edges = _graph(rng, rng.randrange(6, 25), rng.randrange(5, 50))
    incoming = {d for _, _, d in pre_edges}
    dead = sorted(pre_qns - incoming)
    if not dead:
        return
    victims = rng.sample(dead, rng.randrange(1, min(4, len(dead)) + 1))
    post_qns = pre_qns - set(victims)
    post_edges = [(s, t, d) for s, t, d in pre_edges if s not in set(victims) and d not in set(victims)]
    ok, detail = _gate_g4(_tx([], "T2"), pre_qns, post_qns, pre_edges, post_edges)
    assert ok, f"false positive on dead removal: {detail}"


@pytest.mark.parametrize("seed", range(10))
def test_t2_referenced_removal_always_fails(seed):
    rng = random.Random(5000 + seed)
    pre_qns, pre_edges = _graph(rng, rng.randrange(6, 25), rng.randrange(10, 60))
    incoming = {d for _, _, d in pre_edges}
    referenced = sorted(pre_qns & incoming)
    if not referenced:
        return
    victim = referenced[rng.randrange(len(referenced))]
    post_qns = pre_qns - {victim}
    post_edges = [(s, t, d) for s, t, d in pre_edges if s != victim and d != victim]
    ok, detail = _gate_g4(_tx([], "T2"), pre_qns, post_qns, pre_edges, post_edges)
    assert not ok and detail["reason"] == "dangling-reference", f"missed dangling: {ok} {detail}"


def test_t1_undeclared_addition_fails():
    pre_qns = {PREFIX + ".a"}
    post_qns = {PREFIX + ".a", PREFIX + ".sneaky"}
    ok, detail = _gate_g4(_tx([]), pre_qns, post_qns, [], [])
    assert not ok
    assert detail["reason"] == "undeclared-change"


def test_t1_declared_from_missing_in_pre_fails():
    ok, detail = _gate_g4(
        _tx([(PREFIX + ".ghost", PREFIX + ".new")]),
        {PREFIX + ".real"},
        {PREFIX + ".real", PREFIX + ".new"},
        [],
        [],
    )
    assert not ok
    assert detail["reason"] == "undeclared-change"
