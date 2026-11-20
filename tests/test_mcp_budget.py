"""AA2 预算层测试（设计稿-AA1AA2-MCP消费面改造.md §4）。

预算魔数全部钉死（codegraph ITER1/2/3 预算反复横跳教训——回归测试是唯一
防漂移手段）；emit 收缩序确定性与 marker 定点回归（截后串若仍 > 
VALUE_HEAD_CHARS 会再截出同一字符串 → 死循环）逐件锁死。
"""

import json

import pytest

from tests.test_mcp_server import _call, _drive, _mk_repo, _run
from vrp import mcp_budget, mcp_server, nav


def test_budget_constants_pinned():
    """L1/L2/L5 魔数钉死：静默漂移在 CI 面即红。"""
    assert mcp_budget.MAX_TOOL_OUTPUT_CHARS == 24000
    assert mcp_budget.MAX_JSON_INPUT_CHARS == 1 << 20
    assert mcp_budget.MAX_QUERY_INPUT_CHARS == 10_000
    assert mcp_budget.VALUE_HEAD_CHARS == 2000
    assert mcp_budget.TOOL_RESULT_LIMITS == {
        "nav_search": 20,
        "nav_module": 200,
        "nav_callers": 50,
        "nav_callees": 50,
        "query_facts": 100,
    }


def test_nav_sql_budgets_pinned():
    """L4 预算钉死：nav_path 300s（10× 实测律对齐 CGC）、query_facts 10s。"""
    assert nav.PATH_TIME_BUDGET_S == 300.0
    assert nav.QUERY_TIME_BUDGET_S == 10.0


def test_sql_budget_env_states(monkeypatch):
    """L4 VRP_TOOL_QUERY_TIMEOUT 单旋钮：合法>0 覆盖；缺省/非法/零/负回落默认。"""
    monkeypatch.delenv("VRP_TOOL_QUERY_TIMEOUT", raising=False)
    assert nav._sql_budget_s(300.0) == 300.0
    monkeypatch.setenv("VRP_TOOL_QUERY_TIMEOUT", "0.5")
    assert nav._sql_budget_s(300.0) == 0.5
    monkeypatch.setenv("VRP_TOOL_QUERY_TIMEOUT", "2")
    assert nav._sql_budget_s(300.0) == 2.0
    monkeypatch.setenv("VRP_TOOL_QUERY_TIMEOUT", "abc")
    assert nav._sql_budget_s(10.0) == 10.0
    monkeypatch.setenv("VRP_TOOL_QUERY_TIMEOUT", "0")
    assert nav._sql_budget_s(10.0) == 10.0
    monkeypatch.setenv("VRP_TOOL_QUERY_TIMEOUT", "-1")
    assert nav._sql_budget_s(10.0) == 10.0


def test_limit_resolution_env_default_table(monkeypatch):
    """L2 解析序（CGC tool_limits.py 律）：env 覆盖 → default 实参 → 内置表。"""
    monkeypatch.delenv("VRP_TOOL_RESULT_LIMITS", raising=False)
    assert mcp_budget.get_tool_result_limit("nav_search") == 20  # 无 default → 表
    assert mcp_budget.get_tool_result_limit("nav_search", 999) == 999  # default 优先于表
    assert mcp_budget.get_tool_result_limit("bogus_tool") is None  # 两处皆无
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": 5}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 5  # env > default
    assert mcp_budget.get_tool_result_limit("nav_module", 200) == 200  # env 缺项 → default


def test_limit_env_invalid_falls_back(monkeypatch):
    """env 非法三态静默回落：非 JSON / 值非整数 / JSON 非 object。"""
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", "not json at all")
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 20
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": "fifty"}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 20
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": ["x"]}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 20
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps([1, 2]))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 20


def test_limit_env_clamped_min_one(monkeypatch):
    """env 值 max(1, int)：零/负夹到 1；数字串/浮点取整。"""
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": 0}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 1
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": -3}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 1
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": "5"}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 5
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": 2.9}))
    assert mcp_budget.get_tool_result_limit("nav_search", 20) == 2


def test_check_input_bounds_and_message():
    """L5 check_input：None 放行；未超放行；超界 ValueError 带精确消息。"""
    mcp_budget.check_input("target", None, 10)
    mcp_budget.check_input("target", "abc", 10)
    with pytest.raises(ValueError) as excinfo:
        mcp_budget.check_input("target", "x" * 11, 10)
    assert str(excinfo.value) == "target is 11 chars, max 10"


