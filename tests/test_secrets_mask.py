# -*- coding: utf-8 -*-
"""报告产物密钥卫生（交接 #141，学 code-modernization/unwind 01b #13）测试。

覆盖：单行高置信探测器（云 token/JWT/URL 凭据）掩蔽+前 4 字符预览/
私钥块多行状态机（BEGIN..END 整行掩蔽；截断无 END 偏保守全掩蔽）/
良性代码零误伤/确定性+输入不变/build_chain 渲染层联动（行数与 #L 锚点
保持、secrets_hits 进返回值、notes 诚实注记）/render._write_secrets_manifest
落盘、确定性覆盖写与无命中陈旧清理。
"""
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from vrp import render, render_chain

SCHEMA = Path(__file__).resolve().parent.parent / "vrp" / "schema" / "facts_schema.sql"

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_mask_inline_patterns():
    cases = [
        (f"key = {AWS_KEY}", "aws-access-key", AWS_KEY),
        ("t = ghp_" + "a" * 36, "github-token", "ghp_" + "a" * 36),
        ("t = github_pat_" + "a" * 22, "github-token", "github_pat_" + "a" * 22),
        ("t = glpat-" + "a1b2c3d4e5f6g7h8i9j0", "gitlab-token",
         "glpat-" + "a1b2c3d4e5f6g7h8i9j0"),
        ("t = xoxb-" + "1234567890abcdef", "slack-token", "xoxb-" + "1234567890abcdef"),
        ("t = AIza" + "a" * 35, "google-api-key", "AIza" + "a" * 35),
        ("t = sk-" + "a" * 24, "openai-key", "sk-" + "a" * 24),
        ("t = sk-ant-" + "a" * 24, "openai-key", "sk-ant-" + "a" * 24),
        ("t = eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 8, "jwt",
         "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 8),
    ]
    for line, typ, secret in cases:
        out, hits = render_chain.mask_source_lines([line])
        assert secret not in out[0], line
        assert f"[VRP-MASKED:{typ}:" in out[0], line
        assert len(out) == 1  # 行数不变（#L 锚点/file_caps 语义保持）
        assert hits == [{"line": 1, "type": typ, "preview": secret[:4]}]


def test_mask_url_credential():
    line = "db = postgres://produser:S3cretPw@db.internal:5432/app"
    out, hits = render_chain.mask_source_lines([line])
    assert "S3cretPw" not in out[0]
    assert "postgres://produser:" in out[0]
    assert "@db.internal:5432/app" in out[0]
    assert "[VRP-MASKED:url-credential:S3cr…]" in out[0]
    assert hits == [{"line": 1, "type": "url-credential", "preview": "S3cr"}]


def test_mask_url_credential_false_positive_guard():
    """tsa 实证误报回归（交接 #141）：裸 URL+mailto 组合无凭据——
    userinfo 收紧为 RFC 3986 用户段字符后不命中、不掩蔽。"""
    line = '"<http://example.com>\\n<mailto:test@example.com>",  # pragma'
    out, hits = render_chain.mask_source_lines([line])
    assert out == [line]
    assert hits == []


def test_mask_private_key_block():
    lines = [
        "import os",
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEpAIBAAKCAQEA7wholelotofbase64stuff",
        "MIIEogIBAAKCAQEA8secondlineofbase64",
        "-----END RSA PRIVATE KEY-----",
        "print('done')",
    ]
    out, hits = render_chain.mask_source_lines(lines)
    assert len(out) == 6
    assert out[0] == "import os"
    assert out[1] == "-----BEGIN RSA PRIVATE KEY-----"  # 标记行保留（非密料）
    assert out[2] == "[VRP-MASKED:private-key-body:MIIE…]"
    assert out[3] == "[VRP-MASKED:private-key-body:MIIE…]"
    assert out[4] == "-----END RSA PRIVATE KEY-----"
    assert out[5] == "print('done')"
    assert hits == [
        {"line": 2, "type": "private-key-header", "preview": ""},
        {"line": 3, "type": "private-key-body", "preview": "MIIE"},
        {"line": 4, "type": "private-key-body", "preview": "MIIE"},
    ]


def test_mask_private_key_block_truncated():
    """BEGIN 后无 END（CHAIN_FILE_LINES_CAP 截断）：后续行偏保守全掩蔽。"""
    lines = ["-----BEGIN PRIVATE KEY-----", "bodyline1", "bodyline2"]
    out, hits = render_chain.mask_source_lines(lines)
    assert out[1] == "[VRP-MASKED:private-key-body:body…]"
    assert out[2] == "[VRP-MASKED:private-key-body:body…]"
    assert hits[-1]["line"] == 3


def test_mask_benign_untouched():
    lines = [
        "url = 'https://example.com/api?q=page'",
        "short = 'AKIAX'",
        "word = 'sk-short'",
        "x = 'xoxp-hello'",
        "from sk_learn import x",
        "fmt = 'eyJshort.token.sig'",
        "normal code line 123",
    ]
    out, hits = render_chain.mask_source_lines(lines)
    assert out == lines
    assert hits == []


def test_mask_long_line_linear():
    """量词上界纪律回归（#176 faulthandler 实录）：无界 scheme `*` 在 2MB
    单行（minified/生成物形态）每起点贪吃全行再回溯，O(n²)≈4e12 步假挂；
    有界 {0,15}/{…,4096} 后单遍线性。宽松墙钟 30s 只作挂→红转换（实际 <1s）。"""
    import time
    t0 = time.time()
    bomb = "a" * (2 * 1024 * 1024)
    out, hits = render_chain.mask_source_lines([bomb])
    assert out == [bomb] and hits == []
    eyj = "eyJ" + "a" * (2 * 1024 * 1024)  # jwt 无上界 {10,} 同型隐患面
    _, hits = render_chain.mask_source_lines([eyj])
    assert hits == []
    assert time.time() - t0 < 30


def test_mask_deterministic():
    lines = [f"k = {AWS_KEY}", "plain", "-----BEGIN PRIVATE KEY-----", "secret-body"]
    a = render_chain.mask_source_lines(lines)
    b = render_chain.mask_source_lines(lines)
    assert a == b
    assert lines[0] == f"k = {AWS_KEY}"  # 输入不变（数据层不动）


def _mk_repo():
    tmp = Path(tempfile.mkdtemp(prefix="t_secrets_"))
    repo = tmp / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return tmp, repo, con


def _seed(con):
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'abc1234')")
    for i, name in enumerate(("alpha", "beta")):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
            " start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, 'pkg/x.py', ?, ?, 'h')",
            (f"fid-{name}", f"pkg.{name}", name, i * 3 + 1, i * 3 + 2))
    con.execute("INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
                (json.dumps({"modules": [
                    {"module_id": "m1", "name": "甲", "responsibility": "负责一",
                     "symbol_fids": ["fid-alpha", "fid-beta"], "layer": "domain"}],
                    "layers": [{"name": "domain", "may_depend_on": []}]}),))
    con.commit()
    con.close()


