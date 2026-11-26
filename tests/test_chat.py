"""`vrp chat` 测试（终端原生交互问答 REPL + 只读查询工具环）。

注入面：input_fn/output_fn/chat_fn/run_pipeline_fn/isatty 全 mock，零真 API、
零真 TTY。红线：非 TTY 诚实拒绝 / 滑动窗口 ≤ TURNS_WINDOW 轮 / 日志默认关 /
key 零落盘 / 工具面恰为 17 只读类型白名单 / 单问跳数 ≤5 / 只读边界。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import chat, etl, mcp_budget, mcp_server, nav

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

API_KEY = "sk-test-secret-123"


@pytest.fixture(autouse=True)
def _fast_git_probe(monkeypatch):
    """git 探针桩掉（test_fixpropose/test_analyze 同型）。"""
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "")


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f1', 'p', 'pkg.calc.total', 'Function', 'total', 'pkg/calc.py', 1, 3, 'h')"
    )
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid,"
        " file_path, start_line, end_line, payload_json)"
        " VALUES ('P1', 'dead-import', 's2-deadcode', 'f1', NULL, 'pkg/calc.py', 1, 1, '{\"reason\": \"x\"}')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "calc.py").write_text("def total(a, b):\n    return a + b\n", encoding="utf-8")
    _write_config(repo)
    return repo


def _write_config(repo: Path) -> None:
    payload = {"llm": {"base_url": "http://llm.example/v1", "api_key": API_KEY, "model": "test-model"}}
    (repo / ".vrp" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _inputs(lines):
    it = iter(lines)

    def ask(prompt):
        assert prompt == chat.PROMPT
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return ask


def _fake_chat():
    prompts = []

    def call(config, messages):
        prompts.append((config, [dict(m) for m in messages]))
        return {"content": f"answer-{len(prompts)}", "finish_reason": "stop", "usage": {}}

    call.prompts = prompts
    return call


def _scripted_chat(responses):
    """按脚本逐条回放响应（str=content；dict=原样响应；异常实例=抛出）。"""
    prompts = []
    it = iter(list(responses))

    def call(config, messages):
        prompts.append([dict(m) for m in messages])
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return {"content": item, "finish_reason": "stop", "usage": {}}
        return dict(item)

    call.prompts = prompts
    return call


# ---------- 入口门 ----------

def test_non_tty_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="pipe mode is not supported.*vrp analyze"):
        chat.run_chat(repo, isatty=False)


def test_missing_config_human_exit(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "config.json").unlink()
    with pytest.raises(SystemExit, match="run 'vrp config' first"):
        chat.run_chat(repo, isatty=True)


def test_first_turn_injection_line(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []
    summary = chat.run_chat(repo, isatty=True, input_fn=_inputs(["/quit"]), output_fn=out.append)
    assert any("dossier brief injected (0 modules / top files" in line for line in out)
    assert summary["rc"] == 0


def test_missing_facts_hint_and_bare_system(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "facts.db").unlink()
    out = []
    fake = _fake_chat()
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["你好", "/quit"]), output_fn=out.append
    )
    assert any("facts.db missing" in line for line in out)
    assert summary["turns"] == 1
    assert fake.prompts[0][1][0]["role"] == "system"
    assert "dossier T1 brief" not in fake.prompts[0][1][0]["content"]  # 未假装有材料


# ---------- 干净退出 ----------

def test_quit_bye(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []
    summary = chat.run_chat(repo, isatty=True, input_fn=_inputs(["/quit"]), output_fn=out.append)
    assert out[-1] == "bye"
    assert summary["rc"] == 0


def test_eof_bye(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []
    summary = chat.run_chat(repo, isatty=True, input_fn=_inputs([]), output_fn=out.append)
    assert out[-1] == "bye"
    assert summary["turns"] == 0


def test_ctrl_c_bye(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []

    def interrupt(prompt):
        raise KeyboardInterrupt

    summary = chat.run_chat(repo, isatty=True, input_fn=interrupt, output_fn=out.append)
    assert out[-1] == "bye"
    assert summary["rc"] == 0


# ---------- 斜杠命令路由 ----------

def test_unknown_slash_lists_commands(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []
    chat.run_chat(repo, isatty=True, input_fn=_inputs(["/nope", "/quit"]), output_fn=out.append)
    hit = [line for line in out if "unknown slash command: /nope" in line]
    assert hit and "/quit" in hit[0] and "/dossier" in hit[0] and "/findings" in hit[0]


def test_slash_findings_injects(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat()
    out = []
    chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["/findings", "问点啥", "/quit"]), output_fn=out.append
    )
    assert any("findings top 20 injected" in line for line in out)
    system = fake.prompts[0][1][0]["content"]
    assert "findings top 20" in system
    assert "P1" in system


def test_slash_dossier_full_material(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat()
    out = []
    chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["/dossier", "问点啥", "/quit"]), output_fn=out.append
    )
    assert any("full dossier material re-injected" in line for line in out)
    system = fake.prompts[0][1][0]["content"]
    assert "dossier_t1" in system  # analyze 同源装配的完整材料
    assert "findings_top" in system


def test_slash_clear_resets_to_first_turn(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat()
    out = []
    chat.run_chat(
        repo,
        isatty=True,
        chat_fn=fake,
        input_fn=_inputs(["第一问", "/clear", "第二问", "/quit"]),
        output_fn=out.append,
    )
    assert any("context cleared - back to first-turn state" in line for line in out)
    # 清空后第二轮：仅 system + 1 条 user（首轮状态）
    last_messages = fake.prompts[-1][1]
    assert len(last_messages) == 2
    assert last_messages[0]["role"] == "system"
    assert last_messages[1]["content"] == "第二问"
    assert "第一问" not in json.dumps(last_messages, ensure_ascii=False)


def test_slash_refresh_stale_runs_pipeline(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute("UPDATE meta SET value = 'deadbeef' WHERE key = 'git_head'")
        con.commit()
    finally:
        con.close()
    ran = []

    def fake_pipeline(r, name, print_fn=None):
        ran.append(name)
        con = sqlite3.connect(r / ".vrp" / "facts.db")
        try:
            con.execute("UPDATE meta SET value = '' WHERE key = 'git_head'")
            con.commit()
        finally:
            con.close()

    out = []
    fake = _fake_chat()
    chat.run_chat(
        repo,
        isatty=True,
        chat_fn=fake,
        input_fn=_inputs(["/refresh", "/quit"]),
        output_fn=out.append,
        run_pipeline_fn=fake_pipeline,
    )
    assert ran == ["repo"]
    assert any("facts stale - rebuilding" in line for line in out)
    assert any("facts rebuilt" in line for line in out)


def test_slash_refresh_fresh_noop(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []

    def must_not_run(r, name, print_fn=None):
        pytest.fail("fresh facts must not rebuild")

    chat.run_chat(
        repo,
        isatty=True,
        input_fn=_inputs(["/refresh", "/quit"]),
        output_fn=out.append,
        run_pipeline_fn=must_not_run,
    )
    assert any("facts fresh - no rebuild needed" in line for line in out)


# ---------- 多轮上下文 ----------

def test_sliding_window(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat()
    lines = [f"问题{i}" for i in range(15)] + ["/quit"]
    summary = chat.run_chat(repo, isatty=True, chat_fn=fake, input_fn=_inputs(lines), output_fn=lambda _t: None)
    assert summary["turns"] == 15
    # 最近 TURNS_WINDOW 轮滑动窗口（整轮对齐）：system + 当前问 + 前 9 完整轮
    last_messages = fake.prompts[-1][1]
    assert len(last_messages) == 1 + (2 * chat.TURNS_WINDOW - 1)
    assert last_messages[1]["content"] == "问题5"  # u6（问题从 0 编号）
    # 窗口外轮次（含其 assistant 回答）与窗口对齐裁掉的孤 assistant 诚实丢弃
    for dropped in ("问题4", "answer-5"):
        assert dropped not in json.dumps(last_messages, ensure_ascii=False)
    for kept in ("问题5", "answer-6", "问题14"):
        assert kept in json.dumps(last_messages, ensure_ascii=False)


def test_short_history_under_window(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat()
    chat.run_chat(repo, isatty=True, chat_fn=fake, input_fn=_inputs(["a", "b", "/quit"]), output_fn=lambda _t: None)
    last_messages = fake.prompts[-1][1]
    # 第二问调用时：system + u1/a1 完整轮 + 当前 u2（a2 未发生）
    assert len(last_messages) == 4
    assert last_messages[1]["content"] == "a"
    assert last_messages[2]["content"] == "answer-1"
    assert last_messages[3]["content"] == "b"


def test_chat_error_pops_turn_and_continues(tmp_path):
    repo = _mk_repo(tmp_path)
    prompts = []
    state = {"n": 0}

    def flaky(config, messages):
        state["n"] += 1
        prompts.append([dict(m) for m in messages])
        if state["n"] == 1:
            raise ValueError("HTTP 503: down")
        return {"content": "恢复后的回答", "finish_reason": "stop", "usage": {}}

    out = []
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=flaky, input_fn=_inputs(["第一问", "第二问", "/quit"]), output_fn=out.append
    )
    assert any("LLM call failed: HTTP 503" in line for line in out)
    assert summary["turns"] == 1
    # 失败轮不进窗口：成功轮的上下文里没有"第一问"
    assert "第一问" not in json.dumps(prompts[-1], ensure_ascii=False)


# ---------- 日志 ----------

def test_log_default_off(tmp_path):
    repo = _mk_repo(tmp_path)
    chat.run_chat(repo, isatty=True, input_fn=_inputs(["你好", "/quit"]), output_fn=lambda _t: None)
    assert not (repo / ".vrp" / "chat").exists()  # 交互隐私默认关


def test_log_on_writes_session(tmp_path):
    repo = _mk_repo(tmp_path)
    summary = chat.run_chat(
        repo,
        log=True,
        isatty=True,
        chat_fn=_fake_chat(),
        input_fn=_inputs(["你好", "/findings", "/quit"]),
        output_fn=lambda _t: None,
    )
    log_path = Path(summary["log_path"])
    assert log_path.exists()
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").strip().splitlines()]
    kinds = [e["kind"] for e in entries]
    # 你好→回答；/findings；/quit——user 行为 + slash 命令各留一条
    assert kinds == ["user", "assistant", "user", "slash", "user", "slash"]
    assert entries[0]["content"] == "你好"
    assert entries[1]["content"].startswith("answer-")
    assert entries[3]["content"] == "/findings"
    # 红线：日志零 key
    assert API_KEY not in log_path.read_text(encoding="utf-8")


def test_log_records_all_turns(tmp_path):
    repo = _mk_repo(tmp_path)
    summary = chat.run_chat(
        repo,
        log=True,
        isatty=True,
        chat_fn=_fake_chat(),
        input_fn=_inputs(["a", "b", "c", "/quit"]),
        output_fn=lambda _t: None,
    )
    entries = [json.loads(line) for line in Path(summary["log_path"]).read_text(encoding="utf-8").strip().splitlines()]
    assert [e["kind"] for e in entries] == ["user", "assistant"] * 3 + ["user", "slash"]


# ---------- 工具环（Gap 1）----------

def test_tool_whitelist_exactly_17_readonly():
    # 只读边界：显式白名单恰为 vrp_query 的 17 只读类型（与 mcp_server 钉死防漂移）
    assert len(chat.CHAT_TOOL_TYPES) == 17
    assert set(chat.CHAT_TOOL_TYPES) == set(mcp_server._VRP_QUERY_TYPES)
    for write_op in (
        "curate_submit", "curate_reject", "curate_refresh", "dossier",
        "tx_preview", "tx_submit", "tx_run", "render", "session", "enforce",
    ):
        assert write_op not in chat.CHAT_TOOL_TYPES
    assert chat.MAX_TOOL_HOPS == 5
    assert chat.MAX_PROTOCOL_RETRIES == 2


def test_tool_loop_single_hop(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat([
        json.dumps({"tool": "search", "args": {"target": "total"}}),
        json.dumps({"answer": "found it"}),
    ])
    out = []
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["where is total?", "/quit"]), output_fn=out.append
    )
    assert summary["turns"] == 1
    assert "[query: search total]" in out
    assert "found it" in out
    # 回喂：第二次调用收到真实查询结果（复用 nav_search + mcp_budget.emit 预算）
    fed = fake.prompts[1][-1]
    assert fed["role"] == "user"
    assert "## Query result (search)" in fed["content"]
    assert mcp_budget.emit(nav.nav_search(repo, "total", 20)) in fed["content"]
    assert "pkg.calc.total" in fed["content"]


def test_tool_loop_multi_hop_transient_exchanges(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat([
        json.dumps({"tool": "search", "args": {"target": "total"}}),
        json.dumps({"tool": "symbol", "args": {"target": "f1"}}),
        json.dumps({"answer": "final synthesis"}),
        json.dumps({"answer": "second"}),
    ])
    out = []
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["q1", "q2", "/quit"]), output_fn=out.append
    )
    assert summary["turns"] == 2
    assert "[query: search total]" in out
    assert "[query: symbol f1]" in out
    # 第二跳调用时上下文含第一跳完整交换（assistant tool call + 回喂结果）
    assert len(fake.prompts[1]) == 4
    assert len(fake.prompts[2]) == 6
    assert "## Query result (symbol)" in fake.prompts[2][-1]["content"]
    # 工具环中间交换是瞬态 loop 上下文：q2 的滑动窗只见 q1/最终回答/q2
    last_msgs = fake.prompts[3]
    assert [m["role"] for m in last_msgs] == ["system", "user", "assistant", "user"]
    assert last_msgs[2]["content"] == "final synthesis"
    assert "Query result" not in json.dumps(last_msgs, ensure_ascii=False)


def test_tool_loop_hop_limit_honest_abort(tmp_path):
    repo = _mk_repo(tmp_path)
    always_tool = json.dumps({"tool": "architecture", "args": {}})
    fake = _scripted_chat([always_tool] * 6 + [json.dumps({"answer": "after"})])
    out = []
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["deep question", "next q", "/quit"]), output_fn=out.append
    )
    # 跳数硬顶：恰执行 MAX_TOOL_HOPS 次查询后第 6 次工具请求诚实终止
    assert out.count("[query: architecture]") == chat.MAX_TOOL_HOPS
    assert any(f"reached the max {chat.MAX_TOOL_HOPS} query hops" in line for line in out)
    # 超限轮不进窗口：后续问的上下文没有 "deep question"
    last_msgs = fake.prompts[-1]
    assert "deep question" not in json.dumps(last_msgs, ensure_ascii=False)
    assert summary["turns"] == 1  # 仅 "next q" 计一轮


def test_tool_loop_unknown_tool_feedback_then_answer(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat([
        json.dumps({"tool": "curate_submit", "args": {"target": "x"}}),  # 写操作不在白名单
        json.dumps({"answer": "ok"}),
    ])
    out = []
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=fake, input_fn=_inputs(["q", "/quit"]), output_fn=out.append
    )
    assert summary["turns"] == 1
    # 白名单拒绝发生在执行前：无任何 [query:] 行
    assert not any(line.startswith("[query:") for line in out)
    feedback = fake.prompts[1][-1]["content"]
    assert "unknown tool type" in feedback and "curate_submit" in feedback


def test_tool_loop_protocol_fault_abort(tmp_path):
    repo = _mk_repo(tmp_path)
    bad = json.dumps({"foo": "bar"})
    fake = _scripted_chat([bad] * 3)
    out = []
    summary = chat.run_chat(repo, isatty=True, chat_fn=fake, input_fn=_inputs(["q", "/quit"]), output_fn=out.append)
    # 坏结构化输出：2 次回执重试后第 3 次诚实终止
    assert summary["turns"] == 0
    assert len(fake.prompts) == 3
    assert any("repeated malformed responses" in line for line in out)


def test_tool_loop_query_error_fed_back_honestly(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat([
        json.dumps({"tool": "symbol", "args": {"target": "deadbeef"}}),
        json.dumps({"answer": "handled"}),
    ])
    out = []
    summary = chat.run_chat(repo, isatty=True, chat_fn=fake, input_fn=_inputs(["q", "/quit"]), output_fn=out.append)
    assert summary["turns"] == 1
    assert "[query: symbol deadbeef]" in out
    fed = fake.prompts[1][-1]["content"]
    assert '"error"' in fed and "unknown fid" in fed  # 查询失败诚实回喂，不编造


def test_tool_loop_readonly_sql_rejected_db_intact(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat([
        json.dumps({"tool": "facts", "args": {"target": "DELETE FROM symbols"}}),
        json.dumps({"answer": "done"}),
    ])
    summary = chat.run_chat(repo, isatty=True, chat_fn=fake, input_fn=_inputs(["q", "/quit"]), output_fn=lambda _t: None)
    assert summary["turns"] == 1
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    finally:
        con.close()
    assert count == 1  # 只读边界：DELETE 被现有 query_facts SELECT/WITH 白名单拒绝
    fed = fake.prompts[1][-1]["content"]
    assert "only SELECT/WITH allowed" in fed


def test_non_json_response_is_plain_answer(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat(["just plain prose, no JSON"])
    out = []
    summary = chat.run_chat(repo, isatty=True, chat_fn=fake, input_fn=_inputs(["q", "/quit"]), output_fn=out.append)
    # 非 JSON 回复按普通回答原样落地（诚实回退，不强迫协议）
    assert summary["turns"] == 1
    assert "just plain prose, no JSON" in out


# ---------- 进度提示（Gap 2）----------

def test_progress_thinking_line_before_answer(tmp_path):
    repo = _mk_repo(tmp_path)
    out = []
    summary = chat.run_chat(
        repo, isatty=True, chat_fn=_fake_chat(), input_fn=_inputs(["q", "/quit"]), output_fn=out.append
    )
    assert summary["turns"] == 1
    assert out.count("[thinking...]") == 1
    assert out[out.index("[thinking...]") + 1] == "answer-1"


# ---------- 工具环 --log 留痕 ----------

def test_log_tool_loop_trace(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _scripted_chat([
        json.dumps({"tool": "search", "args": {"target": "total"}}),
        json.dumps({"answer": "done"}),
    ])
    summary = chat.run_chat(
        repo, log=True, isatty=True, chat_fn=fake, input_fn=_inputs(["q", "/quit"]), output_fn=lambda _t: None
    )
    entries = [json.loads(line) for line in Path(summary["log_path"]).read_text(encoding="utf-8").strip().splitlines()]
    # 工具环留痕仅 --log 时：user → tool_call → tool_result → assistant → slash
    assert [e["kind"] for e in entries] == ["user", "tool_call", "tool_result", "assistant", "user", "slash"]
    assert json.loads(entries[1]["content"]) == {"tool": "search", "args": {"target": "total"}}
    assert "pkg.calc.total" in entries[2]["content"]
    # 红线：日志零 key
    assert API_KEY not in Path(summary["log_path"]).read_text(encoding="utf-8")
