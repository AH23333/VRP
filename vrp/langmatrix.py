"""N18（构想v11 甜点）：语言能力矩阵——21+ 语言 × S2/S7 各面覆盖实时核码生成。

tree-sitter README 语言支持表形态。#153 纪律全量落实：**每个格子的数字/名字
都从单源模块常量实时派生**，本模块零硬编码语言清单——新语言接引擎即自动
入矩阵，漏接面即自动显缺口：

- 行语言集 = s7 引擎 map（assess._s7_engine_suffixes，21 键）∪
  evidence.EXT_LANGUAGES 认知的无引擎语言（C++/GDScript——CBM/facts 认知面
  有、S7 执法面无，"覆盖不均"的诚实证据行）
- enforce 列 = 行键本身（引擎在码即行在）
- astgrep 列 = s2_astgrep.RULE_META 规则 id 语言前缀计数（py/ts/js）
- mutate 列 = s2_mutate_run 三引擎扩展名常量映射（mutmut/stryker/cargo-mutants）
- deadcode-p4 列 = knip（JS/TS）/vulture（Python）——门控在 s2_p4 码内
  （package.json 门/*.py glob 门），此处镜像并由 test_langmatrix 锚点锁
  （改门必须同步本模块）
- testsig 自建面 = testsig._JS_TS_EXTS（JS/TS TESTS self-build）
- jscpd/S3/S5/dossier/rawpack/render = 语言无关面（facts.db 下游），不入
  逐语言列（全 ✓ 噪声），render 一行注记

`matrix_rc` 恒 0：矩阵是报告非门（缺口是待办不是错误）。
"""

from __future__ import annotations

from pathlib import Path

from . import assess, evidence, s2_astgrep
from . import testsig as testsig_mod

REPO_ROOT = Path(__file__).resolve().parents[1]

# 证据标注用数据/配置格式（EXT_LANGUAGES 内非符号语言）——非引擎候选，
# 不入 indexed-only 行（分类常量非计数镜像）
DATA_FORMAT_LANGUAGES = frozenset({"JSON", "YAML", "TOML", "Markdown", "SQL", "Shell", "PowerShell"})

_ENGINE_LANG_NOTE = "py 无独立文件（router 内联 .py 判定）；js 并入 ts 引擎（TS_SUFFIXES 含 .js）；C++ 并入 c 引擎（C_SUFFIXES 含 .cpp 族）"


def engine_languages() -> list[str]:
    """S7 执法面语言集（assess 单源，升序）。"""
    return sorted(assess._s7_engine_suffixes().keys())


def extra_indexed_languages() -> list[str]:
    """EXT_LANGUAGES 认知但无 S7 引擎覆盖的语言（升序）——indexed-only 诚实行。

    判定=该语言的全部扩展名均不在任何引擎后缀集内（≥1 扩展名被引擎覆盖即
    并入该引擎行，js→ts/C++→c 并 engine 先例）；数据/配置格式排除（非符号
    语言非引擎候选）。"""
    engine_exts: set[str] = set()
    for suffixes in assess._s7_engine_suffixes().values():
        engine_exts.update(suffixes)
    lang_exts: dict[str, set[str]] = {}
    for ext, language in evidence.EXT_LANGUAGES.items():
        lang_exts.setdefault(language, set()).add(ext)
    extras: set[str] = set()
    for language, exts in lang_exts.items():
        if language in DATA_FORMAT_LANGUAGES:
            continue
        if not (exts & engine_exts):
            extras.add(language)
    return sorted(extras)


def astgrep_rules_by_engine() -> dict[str, int]:
    """astgrep 规则面：规则 yml language 字段（单源）→EXT_LANGUAGES 反查扩展名
    →引擎后缀交集归行——js 规则落 ts 行（TS_SUFFIXES 含 .js 归并同 engine 面）。"""
    lang_to_exts: dict[str, set[str]] = {}
    for ext, language in evidence.EXT_LANGUAGES.items():
        lang_to_exts.setdefault(language, set()).add(ext)
    engine_exts = {lang: set(sufs) for lang, sufs in assess._s7_engine_suffixes().items()}
    counts: dict[str, int] = {}
    for rule_id in s2_astgrep.RULE_META:
        yml = REPO_ROOT / "vrp" / "rules" / "s2" / f"{rule_id}.yml"
        language = _rule_language(yml)
        exts = lang_to_exts.get(language)
        if exts is None:
            raise RuntimeError(f"astgrep rule {rule_id} declares unknown language: {language!r}")
        for engine, sufs in engine_exts.items():
            if exts & sufs:
                counts[engine] = counts.get(engine, 0) + 1
    return dict(sorted(counts.items()))


