"""L1: discover_commands must survive a non-UTF-8 package.json.

Every other read_text in s0_precheck uses errors="replace"; the package.json
read did not, so invalid bytes raise UnicodeDecodeError (a ValueError, not
caught by the (JSONDecodeError, OSError) handler) and crash precheck.
"""

from __future__ import annotations

from vrp import s0_precheck


def test_discover_commands_survives_invalid_utf8_package_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_bytes(b'{"scripts": {"test": "jest \xff\xfe"}}')
    result = s0_precheck.discover_commands(repo)  # must not raise
    assert isinstance(result, dict)


def test_discover_commands_still_reads_valid_package_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    result = s0_precheck.discover_commands(repo)
    assert result.get("test") == ["npm run test"]


def test_discover_commands_survives_array_toplevel_package_json(tmp_path):
    """R-D C1：package.json 顶层非对象 / scripts 非对象——防御不崩。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('["not", "an", "object"]', encoding="utf-8")
    result = s0_precheck.discover_commands(repo)  # must not raise
    assert result == {}
    (repo / "package.json").write_text('{"scripts": ["also", "wrong"]}', encoding="utf-8")
    result = s0_precheck.discover_commands(repo)  # must not raise
    assert result == {}


def test_census_fallback_ignores_hidden_and_ignored_dirs(tmp_path):
    """R-D C2：os.walk 剪枝后行为等价——IGNORE_DIRS 目录内文件不计。"""
    from vrp.evidence import language_for_path
    assert language_for_path("a.py")  # 前置：fixture 后缀可识别
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "b.py").write_text("y = 2\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "c.py").write_text("z = 3\n", encoding="utf-8")
    census = s0_precheck.census_fallback(repo)
    py = next(l for l in census["languages"] if l["name"] == "Python")
    assert py["files"] == 1
