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
_TEST_DIRS = ("__tests__/", "tests/", "test/", "spec/")


def _basename(path: str) -> str:
    slash = max(path.rfind("/"), path.rfind("\\"))
    return path[slash + 1 :] if slash >= 0 else path


# Decision (2026-11-03): substring matching below is the agreed test-path
# criterion and is kept as-is. Coexisting with the GLOB patterns in
# facts_schema (v_dead_code) is a known trade-off - do not change either
# side unilaterally.
def is_test_path(path: str) -> bool:
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
    if any(d in norm for d in _TEST_DIRS):
        return True
    for d in _TEST_DIRS:
        if norm.startswith(d):
            return True
    return False


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
