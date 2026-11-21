"""C9 修复提案闭环测试（设计稿-C9-修复提案闭环.md D1-D8）。

单元：load_finding 诚实拒绝/build_material 全文材料+三种拒绝/
validate_draft 形状复用+三加验+unfixable 分支。
E2E（chat_fn 注入）：happy path / 拒收重试 / 3 拒中止 / unfixable 转义 /
空响应快速失败 / 防覆写 / 同秒后缀 / key 零落盘。
红线：fixpropose 源码无执行面（仅 load_transaction 单一契约源）。
"""

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from vrp import etl, fixpropose
from vrp.s6_transact import load_transaction

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

API_KEY = "sk-test-secret-123"


@pytest.fixture(autouse=True)
def _fast_git_probe(monkeypatch):
    """本机 git rev-parse 固定 ~10s（AV/低优先级环境成本）——git 探针语义已由
    tests/test_facts_freshness.py 覆盖，本文件统一桩掉探针省 14×10s；freshness
    匹配逻辑照常真实运行（fixture meta git_head='' == 桩值 '' → fresh；
    test_run_facts_stale 的 'deadbeef' 失配照常硬崩）。"""
    monkeypatch.setattr(etl, "current_git_head", lambda repo: "")

CALC = "import os\n\n\ndef total(a, b):\n    return a + b\n"

VALID_DRAFT = {
    "id": "fix-1",
    "type": "T2",
    "summary": "drop unused import",
    "declared_moves": [],
    "edits": [{"file": "pkg/calc.py", "op": "modify", "content": "def total(a, b):\n    return a + b\n"}],
}


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid,"
        " file_path, start_line, end_line, payload_json)"
        " VALUES ('P1', 'dead-import', 's2-deadcode', NULL, NULL, 'pkg/calc.py', 1, 1,"
        " '{\"reason\": \"import os is unused\"}')"
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "calc.py").write_text(CALC, encoding="utf-8")
    _write_config(repo)
    return repo


def _write_config(repo: Path) -> None:
    payload = {"llm": {"base_url": "http://llm.example/v1", "api_key": API_KEY, "model": "test-model"}}
    (repo / ".vrp" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_draft(tmp_path: Path, draft) -> Path:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft), encoding="utf-8")
    return path


def _fake_chat(responses):
    prompts = []

    def call(config, messages):
        prompts.append((config, messages))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return dict(item)
        return {"content": item, "finish_reason": "stop", "usage": {}}

    call.prompts = prompts
    return call


# ---------- load_finding ----------

