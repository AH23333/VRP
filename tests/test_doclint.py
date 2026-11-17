"""P23（构想v11 亮点首推）：活文档防漂移 `vrp doclint` 测试。

自家 test_docs_drift 守卫（Face A-E）的产品化泛化面：
- 标记解析：JSON 体/残缺标记/非对象体诚实报错不崩
- 真值源三件：glob 计数 / recount 文件正则计数 / pytest-collect
  （run_tree 替身注入——不真跑 pytest，确定性+快）
- 求值契约：claim 首捕全文 vs actual；漂移/错误两类红面分开呈现
- dogfood：vrp 自仓 README 四标记全绿（自家活样例——pytest-collect
  真值源真跑一次，~5s）
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from vrp import doclint
from vrp.cli import build_parser, cmd_doclint
from vrp.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]
_REPO_SEQ = itertools.count()


def _marker(spec: dict) -> str:
    """构造 doclint 标记行（JSON 体，同 README dogfood 标记形态）。"""
    return f"<!-- doclint {json.dumps(spec, ensure_ascii=False)} -->"


def _mk_repo(tmp_path: Path, readme: str, files: dict | None = None, name: str | None = None) -> Path:
    repo = tmp_path / (name or f"repo{next(_REPO_SEQ)}")
    repo.mkdir()
    (repo / "README.md").write_text(readme, encoding="utf-8")
    for rel, content in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return repo


def test_find_markers_extracts_json_bodies():
    """两标记+普通注释：只取 doclint 标记，体按序、偏移递增。"""
    text = (
        "<!-- plain comment -->\n"
        + _marker({"id": "a", "claim": "x (\\d+)", "source": {"kind": "glob", "pattern": "*.yml"}})
        + "\nbody\n"
        + _marker({"id": "b", "claim": "y (\\d+)", "source": {"kind": "glob", "pattern": "*.md"}})
        + "\n"
    )
    markers = doclint.find_markers(text)
    assert len(markers) == 2
    assert markers[0][0] < markers[1][0]
    bodies = [json.loads(body) for _, body in markers]
    assert [b["id"] for b in bodies] == ["a", "b"]


def test_find_markers_unterminated_ignored():
    """无闭合 `-->` 的残缺标记诚实忽略（不造幻影断言）。"""
    text = "<!-- doclint {\"id\": \"x\"} 无闭合"
    assert doclint.find_markers(text) == []


def test_parse_body_rejects_non_object():
    """数组/标量/坏 JSON → None；只有 JSON 对象成活。"""
    assert doclint._parse_body('{"id": 1}') == {"id": 1}
    assert doclint._parse_body("[1, 2]") is None
    assert doclint._parse_body("42") is None
    assert doclint._parse_body("{broken") is None


def test_invalid_marker_json_reported_not_crash(tmp_path):
    """`{"` 起始但坏 JSON → 错误条目（真打字错误抓红）；`{...}` 示例形态
    → 静默跳过（README CLI 表活例——文档描述 doclint 不自误报）。"""
    repo = _mk_repo(
        tmp_path,
        'doc\n<!-- doclint {"id": broken -->\nmore\n<!-- doclint {...} -->\n',
    )
    result = doclint.run_doclint(repo)
    assert result["summary"]["assertions"] == 1
    entry = result["assertions"][0]
    assert entry["ok"] is False
    assert entry["id"] == "?"
    assert entry["error"] == "marker body is not a JSON object"


def test_marker_missing_claim_reported(tmp_path):
    """缺 claim 正则 / claim 无捕获组：错误条目分开报。"""
    repo = _mk_repo(
        tmp_path,
        "n=3\n" + _marker({"id": "no-claim", "source": {"kind": "glob", "pattern": "*.md"}}) + "\n",
    )
    result = doclint.run_doclint(repo)
    assert result["assertions"][0]["error"] == "marker missing claim regex"
    repo2 = _mk_repo(
        tmp_path,
        "n=3\n" + _marker({"id": "no-group", "claim": "n=", "source": {"kind": "glob", "pattern": "*.md"}}) + "\n",
        files={"repo.md": ""},
    )
    result2 = doclint.run_doclint(repo2)
    assert "group 1" in result2["assertions"][0]["error"]


def test_glob_source_counts_files_and_drift(tmp_path):
    """glob 计数对账：3 文件 claimed 3 绿；claimed 5 红（漂移+hint）。"""
    files = {f"rules/r{i}.yml": "rule:" for i in range(3)}
    marker = _marker(
        {"id": "rules", "claim": "(\\d+) rules", "source": {"kind": "glob", "pattern": "rules/*.yml"}}
    )
    repo = _mk_repo(tmp_path, f"we have 3 rules\n{marker}\n", files=files)
    result = doclint.run_doclint(repo)
    entry = result["assertions"][0]
    assert entry["ok"] is True and entry["claimed"] == 3 and entry["actual"] == 3
    repo2 = _mk_repo(tmp_path, f"we have 5 rules\n{marker}\n", files=files)
    result2 = doclint.run_doclint(repo2)
    entry2 = result2["assertions"][0]
    assert entry2["ok"] is False and entry2["claimed"] == 5 and entry2["actual"] == 3
    assert entry2["hint"] == doclint.DRIFT_HINT


def test_glob_zero_matches_honest_zero(tmp_path):
    """glob 零匹配=诚实 0（不是错误）；claimed 0 对账绿。"""
    marker = _marker(
        {"id": "zero", "claim": "(\\d+) things", "source": {"kind": "glob", "pattern": "nothing/*.yml"}}
    )
    repo = _mk_repo(tmp_path, f"0 things\n{marker}\n")
    result = doclint.run_doclint(repo)
    entry = result["assertions"][0]
    assert entry["ok"] is True and entry["actual"] == 0


def test_recount_source_ok_missing_invalid(tmp_path):
    """recount：4 匹配对账绿；目标缺失/正则坏 → 错误条目不崩。"""
    src = "server.add_tool(a)\nserver.add_tool(b)\nserver.add_tool(c)\nserver.add_tool(d)\n"
    marker = _marker(
        {
            "id": "tools",
            "claim": "(\\d+) tools",
            "source": {"kind": "recount", "path": "src/mcp.py", "regex": r"server\.add_tool\("},
        }
    )
    repo = _mk_repo(tmp_path, f"4 tools\n{marker}\n", files={"src/mcp.py": src})
    entry = doclint.run_doclint(repo)["assertions"][0]
    assert entry["ok"] is True and entry["actual"] == 4
    marker_missing = _marker(
        {
            "id": "tools",
            "claim": "(\\d+) tools",
            "source": {"kind": "recount", "path": "src/gone.py", "regex": "x"},
        }
    )
    repo2 = _mk_repo(tmp_path, f"4 tools\n{marker_missing}\n")
    assert "recount target missing" in doclint.run_doclint(repo2)["assertions"][0]["error"]
    marker_bad_rx = _marker(
        {
            "id": "tools",
            "claim": "(\\d+) tools",
            "source": {"kind": "recount", "path": "README.md", "regex": "(unclosed"},
        }
    )
    repo3 = _mk_repo(tmp_path, f"4 tools\n{marker_bad_rx}\n")
    assert "recount regex invalid" in doclint.run_doclint(repo3)["assertions"][0]["error"]


def test_claim_not_found_or_not_integer(tmp_path):
    """claim 不中文本/捕获非整数：错误条目分开报。"""
    marker_missing = _marker(
        {"id": "gone", "claim": "missing (\\d+) claim", "source": {"kind": "glob", "pattern": "*.md"}}
    )
    repo = _mk_repo(tmp_path, f"nothing here\n{marker_missing}\n")
    assert "claim pattern not found" in doclint.run_doclint(repo)["assertions"][0]["error"]
    marker_word = _marker(
        {"id": "word", "claim": "v(\\w+) count", "source": {"kind": "glob", "pattern": "*.md"}}
    )
    repo2 = _mk_repo(tmp_path, f"vx count\n{marker_word}\n")
    assert "not an integer" in doclint.run_doclint(repo2)["assertions"][0]["error"]


def test_pytest_collect_source_both_forms_and_cache(tmp_path):
    """pytest-collect：分层/普通两形态解析对；3 标记共享 1 次采集（缓存）。"""
    calls: list[list] = []

    def fake_run_tree(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        calls.append(cmd)
        return {
            "returncode": 0,
            "timed_out": False,
            "stdout": "1591/3116 tests collected (1523 deselected)\n",
            "stderr": "",
        }

    marker = _marker(
        {"id": "q", "claim": "quick (\\d+)", "source": {"kind": "pytest-collect", "component": "quick"}}
    )
    marker_d = _marker(
        {"id": "d", "claim": "deep (\\d+)", "source": {"kind": "pytest-collect", "component": "deep"}}
    )
    marker_t = _marker(
        {"id": "t", "claim": "total (\\d+)", "source": {"kind": "pytest-collect", "component": "total"}}
    )
    readme = f"quick 1591 deep 1523 total 3116\n{marker}\n{marker_d}\n{marker_t}\n"
    repo = _mk_repo(tmp_path, readme)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(doclint, "run_tree", fake_run_tree)
    try:
        result = doclint.run_doclint(repo)
    finally:
        monkeypatch.undo()
    by_id = {e["id"]: e for e in result["assertions"]}
    assert all(e["ok"] for e in result["assertions"]), result["assertions"]
    assert by_id["q"]["actual"] == 1591 and by_id["d"]["actual"] == 1523 and by_id["t"]["actual"] == 3116
    assert len(calls) == 1, "3 markers must share one collect run (cache)"

    def fake_plain(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        return {"returncode": 0, "timed_out": False, "stdout": "12 tests collected\n", "stderr": ""}

    monkeypatch2 = pytest.MonkeyPatch()
    monkeypatch2.setattr(doclint, "run_tree", fake_plain)
    try:
        result2 = doclint.run_doclint(_mk_repo(tmp_path, f"quick 12\n{marker}\n"))
    finally:
        monkeypatch2.undo()
    assert result2["assertions"][0]["ok"] is True and result2["assertions"][0]["actual"] == 12


def test_pytest_collect_failure_paths(tmp_path):
    """超时/不可解析/未知 component → 错误条目（不崩、不猜数）。"""
    marker = _marker(
        {"id": "q", "claim": "quick (\\d+)", "source": {"kind": "pytest-collect", "component": "quick"}}
    )
    repo = _mk_repo(tmp_path, f"quick 5\n{marker}\n")

    def fake_timeout(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        return {"returncode": -1, "timed_out": True, "stdout": "", "stderr": "boom"}

    def fake_garbage(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        return {"returncode": 1, "timed_out": False, "stdout": "no tests here\n", "stderr": ""}

    mp = pytest.MonkeyPatch()
    mp.setattr(doclint, "run_tree", fake_timeout)
    try:
        assert "timed out" in doclint.run_doclint(repo)["assertions"][0]["error"]
    finally:
        mp.undo()
    mp2 = pytest.MonkeyPatch()
    mp2.setattr(doclint, "run_tree", fake_garbage)
    try:
        assert "not parseable" in doclint.run_doclint(repo)["assertions"][0]["error"]
    finally:
        mp2.undo()
    marker_bad = _marker(
        {"id": "q", "claim": "quick (\\d+)", "source": {"kind": "pytest-collect", "component": "sometimes"}}
    )
    repo_bad = _mk_repo(tmp_path, f"quick 5\n{marker_bad}\n")
    assert "unknown pytest-collect component" in doclint.run_doclint(repo_bad)["assertions"][0]["error"]


def test_unknown_source_kind_rejected(tmp_path):
    """kind 白名单外诚实报错不猜。"""
    marker = _marker(
        {"id": "x", "claim": "n (\\d+)", "source": {"kind": "shell", "cmd": "anything"}}
    )
    repo = _mk_repo(tmp_path, f"n 1\n{marker}\n")
    assert "unknown source kind" in doclint.run_doclint(repo)["assertions"][0]["error"]


def test_scan_skips_dirs_and_unreadable(tmp_path):
    """生成物目录不入扫描；坏编码文件降级跳过并计数（不崩整跑）。"""
    marker = _marker(
        {"id": "n", "claim": "docs (\\d+)", "source": {"kind": "glob", "pattern": "docs/*.md"}}
    )
    repo = _mk_repo(
        tmp_path,
        f"docs 2\n{marker}\n",
        files={
            "node_modules/pkg/x.md": "<!-- doclint {} -->",
            "work/sandbox.md": "<!-- doclint {} -->",
            "docs/a.md": "a",
            "docs/b.md": "b",
            "broken.md": b"\xff\xfe garbage \x81 not utf8",
        },
    )
    result = doclint.run_doclint(repo)
    assert result["summary"]["unreadable_files"] == 1
    # scanned = README + docs/a + docs/b（node_modules/work 跳过，broken 降级）
    assert result["summary"]["files_scanned"] == 3
    assert result["summary"]["assertions"] == 1  # node_modules 的标记不进面
    assert result["assertions"][0]["ok"] is True and result["assertions"][0]["actual"] == 2


def test_run_doclint_deterministic_shape(tmp_path):
    """同库双跑全等（确定性红线：无时间戳、序=扫描序+文档序）。"""
    marker_ok = _marker(
        {"id": "ok1", "claim": "n (\\d+)", "source": {"kind": "glob", "pattern": "*.yml"}}
    )
    marker_drift = _marker(
        {"id": "bad1", "claim": "m (\\d+)", "source": {"kind": "glob", "pattern": "*.yml"}}
    )
    repo = _mk_repo(
        tmp_path,
        f"n 1 but m 9\n{marker_ok}\n{marker_drift}\n",
        files={"a.yml": "x"},
    )
    r1 = doclint.run_doclint(repo)
    r2 = doclint.run_doclint(repo)
    assert r1 == r2
    assert r1["summary"] == {
        "files_scanned": 1,
        "unreadable_files": 0,
        "assertions": 2,
        "ok": 1,
        "drift": 1,
    }


def test_render_text_shape(tmp_path):
    """人读呈现：漂移行含 claimed/actual/hint；零断言含指引 note。"""
    marker_drift = _marker(
        {"id": "bad1", "claim": "m (\\d+)", "source": {"kind": "glob", "pattern": "*.yml"}}
    )
    repo = _mk_repo(tmp_path, f"m 9\n{marker_drift}\n", files={"a.yml": "x"})
    text = doclint.render_text(doclint.run_doclint(repo))
    assert "[fail] bad1" in text and "claims 9 but actual is 1" in text
    empty = _mk_repo(tmp_path, "no markers at all\n")
    text2 = doclint.render_text(doclint.run_doclint(empty))
    assert "no doclint markers" in text2


def test_cli_parser_accepts_doclint():
    """build_parser：doclint 子命令接 --json，func 钉 cmd_doclint。"""
    args = build_parser().parse_args(["doclint", "some-repo", "--json"])
    assert args.func is cmd_doclint
    assert args.repo == "some-repo" and args.json is True


def test_cli_doclint_end_to_end(tmp_path, capsys):
    """CLI E2E：绿面 rc 0；漂移面 rc 1（json 出口可解析）。"""
    marker_ok = _marker(
        {"id": "ok1", "claim": "n (\\d+)", "source": {"kind": "glob", "pattern": "*.yml"}}
    )
    repo_ok = _mk_repo(tmp_path, f"n 1\n{marker_ok}\n", files={"a.yml": "x"})
    rc = cli_main(["doclint", str(repo_ok), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["summary"]["drift"] == 0
    repo_bad = _mk_repo(tmp_path, f"n 5\n{marker_ok}\n", files={"a.yml": "x"})
    rc2 = cli_main(["doclint", str(repo_bad), "--json"])
    payload2 = json.loads(capsys.readouterr().out)
    assert rc2 == 1 and payload2["summary"]["drift"] == 1


def test_dogfood_readme_markers_green():
    """dogfood：vrp 自仓 README 四标记全绿（自家活样例——pytest-collect
    真值源真跑一次 ~5s；README 计数须与 collect 同批同步）。"""
    result = doclint.run_doclint(ROOT)
    by_id = {e["id"]: e for e in result["assertions"]}
    for aid in ("readme-quick-count", "readme-deep-count", "readme-total-count", "readme-astgrep-rules"):
        assert aid in by_id, f"README dogfood marker missing: {aid}"
        assert by_id[aid]["ok"] is True, f"README marker {aid} drifted: {by_id[aid]}"
    assert result["summary"]["drift"] == 0, [
        e for e in result["assertions"] if not e.get("ok")
    ]
