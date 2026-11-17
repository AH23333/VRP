"""T10 vrp assess：迁移前评估（只读预检面，构想v12）。

守卫重点：引擎映射 map↔文件系统一致（不写死摘要数字，#153 纪律）、
只读性（不落 .vrp/）、混仓/内容仓/非 git 风险提示、.r 类
EXT_LANGUAGES 盲区由引擎面兜底。
"""

from __future__ import annotations

import json
from pathlib import Path

from vrp import assess as assess_mod


def _mkrepo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return repo


def test_assess_engine_map_matches_engine_files():
    """map↔文件系统守卫：除 py（router 内联）外每个 key 必有
    vrp/s7_enforce_{key}.py，反向亦然——防新引擎接入漏登 map（静默分叉）。
    """
    keys = set(assess_mod._s7_engine_suffixes())
    here = Path(assess_mod.__file__).parent
    files = {p.stem.replace("s7_enforce_", "") for p in here.glob("s7_enforce_*.py")}
    assert keys - {"py"} == files
    assert "py" not in files
    assert keys == {  # 21 语言口径与 s7 路由对齐（#152/#153 实测口径）
        "py", "ts", "go", "rs", "java", "cs", "kt", "c", "objc", "php",
        "scala", "ruby", "dart", "swift", "zig", "lua", "perl", "erlang",
        "r", "clj", "ocaml",
    }


def test_assess_py_repo_run_verdict(tmp_path):
    repo = _mkrepo(tmp_path, {"a.py": "x = 1\n"})
    result = assess_mod.assess(repo)
    assert result["verdict"] == "run"
    assert result["next"] == "vrp run <repo>"
    assert [e["engine"] for e in result["engines"]["s7"]] == ["py"]
    assert result["engines"]["s7_available"] == 21
    assert [m["engine"] for m in result["engines"]["mutate"]] == ["mutmut"]
    assert not (repo / ".vrp").exists()  # 只读：不落 .vrp/


def test_assess_mixed_language_warning(tmp_path):
    repo = _mkrepo(tmp_path, {"a.py": "x = 1\n", "b.ts": "const b = 1;\n"})
    result = assess_mod.assess(repo)
    assert {e["engine"] for e in result["engines"]["s7"]} == {"py", "ts"}
    assert any("mixed-language (py+ts)" in w for w in result["warnings"])


def test_assess_content_repo_skip(tmp_path):
    repo = _mkrepo(tmp_path, {"notes.md": "# notes\n", "data.pdf": "pdf"})
    result = assess_mod.assess(repo)
    assert result["verdict"] == "skip (content repository)"
    assert result["next"] is None


def test_assess_no_source_skip(tmp_path):
    # .cfg 既非 content 也非 source：内容探针不触发、源码面为零
    repo = _mkrepo(tmp_path, {"setup.cfg": "[metadata]\n"})
    result = assess_mod.assess(repo)
    assert result["verdict"] == "skip (no source files)"
    assert any("no source-like files" in w for w in result["warnings"])


def test_assess_r_ext_covered_by_engine_face(tmp_path):
    """EXT_LANGUAGES 无 .r（census 盲区）但 s7 有 r 引擎——引擎面兜底，
    R 仓不得被误判 skip。
    """
    repo = _mkrepo(tmp_path, {"stat.r": "x <- 1\n"})
    result = assess_mod.assess(repo)
    assert [e["engine"] for e in result["engines"]["s7"]] == ["r"]
    assert result["verdict"] == "run"


def test_assess_rs_mutate_cargo_mutants(tmp_path):
    repo = _mkrepo(tmp_path, {"m.rs": "fn main() {}\n"})
    result = assess_mod.assess(repo)
    assert [e["engine"] for e in result["engines"]["s7"]] == ["rs"]
    assert [m["engine"] for m in result["engines"]["mutate"]] == ["cargo-mutants"]


def test_assess_indexed_no_engine(tmp_path):
    repo = _mkrepo(tmp_path, {"a.py": "x = 1\n", "types.pyi": "x: int\n"})
    result = assess_mod.assess(repo)
    exts = [e["ext"] for e in result["indexed_no_engine"]]
    assert ".pyi" in exts  # EXT_LANGUAGES 有 .pyi 但无专用 s7 引擎


def test_assess_not_git_warning(tmp_path):
    repo = _mkrepo(tmp_path, {"a.py": "x = 1\n"})
    result = assess_mod.assess(repo)
    assert any("not a git repository" in w for w in result["warnings"])


def test_assess_header_ownership_note(tmp_path):
    repo = _mkrepo(tmp_path, {"m.c": "#include \"h.h\"\n", "h.h": "", "x.m": "#import <Foundation/Foundation.h>\n"})
    result = assess_mod.assess(repo)
    assert {e["engine"] for e in result["engines"]["s7"]} == {"c", "objc"}
    assert any("headers (.h) claimed by both" in w for w in result["warnings"])


def test_assess_render_text_and_json_roundtrip(tmp_path):
    repo = _mkrepo(tmp_path, {"a.py": "x = 1\n", "README.md": "# r\n"})
    result = assess_mod.assess(repo)
    text = assess_mod.render_text(result)
    assert text.startswith("vrp assess - repo")
    assert "s7 engines : py (1)   [21 available]" in text
    assert "verdict    : run -> vrp run <repo>" in text
    assert "warnings" in text  # 非 git 仓至少一条
    payload = json.loads(json.dumps(result, ensure_ascii=False))
    assert payload["verdict"] == "run"


def test_assess_gdscript_no_s7_engine_warning(tmp_path):
    """有源码但无 s7 引擎覆盖（GDScript）：verdict 仍 run（indexing/s2 可用），
    但必须诚实告警 enforcement unavailable。
    """
    repo = _mkrepo(tmp_path, {"player.gd": "extends Node\n"})
    result = assess_mod.assess(repo)
    assert result["engines"]["s7"] == []
    assert result["verdict"] == "run"
    assert any("no s7 engine covers" in w for w in result["warnings"])
