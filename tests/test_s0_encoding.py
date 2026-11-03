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
