"""内置 LLM 梳理循环测试（构想v7 §13 G 批次）。

单元：config 解析/校验/脱敏、chat 传输重试、extract_json 剥离。
E2E：注入 fake chat_fn 跑六步循环——happy path / 拒收重试 / 3 拒中止。
红线：prompt 默认英文句、留痕零 key、循环源码无重构事务面引用。
"""

import json
import sqlite3
from pathlib import Path
from urllib.error import URLError

import pytest

from vrp import auto_curate, llm_client
from vrp.llm_client import LlmConfig, chat, extract_json, load_config

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

API_KEY = "sk-test-secret-123"


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
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES ('f2', 'p', 'pkg.calc.double', 'Function', 'double', 'pkg/calc.py', 5, 7, 'h')"
    )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('f1', 'f2', 'CALLS', 'structural')")
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "calc.py").write_text("def total(a, b):\n    return a + b\n", encoding="utf-8")
    _write_config(repo)
    return repo


def _write_config(repo: Path, llm: dict | None = None) -> None:
    payload = {"llm": llm or {"base_url": "http://llm.example/v1", "api_key": API_KEY, "model": "test-model"}}
    (repo / ".vrp" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


VALID_SLICES = [
    {
        "slice_id": "calc-flow",
        "name": "Calculation flow",
        "summary": "total feeds double across the calc module",
        "steps": [{"fid": "f1", "role": "entry"}, {"fid": "f2", "role": "process"}],
    }
]

VALID_STRUCTURE = {
    "modules": [
        {
            "module_id": "calc",
            "name": "Calc",
            "responsibility": "Arithmetic helpers",
            "symbol_fids": ["f1", "f2"],
            "layer": "domain",
        }
    ],
    "layers": [{"name": "domain", "may_depend_on": []}],
}


# ---------- config ----------

def test_load_config_missing_file(tmp_path):
    with pytest.raises(ValueError, match="config missing"):
        load_config(tmp_path)


def test_load_config_inline_key_and_echo_masks(tmp_path):
    repo = tmp_path
    (repo / ".vrp").mkdir()
    _write_config(repo)
    config = load_config(repo)
    assert config.base_url == "http://llm.example/v1"
    assert config.model == "test-model"
    assert config.api_key == API_KEY
    assert config.api_key_source == "inline"
    echo = config.echo()
    assert echo["api_key"] == "***"
    assert API_KEY not in json.dumps(echo)


def test_load_config_env_key(tmp_path):
    repo = tmp_path
    (repo / ".vrp").mkdir()
    _write_config(repo, {"base_url": "http://x/v1/", "api_key_env": "VRP_KEY", "model": "m"})
    config = load_config(repo, env={"VRP_KEY": "k-1"})
    assert config.api_key == "k-1"
    assert config.api_key_source == "env:VRP_KEY"
    assert config.base_url == "http://x/v1"  # 尾斜杠剥除
    with pytest.raises(ValueError, match="unset environment variable"):
        load_config(repo, env={})


@pytest.mark.parametrize(
    "llm,match",
    [
        ("__MISSING__", "missing 'llm'"),
        ({"model": "m", "api_key": "k"}, "base_url"),
        ({"base_url": "ftp://x", "model": "m", "api_key": "k"}, "base_url"),
        ({"base_url": "http://x", "api_key": "k"}, "model"),
        ({"base_url": "http://x", "model": "m"}, "API key"),
        ({"base_url": "http://x", "model": "m", "api_key": "k", "timeout": 0}, "timeout"),
        ({"base_url": "http://x", "model": "m", "api_key": "k", "max_output_tokens": "x"}, "max_output_tokens"),
    ],
)
def test_load_config_invalid(tmp_path, llm, match):
    repo = tmp_path
    (repo / ".vrp").mkdir()
    if llm == "__MISSING__":
        (repo / ".vrp" / "config.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    else:
        _write_config(repo, llm)
    with pytest.raises(ValueError, match=match):
        load_config(repo)


def test_load_config_bad_json(tmp_path):
    repo = tmp_path
    (repo / ".vrp").mkdir()
    (repo / ".vrp" / "config.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        load_config(repo)


def test_load_config_bom_tolerated(tmp_path):
    """dogfood 抓到的真坑：PS 5.1/记事本写 UTF-8 BOM 配置必须可读（utf-8-sig）。"""
    repo = tmp_path
    (repo / ".vrp").mkdir()
    payload = json.dumps({"llm": {"base_url": "http://x/v1", "api_key": "k", "model": "m"}})
    (repo / ".vrp" / "config.json").write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    config = load_config(repo)
    assert config.model == "m"


def test_load_config_thinking_toggle(tmp_path):
    """思考开关：显式配置才发送（智谱 thinking 模型 reasoning 烧光 tokens 实证）。"""
    repo = tmp_path
    (repo / ".vrp").mkdir()
    base = {"base_url": "http://x/v1", "api_key": "k", "model": "m"}
    _write_config(repo, {**base, "thinking": "disabled"})
    config = load_config(repo)
    assert config.thinking == "disabled"
    assert config.echo()["thinking"] == "disabled"
    _write_config(repo, base)
    assert load_config(repo).thinking is None
    _write_config(repo, {**base, "thinking": "sometimes"})
    with pytest.raises(ValueError, match="thinking"):
        load_config(repo)


# ---------- chat client ----------

def _fake_transport(statuses):
    calls = []

    def post(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        status, text = statuses[len(calls) - 1]
        if isinstance(status, Exception):
            raise status
        return status, text

    return post, calls


def _ok_body(content="{}"):
    return json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}})


def test_chat_retry_429_then_ok():
    post, calls = _fake_transport([(429, "busy"), (200, _ok_body('{"a": 1}'))])
    sleeps = []
    result = chat(_cfg(), [{"role": "user", "content": "x"}], transport=post, sleep=sleeps.append)
    assert len(calls) == 2
    assert sleeps == [2.0]
    assert result["content"] == '{"a": 1}'
    assert result["finish_reason"] == "stop"
    assert "thinking" not in calls[0]["body"]  # 未配置不发送


def test_chat_thinking_param_sent_when_configured(tmp_path):
    repo = tmp_path
    (repo / ".vrp").mkdir()
    _write_config(
        repo,
        {"base_url": "http://llm.example/v1", "api_key": API_KEY, "model": "test-model", "thinking": "disabled"},
    )
    config = load_config(repo)
    post, calls = _fake_transport([(200, _ok_body())])
    chat(config, [{"role": "user", "content": "x"}], transport=post, sleep=lambda s: None)
    assert calls[0]["body"]["thinking"] == {"type": "disabled"}


def test_chat_4xx_no_retry():
    post, calls = _fake_transport([(401, "bad key")])
    with pytest.raises(ValueError, match="HTTP 401"):
        chat(_cfg(), [{"role": "user", "content": "x"}], transport=post, sleep=lambda s: None)
    assert len(calls) == 1


def test_chat_retry_exhausted():
    post, calls = _fake_transport([(500, "a"), (500, "b"), (500, "c")])
    with pytest.raises(ValueError, match="HTTP 500: c"):
        chat(_cfg(), [{"role": "user", "content": "x"}], transport=post, sleep=lambda s: None)
    assert len(calls) == 3


def test_chat_network_error():
    post, _ = _fake_transport([(URLError("conn refused"), "")])
    with pytest.raises(ValueError, match="conn refused"):
        chat(_cfg(), [{"role": "user", "content": "x"}], transport=post)


def test_post_json_wire_body_cjk_unescaped(monkeypatch):
    """R-Z：请求体 wire 字节 CJK 原样（ensure_ascii=False）——转义形态 wire 体积 ~2x。

    transport 注入在 dict 层（chat 的测试面），序列化发生在 _post_json 内部，
    故在此 monkeypatch urlopen 捕获 Request.data 做 wire 级断言。
    """
    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"choices": []}'

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    status, text = llm_client._post_json(
        "http://llm.example/v1/chat/completions",
        {"Authorization": "Bearer k"},
        {"messages": [{"role": "user", "content": "重构中文提示"}]},
        30,
    )
    assert (status, text) == (200, '{"choices": []}')
    assert "重构中文提示".encode("utf-8") in captured["data"]
    assert b"\\u91cd" not in captured["data"]  # ensure_ascii=True 的转义形态不得回潮