def _seed_files(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "x.py").write_text(
        "import os\n"
        f"TOKEN = '{AWS_KEY}'\n"
        "def alpha():\n"
        "    return 1\n"
        "def beta():\n"
        "    return 2\n",
        encoding="utf-8")


def test_build_chain_masks_secrets(tmp_path):
    mjs = render.find_archify_mjs()
    if mjs is None or shutil.which("node") is None:
        pytest.skip("archify sandbox / node not available")
    tmp, repo, con = _mk_repo()
    _seed(con)
    _seed_files(repo)
    out = tmp_path / "chain"
    res = render_chain.build_chain(repo, out, mjs)
    assert res["ok"], res["notes"]
    assert res["secrets_hits"] == [
        {"file": "pkg/x.py", "line": 2, "type": "aws-access-key", "preview": "AKIA"}]
    assert any("secrets masked" in n for n in res["notes"])
    page = out / res["file_pages"]["pkg/x.py"]
    html_text = page.read_text(encoding="utf-8")
    assert AWS_KEY not in html_text
    assert "[VRP-MASKED:aws-access-key:AKIA…]" in html_text
    # 行数不变：#L 锚点全在（caps == 页内最大行锚点）
    assert res["file_caps"]["pkg/x.py"] == 6
    assert 'id="L2"' in html_text and 'id="L6"' in html_text
    shutil.rmtree(tmp, ignore_errors=True)


def test_write_secrets_manifest(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    hits = [{"file": "pkg/x.py", "line": 2, "type": "aws-access-key",
             "preview": "AKIA"}]
    render._write_secrets_manifest(repo, hits)
    path = repo / ".vrp" / "SECRETS.local.md"
    text = path.read_text(encoding="utf-8")
    assert "pkg/x.py:2 aws-access-key AKIA…" in text
    assert "勿提交" in text
    # 确定性覆盖写
    render._write_secrets_manifest(repo, hits)
    assert path.read_text(encoding="utf-8") == text
    # 无命中 → 陈旧清单清理（R6'：清单与最新一次渲染同步）
    render._write_secrets_manifest(repo, [])
    assert not path.exists()
