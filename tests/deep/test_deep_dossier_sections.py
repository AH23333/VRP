import sqlite3

import pytest

from vrp import s2_deadcode, s5_dossier
from vrp.etl import SCHEMA_PATH

pytestmark = pytest.mark.deep


def _make_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def _sym(con, fid, qn, label="Function", path="src/a.py", start_line=1, end_line=5, chash="h"):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (fid, "p", qn, label, qn.split(".")[-1], path, start_line, end_line, chash),
    )


def _edge(con, src, dst, etype="CALLS"):
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,?,?)",
        (src, dst, etype, "E"),
    )


def _hint(con, src, dst):
    con.execute(
        "INSERT INTO usage_hints (src_fid, dst_fid, properties_json) VALUES (?,?,?)",
        (src, dst, "{}"),
    )


def _test_link(con, test_fid, target_fid):
    con.execute(
        "INSERT INTO test_linkage (test_fid, target_fid, source, properties_json) VALUES (?,?,?,?)",
        (test_fid, target_fid, "guess", "{}"),
    )


def test_v_hotspots_hand_computed():
    con = _make_con()
    _sym(con, "a", "p.a", path="src/a.py")
    _sym(con, "b", "p.b", path="src/b.py")
    _sym(con, "t", "p.t", path="src/t.py")
    _edge(con, "b", "a")
    _edge(con, "a", "b")
    _edge(con, "a", "b", etype="MENTIONS")
    _hint(con, "b", "a")
    _test_link(con, "t", "a")
    rows = {
        r[0]: r
        for r in con.execute(
            "SELECT qualified_name, fan_in, fan_out, hint_in, tests, score FROM v_hotspots"
        )
    }
    assert rows["p.a"][1] == 1
    assert rows["p.a"][2] == 2
    assert rows["p.a"][3] == 1
    assert rows["p.a"][4] == 1
    assert rows["p.a"][5] == 3
    assert rows["p.b"][1] == 2
    assert rows["p.b"][2] == 1
    assert rows["p.b"][3] == 0
    assert rows["p.b"][5] == 2
    assert rows["p.t"][5] == 0


def test_v_hotspots_score_excludes_fan_out():
    con = _make_con()
    _sym(con, "caller", "p.caller", path="src/c.py")
    _sym(con, "callee", "p.callee", path="src/e.py")
    for _ in range(10):
        _edge(con, "caller", "callee")
    rows = {r[0]: r for r in con.execute("SELECT qualified_name, score FROM v_hotspots")}
    assert rows["p.caller"][1] == 0
    assert rows["p.callee"][1] == 10


def test_v_cycles_detects_mutual_pairs_only():
    con = _make_con()
    _sym(con, "a", "p.a", path="src/a.py")
    _sym(con, "b", "p.b", path="src/b.py")
    _sym(con, "c", "p.c", path="src/c.py")
    _edge(con, "a", "b")
    _edge(con, "b", "a")
    _edge(con, "b", "c")
    _edge(con, "a", "a")
    pairs = set(con.execute("SELECT fid_a, fid_b FROM v_cycles"))
    assert pairs == {("a", "b")}


def test_v_cycles_empty_when_acyclic():
    con = _make_con()
    _sym(con, "a", "p.a", path="src/a.py")
    _sym(con, "b", "p.b", path="src/b.py")
    _edge(con, "a", "b")
    assert list(con.execute("SELECT * FROM v_cycles")) == []


def _reference_sccs(edges):
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, [])
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []

    def strongconnect(root):
        nonlocal counter
        work = [(root, 0)]
        while work:
            node, ptr = work[-1]
            if ptr == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbors = adjacency[node]
            while ptr < len(neighbors):
                nxt = neighbors[ptr]
                ptr += 1
                if nxt not in index:
                    work[-1] = (node, ptr)
                    work.append((nxt, 0))
                    descended = True
                    break
                if nxt in on_stack and index[nxt] < low[node]:
                    low[node] = index[nxt]
            if descended:
                continue
            work.pop()
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
            if work:
                parent = work[-1][0]
                if low[node] < low[parent]:
                    low[parent] = low[node]

    for node in adjacency:
        if node not in index:
            strongconnect(node)
    return components