def test_chat_bad_response_shape():
    post, _ = _fake_transport([(200, '{"unexpected": true}')])
    with pytest.raises(ValueError, match="choices"):
        chat(_cfg(), [{"role": "user", "content": "x"}], transport=post)


def _cfg():
    return LlmConfig("http://llm.example/v1", "test-model", API_KEY, 30, 128, "inline")


# ---------- extract_json ----------

@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go:\n```\n[{"a": 1}]\n```\nDone.', [{"a": 1}]),
        ('prefix {"a": [1, 2]} suffix', {"a": [1, 2]}),
        ("[1, 2, 3]", [1, 2, 3]),
    ],
)
def test_extract_json_variants(text, expected):
    assert extract_json(text) == expected


def test_extract_json_invalid():
    with pytest.raises(ValueError, match="unparseable|no JSON"):
        extract_json("no json here at all")


def test_extract_json_multiblock_shorter_json_recovered():
    """R-AK：最长围栏块非 JSON（LLM 夹带解释代码块）时，较短有效 JSON 块须被采纳，
    而非白耗一次验证重试额度（auto_curate MAX_ATTEMPTS 循环）。"""
    content = (
        "Here is the plan:\n"
        "```bash\n"
        "pip install vrp && vrp curate --run --verbose --dry-run --with-all-extras\n"
        "```\n"
        "The artifact:\n"
        "```json\n"
        '{"slices": [{"id": "calc-flow"}]}\n'
        "```\n"
    )
    assert extract_json(content) == {"slices": [{"id": "calc-flow"}]}