def test_emit_under_cap_passthrough():
    """未超帽原样直通（与 dumps 逐字节一致——不无谓复制/注记）。"""
    payload = {"count": 2, "results": [{"fid": "f1"}, {"fid": "f2"}]}
    assert mcp_budget.emit(payload) == mcp_budget.dumps(payload)


def test_emit_shrinks_longest_list_keep_head():
    """最长列表对半截保头部：头部元素原序保留，_truncated 备注记收缩账。"""
    payload = {"results": [f"item{i:03d}" for i in range(200)]}
    text = mcp_budget.emit(payload, cap=2000)
    assert len(text) <= 2000
    out = json.loads(text)
    assert out["results"] == [f"item{i:03d}" for i in range(100)]  # 200//2 头部
    assert len(out["_truncated"]) == 1
    assert out["_truncated"][0] == "results: 200->100 items"


def test_emit_longest_list_wins():
    """最长者优先于先见者：120>80，单轮收缩仅记 long 一账。"""
    payload = {"short": ["s"] * 80, "long": ["l"] * 120}
    text = mcp_budget.emit(payload, cap=1500)
    out = json.loads(text)
    assert out["_truncated"] == ["long: 120->60 items"]


def test_emit_truncates_longest_string_with_marker():
    """最长字符串截头加 marker：截后恰 VALUE_HEAD_CHARS（头长扣除 marker）。"""
    payload = {"big": "x" * 5000}
    text = mcp_budget.emit(payload, cap=3000)
    assert len(text) <= 3000
    out = json.loads(text)
    assert out["big"] == "x" * (2000 - len(mcp_budget.TRUNCATED_MARKER)) + mcp_budget.TRUNCATED_MARKER
    assert out["_truncated"] == ["big: 5000->2000 chars (marker appended)"]


def test_emit_list_priority_over_string():
    """收缩序：长列表先于长字符串（notes[0] 必是列表账）。"""
    payload = {"items": ["y" * 30] * 200, "s": "x" * 5000}
    text = mcp_budget.emit(payload, cap=3000)
    out = json.loads(text)
    assert out["_truncated"][0].startswith("items:")
    assert any(note.startswith("s:") for note in out["_truncated"])


def test_emit_deterministic_first_seen_tie():
    """确定性：同输入同输出；等长并列取先序先见者（dict 插入序）。"""
    p1 = {"a": ["z"] * 400, "b": ["z"] * 400}
    p2 = {"b": ["z"] * 400, "a": ["z"] * 400}
    t1 = mcp_budget.emit(p1, cap=6000)
    assert mcp_budget.emit(p1, cap=6000) == t1  # 同输入同输出
    out1 = json.loads(t1)
    out2 = json.loads(mcp_budget.emit(p2, cap=6000))
    assert out1["_truncated"] == ["a: 400->200 items"]  # 并列取先见，单轮即达
    assert out2["_truncated"] == ["b: 400->200 items"]
    assert len(out1["a"]) == 200 and len(out1["b"]) == 400
    assert len(out2["b"]) == 200 and len(out2["a"]) == 400


def test_emit_nested_list_path_note():
    """嵌套列表路径记账：点路径 a.0.b 定位收缩点。"""
    payload = {"a": [{"b": [f"n{i:03d}" for i in range(300)]}]}
    text = mcp_budget.emit(payload, cap=2000)
    out = json.loads(text)
    assert out["_truncated"][0].startswith("a.0.b: 300->")


def test_emit_top_level_list_wraps_notes():
    """非 dict 根收缩后注记包 _truncated+value 信封（不丢原值）；
    根级路径为空——回归锁 _set_at 空路径 IndexError（修复前即崩）。"""
    payload = ["x" * 50] * 300
    text = mcp_budget.emit(payload, cap=2000)
    assert len(text) <= 2000
    out = json.loads(text)
    assert out["value"]  # 原值（收缩后）保留
    assert isinstance(out["_truncated"], list) and out["_truncated"]
    assert out["_truncated"][0].startswith(": 300->")  # 根路径 fmt 为空串


def test_emit_top_level_string_root():
    """根级字符串载荷：收缩走同一空路径分支（回归锁），marker 收尾可见。"""
    text = mcp_budget.emit("x" * 30000)
    out = json.loads(text)
    assert out["value"] == "x" * (2000 - len(mcp_budget.TRUNCATED_MARKER)) + mcp_budget.TRUNCATED_MARKER
    assert out["_truncated"] == [": 30000->2000 chars (marker appended)"]


