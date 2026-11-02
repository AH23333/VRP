import hashlib

import pytest

from vrp.etl import _ContentHasher

pytestmark = pytest.mark.deep


def _expected_hash(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    chunk = "\n".join(lines[start_line - 1 : end_line])
    return hashlib.sha256(chunk.encode("utf-8", errors="replace")).hexdigest()


def test_definition_hash_basic(tmp_path):
    f = tmp_path / "mod.py"
    text = "def a():\n    pass\n\n\ndef b():\n    pass\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("mod.py", 1, 2) == _expected_hash(text, 1, 2)
    assert h.definition_hash("mod.py", 4, 5) == _expected_hash(text, 4, 5)


def test_definition_hash_empty_inputs(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("", 1, 1) == ""
    assert h.definition_hash("mod.py", 0, 1) == ""
    assert h.definition_hash("mod.py", None, 1) == ""
    assert h.definition_hash("mod.py", 1, None) == ""
    assert h.definition_hash("mod.py", 1, 0) == ""


def test_definition_hash_missing_file(tmp_path):
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("gone.py", 1, 1) == ""
    assert h.definition_hash("sub/gone.py", 1, 1) == ""


def test_definition_hash_start_after_end(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("mod.py", 3, 2) == ""
    assert h.definition_hash("mod.py", 10, 5) == ""


def test_definition_hash_end_beyond_eof_truncates(tmp_path):
    f = tmp_path / "mod.py"
    text = "a\nb\nc\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("mod.py", 1, 999) == _expected_hash(text, 1, 3)
    assert h.definition_hash("mod.py", 1, 999) == h.definition_hash("mod.py", 1, 3)


def test_definition_hash_start_beyond_eof(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("a\n", encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("mod.py", 99, 100) == ""


def test_definition_hash_cached_after_file_deleted(tmp_path):
    f = tmp_path / "mod.py"
    text = "line1\nline2\nline3\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    first = h.definition_hash("mod.py", 1, 3)
    assert first == _expected_hash(text, 1, 3)
    f.unlink()
    assert h.definition_hash("mod.py", 1, 3) == first


def test_definition_hash_separator_forms(tmp_path):
    (tmp_path / "pkg").mkdir()
    f = tmp_path / "pkg" / "mod.py"
    text = "def a():\n    pass\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    expected = _expected_hash(text, 1, 2)
    assert h.definition_hash("pkg/mod.py", 1, 2) == expected
    assert h.definition_hash("pkg\\mod.py", 1, 2) == expected


def test_definition_hash_unicode_deterministic(tmp_path):
    f = tmp_path / "u.py"
    text = "def 你好():\n    return 'héllo'\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    a = h.definition_hash("u.py", 1, 2)
    h2 = _ContentHasher(tmp_path)
    b = h2.definition_hash("u.py", 1, 2)
    assert a == b == _expected_hash(text, 1, 2)
    assert a != ""


def test_definition_hash_non_utf8_bytes_deterministic(tmp_path):
    f = tmp_path / "gbk.py"
    f.write_bytes("def 名():\n    return '中文'\n".encode("gbk"))
    h = _ContentHasher(tmp_path)
    a = h.definition_hash("gbk.py", 1, 2)
    h2 = _ContentHasher(tmp_path)
    b = h2.definition_hash("gbk.py", 1, 2)
    assert a == b
    assert a != ""


def test_definition_hash_string_line_numbers(tmp_path):
    f = tmp_path / "mod.py"
    text = "a\nb\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("mod.py", "1", "2") == _expected_hash(text, 1, 2)


def test_definition_hash_large_file(tmp_path):
    f = tmp_path / "big.py"
    text = "\n".join(f"def f{i}(): pass" for i in range(10000)) + "\n"
    f.write_text(text, encoding="utf-8")
    h = _ContentHasher(tmp_path)
    assert h.definition_hash("big.py", 1, 10000) == _expected_hash(text, 1, 10000)


def test_definition_hash_crlf_normalization(tmp_path):
    f = tmp_path / "crlf.py"
    f.write_bytes(b"def a():\r\n    pass\r\n")
    h = _ContentHasher(tmp_path)
    lf = tmp_path / "lf.py"
    lf.write_bytes(b"def a():\n    pass\n")
    h2 = _ContentHasher(tmp_path)
    assert h.definition_hash("crlf.py", 1, 2) == h2.definition_hash("lf.py", 1, 2)
