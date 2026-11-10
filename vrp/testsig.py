"""Test-path detection and JS/TS TESTS self-build (v6 section 12.4)."""

from __future__ import annotations

_JS_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TEST_SUFFIXES = (
    "_test.go",
    "_test.py",
    "_test.rs",
    "_test.cpp",
    "_test.lua",
    "Test.java",
    "Test.kt",
    "Test.cs",
    "Test.php",
    "Spec.scala",
    "_spec.rb",
)
_TEST_SUBSTR = (
    ".test.ts",
    ".spec.ts",
    ".test.js",
    ".spec.js",
    ".test.tsx",
    ".spec.tsx",
)
_TEST_DIR_SEGMENTS = frozenset({"test", "tests", "spec", "__tests__"})


def _basename(path: str) -> str:
    slash = max(path.rfind("/"), path.rfind("\\"))
    return path[slash + 1 :] if slash >= 0 else path


def is_test_path(path: str) -> bool:
    """目录段采用段式精确匹配（2026-11-10，R-I 批）：目录段必须整段等于
    test/tests/spec/__tests__ 才算测试目录。旧子串语义（"test/" in norm）
    对 docs/latest/、contests/、protest/、baseline-test/、mytests/ 等真实
    目录恒真（实测 15 项目 147 个文件假阳性），与 s7 引擎族 _is_test 的
    「子串污染防护」口径不一致，且 v_dead_code SQL 侧 '*test/*' GLOB 同假
    阳性——本函数与 facts_schema v_dead_code 的目录 GLOB 于同一批改为
    段式（Python/SQL 双侧协调变更，对齐由 view_alignment 30x32 组合锁定）。
    文件名级规则（test_ 前缀/_test 与 *Test.* 等后缀/.test.ts 点式）不变。
    """
    if not path:
        return False
    norm = path.replace("\\", "/")
    base = _basename(norm)
    if base.startswith("test_"):
        return True
    if norm.endswith(_TEST_SUFFIXES):
        return True
    if any(s in norm for s in _TEST_SUBSTR):
        return True
    return any(seg in _TEST_DIR_SEGMENTS for seg in norm.split("/")[:-1])


def is_synthetic_path(path: str) -> bool:
    return path.startswith("<") or path.startswith(">")


def is_js_ts(path: str) -> bool:
    return path.lower().endswith(_JS_TS_EXTS)


def is_rust(path: str) -> bool:
    return path.lower().endswith(".rs")


def should_self_build_tests(src_path: str, dst_path: str) -> bool:
    """JS/TS test-linkage rule: CALLS/USAGE from a test file to a non-test, non-synthetic target."""
    if not is_js_ts(src_path) or not is_js_ts(dst_path):
        return False
    if not is_test_path(src_path):
        return False
    if is_test_path(dst_path):
        return False
    if is_synthetic_path(dst_path):
        return False
    return True


def should_self_build_tests_rust(src_path: str, dst_path: str) -> bool:
    """Rust test-linkage rule (same shape as JS/TS, section 12.4 gap): CALLS/USAGE
    from a .rs test file to a non-test .rs target - CBM emits TESTS=0 for Rust
    (probed 2026-11-04, work/rust-calc-probe) but resolves test->lib CALLS edges,
    so the ETL rebuilds the symbol->tests pre-filter the same way as for JS/TS."""
    if not is_rust(src_path) or not is_rust(dst_path):
        return False
    if not is_test_path(src_path):
        return False
    if is_test_path(dst_path):
        return False
    if is_synthetic_path(dst_path):
        return False
    return True
