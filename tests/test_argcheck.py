"""N19 (#219): argcheck - CALLS args x callee signature reconciliation.

signature 字符串是唯一对账基准（CBM param_count=类型标注参数计数，
dogfood 实测 ~21% 与签名字符串不一致——参考值非基准）；报告非门
（mdcode/langmatrix 先例，rc 恒 0）。
"""

import json
import sqlite3

from vrp import argcheck


# ---- split_params：顶层逗号拆（多行/嵌套容忍）----

def test_split_params_plain():
    assert argcheck.split_params("(a, b)") == ["a", "b"]


def test_split_params_nested_brackets_do_not_split():
    assert argcheck.split_params("(x: dict[int, str], y)") == ["x: dict[int, str]", "y"]


def test_split_params_multiline_signature():
    sig = "(\r\n    repo: Path,\r\n    budget: int = 7,\r\n)"
    assert argcheck.split_params(sig) == ["repo: Path", "budget: int = 7"]


def test_split_params_empty_and_bare():
    assert argcheck.split_params("()") == []
    assert argcheck.split_params("( )") == []
    assert argcheck.split_params("a, b") == ["a", "b"]


# ---- parse_param：默认值/标注/变长/ts 可选 ----

def test_parse_param_typed_required():
    assert argcheck.parse_param("repo: Path") == {"name": "repo", "required": True, "kind": "pos"}


def test_parse_param_default_optional():
    assert argcheck.parse_param("env: dict | None = None") == {
        "name": "env", "required": False, "kind": "pos",
    }


def test_parse_param_variadic():
    assert argcheck.parse_param("*args")["kind"] == "star"
    assert argcheck.parse_param("**kwargs")["kind"] == "dstar"
    assert argcheck.parse_param("*args")["required"] is False


def test_parse_param_ts_optional_suffix():
    p = argcheck.parse_param("b?: number")
    assert p == {"name": "b", "required": False, "kind": "pos"}


def test_parse_param_untyped():
    assert argcheck.parse_param("payload") == {"name": "payload", "required": True, "kind": "pos"}


# ---- parse_signature：模型组装 ----

def test_parse_signature_method_strips_self():
    m = argcheck.parse_signature("(self, rel: str)", "Method")
    assert m["names"] == ["rel"]
    assert m["required"] == 1
    assert m["total"] == 1


def test_parse_signature_function_keeps_name_self():
    m = argcheck.parse_signature("(self, x)", "Function")
    assert m["required"] == 2  # 不是 Method 不剥——诚实面


def test_parse_signature_required_and_optional_split():
    m = argcheck.parse_signature("(repo: Path, env: dict | None = None)", "Function")
    assert m["required"] == 1
    assert m["total"] == 2
    assert m["names"] == ["repo", "env"]


def test_parse_signature_variadic_flags():
    m = argcheck.parse_signature("(a, *args, **kwargs)", "Function")
    assert m["has_star"] is True
    assert m["has_dstar"] is True
    assert m["total"] == 3
    assert m["required"] == 1


def test_parse_signature_empty_and_invalid():
    assert argcheck.parse_signature(None, "Function") is None
    assert argcheck.parse_signature("", "Function") is None
    assert argcheck.parse_signature("no parens here", "Function") is None
    assert argcheck.parse_signature("()", "Function")["total"] == 0


# ---- reconcile_call：裁决 ----

def _model(sig, label="Function"):
    return argcheck.parse_signature(sig, label)


def test_reconcile_ok_exact_and_default_range():
    m = _model("(repo: Path, env: dict | None = None)")
    assert argcheck.reconcile_call([{"i": 0, "e": "r"}, {"i": 1, "e": "e"}], m) is None
    assert argcheck.reconcile_call([{"i": 0, "e": "r"}], m) is None  # 默认值可省


def test_reconcile_too_few():
    m = _model("(path: Path, payload)")
    assert argcheck.reconcile_call([{"i": 0, "e": "p"}], m) == "too_few"
    assert argcheck.reconcile_call([], m) == "too_few"  # 显式零参 vs 必选 2


def test_reconcile_too_many():
    m = _model("(path: Path, payload)")
    assert argcheck.reconcile_call(
        [{"i": 0, "e": "a"}, {"i": 1, "e": "b"}, {"i": 2, "e": "c"}], m
    ) == "too_many"


def test_reconcile_star_suppresses_upper_bound():
    m = _model("(a, *args)")
    assert argcheck.reconcile_call(
        [{"i": 0, "e": "x"}, {"i": 1, "e": "y"}, {"i": 2, "e": "z"}], m
    ) is None


