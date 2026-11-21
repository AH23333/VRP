"""N20 mdcode 测试面：fence 解析/py·ts 提取/对账漂移/排除/确定性/渲染/CLI E2E。"""

import json
import sqlite3
from pathlib import Path

from vrp import mdcode
from vrp.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _seed(con):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f-foo','p','pkg.foo','Function','foo','src/pkg.py',1,3,'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f-baz','p','pkg.Baz','Function','Baz','src/pkg.py',5,9,'h')"
    )
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f-qux','p','pkg.Qux','Class','Qux','src/pkg.py',11,20,'h')"
    )
    con.commit()


MD_SAMPLE = """# Guide

```python
def foo(x):
    return x

def bar(y):
    return y

class Baz:
    pass
```

```ts
function qux(): void {}
interface Iface {}
```

```text
plain block
```
"""


def test_extract_blocks_multiple_fences_and_langs():
    blocks = mdcode.extract_blocks(MD_SAMPLE)
    assert [b["lang"] for b in blocks] == ["python", "ts", "text"]
    assert all(b["closed"] for b in blocks)
    assert blocks[0]["fence_line"] == 3
    assert "def foo(x):" in blocks[0]["code"]
    assert "```" not in blocks[0]["code"]


def test_extract_blocks_unclosed_fence_still_extracted():
    blocks = mdcode.extract_blocks("intro\n```python\ndef a():\n    pass\n")
    assert len(blocks) == 1
    assert blocks[0]["closed"] is False
    assert "def a():" in blocks[0]["code"]


def test_extract_py_symbols_names_kinds_and_lines():
    code = "def top():\n    pass\n\n\nclass C:\n    def m(self):\n        pass\n\nasync def go():\n    pass\n"
    syms, ok = mdcode.extract_py_symbols(code)
    assert ok
    assert syms == [
        ("top", "Function", 1),
        ("C", "Class", 5),
        ("m", "Function", 6),
        ("go", "Function", 9),
    ]


def test_extract_py_symbols_syntax_error_honest():
    syms, ok = mdcode.extract_py_symbols("def broken(:\n    pass\n")
    assert ok is False
    assert syms == []


def test_extract_ts_symbols_shapes_and_dedup():
    code = (
        "function go(): void {}\n"
        "class Thing {}\n"
        "interface Iface {}\n"
        "type Alias = string;\n"
        "const fn = (x: number) => x;\n"
        "function go(): void {}\n"
    )
    syms = mdcode.extract_ts_symbols(code)
    names_kinds = [(n, k) for n, k, _ in syms]
    assert ("go", "Function") in names_kinds
    assert names_kinds.count(("go", "Function")) == 1  # 去重保首见
    assert ("Thing", "Class") in names_kinds
    assert ("Iface", "Interface") in names_kinds
    assert ("Alias", "Type") in names_kinds
    assert ("fn", "Function") in names_kinds
    assert syms[0] == ("go", "Function", 1)


def test_run_mdcode_drift_doc_only_and_kind_mismatch(tmp_path, facts_db):
    _seed(facts_db)
    (tmp_path / "guide.md").write_text(MD_SAMPLE, encoding="utf-8")
    result = mdcode.run_mdcode(facts_db, tmp_path)
    assert result["md_files"] == 1
    assert result["per_lang"] == {"py": 1, "ts": 1}
    assert result["plain_blocks"] == 1
    assert result["extracted"] == 5  # foo bar Baz / qux Iface
    assert result["matched"] == 1  # foo（大小写敏感：qux != db Qux）
    by_name = {d["name"]: d for d in result["drift"]}
    assert set(by_name) == {"bar", "Baz", "qux", "Iface"}
    assert by_name["bar"]["reason"] == "doc-only"
    assert by_name["bar"]["db_label"] is None
    assert by_name["Baz"]["reason"] == "kind-mismatch"
    assert by_name["Baz"]["db_label"] == "Function"
    assert by_name["Baz"]["line"] == 10  # fence_line 3 + code lineno 7
    assert by_name["qux"]["reason"] == "doc-only"
    assert by_name["Iface"]["reason"] == "doc-only"


