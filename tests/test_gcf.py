"""AA3 GCF 压缩编码测试面：形状/转义/回退/env 接线/超帽回退/确定性/token 收益。"""

import json

from vrp import gcf, mcp_budget


def test_dict_encode_keys_declared_once():
    assert gcf.dumps({"a": 1, "b": "x"}) == "a|b\n1|x"


def test_dict_none_bool_scalars():
    assert gcf.dumps({"n": None, "flag": True, "off": False}) == "n|flag|off\n|true|false"


def test_nested_value_inline_json_roundtrip():
    out = gcf.dumps({"rows": [1, 2, 3], "meta": {"k": "v"}})
    header, values = out.split("\n")
    assert header == "rows|meta"
    rows_cell, meta_cell = values.split("|")
    assert json.loads(rows_cell) == [1, 2, 3]
    assert json.loads(meta_cell) == {"k": "v"}


def test_uniform_rows_header_once_and_token_saving():
    rows = [{"fid": f"f{i}", "label": "Function", "n": i} for i in range(20)]
    out = gcf.dumps(rows)
    lines = out.split("\n")
    assert lines[0] == "fid|label|n"
    assert len(lines) == 21
    assert lines[1] == "f0|Function|0"
    assert len(out) < len(mcp_budget.dumps(rows))  # token 收益（AA3 主张）


def test_scalar_and_mixed_shapes_fallback_json():
    for payload in ("plain", 42, [1, "a"], [{"a": 1}, {"b": 2}], [], {}):
        out = gcf.dumps(payload)
        assert json.loads(out) == payload  # 回退面 json.loads 可还原


def test_escapes_pipe_and_newline_no_column_break():
    out = gcf.dumps({"cmd": "a | b", "desc": "line1\nline2"})
    header, values = out.split("\n")
    assert header == "cmd|desc"
    cells = __import__("re").split(r"(?<!\\)\|", values)  # 只按未转义管道切列
    assert len(cells) == 2  # 转义后不断列
    assert cells[0] == "a \\| b"
    assert cells[1] == "line1\\nline2"
    assert cells[0].replace("\\|", "|") == "a | b"  # 模型侧可读还原


def test_dumps_deterministic():
    payload = {"z": [1, {"x": 1}], "a": "s", "rows": [{"k": i} for i in range(3)]}
    assert gcf.dumps(payload) == gcf.dumps(payload)


def test_emit_env_gcf_on_off(monkeypatch):
    payload = {"a": 1, "b": [1, 2]}
    monkeypatch.delenv("VRP_OUTPUT_FORMAT", raising=False)
    json_out = mcp_budget.emit(payload)
    assert json.loads(json_out) == payload
    monkeypatch.setenv("VRP_OUTPUT_FORMAT", "gcf")
    gcf_out = mcp_budget.emit(payload)
    assert gcf_out == gcf.dumps(payload)
    assert gcf_out.split("\n")[0] == "a|b"
    monkeypatch.setenv("VRP_OUTPUT_FORMAT", "GCF")  # 大小写宽容
    assert mcp_budget.emit(payload) == gcf_out


def test_emit_gcf_oversized_falls_back_to_json_shrink(monkeypatch):
    monkeypatch.setenv("VRP_OUTPUT_FORMAT", "gcf")
    payload = {"rows": [{"k": i, "v": "x" * 100} for i in range(400)]}
    gcf_len = len(gcf.dumps(payload))
    cap = 2000
    assert gcf_len > cap
    out = mcp_budget.emit(payload, cap=cap)
    assert len(out) <= cap
    parsed = json.loads(out)  # 超帽回退 JSON 收缩链（GCF 面不收缩——诚实边界）
    assert "_truncated" in parsed
    monkeypatch.delenv("VRP_OUTPUT_FORMAT", raising=False)


def test_empty_key_rows_fallback_json():
    out = gcf.dumps([{}, {}])
    assert json.loads(out) == [{}, {}]  # 空键行不可表头化——回退 JSON 宁缺勿错（:51 面）
