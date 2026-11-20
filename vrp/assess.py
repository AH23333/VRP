"""T10（构想v12）：迁移前评估——新仓接入前的只读预检面。

组装既有只读组件（s0 census/content 探针/git 快照）+ 语言×引擎能力矩阵
（扩展名→s7 引擎映射，后缀集合从各引擎模块常量实时取用——单一事实源，
不写死摘要数字，#153 纪律）+ 风险提示，给"值不值得跑/能跑哪些面"结论。
纯只读：不写 .vrp/、不建 CBM 索引、不跑 ETL。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import s0_precheck
from .evidence import EXT_LANGUAGES

# 大仓提示阈值（walked files，IGNORE_DIRS 剪枝后）——提示性质非硬门
LARGE_REPO_HINT_FILES = 20_000
_INDEXED_NO_ENGINE_CAP = 8


def _s7_engine_suffixes() -> dict[str, tuple[str, ...]]:
    """扩展名→s7 引擎映射：后缀集合从各引擎模块常量实时取用（单一事实源）。

    py 无独立文件（router s7_enforce.derive_containers 内联 .py 判定），
    此处声明 ("py", (".py",)) 并由 test_assess 守卫锁 map↔文件系统一致
    （除 py 外每个 key 必有 vrp/s7_enforce_{key}.py，反向亦然）。
    """
    from . import (
        s7_enforce_c,
        s7_enforce_clj,
        s7_enforce_cs,
        s7_enforce_dart,
        s7_enforce_erlang,
        s7_enforce_go,
        s7_enforce_java,
        s7_enforce_kt,
        s7_enforce_lua,
        s7_enforce_objc,
        s7_enforce_ocaml,
        s7_enforce_perl,
        s7_enforce_php,
        s7_enforce_r,
        s7_enforce_ruby,
        s7_enforce_rs,
        s7_enforce_scala,
        s7_enforce_swift,
        s7_enforce_ts,
        s7_enforce_zig,
    )

    return {
        "py": (".py",),
        "ts": tuple(s7_enforce_ts.TS_SUFFIXES),
        "go": (s7_enforce_go.GO_SUFFIX,),
        "rs": (s7_enforce_rs.RS_SUFFIX,),
        "java": (s7_enforce_java.JAVA_SUFFIX,),
        "cs": (s7_enforce_cs.CS_SUFFIX,),
        "kt": (s7_enforce_kt.KT_SUFFIX,),
        "c": tuple(s7_enforce_c.C_SUFFIXES),
        "objc": tuple(s7_enforce_objc.OBJC_SUFFIXES),
        "php": (s7_enforce_php.PHP_SUFFIX,),
        "scala": (s7_enforce_scala.SCALA_SUFFIX,),
        "ruby": (s7_enforce_ruby.RUBY_SUFFIX,),
        "dart": (s7_enforce_dart.DART_SUFFIX,),
        "swift": (s7_enforce_swift.SWIFT_SUFFIX,),
        "zig": (s7_enforce_zig.ZIG_SUFFIX,),
        "lua": (s7_enforce_lua.LUA_SUFFIX,),
        "perl": tuple(s7_enforce_perl.PERL_SUFFIXES),
        "erlang": tuple(s7_enforce_erlang.ERLANG_SUFFIXES),
        "r": tuple(s7_enforce_r.R_SUFFIXES),
        "clj": tuple(s7_enforce_clj.CLJ_SUFFIXES),
        "ocaml": tuple(s7_enforce_ocaml.OCAML_SUFFIXES),
    }


def _mutate_engines(ext_counts: dict[str, int]) -> list[dict]:
    """mutate 引擎面：扩展名集合直接取 s2_mutate_run 常量（单一事实源，
    #89 教训——多实现同语义必分叉）。
    """
    from . import s2_mutate_run

    specs = (
        ("mutmut", s2_mutate_run._PY_EXTS),
        ("stryker", s2_mutate_run._TS_EXTS),
        ("cargo-mutants", s2_mutate_run._RS_EXTS),
    )
    out: list[dict] = []
    for engine, exts in specs:
        files = sum(ext_counts.get(e, 0) for e in exts)
        if files:
            out.append({"engine": engine, "files": files})
    return out


def _scan_exts(repo: Path) -> tuple[dict[str, int], int]:
    """os.walk（IGNORE_DIRS 剪枝，与 s0 同口径）按扩展名计数。

    返回 (ext_counts, total_files)；扩展名统一小写（Windows 大小写不敏感
    文件系统上 .R/.r 混写归一——r 引擎 R_SUFFIXES 即小写口径）。
    """
    counts: dict[str, int] = {}
    total = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in s0_precheck.IGNORE_DIRS]
        for fname in files:
            total += 1
            ext = Path(fname).suffix.lower()
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
    return counts, total


def assess(repo: Path) -> dict:
    """迁移前评估（只读）。返回结构化结果；呈现见 render_text / CLI --json。"""
    repo = Path(repo).resolve()
    ext_counts, total_files = _scan_exts(repo)
    engine_suffixes = _s7_engine_suffixes()
    ext_to_engines: dict[str, list[str]] = {}
    for engine, suffixes in engine_suffixes.items():
        for suffix in suffixes:
            ext_to_engines.setdefault(suffix, []).append(engine)
    engine_files: dict[str, int] = {}
    for ext, engines in ext_to_engines.items():
        for engine in engines:
            engine_files[engine] = engine_files.get(engine, 0) + ext_counts.get(ext, 0)
    s7_engines = [
        {"engine": engine, "files": files}
        for engine, files in sorted(engine_files.items(), key=lambda kv: (-kv[1], kv[0]))
        if files > 0
    ]
    mutate = _mutate_engines(ext_counts)
    covered_exts = frozenset(ext_to_engines)
    indexed_no_engine = [
        {"ext": ext, "files": ext_counts[ext]}
        for ext in sorted(
            (e for e in ext_counts if e in EXT_LANGUAGES and e not in covered_exts),
            key=lambda e: (-ext_counts[e], e),
        )
    ][:_INDEXED_NO_ENGINE_CAP]
    # source-like：s0 源码判定 ∪ 引擎覆盖面（.r/.erl/.clj 等不在
    # EXT_LANGUAGES 但有专用引擎——census 不计，引擎面必须计）
    source_like_exts = frozenset(s0_precheck.SOURCE_EXTS) | covered_exts
    source_files = sum(n for ext, n in ext_counts.items() if ext in source_like_exts)
    census = s0_precheck.census_scc(repo)
    if census is None:
        census = s0_precheck.census_fallback(repo)
    content = s0_precheck.content_repo_probe(repo)
    git = s0_precheck.git_snapshot(repo)

    warnings: list[str] = []
    if content["detected"]:
        warnings.append(
            "content repository detected - code analysis not applicable (hint, not a gate)"
        )
    if not git.get("is_repo"):
        warnings.append("not a git repository - etl snapshot requires git (facts.git_head)")
    elif git.get("dirty"):
        warnings.append(
            f"working tree dirty ({git.get('dirty_count', 0)} files)"
            " - commit or stash for a clean snapshot"
        )
    if len(s7_engines) > 1:
        names = "+".join(e["engine"] for e in s7_engines[:3])
        warnings.append(
            f"mixed-language ({names}):"
            " s7 enforcement runs per-language engines (divide-and-conquer)"
        )
    if {e["engine"] for e in s7_engines} >= {"c", "objc"} and ext_counts.get(".h", 0) > 0:
        warnings.append(
            "headers (.h) claimed by both c and objc engines"
            " - ownership decided at runtime by .m/.c coexistence"
        )
    if source_files > 0 and not s7_engines:
        warnings.append(
            "no s7 engine covers these extensions - enforcement unavailable"
            " (indexing/s2 findings may still work)"
        )
    if total_files > LARGE_REPO_HINT_FILES:
        warnings.append(
            f"large repository ({total_files} walked files)"
            " - full pipeline may take significant time"
        )
    if source_files == 0 and not content["detected"]:
        warnings.append("no source-like files found")

    if content["detected"]:
        verdict = "skip (content repository)"
    elif source_files == 0:
        verdict = "skip (no source files)"
    else:
        verdict = "run"
    return {
        "repo": str(repo),
        "name": repo.name,
        "git": git,
        "files": {"walked": total_files, "source": source_files},
        "census": census,
        "content_repo": content,
        "engines": {
            "s7": s7_engines,
            "s7_available": len(engine_suffixes),
            "mutate": mutate,
        },
        "indexed_no_engine": indexed_no_engine,
        "warnings": warnings,
        "verdict": verdict,
        "next": "vrp run <repo>" if verdict == "run" else None,
    }


def render_text(result: dict) -> str:
    """人读终端呈现（chrome 英文，惯例同报告链）。"""
    lines: list[str] = [f"vrp assess - {result['name']}"]
    git = result["git"]
    if git.get("is_repo"):
        head = git.get("head") or "?"
        lines.append(
            f"  git        : {head[:12]} branch={git.get('branch')}"
            f" dirty={git.get('dirty_count', 0)}"
        )
    else:
        lines.append("  git        : not a git repository")
    census = result["census"]
    langs = "; ".join(
        f"{l['name']} {l['files']} files/{l['lines']:,} lines"
        for l in census.get("languages", [])[:5]
    )
    lines.append(f"  census     : via {census.get('tool', '?')}")
    lines.append(f"  languages  : {langs or '(none)'}")
    files = result["files"]
    lines.append(f"  files      : {files['walked']:,} walked / {files['source']:,} source-like")
    s7_txt = ", ".join(f"{e['engine']} ({e['files']})" for e in result["engines"]["s7"])
    lines.append(f"  s7 engines : {s7_txt or '(none)'}   [{result['engines']['s7_available']} available]")
    mut = ", ".join(f"{m['engine']} ({m['files']})" for m in result["engines"]["mutate"])
    lines.append(f"  mutate     : {mut or '(none)'}")
    if result["indexed_no_engine"]:
        ine = ", ".join(f"{e['ext']} ({e['files']})" for e in result["indexed_no_engine"])
        lines.append(f"  indexed-no-engine: {ine}")
    if result["warnings"]:
        lines.append("  warnings   :")
        for w in result["warnings"]:
            lines.append(f"    - {w}")
    verdict = result["verdict"] + (f" -> {result['next']}" if result.get("next") else "")
    lines.append(f"  verdict    : {verdict}")
    return "\n".join(lines)