def test_reconcile_kwarg_ok_and_unknown():
    m = _model("(repo: Path, budget: int = 7)")
    assert argcheck.reconcile_call(
        [{"i": 0, "e": "r"}, {"k": "budget", "v": 1}], m
    ) is None
    assert argcheck.reconcile_call(
        [{"i": 0, "e": "r"}, {"k": "zzz", "v": 1}], m
    ) == "unknown_kwarg"


def test_reconcile_dstar_suppresses_kwarg_check():
    m = _model("(a, **kwargs)")
    assert argcheck.reconcile_call([{"i": 0, "e": "x"}, {"k": "any", "v": 1}], m) is None


def test_reconcile_ignores_non_dict_noise():
    m = _model("(a)")
    assert argcheck.reconcile_call([{"i": 0, "e": "x"}, "noise", 3], m) is None


# ---- run_argcheck：E2E（最小库）----

def _mk_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE symbols (fid TEXT, qualified_name TEXT, signature TEXT, label TEXT)")
    con.execute(
        "CREATE TABLE edges (src_fid TEXT, dst_fid TEXT, type TEXT, properties_json TEXT)"
    )
    return con


def _add_sym(con, fid, qn, sig, label):
    con.execute("INSERT INTO symbols VALUES (?,?,?,?)", (fid, qn, sig, label))


def _add_call(con, src, dst, args):
    props = json.dumps({"args": args}) if args is not None else json.dumps({"callee": "x"})
    con.execute("INSERT INTO edges VALUES (?,?,?,?)", (src, dst, "CALLS", props))


def test_run_argcheck_counters_and_samples():
    con = _mk_db()
    _add_sym(con, "f-caller", "m.caller", "(self)", "Method")
    _add_sym(con, "b-len", "builtins.len", "()", "Function")
    _add_sym(con, "b-plain", "m.plain", None, "Function")
    _add_sym(con, "f-two", "m.two_req", "(path: Path, payload)", "Function")
    _add_sym(con, "f-def", "m.with_default", "(repo: Path, env: dict | None = None)", "Function")
    _add_sym(con, "f-kw", "m.kw_face", "(repo: Path, budget: int = 7)", "Function")
    _add_call(con, "f-caller", "b-len", [{"i": 0, "e": "x"}])          # builtins skip
    _add_call(con, "f-caller", "b-plain", [{"i": 0, "e": "x"}])        # no signature skip
    _add_call(con, "f-two", "f-def", [{"i": 0, "e": "r"}])             # ok（默认值省）
    _add_call(con, "f-def", "f-two", [{"i": 0, "e": "p"}])             # too_few
    _add_call(con, "f-def", "f-two", [{"i": 0, "e": "a"}, {"i": 1, "e": "b"}, {"i": 2, "e": "c"}])  # too_many
    _add_call(con, "f-two", "f-kw", [{"i": 0, "e": "r"}, {"k": "zzz", "v": 1}])  # unknown_kwarg
    _add_call(con, "f-two", "f-def", None)                             # args 键缺席 skip
    try:
        result = argcheck.run_argcheck(con)
    finally:
        con.close()
    c = result["counters"]
    assert c["total_calls"] == 7
    assert c["builtins_skipped"] == 1
    assert c["no_signature_skipped"] == 1
    assert c["no_args_skipped"] == 1
    assert c["checked"] == 4
    assert c["ok"] == 1
    assert c["too_few"] == 1
    assert c["too_many"] == 1
    assert c["unknown_kwarg"] == 1
    s = result["samples"]["too_few"][0]
    assert s["callee"] == "m.two_req"
    assert s["caller"] == "m.with_default"


def test_run_argcheck_empty_list_is_checked_not_skipped():
    con = _mk_db()
    _add_sym(con, "f-caller", "m.caller", "()", "Function")
    _add_sym(con, "f-one", "m.one_req", "(a)", "Function")
    _add_call(con, "f-caller", "f-one", [])  # 显式零参 vs 必选 1 = too_few
    try:
        result = argcheck.run_argcheck(con)
    finally:
        con.close()
    assert result["counters"]["checked"] == 1
    assert result["counters"]["too_few"] == 1


def test_run_argcheck_no_calls_edges_at_all():
    con = _mk_db()
    _add_sym(con, "f-x", "m.x", "(a)", "Function")
    con.execute("INSERT INTO edges VALUES ('f-x','f-x','USAGE','{}')")
    try:
        result = argcheck.run_argcheck(con)
    finally:
        con.close()
    assert result["counters"]["total_calls"] == 0


def test_render_text_mentions_counters():
    con = _mk_db()
    try:
        result = argcheck.run_argcheck(con)
    finally:
        con.close()
    text = argcheck.render_text(result)
    assert "argcheck" in text
    assert "too_few" in text
    assert "rc always 0" in text
