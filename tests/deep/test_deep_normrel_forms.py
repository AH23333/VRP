import pytest

from vrp.s6_transact import _norm_rel

pytestmark = pytest.mark.deep


@pytest.mark.parametrize(
    "value,expected",
    [
        (r"src\a.py", "src/a.py"),
        ("src/a.py", "src/a.py"),
        (r".\src\a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        (r"src\\a.py", "src/a.py"),
        ("src//a.py", "src/a.py"),
        ("src/./a.py", "src/a.py"),
        ("a.py", "a.py"),
        ("SRC/A.PY", "SRC/A.PY"),
        ("pkg/sub/mod.py", "pkg/sub/mod.py"),
        (r"pkg\sub\mod.py", "pkg/sub/mod.py"),
        ("my file.py", "my file.py"),
        (" src/a.py", "src/a.py"),
        ("src/a.py ", "src/a.py"),
        ("\tsrc/a.py\n", "src/a.py"),
    ],
)
def test_norm_rel_accepts_and_normalizes(value, expected):
    result = _norm_rel(value)
    assert result.as_posix() == expected
    assert not result.is_absolute()
    assert ".." not in result.parts


@pytest.mark.parametrize(
    "value",
    [
        r"\src\a.py",
        "/src/a.py",
        r"..\outside.py",
        "../outside.py",
        "src/../a.py",
        "src/../../a.py",
        "..",
        "../",
        "C:\\repo\\src\\a.py",
        "c:/repo/src/a.py",
        "C:repo/src/a.py",
        "",
        "   ",
        "\t\n",
        None,
        123,
        ["a.py"],
    ],
)
def test_norm_rel_rejects(value):
    with pytest.raises(ValueError):
        _norm_rel(value)


def test_norm_rel_separator_equivalence_class():
    forms = [
        r"src\pkg\a.py",
        "src/pkg/a.py",
        r".\src\pkg\a.py",
        "src//pkg//a.py",
        "src/pkg/./a.py",
        r"src\pkg\\a.py",
    ]
    results = {_norm_rel(f).as_posix() for f in forms}
    assert results == {"src/pkg/a.py"}


def test_norm_rel_never_absolute_and_never_escapes():
    samples = ["a.py", "src/a.py", r"deep\nest\path\x.py", "z/y.py"]
    for s in samples:
        p = _norm_rel(s)
        assert not p.is_absolute()
        assert p.as_posix()[0] != "/"
        assert ".." not in p.parts
        assert str(p).startswith(p.parts[0])


def test_norm_rel_preserves_intermediate_whitespace():
    assert _norm_rel("my dir/file.py").as_posix() == "my dir/file.py"
    assert _norm_rel("a b.py").as_posix() == "a b.py"
