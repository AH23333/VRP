import hashlib

from vrp.etl import cast_fid, _decode_props


def test_cast_fid_stable():
    a = cast_fid("proj", "src.a.foo", "deadbeef")
    b = cast_fid("proj", "src.a.foo", "deadbeef")
    c = cast_fid("proj", "src.a.foo", "other")
    d = cast_fid("other", "src.a.foo", "deadbeef")
    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 64
    int(a, 16)


def test_cast_fid_unicode_safe():
    fid = cast_fid("proj", "src.中文.函数", "")
    assert len(fid) == 64


def test_decode_props():
    assert _decode_props(None) == {}
    assert _decode_props("") == {}
    assert _decode_props('{"strategy":"lsp_direct"}') == {"strategy": "lsp_direct"}
    assert _decode_props("[1,2]") == {}
    assert _decode_props("not json") == {}
