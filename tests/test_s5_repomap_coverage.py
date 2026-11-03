import sqlite3
from pathlib import Path

import pytest

from vrp import s5_repomap

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

EXTENDED_LABELS = (
    "Function",
    "Method",
    "Class",
    "Interface",
    "Struct",
    "Enum",
    "Type",
    "Trait",
    "Protocol",
    "Macro",
    "Union",
    "Record",
    "Constant",
)

NON_DEFINITION_LABELS = (
    "Module",
    "File",
    "Folder",
    "Section",
    "Field",
    "EnvVar",
    "Route",
    "Channel",
    "Process",
    "Decorator",
)

CONFIG_EXTENSIONS = (".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".xml")
CODE_EXTENSIONS = (".ts", ".tsx", ".mjs", ".py", ".go", ".rs", ".rb", ".c")


def _seed(con, fid, qn, name, label, file_path, start, end):
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, ?, ?, ?, ?, ?, 'h')",
        (fid, qn, label, name, file_path, start, end),
    )


def _fresh_con():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


@pytest.mark.parametrize("label", EXTENDED_LABELS)
def test_extended_definition_label_renders(label):
    con = _fresh_con()
    _seed(con, "fa", "mod.a", "a", "Function", "src/one.ts", 1, 2)
    _seed(con, "fx", f"mod.x_{label}", f"x_{label}", label, "src/one.ts", 4, 6)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fx','CALLS','E2')")
    con.commit()
    ranked = s5_repomap._ranked_definitions(con)
    names = {meta["name"] for _, meta in ranked}
    assert f"x_{label}" in names
    con.close()


@pytest.mark.parametrize("ext", CODE_EXTENSIONS)
def test_variable_renders_from_code_file(ext):
    con = _fresh_con()
    _seed(con, "fa", "mod.a", "a", "Function", "src/one.ts", 1, 2)
    _seed(con, "fv", "mod.SCORES", "SCORES", "Variable", f"src/conf{ext}", 4, 4)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fv','CALLS','E2')")
    con.commit()
    ranked = s5_repomap._ranked_definitions(con)
    names = {meta["name"] for _, meta in ranked}
    assert "SCORES" in names
    con.close()


@pytest.mark.parametrize("ext", CONFIG_EXTENSIONS)
def test_variable_rejected_from_config_file(ext):
    con = _fresh_con()
    _seed(con, "fa", "mod.a", "a", "Function", "src/one.ts", 1, 2)
    _seed(con, "fv", "cfg.keys", "keys", "Variable", f"config/settings{ext}", 4, 4)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fv','CALLS','E2')")
    con.commit()
    ranked = s5_repomap._ranked_definitions(con)
    names = {meta["name"] for _, meta in ranked}
    assert "keys" not in names
    con.close()


@pytest.mark.parametrize("label", NON_DEFINITION_LABELS)
def test_non_definition_labels_never_render(label):
    con = _fresh_con()
    _seed(con, "fa", "mod.a", "a", "Function", "src/one.ts", 1, 2)
    _seed(con, "fn", f"mod.n_{label}", f"n_{label}", label, "src/one.ts", 4, 6)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fn','CALLS','E2')")
    con.commit()
    ranked = s5_repomap._ranked_definitions(con)
    names = {meta["name"] for _, meta in ranked}
    assert f"n_{label}" not in names
    con.close()


def test_variable_without_extension_rejected():
    con = _fresh_con()
    _seed(con, "fa", "mod.a", "a", "Function", "src/one.ts", 1, 2)
    _seed(con, "fv", "cfg.keys", "keys", "Variable", "config/settings", 4, 4)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fa','fv','CALLS','E2')")
    con.commit()
    ranked = s5_repomap._ranked_definitions(con)
    names = {meta["name"] for _, meta in ranked}
    assert "keys" not in names
    con.close()


def test_is_definition_label_predicate():
    assert s5_repomap.is_definition_label("Type", "src/a.ts") is True
    assert s5_repomap.is_definition_label("Variable", "src/a.ts") is True
    assert s5_repomap.is_definition_label("Variable", "config/a.json") is False
    assert s5_repomap.is_definition_label("Section", "docs/a.md") is False
