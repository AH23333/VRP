"""R3（构想v12）：复杂度度量族 v1 测试——py/ts 先行（口径手算锚定+真 node 沙箱）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrp import cli, complexity

_HAS_SANDBOX_TS = complexity.find_typescript() is not None

# ---- py 圈复杂度（radon 口径手算锚定） ----

_PY_SAMPLE = '''\
def sample(a, b, c):
    if a and b:
        for i in range(3):
            while c:
                pass
    elif a or c:
        return a
    else:
        pass
    try:
        assert a
        x = [i for i in range(3) if i]
    except ValueError:
        pass
    y = 1 if a else 2
    return y

def flat(a):
    return a
'''


def test_py_cc_hand_computed(tmp_path):
    """sample 手算：1 基底 +if1+and1+for1+while1 +elif1+or1 +assert1
    +comprehension(1 for+1 if)=2 +except1 +ternary1 = **12**；
    flat=1（零决策点）。"""
    f = tmp_path / "sample.py"
    f.write_text(_PY_SAMPLE, encoding="utf-8")
    rows, ok = complexity.scan_py_file(f)
    assert ok
    by_name = {r["name"]: r for r in rows}
    assert by_name["sample"]["cc"] == 12
    assert by_name["flat"]["cc"] == 1


def test_py_cognitive_hand_computed(tmp_path):
    """sample 认知手算（简化口径）：if 1 +and 1 +for(1+1) +while(1+2)
    +elif 1 +or 1 +else 1 +comprehension(1+0+1if)=2 +except 1 +ternary 1
    = **14**；assert 不计（SonarQube 无 assert 面认知点）。"""
    f = tmp_path / "sample.py"
    f.write_text(_PY_SAMPLE, encoding="utf-8")
    rows, _ = complexity.scan_py_file(f)
    by_name = {r["name"]: r for r in rows}
    assert by_name["sample"]["cognitive"] == 14
    assert by_name["flat"]["cognitive"] == 0


def test_py_nested_functions_independent(tmp_path):
    """嵌套函数复杂度归自己不归外层（radon 口径）。"""
    f = tmp_path / "nested.py"
    f.write_text(
        "def outer(a):\n"
        "    if a:\n"
        "        pass\n"
        "    def inner(b):\n"
        "        for i in b:\n"
        "            pass\n"
        "    return inner\n",
        encoding="utf-8",
    )
    rows, _ = complexity.scan_py_file(f)
    by_name = {r["name"]: r for r in rows}
    assert by_name["outer"]["cc"] == 2  # 只计自己的 if
    assert by_name["inner"]["cc"] == 2  # 只计自己的 for


def test_py_cognitive_recursion_and_boolop(tmp_path):
    """直接递归 +1/次；逻辑运算符每运算符 +1（a and b and c = 2）。"""
    f = tmp_path / "rec.py"
    f.write_text(
        "def fact(n):\n"
        "    if n and n > 1 and n < 99:\n"
        "        return n * fact(n - 1)\n"
        "    return 1\n",
        encoding="utf-8",
    )
    rows, _ = complexity.scan_py_file(f)
    r = rows[0]
    # cc: 1 + if 1 + boolop(3 values -> +2) = 4
    assert r["cc"] == 4
    # cognitive: if 1 + boolop 2 + recursion 1 = 4
    assert r["cognitive"] == 4


def test_py_cognitive_deep_nesting_penalized(tmp_path):
    """嵌套惩罚：if>if>if = 1 + 2 + 3 = 6（深度递增）；平级三 if = 3。"""
    f = tmp_path / "deep.py"
    f.write_text(
        "def deep(a, b, c):\n"
        "    if a:\n"
        "        if b:\n"
        "            if c:\n"
        "                pass\n"
        "    return 0\n"
        "def flat3(a, b, c):\n"
        "    if a:\n"
        "        pass\n"
        "    if b:\n"
        "        pass\n"
        "    if c:\n"
        "        pass\n",
        encoding="utf-8",
    )
    rows, _ = complexity.scan_py_file(f)
    by_name = {r["name"]: r for r in rows}
    assert by_name["deep"]["cognitive"] == 6
    assert by_name["flat3"]["cognitive"] == 3
    assert by_name["deep"]["cc"] == by_name["flat3"]["cc"] == 4  # CC 对嵌套不敏感（这正是认知复杂度的存在理由）


def test_py_syntax_error_honest(tmp_path):
    """解析失败诚实返回（不崩、供 unparseable 计数）。"""
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n  pass\n", encoding="utf-8")
    rows, ok = complexity.scan_py_file(f)
    assert ok is False and rows == []


# ---- ts 面（真 node+沙箱 typescript） ----

_TS_SAMPLE = '''\
export function sample(a: boolean, b: boolean, c: boolean): number {
  if (a && b) {
    for (let i = 0; i < 3; i++) {
      while (c) {
        c = false;
      }
    }
  } else if (a || c) {
    return 1;
  } else {
    return 2;
  }
  try {
    const x = [1, 2, 3].map((v) => v * 2);
  } catch (e) {
    return 3;
  }
  return a ? 4 : 5;
}

export function fact(n: number): number {
  if (n > 1) {
    return n * fact(n - 1);
  }
  return 1;
}

export class Foo {
  method(x: number): number {
    return x > 0 ? x : -x;
  }
  static make(): Foo {
    return new Foo();
  }
}

const arrow = (a: boolean): boolean => a || false;
'''


@pytest.mark.skipif(not _HAS_SANDBOX_TS, reason="dep-cruise sandbox typescript not installed")
def test_ts_cc_and_cognitive_hand_computed(tmp_path):
    """ts 真沙箱 AST 手算：sample cc=1+if1+&&1+for1+while1+elseif1+||1
    +catch1+ternary1=9（箭头函数独立成条不计入）；cognitive=if1+&&1
    +for2+while3+elseif1+||1+else1+catch1+ternary1=12；fact 递归认知+1。"""
    f = tmp_path / "sample.ts"
    f.write_text(_TS_SAMPLE, encoding="utf-8")
    rows = complexity.scan_ts_files([f], complexity.find_typescript())
    by_name = {r["name"]: r for r in rows}
    assert by_name["sample"]["cc"] == 9
    assert by_name["sample"]["cognitive"] == 12
    assert by_name["fact"]["cognitive"] == 2  # if 1 + 递归 1
    assert by_name["fact"]["cc"] == 2
    assert by_name["method"]["cc"] == 2  # 1 + ternary
    assert by_name["arrow"]["cc"] == 2  # 1 + ||


@pytest.mark.skipif(not _HAS_SANDBOX_TS, reason="dep-cruise sandbox typescript not installed")
def test_ts_lines_and_files(tmp_path):
    f = tmp_path / "sample.ts"
    f.write_text("export function alpha(): number {\n  return 1;\n}\n", encoding="utf-8")
    rows = complexity.scan_ts_files([f], complexity.find_typescript())
    assert len(rows) == 1
    assert rows[0]["name"] == "alpha" and rows[0]["line"] == 1
    assert rows[0]["file"].endswith("sample.ts")


# ---- 汇总/CLI 面 ----


def test_run_complexity_py_only_honest_ts_skip(tmp_path):
    """纯 py 仓：ts_status=skipped-no-ts-files 诚实呈现；阈值告警计数。"""
    (tmp_path / "heavy.py").write_text(
        "def heavy(a, b, c, d, e, f, g, h, i, j, k, l):\n"
        + "".join(f"    if a == {n}:\n        pass\n" for n in range(12))
        + "    return a\n",
        encoding="utf-8",
    )
    result = complexity.run_complexity(tmp_path, cc_threshold=10)
    assert result["summary"]["py_files"] == 1
    assert result["summary"]["ts_files"] == 0
    assert result["summary"]["ts_status"] == "skipped-no-ts-files"
    assert result["summary"]["exceeds_threshold"] == 1  # cc 13 > 10
    assert result["summary"]["max_cc"] == 13
    assert any(e["name"] == "heavy" for e in result["exceeds"])


def test_run_complexity_ts_unavailable_honest(tmp_path, monkeypatch):
    """ts 文件在但沙箱缺 typescript：诚实 unavailable，py 面照常。"""
    (tmp_path / "m.py").write_text("def m():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    monkeypatch.setattr(complexity, "find_typescript", lambda: None)
    result = complexity.run_complexity(tmp_path)
    assert result["summary"]["ts_status"] == "unavailable-no-typescript"
    assert result["summary"]["py_files"] == 1
    assert any(r["name"] == "m" for r in result["functions"])


def test_run_complexity_deterministic_rerun(tmp_path):
    (tmp_path / "a.py").write_text(
        "def a(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    first = complexity.run_complexity(tmp_path)
    second = complexity.run_complexity(tmp_path)
    assert first == second


def test_run_complexity_empty_repo(tmp_path):
    result = complexity.run_complexity(tmp_path)
    assert result["summary"]["functions"] == 0
    assert result["summary"]["avg_cc"] == 0.0
    assert result["summary"]["max_cc"] == 0


def test_render_text_shape(tmp_path):
    (tmp_path / "a.py").write_text(
        "def a(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    result = complexity.run_complexity(tmp_path)
    text = complexity.render_text(result)
    assert "ts engine:" in text
    assert "cc > 10" in text
    assert "cognitive" in text


def test_cli_complexity_end_to_end(tmp_path, capsys):
    (tmp_path / "a.py").write_text(
        "def a(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    rc = cli.main(["complexity", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["summary"]["functions"] == 1
    assert out["functions"][0]["cc"] == 2
    # --cc-threshold 全链：阈值 1 时 cc 2 告警
    rc = cli.main(["complexity", str(tmp_path), "--json", "--cc-threshold", "1"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["summary"]["exceeds_threshold"] == 1


def test_cli_parser_accepts_complexity():
    parser = cli.build_parser()
    args = parser.parse_args(["complexity", "some/repo", "--json", "--cc-threshold", "5", "--limit", "3"])
    assert args.repo == "some/repo"
    assert args.json and args.cc_threshold == 5 and args.limit == 3
