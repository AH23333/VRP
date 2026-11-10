from vrp.testsig import (
    is_js_ts,
    is_rust,
    is_synthetic_path,
    is_test_path,
    should_self_build_tests,
    should_self_build_tests_rust,
)


def test_prefix_python():
    assert is_test_path("tests/unit/test_cache.py")
    assert is_test_path("test_foo.py")


def test_suffix_patterns():
    assert is_test_path("pkg/foo_test.go")
    assert is_test_path("pkg/store_test.rs")
    assert is_test_path("src/MyActorTest.java")
    assert is_test_path("spec/models/user_spec.rb")


def test_js_ts_dot_patterns():
    assert is_test_path("test/lsp.test.ts")
    assert is_test_path("src/foo.spec.tsx")
    assert is_test_path("test/health.test.js")


def test_directory_patterns():
    assert is_test_path("__tests__/helper.ts")
    assert is_test_path("tests/unit/test_x.py")
    assert is_test_path("spec/models/user.rb")
    assert is_test_path("test/foo.ts")


def test_non_test_paths():
    assert not is_test_path("src/db/store.ts")
    assert not is_test_path("src/testish.ts")
    assert not is_test_path("src/latest.ts")
    assert not is_test_path("")


def test_js_ts_ext():
    assert is_js_ts("a.ts")
    assert is_js_ts("a.tsx")
    assert is_js_ts("a.mjs")
    assert not is_js_ts("a.py")
    assert not is_js_ts("a.cs")


def test_synthetic():
    assert is_synthetic_path("<python-builtins>")
    assert is_synthetic_path(">unknown")
    assert not is_synthetic_path("src/a.ts")


def test_self_build_rule():
    assert should_self_build_tests("test/lsp.test.ts", "src/indexer/indexer.ts")
    assert not should_self_build_tests("src/indexer.ts", "src/server.ts")
    assert not should_self_build_tests("test/lsp.test.ts", "test/health.test.ts")
    assert not should_self_build_tests("test/lsp.test.ts", "src/store.py")
    assert not should_self_build_tests("tests/test_x.py", "src/mod.py")
    assert not should_self_build_tests("test/lsp.test.ts", "<builtins>")


def test_rust_ext():
    assert is_rust("src/lib.rs")
    assert is_rust("src/lib.RS")
    assert not is_rust("src/lib.ts")
    assert not is_rust("src/lib")
    assert not is_rust("src/lib.rs.bak")


def test_dir_segment_exact_matching():
    """R-I：目录段段式精确匹配——子串陷阱（latest/contests/protest 等真实目录）不再误判。"""
    assert not is_test_path("docs/latest/guide.md")
    assert not is_test_path("src/contests/foo.py")
    assert not is_test_path("protest/x.py")
    assert not is_test_path("skills/ui2v-motions/baseline-test/index.html")
    assert not is_test_path("verify/unwind/skills/uw-analyze-e2e-tests/SKILL.md")
    assert not is_test_path("compatibility_test/README.md")
    assert not is_test_path("itelmn/kallaway-t2-lb-spec/index.html")


def test_dir_segment_genuine_dirs():
    assert is_test_path("a/b/test/c.py")
    assert is_test_path("deep/nested/tests/x.py")
    assert is_test_path("root/spec/x.rb")


def test_dir_segment_behavior_change_locked():
    """R-I：段式语义行为变更锁定——mytests 目录/文件名 test 均不再命中（与 v_dead_code SQL 同批协调变更）。"""
    assert not is_test_path("src/mytests/x.py")
    assert not is_test_path("bin/test")
    assert not is_test_path("tests")


def test_rust_self_build_rule():
    # CBM emits TESTS=0 for Rust (probed 2026-11-04) but resolves test->lib
    # CALLS edges, so the ETL rebuilds test linkage for .rs the same way
    assert should_self_build_tests_rust("tests/calc.rs", "src/lib.rs")
    assert not should_self_build_tests_rust("src/lib.rs", "src/main.rs")
    assert not should_self_build_tests_rust("tests/calc.rs", "tests/other.rs")
    assert not should_self_build_tests_rust("tests/calc.rs", "src/store.py")
    assert not should_self_build_tests_rust("tests/calc.rs", "<builtins>")
