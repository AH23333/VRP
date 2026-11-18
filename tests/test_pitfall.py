"""F16（构想v10）：坑→测试防复发索引测试——run_tree 替身注入（doclint 先例）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrp import cli, pitfall


def _mk_repo(tmp_path: Path, md: str, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "踩坑历史.md").write_text(md, encoding="utf-8")
    return repo


def _fake_collect(names: list[str]):
    """run_tree 替身：`N/M tests collected` 汇总行+逐测试 `path::name` 行。"""

    def fake(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        lines = [*names, f"{len(names)} tests collected"]
        return {
            "returncode": 0,
            "timed_out": False,
            "stdout": "\n".join(lines),
            "stderr": "",
        }

    return fake


_MD = """# 踩坑历史

## SQLite / ETL

1. **缓存目录安全检查**：`win_private_directory_tree_secure` 要求缓存目录 ACL 全可信。
   解法：缓存目录放盘根。锁定：test_cache_acl_secure
2. **裸 INSERT 追加不清理**：重跑膨胀，修=写入前 DELETE 镜像。锁定：test_ledger_idempotent

## vrp 自身

3. **argparse help 内字面 %**：须写 %%，锁定 test_help_percent
4. **无测试锁定的坑**：纯经验记录。
"""


def test_parse_pitfalls_sections_entries_tokens():
    entries = pitfall.parse_pitfalls(_MD)
    assert len(entries) == 4
    assert entries[0]["section"] == "SQLite / ETL" and entries[0]["index"] == 1
    assert entries[0]["tests"] == ["test_cache_acl_secure"]  # 缩进续行归条目
    assert entries[2]["section"] == "vrp 自身" and entries[2]["tests"] == ["test_help_percent"]
    assert entries[3]["tests"] == []  # 无锁定引用
    assert entries[0]["ref"] == "SQLite / ETL #1"


def test_parse_pitfalls_no_section_headers(tmp_path):
    entries = pitfall.parse_pitfalls("1. 裸条目锁定 test_a_b_c\n")
    assert len(entries) == 1
    assert entries[0]["ref"] == "#1"
    assert entries[0]["tests"] == ["test_a_b_c"]


def test_run_pitindex_gap_drift_locked(tmp_path, monkeypatch):
    """三类分开：locked（token 命中）/gap（无 token 或全漂移）/drifted（部分失锚）。"""
    repo = _mk_repo(tmp_path, _MD)
    monkeypatch.setattr(
        pitfall,
        "run_tree",
        _fake_collect([
            "tests/test_x.py::test_cache_acl_secure",
            "tests/test_x.py::test_ledger_idempotent[case1]",  # 参数化形态
            "tests/test_x.py::test_ledger_idempotent[case2]",
            "tests/test_y.py::test_help_percent",
        ]),
    )
    result = pitfall.run_pitindex(repo)
    s = result["summary"]
    assert s["total"] == 4
    assert s["locked"] == 3  # 1/2/3 锁定（2 参数化前缀匹配）
    assert s["gaps"] == 1  # 4 无 token
    assert s["drifted"] == 0
    assert result["gaps"][0]["ref"] == "vrp 自身 #4"


def test_run_pitindex_drift_detected(tmp_path, monkeypatch):
    """token 在收集面不存在=漂移红面（改名/删测试后坑文档失锚）。"""
    md = """# 踩坑历史

## vrp 自身

1. **老坑**：锁定 test_renamed_away
"""
    repo = _mk_repo(tmp_path, md, name="repo2")
    monkeypatch.setattr(pitfall, "run_tree", _fake_collect(["tests/test_x.py::test_other"]))
    result = pitfall.run_pitindex(repo)
    assert result["summary"]["drifted"] == 1
    assert result["drifted"][0]["drifted_tokens"] == ["test_renamed_away"]
    assert result["summary"]["locked"] == 0
    assert result["summary"]["gaps"] == 1  # 全漂移=事实未锁，同计 gap


def test_token_prefix_boundary_not_substring(tmp_path, monkeypatch):
    """test_a 不误锁 test_abc（`[` 边界匹配非子串）。"""
    assert pitfall._token_locked("test_a", {"test_abc"}, set()) is False
    assert pitfall._token_locked("test_a", {"test_a"}, set()) is True
    assert pitfall._token_locked("test_a", {"test_a[x]"}, set()) is True


def test_parse_excludes_path_and_py_mentions():
    """路径提及（/前缀、.py 后缀）是文件引用非测试名引用——首跑 dogfood
    8 条"漂移"全为此类假阳性（tests/unit/test_uml_activity.py、
    test_s7_enforce_c.py:168 形态）。"""
    md = """# 踩坑历史