def _rule_language(yml: Path) -> str:
    for line in yml.read_text(encoding="utf-8").splitlines():
        if line.startswith("language:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"astgrep rule has no language field: {yml}")


def mutate_engines_by_engine() -> dict[str, str]:
    """mutate 面：引擎行→mutate 引擎名（s2_mutate_run 扩展名常量∩引擎后缀，
    行粒度=引擎键——js 归 ts 行先例同 astgrep 面；冲突保首引擎诚实）。"""
    from . import s2_mutate_run

    specs = (
        ("mutmut", s2_mutate_run._PY_EXTS),
        ("stryker", s2_mutate_run._TS_EXTS),
        ("cargo-mutants", s2_mutate_run._RS_EXTS),
    )
    out: dict[str, str] = {}
    for mutate_engine, exts in specs:
        for engine, suffixes in assess._s7_engine_suffixes().items():
            if set(exts) & set(suffixes):
                out.setdefault(engine, mutate_engine)
    return dict(sorted(out.items()))


def deadcode_p4_by_engine() -> dict[str, str]:
    """deadcode-p4 面：引擎行→适配器（knip JS/TS→ts 行 + vulture Python→py 行）。

    门控镜像：s2_p4 码内 knip 走 package.json 门（JS/TS 生态）、vulture 走
    *.py glob 门——本映射是门的镜像非第二实现，test_langmatrix 锁 s2_p4
    源内两门锚点在（改门不同步=红）。
    """
    source = (REPO_ROOT / "vrp" / "s2_p4.py").read_text(encoding="utf-8")
    if "package.json" not in source or "*.py" not in source:
        return {}
    return {"py": "vulture", "ts": "knip"}


def testsig_selfbuild_engines() -> set[str]:
    """testsig 自建面（JS/TS TESTS self-build，v6 §12.4）——_JS_TS_EXTS ∩
    引擎后缀归行（js/.ts 全在 TS_SUFFIXES→ts 行）。"""
    exts = set(testsig_mod._JS_TS_EXTS)
    engines: set[str] = set()
    for engine, suffixes in assess._s7_engine_suffixes().items():
        if exts & set(suffixes):
            engines.add(engine)
    return engines


def matrix() -> list[dict]:
    """矩阵主体：行=引擎语言 21+认知无引擎语言，列=五逐语言面。"""
    astgrep = astgrep_rules_by_engine()
    mutate = mutate_engines_by_engine()
    p4 = deadcode_p4_by_engine()
    selfbuild = testsig_selfbuild_engines()
    rows: list[dict] = []
    for lang in engine_languages():
        rows.append(_row(lang, astgrep.get(lang, 0), mutate.get(lang), p4.get(lang), lang in selfbuild))
    for lang in extra_indexed_languages():
        rows.append(_row(lang, 0, None, None, False, engine=False))
    return rows


def _row(lang: str, astgrep_rules: int, mutate_engine: str | None, p4_adapter: str | None,
         selfbuild: bool, engine: bool = True) -> dict:
    return {
        "language": lang,
        "enforce": lang if engine else None,
        "astgrep_rules": astgrep_rules,
        "mutate_engine": mutate_engine,
        "deadcode_p4": p4_adapter,
        "testsig_selfbuild": selfbuild,
    }


def face_summary() -> dict[str, dict]:
    """各逐语言面覆盖计数（缺口=引擎语言 21 − 覆盖数）。"""
    rows = [r for r in matrix() if r["enforce"] is not None]
    total = len(rows)
    return {
        "enforce": {"covered": total, "total": total},
        "astgrep": {
            "covered": sum(1 for r in rows if r["astgrep_rules"] > 0),
            "total": total,
            "rules": sum(r["astgrep_rules"] for r in rows),
        },
        "mutate": {"covered": sum(1 for r in rows if r["mutate_engine"]), "total": total},
        "deadcode_p4": {"covered": sum(1 for r in rows if r["deadcode_p4"]), "total": total},
        "testsig_selfbuild": {"covered": sum(1 for r in rows if r["testsig_selfbuild"]), "total": total},
    }


def render_text() -> str:
    rows = matrix()
    summary = face_summary()
    lines = ["vrp langmatrix - 语言能力矩阵（实时核码生成 #153 纪律，零硬编码语言清单）"]
    lines.append(
        f"  {'language':<12} {'enforce':<10} {'astgrep':>7}  {'mutate':<15} {'deadcode-p4':<12} selfbuild"
    )
    for row in rows:
        enforce = row["enforce"] or "—"
        rules = str(row["astgrep_rules"]) if row["astgrep_rules"] else "—"
        mutate = row["mutate_engine"] or "—"
        p4 = row["deadcode_p4"] or "—"
        selfbuild = "✓" if row["testsig_selfbuild"] else "—"
        lines.append(
            f"  {row['language']:<12} {enforce:<10} {rules:>7}  {mutate:<15} {p4:<12} {selfbuild}"
        )
    lines.append("")
    for face, stats in summary.items():
        covered, total = stats["covered"], stats["total"]
        gap = total - covered
        extra = f" rules={stats['rules']}" if "rules" in stats else ""
        lines.append(f"  {face:<17} {covered}/{total} 覆盖  缺口 {gap} 语言{extra}")
    lines.append("  语言无关面（不入逐语言列）: jscpd token-clone / S3 anchor / S5 dossier / rawpack / render — 全语言经 facts.db")
    lines.append(f"  注记: {_ENGINE_LANG_NOTE}")
    return "\n".join(lines)


def matrix_rc() -> int:
    """报告非门：矩阵恒 0（缺口是待办不是错误）。"""
    return 0