def test_extract_json_multiblock_longest_json_still_wins():
    """R-AK 防倒退 pin：多块均含 JSON 时保持最长块优先的既有语义。"""
    content = (
        "```json\n"
        '{"short": 1}\n'
        "```\n"
        "```json\n"
        '{"longer": {"nested": [1, 2, 3]}, "pad": "xxxxxxxxxxxxxxxx"}\n'
        "```\n"
    )
    assert extract_json(content) == {"longer": {"nested": [1, 2, 3]}, "pad": "xxxxxxxxxxxxxxxx"}


# ---------- loop E2E (fake chat_fn) ----------

def _fake_chat(responses):
    """按消息内容路由：system 含 slices 契约 → slices 队列；含 structure → structure 队列。"""
    prompts = []
    state = {"slices": list(responses.get("slices", [])), "structure": list(responses.get("structure", []))}

    def call(config, messages):
        prompts.append(messages)
        system = messages[0]["content"]
        if "business-line slices" in system:
            queue = "slices"
        elif "overall structure" in system:
            queue = "structure"
        else:
            raise AssertionError(f"unexpected system prompt: {system[:80]}")
        item = state[queue].pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return dict(item)
        return {"content": item, "finish_reason": "stop", "usage": {}}

    call.prompts = prompts
    return call


