from vrp.evidence import call_evidence, route_edge, language_for_path, HYBRID_LSP, STRUCTURAL, HEURISTIC


def test_lsp_strategies_are_hybrid():
    for s in ("lsp_ts_local", "lsp_direct", "lsp_method", "lsp_callable_alias", "lsp_kt_method"):
        assert call_evidence(s, 0.95) == HYBRID_LSP


def test_structural_strategies():
    for s in ("import_map", "same_module", "qualified_suffix", "import_map_suffix", "field_type_hint"):
        assert call_evidence(s, 0.9) == STRUCTURAL


def test_heuristic_fallbacks():
    assert call_evidence("unique_name", 0.38) == HEURISTIC
    assert call_evidence("suffix_match", 0.21) == HEURISTIC
    assert call_evidence(None, None) == HEURISTIC
    assert call_evidence("", None) == HEURISTIC


def test_route_special_tables():
    assert route_edge("SIMILAR_TO", None, None) == ("findings_p2", None)
    assert route_edge("FILE_CHANGES_WITH", None, None) == ("history_coupling", None)
    assert route_edge("TESTS", None, None) == ("test_linkage", None)
    assert route_edge("TESTS_FILE", None, None) == ("test_linkage", None)


def test_route_reference_hints():
    assert route_edge("USAGE", None, None) == ("usage_hints", HEURISTIC)
    assert route_edge("SEMANTICALLY_RELATED", None, None) == ("usage_hints", HEURISTIC)


def test_route_calls_by_strategy():
    assert route_edge("CALLS", "lsp_direct", 0.95)[1] == HYBRID_LSP
    assert route_edge("CALL_REFERENCE", "import_map", 0.95)[1] == STRUCTURAL
    assert route_edge("CALLS", "suffix_match", 0.28)[1] == HEURISTIC


def test_route_structural_edge_types():
    for t in ("DEFINES", "IMPORTS", "CONTAINS_FILE", "INHERITS", "IMPLEMENTS", "WRITES", "SPAWNS", "RAISES"):
        assert route_edge(t, None, None) == ("edges", STRUCTURAL)


def test_language_for_path():
    assert language_for_path("src/db/store.ts") == "TypeScript"
    assert language_for_path("a/b/c/main.py") == "Python"
    assert language_for_path("pkg/Mod.cs") == "C#"
    assert language_for_path("no/ext") == ""
    assert language_for_path("") == ""
    assert language_for_path("dir.d/file.ts") == "TypeScript"