## 外仓分析

1. **路径提及不算锁定**：`tests/unit/test_uml_activity.py` 耦合假对；
   `test_s7_enforce_c.py:168` 断言；真正锁定：test_real_guard
"""
    entries = pitfall.parse_pitfalls(md)
    assert entries[0]["tests"] == ["test_real_guard"]


def test_stem_resolution_locks_module_ref(tmp_path, monkeypatch):
    """模块级引用（"守卫在 test_io_census 里"）按测试文件 stem 锁定。"""
    md = """# 踩坑历史

## vrp 自身

1. **编译守卫**：warnings-as-errors（test_io_census）
"""
    repo = _mk_repo(tmp_path, md, name="repo6")
    monkeypatch.setattr(
        pitfall,
        "run_tree",
        _fake_collect(["tests/test_io_census.py::test_guard_bom_rejected"]),
    )
    result = pitfall.run_pitindex(repo)
    assert result["summary"]["locked"] == 1
    assert result["summary"]["drifted"] == 0


def test_run_pitindex_missing_doc_exit(tmp_path):
    with pytest.raises(SystemExit, match="pitfalls doc missing"):
        pitfall.run_pitindex(tmp_path)


def test_run_pitindex_collect_failure_exit(tmp_path, monkeypatch):
    def fail(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        return {"returncode": 3, "timed_out": False, "stdout": "", "stderr": "boom"}

    repo = _mk_repo(tmp_path, "1. x\n")
    monkeypatch.setattr(pitfall, "run_tree", fail)
    with pytest.raises(SystemExit, match="pytest collect failed"):
        pitfall.run_pitindex(repo)


def test_run_pitindex_collect_timeout_exit(tmp_path, monkeypatch):
    def timeout_stub(cmd, cwd=None, timeout=None, env=None, normal_priority=False):
        return {"returncode": -1, "timed_out": True, "stdout": "", "stderr": ""}

    repo = _mk_repo(tmp_path, "1. x\n", name="repo3")
    monkeypatch.setattr(pitfall, "run_tree", timeout_stub)
    with pytest.raises(SystemExit, match="timed out"):
        pitfall.run_pitindex(repo)


def test_render_text_shape(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path, _MD, name="repo4")
    monkeypatch.setattr(
        pitfall,
        "run_tree",
        _fake_collect(["tests/test_x.py::test_cache_acl_secure"]),
    )
    result = pitfall.run_pitindex(repo)
    text = pitfall.render_text(result)
    assert "DRIFT" in text  # 漂移红面呈现（2/3 的 token 未命中）
    assert "GAPS" in text
    assert "locked" in text


def test_cli_pitindex_end_to_end(tmp_path, capsys, monkeypatch):
    repo = _mk_repo(tmp_path, _MD, name="repo5")
    monkeypatch.setattr(
        pitfall,
        "run_tree",
        _fake_collect(["tests/test_x.py::test_cache_acl_secure"]),
    )
    rc = cli.main(["pitindex", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["summary"]["total"] == 4


def test_cli_parser_accepts_pitindex():
    parser = cli.build_parser()
    args = parser.parse_args(["pitindex", "some/repo", "--json"])
    assert args.repo == "some/repo" and args.json


ROOT = Path(__file__).resolve().parents[1]


def test_dogfood_vrp_repo_pitindex():
    """dogfood：vrp 自仓真跑（真 pytest collect ~5s）——100 条坑/24 token
    登记面活样例；本批已锁坑不得新增漂移（drifted 必须为空：token 改名
    先改坑文档）。"""
    result = pitfall.run_pitindex(ROOT)
    s = result["summary"]
    assert s["total"] >= 100
    assert s["collected_tests"] > 3000
    assert s["collected_files"] >= 160
    assert s["locked"] >= 14
    assert s["drifted"] == 0, f"pitfall tokens drifted: {result['drifted']}"
