"""F15 quick 真值矩阵（批⑥ #204）：java/php/ruby 三解析器家族对 committed
golden 真跑审计。

golden JSON（tests/golden/s7/*.json）与测试同 commit——真值即测试数据
（tree-sitter corpus「标注即测试」形态）。审计对独立真值双向计数；
matched==truth/missing==0/spurious==0 是矩阵现状断言（引擎回归在此显形），
非审计器自身的保证——审计在工作非审计在装好（D7）。
"""

import sqlite3
from pathlib import Path

import pytest

from vrp import golden, s7_enforce

GOLDEN_S7 = Path(__file__).parent / "golden" / "s7"
CASES = [
    ("java", "java-import-parser"),
    ("php", "php-namespace-parser"),
    ("ruby", "ruby-module-parser"),
]


@pytest.mark.parametrize("lang,engine", CASES)
def test_s7_golden_matrix_quick(tmp_path, lang, engine):
    spec = golden.load_golden(GOLDEN_S7 / f"{lang}.json")
    repo = golden.write_fixture_files(spec, tmp_path)
    golden.seed_fixture_db(spec, repo)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        out = s7_enforce.run(repo, con)
    finally:
        con.close()
    assert out["status"] == "ok"
    assert out["engine"] == engine
    assert out["violations"] == len(spec["truth"]["violations"])
    assert out["findings_written"] == len(spec["truth"]["violations"])
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
    assert s_face["precision"] == 1.0
    assert s_face["recall"] == 1.0