def test_scc_cross_impl_cycles_and_tarjan(big_repo):
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        edges = list(con.execute("SELECT src_fid, dst_fid FROM edges"))
        cycle_pairs = set(con.execute("SELECT fid_a, fid_b FROM v_cycles"))
        result = s5_dossier.strongly_connected(con, report_cap=10**6)
    finally:
        con.close()
    assert len(cycle_pairs) >= 5
    ref_components = _reference_sccs(edges)
    comp_of: dict[str, int] = {}
    for i, component in enumerate(ref_components):
        for fid in component:
            comp_of[fid] = i
    for fid_a, fid_b in cycle_pairs:
        assert fid_a in comp_of and fid_b in comp_of, f"cycle pair {fid_a} <-> {fid_b} outside any reference SCC"
        assert comp_of[fid_a] == comp_of[fid_b], f"cycle pair {fid_a} <-> {fid_b} spans two SCCs"
    assert result["scc_count"] == len(ref_components)
    assert result["max_size"] == max(len(c) for c in ref_components)
    reported_sizes = [entry["size"] for entry in result["largest"]]
    reference_sizes = sorted((len(c) for c in ref_components), reverse=True)
    assert reported_sizes == reference_sizes


def test_v_coupling_same_file_excluded_directional():
    con = _make_con()
    _sym(con, "a", "p.a", path="src/mod.py")
    _sym(con, "b", "p.b", path="src/mod.py")
    _sym(con, "c", "p.c", path="src/other.py")
    _edge(con, "a", "b")
    _edge(con, "b", "a")
    _edge(con, "a", "c")
    _edge(con, "c", "b")
    pairs = {(r[0], r[1]): r[2] for r in con.execute("SELECT file_a, file_b, edge_count FROM v_coupling")}
    assert pairs == {("src/mod.py", "src/other.py"): 1, ("src/other.py", "src/mod.py"): 1}


def test_v_coupling_rejects_empty_and_synthetic_paths():
    con = _make_con()
    _sym(con, "synthetic", "p.syn", path="")
    _sym(con, "fake", "p.fake", path="<synthetic>")
    _sym(con, "fake2", "p.fake2", path=">weird<")
    _sym(con, "real", "p.real", path="src/mod.py")
    _edge(con, "synthetic", "real")
    _edge(con, "fake", "real")
    _edge(con, "fake2", "real")
    _edge(con, "real", "synthetic")
    assert list(con.execute("SELECT * FROM v_coupling")) == []


def test_v_dup_clusters_grouping():
    con = _make_con()
    _sym(con, "a", "p.a", path="src/a.py", chash="dup1")
    _sym(con, "b", "p.b", path="src/b.py", chash="dup1")
    _sym(con, "c", "p.c", path="src/c.py", chash="dup1")
    _sym(con, "d", "p.d", path="src/d.py", chash="uniq")
    _sym(con, "e", "p.e", path="src/e.py", chash="")
    _sym(con, "f", "p.f", path="src/f.py", chash="")
    rows = list(con.execute("SELECT copies, files, content_hash FROM v_dup_clusters"))
    assert len(rows) == 1
    assert rows[0][0] == 3
    assert set(rows[0][1].split(",")) == {"src/a.py", "src/b.py", "src/c.py"}
    assert rows[0][2] == "dup1"


def test_dossier_sections_consume_views():
    con = _make_con()
    _sym(con, "a", "p.alpha", path="src/a.py")
    _sym(con, "b", "p.beta", path="src/b.py")
    _edge(con, "a", "b")
    _edge(con, "a", "b", etype="MENTIONS")
    hotspots = s5_dossier.hotspots(con, limit=10)
    assert hotspots[0]["qualified_name"] == "p.beta"
    assert hotspots[0]["fan_in"] == 2
    coupling = s5_dossier.coupling(con, limit=10)
    assert len(coupling) == 1
    assert coupling[0]["edge_count"] == 2
    dead = s5_dossier.dead_code(con, limit=10)
    names = {r["qualified_name"] for r in dead}
    assert names <= {"p.alpha", "p.beta"}


def test_mutual_cycles_pairs_sorted():
    con = _make_con()
    _sym(con, "z", "p.zeta", path="src/z.py")
    _sym(con, "y", "p.yota", path="src/y.py")
    _edge(con, "z", "y")
    _edge(con, "y", "z")
    cycles = s5_dossier.mutual_cycles(con, limit=10)
    assert len(cycles) == 1
    assert (cycles[0]["symbol_a"], cycles[0]["symbol_b"]) == ("p.yota", "p.zeta")


def test_dead_code_double_filter_alignment(big_repo):
    con = sqlite3.connect(f"file:{big_repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        view_rows = [
            {"qualified_name": qn, "label": label, "file_path": fp, "name": name}
            for qn, label, fp, name in con.execute(
                "SELECT qualified_name, label, file_path, name FROM v_dead_code"
            )
        ]
        filtered = [r for r in view_rows if s2_deadcode.is_candidate(r["name"], r["file_path"])]
        dossier_dead = s5_dossier.dead_code(con, limit=10**9)
    finally:
        con.close()
    assert [r["qualified_name"] for r in filtered] == [r["qualified_name"] for r in dossier_dead]