def test_run_mdcode_method_label_same_family_no_false_mismatch(tmp_path, facts_db):
    facts_db.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f-m','p','pkg.C.m','Method','m','src/pkg.py',1,2,'h')"
    )
    facts_db.commit()
    (tmp_path / "m.md").write_text("```python\ndef m():\n    pass\n```\n", encoding="utf-8")
    result = mdcode.run_mdcode(facts_db, tmp_path)
    assert result["matched"] == 1
    assert result["drift"] == []


def test_run_mdcode_excludes_and_deterministic(tmp_path, facts_db):
    _seed(facts_db)
    (tmp_path / ".vrp").mkdir()
    (tmp_path / ".vrp" / "hidden.md").write_text("```python\ndef gone():\n    pass\n```\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.md").write_text("```python\ndef dep():\n    pass\n```\n", encoding="utf-8")
    (tmp_path / "ok.md").write_text("```python\ndef foo():\n    pass\n```\n", encoding="utf-8")
    first = mdcode.run_mdcode(facts_db, tmp_path)
    second = mdcode.run_mdcode(facts_db, tmp_path)
    assert first == second
    assert first["md_files"] == 1  # 排除目录 md 不扫
    assert first["matched"] == 1


def test_render_text_shape(tmp_path, facts_db):
    _seed(facts_db)
    (tmp_path / "guide.md").write_text(MD_SAMPLE, encoding="utf-8")
    result = mdcode.run_mdcode(facts_db, tmp_path)
    text = mdcode.render_text(result)
    assert "vrp mdcode" in text
    assert "drift candidates : 4" in text
    assert "[doc-only]" in text and "[kind-mismatch]" in text
    assert "name-level match" in text


def test_render_text_no_drift_note():
    text = mdcode.render_text({"md_files": 0, "blocks": 0, "plain_blocks": 0, "unclosed_fences": 0,
                               "parse_errors": 0, "extracted": 0, "matched": 0, "drift": [],
                               "per_lang": {"py": 0, "ts": 0}})
    assert "no drift" in text


def test_cli_mdcode_end_to_end(tmp_path, facts_db, capsys):
    _seed(facts_db)
    (tmp_path / "guide.md").write_text(MD_SAMPLE, encoding="utf-8")
    db_path = tmp_path / ".vrp" / "facts.db"
    db_path.parent.mkdir()
    disk = sqlite3.connect(db_path)
    disk.executescript((REPO_ROOT / "vrp" / "schema" / "facts_schema.sql").read_text(encoding="utf-8"))
    for row in facts_db.execute("SELECT fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash FROM symbols").fetchall():
        disk.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)", row)
    disk.commit()
    disk.close()
    rc = cli_main(["mdcode", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["matched"] == 1
    assert {d["name"] for d in out["drift"]} == {"bar", "Baz", "qux", "Iface"}


def test_run_mdcode_class_match_and_ts_name_only(tmp_path, facts_db):
    _seed(facts_db)
    (tmp_path / "g.md").write_text(
        "# G\n\n```python\nclass Qux:\n    pass\n```\n\n```ts\nfunction foo(): void {}\n```\n",
        encoding="utf-8",
    )
    result = mdcode.run_mdcode(facts_db, tmp_path)
    # py Class Qux ↔ db Class（Class 分支）+ ts foo name-only（ts 词表未钉不比对类别）
    assert result["matched"] == 2
    assert result["drift"] == []


def test_run_mdcode_unclosed_and_parse_error_counters(tmp_path, facts_db):
    _seed(facts_db)
    (tmp_path / "bad.md").write_text("```python\ndef broken(:\n", encoding="utf-8")
    result = mdcode.run_mdcode(facts_db, tmp_path)
    assert result["unclosed_fences"] == 1  # 未闭合 fence 计数
    assert result["parse_errors"] == 1  # py 语法错计数（提取器诚实面）
    assert result["extracted"] == 0


def test_render_drift_cap_overflow_line(tmp_path, facts_db, monkeypatch):
    _seed(facts_db)
    (tmp_path / "g.md").write_text("```python\ndef zz1():\n    pass\n```\n", encoding="utf-8")
    result = mdcode.run_mdcode(facts_db, tmp_path)
    assert len(result["drift"]) == 1  # 数据面全量（cap 只在渲染面）
    monkeypatch.setattr(mdcode, "DRIFT_CAP", 0)
    text = mdcode.render_text(result)
    assert "... and 1 more (use --json)" in text  # 渲染面截断恒真提示
