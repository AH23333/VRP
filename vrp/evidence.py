"""strategy/edge-type to evidence-level routing (v6 section 2 + 12.3)."""

from __future__ import annotations

RUNTIME_OBSERVED = "runtime-observed"
COMPILER_EXACT = "compiler-exact"
HYBRID_LSP = "hybrid-lsp"
STRUCTURAL = "structural"
HEURISTIC = "heuristic"

CALL_EDGE_TYPES = frozenset({"CALLS", "CALL_REFERENCE"})
TEST_EDGE_TYPES = frozenset({"TESTS", "TESTS_FILE"})
REFERENCE_HINT_TYPES = frozenset({"USAGE", "SEMANTICALLY_RELATED"})

FINDINGS_EDGE_TYPES = frozenset({"SIMILAR_TO"})
HISTORY_EDGE_TYPES = frozenset({"FILE_CHANGES_WITH"})

_STRUCTURAL_STRATEGIES = frozenset(
    {
        "import_map",
        "same_module",
        "qualified_suffix",
        "import_map_suffix",
        "field_type_hint",
    }
)


def call_evidence(strategy: str | None, confidence: float | None) -> str:
    if strategy and strategy.startswith("lsp_"):
        return HYBRID_LSP
    if strategy in _STRUCTURAL_STRATEGIES:
        return STRUCTURAL
    return HEURISTIC


def route_edge(edge_type: str, strategy: str | None, confidence: float | None):
    """Returns (table, evidence): table in {edges, usage_hints, history_coupling, test_linkage, findings_p2}."""
    if edge_type in FINDINGS_EDGE_TYPES:
        return "findings_p2", None
    if edge_type in HISTORY_EDGE_TYPES:
        return "history_coupling", None
    if edge_type in TEST_EDGE_TYPES:
        return "test_linkage", None
    if edge_type in REFERENCE_HINT_TYPES:
        return "usage_hints", HEURISTIC
    if edge_type in CALL_EDGE_TYPES:
        return "edges", call_evidence(strategy, confidence)
    return "edges", STRUCTURAL


EXT_LANGUAGES = {
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".py": "Python",
    ".pyi": "Python",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".cs": "C#",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".scala": "Scala",
    ".lua": "Lua",
    ".gd": "GDScript",
    ".dart": "Dart",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".sql": "SQL",
    ".sh": "Shell",
    ".ps1": "PowerShell",
}


def language_for_path(path: str) -> str:
    if not path:
        return ""
    dot = path.rfind(".")
    slash = max(path.rfind("/"), path.rfind("\\"))
    if dot < 0 or dot < slash:
        return ""
    return EXT_LANGUAGES.get(path[dot:].lower(), "")
