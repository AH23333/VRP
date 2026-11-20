"""F15+BB3 deep 真值矩阵（批⑥ #204）：go/ts s7 真工具链审计 + py/ts ETL
BB3 审计。

go/ts：外链工具 skip-if-missing（find_go/find_depcruise，stryker/work
沙箱先例）；真链产 findings 后对 committed golden 双向计数。py/ts：
run_vrp index+etl 真链（deep conftest 模式）建 facts.db——被审计面由
被审计者自产（非 seed）；symbols 面 pin 观测诚实值（D7——File 行结构
簿记显形为 spurious，不装 100%；「手工改过的 golden 不落盘=自欺」）。
"""

import sqlite3
from pathlib import Path

import pytest

from tests.deep.conftest import CBM_CACHE, ROOT, git, run_vrp
from vrp import golden, s7_enforce, s7_enforce_go, s7_enforce_ts

pytestmark = pytest.mark.deep

GOLDEN = Path(__file__).parent.parent / "golden"


def _s7_matrix(tmp_path, lang: str, engine: str, finder) -> None:
    if finder() is None:
        pytest.skip(f"{engine} toolchain not found")
    spec = golden.load_golden(GOLDEN / "s7" / f"{lang}.json")
    repo = golden.write_fixture_files(spec, tmp_path)
    golden.seed_fixture_db(spec, repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con, timeout=180)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == engine
    assert out["violations"] == len(spec["truth"]["violations"])
    con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
    try:
        v_face = golden.audit_violations(con, spec["truth"]["violations"], "s7-enforce")
        s_face = golden.audit_symbols(con, spec["truth"]["symbols"])
    finally:
        con.close()
    assert (v_face["matched"], v_face["missing"], v_face["spurious"]) == (1, 0, 0)
    assert v_face["precision"] == 1.0
    assert v_face["recall"] == 1.0
    assert (s_face["matched"], s_face["missing"], s_face["spurious"]) == (2, 0, 0)


def test_deep_golden_go_s7(tmp_path):
    _s7_matrix(tmp_path, "go", "go-list", s7_enforce_go.find_go)


def test_deep_golden_ts_s7(tmp_path):
    _s7_matrix(tmp_path, "ts", "dependency-cruiser", s7_enforce_ts.find_depcruise)


def _etl_matrix(tmp_path, rel: str, truth_n: int, actual_n: int, matched_n: int, precision: float):
    spec = golden.load_golden(GOLDEN / "etl" / rel)
    repo = golden.write_fixture_files(spec, tmp_path)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=d@t", "-c", "user.name=d", "commit", "-q", "-m", "init")
    cache_db = CBM_CACHE / f"{repo.name}.db"
    if cache_db.exists():
        cache_db.unlink()
    try:
        run_vrp(["index", str(repo), "--reindex"], cwd=ROOT)
        run_vrp(["etl", str(repo)], cwd=ROOT)
        con = sqlite3.connect(f"file:{repo / '.vrp' / 'facts.db'}?mode=ro", uri=True)
        try:
            s_face = golden.audit_symbols(con, spec["truth"]["symbols"])
            v_face = golden.audit_violations(
                con, spec["truth"]["violations"], spec["truth"]["findings_source"]
            )
        finally:
            con.close()
    finally:
        if cache_db.exists():
            cache_db.unlink()
    # D7 pin 观测值：File 行（label File, 0-0 结构簿记）不入真值 → spurious 诚实显形
    assert (s_face["truth"], s_face["actual"]) == (truth_n, actual_n)
    assert (s_face["matched"], s_face["missing"], s_face["spurious"]) == (matched_n, 0, actual_n - matched_n)
    assert s_face["precision"] == precision
    assert s_face["recall"] == 1.0
    # 期望零违例面（source=etl，净语料）：真值 0 实际 0——precision/recall null（D3）
    assert (v_face["truth"], v_face["actual"], v_face["matched"]) == (0, 0, 0)
    assert v_face["precision"] is None
    assert v_face["recall"] is None


def test_deep_golden_py_etl(tmp_path):
    _etl_matrix(tmp_path, "py.json", 5, 7, 5, round(5 / 7, 4))


def test_deep_golden_ts_etl(tmp_path):
    _etl_matrix(tmp_path, "ts.json", 4, 6, 4, round(4 / 6, 4))