def test_run_happy_path(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat({"slices": [json.dumps(VALID_SLICES)], "structure": [json.dumps(VALID_STRUCTURE)]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 0
    assert summary["slices"]["status"] == "accepted"
    assert summary["structure"]["status"] == "accepted"
    assert len(summary["slices"]["attempts"]) == 1
    # DB 终态
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        assert con.execute("SELECT status FROM slices WHERE slice_id='calc-flow'").fetchone()[0] == "active"
        assert con.execute("SELECT status FROM structure ORDER BY id DESC LIMIT 1").fetchone()[0] == "active"
    finally:
        con.close()
    # 留痕完整性
    audit = Path(summary["audit_dir"])
    assert (audit / "config.echo.json").exists()
    steps = audit / "steps"
    for name in (
        "01-dossier.json",
        "02-slices-attempt-1.request.json",
        "02-slices-attempt-1.response.txt",
        "02-slices-attempt-1.meta.json",
        "02-slices-attempt-1.artifact.json",
        "02-slices-attempt-1.receipt.json",
        "03-structure-attempt-1.receipt.json",
        "04-enforce.json",
        "05-drift.json",
    ):
        assert (steps / name).exists(), f"missing audit file: {name}"
    assert (audit / "summary.json").exists()
    log_line = (repo / ".vrp" / "auto" / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(log_line)["rc"] == 0
    # 红线：prompt 含默认英文句 + 按步骤裁剪的材料（真实冒烟修正）：
    # slices 材料只给 grounding（无 dossier 噪声），dossier 服务 structure
    for messages in fake.prompts:
        assert "Default output language for curated content" in messages[0]["content"]
        assert "English" in messages[0]["content"]
    assert "## Real symbols" in fake.prompts[0][1]["content"]
    assert "## Real call edges" in fake.prompts[0][1]["content"]
    assert "## Suggested step chains" in fake.prompts[0][1]["content"]
    assert f"{('f1')} pkg.calc.total -> f2 pkg.calc.double (CALLS)" in fake.prompts[0][1]["content"]
    assert "Intake material: dossier" not in fake.prompts[0][1]["content"]  # slices 步无 dossier
    assert "Intake material: dossier" in fake.prompts[1][1]["content"]  # structure 步有 dossier
    # 红线：key 零落盘（扫描整个留痕树）
    for path in (repo / ".vrp" / "auto").rglob("*"):
        if path.is_file():
            assert API_KEY not in path.read_text(encoding="utf-8", errors="replace"), f"key leaked: {path}"


def test_run_id_same_second_collision_gets_suffix(tmp_path, monkeypatch):
    """R-AC：同秒双跑 run_id 碰撞——session._next_session_id 有 -2/-3 后缀对称机制，
    auto_curate 修前无（mkdir exist_ok=True 静默合并 audit 目录，覆写前一次留痕）。
    冻结 gmtime 钉住同秒窗口，断言两次留痕独立完整。"""
    import time as _time

    repo = _mk_repo(tmp_path)
    frozen = _time.gmtime()
    monkeypatch.setattr(auto_curate.time, "gmtime", lambda: frozen)
    fake1 = _fake_chat({"slices": [json.dumps(VALID_SLICES)], "structure": [json.dumps(VALID_STRUCTURE)]})
    s1 = auto_curate.run_auto_curate(repo, chat_fn=fake1)
    assert s1["rc"] == 0
    fake2 = _fake_chat({"slices": [json.dumps(VALID_SLICES)], "structure": [json.dumps(VALID_STRUCTURE)]})
    s2 = auto_curate.run_auto_curate(repo, chat_fn=fake2)
    # 同秒 → 后缀分岔，audit 目录独立
    assert s1["run_id"] != s2["run_id"]
    assert s2["run_id"].endswith("-2")
    assert Path(s1["audit_dir"]) != Path(s2["audit_dir"])
    # 两次留痕各自完整
    for s in (s1, s2):
        assert (Path(s["audit_dir"]) / "summary.json").exists()
        assert (Path(s["audit_dir"]) / "config.echo.json").exists()
        assert (Path(s["audit_dir"]) / "steps" / "01-dossier.json").exists()
    # log.jsonl 两条独立记录（修前第二次跑覆写第一条 summary 且 run_id 重复）
    lines = (repo / ".vrp" / "auto" / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    ids = [json.loads(line)["run_id"] for line in lines]
    assert ids == [s1["run_id"], s2["run_id"]]


def test_run_reject_then_accept(tmp_path):
    repo = _mk_repo(tmp_path)
    bad = [{"slice_id": "x", "name": "X", "summary": "s", "steps": [{"fid": "nope"}]}]
    fake = _fake_chat({"slices": [json.dumps(bad), json.dumps(VALID_SLICES)], "structure": [json.dumps(VALID_STRUCTURE)]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 0
    assert len(summary["slices"]["attempts"]) == 2
    assert summary["slices"]["attempts"][0]["outcome"] == "rejected"
    assert summary["slices"]["attempts"][0]["errors"][0]["code"] == "SLICE_UNKNOWN_FID"
    # 回执反馈进第二次请求
    assert "Previous attempt rejected" in fake.prompts[1][1]["content"]
    assert "SLICE_UNKNOWN_FID" in fake.prompts[1][1]["content"]


def test_run_three_rejections_abort(tmp_path):
    repo = _mk_repo(tmp_path)
    bad = json.dumps([{"slice_id": "x", "name": "X", "summary": "s", "steps": [{"fid": "nope"}]}])
    fake = _fake_chat({"slices": [bad, bad, bad]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 1
    assert summary["slices"]["status"] == "failed"
    assert len(summary["slices"]["attempts"]) == auto_curate.MAX_ATTEMPTS
    assert summary["structure"]["status"] == "skipped"
    steps = Path(summary["audit_dir"]) / "steps"
    assert not list(steps.glob("03-structure-*"))
    # enforce/drift 仍诚实落盘（库态报告）
    assert (steps / "04-enforce.json").exists()
    assert (steps / "05-drift.json").exists()


def test_run_structure_failure_rc1(tmp_path):
    repo = _mk_repo(tmp_path)
    bad_structure = json.dumps({"modules": [], "layers": []})
    fake = _fake_chat({"slices": [json.dumps(VALID_SLICES)], "structure": [bad_structure, bad_structure, bad_structure]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 1
    assert summary["slices"]["status"] == "accepted"
    assert summary["structure"]["status"] == "failed"
    assert (Path(summary["audit_dir"]) / "steps" / "04-enforce.json").exists()


def test_run_parse_error_counts_attempt(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat({"slices": ["not json at all", json.dumps(VALID_SLICES)], "structure": [json.dumps(VALID_STRUCTURE)]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 0
    assert summary["slices"]["attempts"][0]["outcome"] == "parse_error"


def test_run_empty_response_length_fails_fast(tmp_path):
    """推理模型烧光预算（content=''+finish=length）→ 快速诚实失败，不烧重试额度。"""
    repo = _mk_repo(tmp_path)
    fake = _fake_chat({"slices": [{"content": "", "finish_reason": "length", "usage": {}}]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 1
    assert summary["slices"]["status"] == "failed"
    assert len(summary["slices"]["attempts"]) == 1  # 快速失败：只烧一次
    assert "max_output_tokens" in summary["slices"]["attempts"][0]["detail"]
    assert summary["structure"]["status"] == "skipped"


def test_run_truncated_json_hints_shorter(tmp_path):
    """截断 JSON（finish=length）→ parse_error 反馈带「keep the artifact short」提示。"""
    repo = _mk_repo(tmp_path)
    truncated = {"content": '[{"slice_id": "x", "name": "X"', "finish_reason": "length", "usage": {}}
    fake = _fake_chat({"slices": [truncated, json.dumps(VALID_SLICES)], "structure": [json.dumps(VALID_STRUCTURE)]})
    summary = auto_curate.run_auto_curate(repo, chat_fn=fake)
    assert summary["rc"] == 0
    assert "keep the artifact short" in summary["slices"]["attempts"][0]["detail"]


def test_run_missing_config(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "config.json").unlink()
    with pytest.raises(ValueError, match="config missing"):
        auto_curate.run_auto_curate(repo)


def test_run_missing_facts_db(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    _write_config(repo)
    with pytest.raises(SystemExit, match="facts.db missing"):
        auto_curate.run_auto_curate(repo)


# ---------- 材料面 grounding（真实冒烟实证后增） ----------

def test_symbol_edge_material_grounding(tmp_path):
    """grounding：真实符号表+边样本+伪符号排除+确定性序+截断诚实标注。"""
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES ('fz', 'p', 'pkg.z', 'File', 'z', 'pkg/z.ts', NULL, NULL, 'h')"
        )  # File 伪符号：file_path 有但 start_line NULL → 排除
        con.commit()
        text = auto_curate._symbol_edge_material(con)
    finally:
        con.close()
    assert "## Real symbols (2 of 2; fid + qualified_name + location)" in text
    assert "## Real call edges (1 of 1; consecutive slice steps MUST be such a pair)" in text
    assert "f1 pkg.calc.total pkg/calc.py:1" in text
    assert "f2 pkg.calc.double pkg/calc.py:5" in text
    assert "f1 pkg.calc.total -> f2 pkg.calc.double (CALLS)" in text
    assert "## Suggested step chains (1;" in text
    assert "f1 pkg.calc.total -> f2 pkg.calc.double" in text
    assert "fz" not in text  # 伪符号排除
    assert "showing first" not in text  # 未截断不标注


def test_symbol_edge_material_truncation_honest(tmp_path):
    """超 cap 截断注记诚实。"""
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        for i in range(auto_curate.MATERIAL_SYMBOLS_CAP + 5):
            con.execute(
                "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
                f" VALUES ('x{i}', 'p', 'pkg.x{i}', 'Function', 'x{i}', 'pkg/x{i}.py', 1, 2, 'h')"
            )
        con.commit()
        text = auto_curate._symbol_edge_material(con)
    finally:
        con.close()
    assert f"showing first {auto_curate.MATERIAL_SYMBOLS_CAP} in deterministic order" in text


# ---------- 红线：循环源码无重构事务面 ----------

def test_no_refactor_transaction_surface():
    for module in (auto_curate, llm_client):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert "s6_transact" not in src, f"{module.__name__} must not reference the transaction surface"
        assert "tx_run" not in src