def test_load_finding_missing_id(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        with pytest.raises(ValueError, match="not found"):
            fixpropose.load_finding(con, 99)
    finally:
        con.close()


def test_load_finding_no_file_path(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute(
            "INSERT INTO findings (pathology, kind, source, payload_json)"
            " VALUES ('P2', 'cyclic', 'xcheck', '{}')"
        )
        con.commit()
        finding_id = con.execute("SELECT MAX(id) FROM findings").fetchone()[0]
        with pytest.raises(ValueError, match="no file_path"):
            fixpropose.load_finding(con, finding_id)
    finally:
        con.close()


def test_load_finding_roundtrip(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    assert finding["id"] == 1
    assert finding["file_path"] == "pkg/calc.py"
    assert finding["pathology"] == "P1"
    assert finding["start_line"] == 1
    assert json.loads(finding["payload_json"])["reason"] == "import os is unused"


# ---------- build_material ----------

def test_build_material_content_and_gutter(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    material = fixpropose.build_material(repo, finding)
    assert "## Finding to fix (id: 1)" in material
    assert '"file_path": "pkg/calc.py"' in material
    assert '"reason": "import os is unused"' in material  # payload 解析为 JSON
    assert "## Current content of pkg/calc.py (5 lines" in material
    assert "1: import os" in material
    assert "5:     return a + b" in material


def test_build_material_payload_non_json_stays_string(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute("UPDATE findings SET payload_json = 'plain text payload' WHERE id = 1")
        con.commit()
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    material = fixpropose.build_material(repo, finding)
    assert '"payload": "plain text payload"' in material


def test_build_material_file_missing(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "pkg" / "calc.py").unlink()
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    with pytest.raises(ValueError, match="missing on disk"):
        fixpropose.build_material(repo, finding)


def test_build_material_non_utf8(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "pkg" / "calc.py").write_bytes(b"\xff\xfe\x00binary")
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    with pytest.raises(ValueError, match="not valid UTF-8"):
        fixpropose.build_material(repo, finding)


def test_build_material_too_large(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "pkg" / "calc.py").write_text("x" * (fixpropose.MAX_FILE_CHARS + 1), encoding="utf-8")
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    with pytest.raises(ValueError, match="too large"):
        fixpropose.build_material(repo, finding)


# ---------- validate_draft ----------

def test_validate_draft_accepted(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    draft_path = _write_draft(tmp_path, VALID_DRAFT)
    receipt, tx = fixpropose.validate_draft(VALID_DRAFT, draft_path, finding)
    assert receipt["status"] == "accepted"
    assert tx["id"] == "fix-1"
    assert tx["edits"][0]["file"].as_posix() == "pkg/calc.py"
    assert tx["edits"][0]["op"] == "modify"


@pytest.mark.parametrize(
    "draft,code",
    [
        (dict(VALID_DRAFT, id="fix-9"), "PROPOSAL_WRONG_ID"),
        (
            dict(VALID_DRAFT, edits=[dict(VALID_DRAFT["edits"][0], file="pkg/other.py")]),
            "PROPOSAL_WRONG_FILE",
        ),
        (
            dict(VALID_DRAFT, edits=[dict(VALID_DRAFT["edits"][0], op="create")]),
            "PROPOSAL_WRONG_OP",
        ),
        ({"id": "fix-1", "type": "T2", "edits": []}, "PROPOSAL_TX_SHAPE"),
        ({"id": "fix-1", "unfixable": True}, "PROPOSAL_UNFIXABLE_SHAPE"),
    ],
)
def test_validate_draft_rejections(tmp_path, draft, code):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    draft_path = _write_draft(tmp_path, draft)
    receipt, tx = fixpropose.validate_draft(draft, draft_path, finding)
    assert receipt["status"] == "rejected"
    assert receipt["errors"][0]["code"] == code
    assert tx is None


def test_validate_draft_unfixable_ok(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        finding = fixpropose.load_finding(con, 1)
    finally:
        con.close()
    draft = {"id": "fix-1", "unfixable": True, "reason": "  cross-file fix required  "}
    receipt, tx = fixpropose.validate_draft(draft, _write_draft(tmp_path, draft), finding)
    assert receipt["status"] == "unfixable"
    assert receipt["reason"] == "cross-file fix required"
    assert tx is None


# ---------- run E2E (fake chat_fn) ----------

def test_run_happy_path(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat([json.dumps(VALID_DRAFT)])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 0
    assert summary["status"] == "accepted"
    assert summary["finding_id"] == 1
    assert summary["model"] == "test-model"
    assert len(summary["attempts"]) == 1
    assert summary["attempts"][0]["outcome"] == "accepted"
    assert summary["next"].startswith(f"vrp transact {repo}")
    # 交付物：草案可 round-trip 过 load_transaction（归一化 edits.file 为 as_posix 字符串）
    proposal_path = repo / ".vrp" / "proposals" / "fix-1.json"
    assert proposal_path.exists()
    tx = load_transaction(proposal_path)
    assert tx["id"] == "fix-1"
    assert tx["edits"][0]["file"].as_posix() == "pkg/calc.py"
    # 留痕完整性
    audit = Path(summary["audit_dir"])
    steps = audit / "steps"
    for name in (
        "config.echo.json",
        "summary.json",
    ):
        assert (audit / name).exists(), f"missing audit file: {name}"
    for name in (
        "01-finding.json",
        "02-fixpropose-attempt-1.request.json",
        "02-fixpropose-attempt-1.response.txt",
        "02-fixpropose-attempt-1.meta.json",
        "02-fixpropose-attempt-1.draft.json",
        "02-fixpropose-attempt-1.receipt.json",
    ):
        assert (steps / name).exists(), f"missing audit file: {name}"
    # 红线：key 零落盘（扫描整个留痕树+交付物）
    for path in (repo / ".vrp" / "fixpropose").rglob("*"):
        if path.is_file():
            assert API_KEY not in path.read_text(encoding="utf-8", errors="replace"), f"key leaked: {path}"
    # 材料面：finding JSON + 行号檐口全文 + 契约句
    config, messages = fake.prompts[0]
    assert config.model == "test-model"
    assert "fix-proposal AI" in messages[0]["content"]
    assert "transaction DRAFT only" in messages[0]["content"]
    assert "PROPOSAL_WRONG_ID" in messages[0]["content"]
    assert "## Finding to fix (id: 1)" in messages[1]["content"]
    assert "1: import os" in messages[1]["content"]


def test_run_reject_then_accept(tmp_path):
    repo = _mk_repo(tmp_path)
    bad = dict(VALID_DRAFT, id="fix-9")
    fake = _fake_chat([json.dumps(bad), json.dumps(VALID_DRAFT)])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 0
    assert summary["status"] == "accepted"
    assert len(summary["attempts"]) == 2
    assert summary["attempts"][0]["outcome"] == "rejected"
    assert summary["attempts"][0]["errors"][0]["code"] == "PROPOSAL_WRONG_ID"
    # 回执反馈进第二次请求
    assert "Previous attempt rejected" in fake.prompts[1][1][1]["content"]
    assert "PROPOSAL_WRONG_ID" in fake.prompts[1][1][1]["content"]
    assert (Path(summary["audit_dir"]) / "steps" / "02-fixpropose-attempt-2.draft.json").exists()


def test_run_three_rejections_abort(tmp_path):
    repo = _mk_repo(tmp_path)
    bad = json.dumps(dict(VALID_DRAFT, id="fix-9"))
    fake = _fake_chat([bad, bad, bad])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 1
    assert summary["status"] == "failed"
    assert len(summary["attempts"]) == fixpropose.MAX_ATTEMPTS
    assert not (repo / ".vrp" / "proposals" / "fix-1.json").exists()
    assert summary["proposal"] is None


def test_run_parse_error_then_accept(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat(["not json at all", json.dumps(VALID_DRAFT)])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 0
    assert summary["attempts"][0]["outcome"] == "parse_error"
    # parse_error 不留 draft.json（无草案可写）
    assert not (Path(summary["audit_dir"]) / "steps" / "02-fixpropose-attempt-1.draft.json").exists()


def test_run_unfixable_verdict(tmp_path):
    repo = _mk_repo(tmp_path)
    verdict = {"id": "fix-1", "unfixable": True, "reason": "fix requires editing a second file"}
    fake = _fake_chat([json.dumps(verdict)])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 0
    assert summary["status"] == "unfixable"
    assert summary["unfixable_reason"] == "fix requires editing a second file"
    assert summary["proposal"] is None
    assert not (repo / ".vrp" / "proposals" / "fix-1.json").exists()


def test_run_empty_response_length_fails_fast(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat([{"content": "", "finish_reason": "length", "usage": {}}])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 1
    assert summary["status"] == "failed"
    assert len(summary["attempts"]) == 1  # 快速失败：只烧一次
    assert summary["attempts"][0]["outcome"] == "request_error"
    assert "max_output_tokens" in summary["attempts"][0]["detail"]


def test_run_request_error_fails_fast(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat([ValueError("connection refused")])
    summary = fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert summary["rc"] == 1
    assert len(summary["attempts"]) == 1
    assert summary["attempts"][0]["outcome"] == "request_error"
    assert "connection refused" in summary["attempts"][0]["detail"]


def test_run_proposal_exists_refuses_without_force(tmp_path):
    repo = _mk_repo(tmp_path)
    proposals = repo / ".vrp" / "proposals"
    proposals.mkdir(parents=True)
    (proposals / "fix-1.json").write_text("{}")
    fake = _fake_chat([json.dumps(VALID_DRAFT)])
    with pytest.raises(ValueError, match="already exists"):
        fixpropose.run_fixpropose(repo, 1, chat_fn=fake)
    assert fake.prompts == []  # 拒绝发生在请求之前
    assert (proposals / "fix-1.json").read_text(encoding="utf-8") == "{}"  # 原文件不动


def test_run_force_overwrites(tmp_path):
    repo = _mk_repo(tmp_path)
    proposals = repo / ".vrp" / "proposals"
    proposals.mkdir(parents=True)
    (proposals / "fix-1.json").write_text("{}")
    fake = _fake_chat([json.dumps(VALID_DRAFT)])
    summary = fixpropose.run_fixpropose(repo, 1, force=True, chat_fn=fake)
    assert summary["rc"] == 0
    assert load_transaction(proposals / "fix-1.json")["id"] == "fix-1"


def test_run_model_override(tmp_path):
    repo = _mk_repo(tmp_path)
    fake = _fake_chat([json.dumps(VALID_DRAFT)])
    summary = fixpropose.run_fixpropose(repo, 1, model_override="other-model", chat_fn=fake)
    assert summary["model"] == "other-model"
    assert fake.prompts[0][0].model == "other-model"


def test_run_id_same_second_collision_gets_suffix(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    frozen = time.gmtime()
    monkeypatch.setattr(fixpropose.time, "gmtime", lambda: frozen)
    s1 = fixpropose.run_fixpropose(repo, 1, chat_fn=_fake_chat([json.dumps(VALID_DRAFT)]))
    s2 = fixpropose.run_fixpropose(repo, 1, force=True, chat_fn=_fake_chat([json.dumps(VALID_DRAFT)]))
    assert s1["rc"] == 0 and s2["rc"] == 0
    assert s1["run_id"] != s2["run_id"]
    assert s2["run_id"].endswith("-2")
    # 两次留痕独立完整
    for s in (s1, s2):
        assert (Path(s["audit_dir"]) / "summary.json").exists()
        assert (Path(s["audit_dir"]) / "config.echo.json").exists()
        assert (Path(s["audit_dir"]) / "steps" / "01-finding.json").exists()


def test_run_missing_config(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp" / "config.json").unlink()
    with pytest.raises(ValueError, match="config missing"):
        fixpropose.run_fixpropose(repo, 1)


def test_run_missing_facts_db(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    _write_config(repo)
    with pytest.raises(SystemExit, match="facts.db missing"):
        fixpropose.run_fixpropose(repo, 1)


def test_run_facts_stale(tmp_path):
    repo = _mk_repo(tmp_path)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        con.execute("UPDATE meta SET value = 'deadbeef' WHERE key = 'git_head'")
        con.commit()
    finally:
        con.close()
    with pytest.raises(RuntimeError, match="stale"):
        fixpropose.run_fixpropose(repo, 1)


# ---------- CLI 接线 ----------

def test_cli_fixpropose_parser_wired():
    from vrp import cli

    parser = cli.build_parser()
    args = parser.parse_args(["fixpropose", "some-repo", "3", "--model", "m2", "--force"])
    assert args.func is cli.cmd_fixpropose
    assert args.repo == "some-repo"
    assert args.finding_id == 3
    assert args.model == "m2"
    assert args.force is True
    args_default = parser.parse_args(["fixpropose", "r", "1"])
    assert args_default.model == ""
    assert args_default.force is False


# ---------- 红线：fixpropose 源码无执行面 ----------

def test_fixpropose_redline_no_execution_surface():
    """C9 红线钉死：LLM 输出物理上只能变成草案文件。

    事务面唯一合法引用 = from .s6_transact import load_transaction（形状校验）；
    禁 s6_transact 限定引用（transact/preview 执行面）、禁子进程、禁 git 写。
    """
    src = Path(fixpropose.__file__).read_text(encoding="utf-8")
    assert "from .s6_transact import load_transaction" in src
    assert not re.search(r"s6_transact\s*\.", src), "no qualified s6_transact access (execution surface)"
    assert not re.search(r"(?<!load_)transact\(", src), "must not call the transact gate"
    assert "preview_transaction" not in src
    assert "transact_draft" not in src
    assert "subprocess" not in src
    assert "from . import procs" not in src
    assert not re.search(r"\bprocs\.", src)
    assert "git push" not in src and "git commit" not in src and "git add" not in src


def test_fixpropose_prompts_are_english():
    """prompt 默认英文句（auto_curate 同型红线）：契约常量零 CJK。"""
    for token in (fixpropose._PROMPT_PREAMBLE, fixpropose._FIX_CONTRACT):
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in token)
    assert "fix-proposal AI" in fixpropose._PROMPT_PREAMBLE