def test_emit_envelope_fallback():
    """信封兜底：无可收缩者仍超帽 → truncated 信封（original_chars+value_head+hint）。"""
    payload = {"a": "short"}
    text = mcp_budget.emit(payload, cap=10)
    out = json.loads(text)
    assert out["truncated"] is True
    assert out["original_chars"] == len(mcp_budget.dumps(payload))
    assert out["value_head"] == mcp_budget.dumps(payload)  # 短载荷整头可见
    assert "hard cap" in out["hint"]


def test_emit_marker_fixed_point_terminates():
    """回归锁：截后串（2000+marker 长度）不得仍 > VALUE_HEAD_CHARS，
    否则再截产出同一字符串 → while True 死循环（多串合计逼近帽即触发）。"""
    payload = {f"s{i}": "x" * 5000 for i in range(13)}
    text = mcp_budget.emit(payload)  # 修复前：全部截成 2014 仍为候选 → 挂死
    assert len(text) <= mcp_budget.MAX_TOOL_OUTPUT_CHARS
    out = json.loads(text)
    assert out["truncated"] is True  # 13×2000 仍超帽 → 信封兜底（诚实可见）
    assert out["original_chars"] == len(mcp_budget.dumps(payload))


def test_guard_keyerror_to_error_json():
    """AA1 D4：KeyError 族（此前逃逸成 isError）→ 成功形状 {"error"}。"""
    @mcp_server._guard
    def boom():
        raise KeyError("missing-key")

    out = json.loads(boom())
    assert "missing-key" in out["error"]


def test_guard_systemexit_to_error_json():
    """SystemExit（nav 层错误约定族，非 Exception 子类）同样入 {"error"}。"""
    @mcp_server._guard
    def boom():
        raise SystemExit("nav_path: unreachable")

    out = json.loads(boom())
    assert out["error"] == "nav_path: unreachable"


def test_guard_empty_message_class_name_fallback():
    """空消息异常回退类名（str(KeyError()) == ''）。"""
    @mcp_server._guard
    def boom():
        raise KeyError()

    out = json.loads(boom())
    assert out["error"] == "KeyError"


def test_guard_keyboard_interrupt_passes_through():
    """KeyboardInterrupt 属 BaseException 照传不捕（与 _open_facts_rw 同理）。"""
    @mcp_server._guard
    def boom():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        boom()


def test_guard_huge_payload_within_budget():
    """_guard 接线 emit：巨 payload 输出 ≤24K 且截断可见。"""
    @mcp_server._guard
    def big():
        return {"results": [{"blob": "x" * 2000} for _ in range(100)]}

    text = big()
    assert len(text) <= mcp_budget.MAX_TOOL_OUTPUT_CHARS
    out = json.loads(text)
    assert "_truncated" in out


def test_emit_end_to_end_over_mcp(tmp_path, monkeypatch):
    """端到端：nav_search 巨 payload 经 vrp_query over the wire → ≤24K+_truncated。"""
    def fake_nav_search(repo, query, limit=20):
        return {
            "level": "search", "query": query, "count": 400,
            "results": [{"blob": "x" * 1000, "i": i} for i in range(400)],
        }

    monkeypatch.setattr(mcp_server.nav, "nav_search", fake_nav_search)
    repo = _mk_repo(tmp_path)

    async def plan(session):
        result = await session.call_tool(
            "vrp_query", {"query_type": "search", "target": "probe"}
        )
        return {"raw": result.content[0].text, "is_error": result.is_error}

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["is_error"] is False
    assert len(out["raw"]) <= mcp_budget.MAX_TOOL_OUTPUT_CHARS
    payload = json.loads(out["raw"])
    assert payload["count"] == 400
    assert "_truncated" in payload
    assert payload["results"][0]["i"] == 0  # 保头部


def test_tool_result_limits_env_over_mcp(tmp_path, monkeypatch):
    """L2 env over the wire：VRP_TOOL_RESULT_LIMITS 覆盖 nav_search 默认限额。"""
    monkeypatch.setenv("VRP_TOOL_RESULT_LIMITS", json.dumps({"nav_search": 1}))
    repo = _mk_repo(tmp_path)  # 两符号同命中 "calc"

    async def plan(session):
        return await _call(session, "vrp_query", {"query_type": "search", "target": "calc"})

    out = _run(_drive(repo, {"plan": plan}))["plan"]
    assert out["count"] == 1  # 无 env 时为 2（SQL LIMIT 生效）
