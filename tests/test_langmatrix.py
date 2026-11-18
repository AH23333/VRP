"""N18（构想v11 甜点）：语言能力矩阵测试——#153 纪律全量锁。

- 单源锁：每个格子从属主模块常量实时派生（assess/s2_astgrep/s2_mutate_run/
  testsig/EXT_LANGUAGES），零硬编码语言清单
- 行集锁：21 引擎语言 = assess map 键集（真树核验）∪ EXT_LANGUAGES 无引擎
  认知语言（C++/GDScript indexed-only 诚实行）
- 面锁：astgrep 规则计数=RULE_META 前缀实数（且与 rules/s2 文件数对账）/
  mutate 三引擎映射 / deadcode-p4 门控镜像锚点（s2_p4 源内 package.json+
  *.py 门在，改门不同步=红）/ testsig 自建面 _JS_TS_EXTS
- render/CLI E2E/确定性
"""

from __future__ import annotations

import json
from pathlib import Path

from vrp import assess, evidence, langmatrix, s2_astgrep, testsig
from vrp.cli import main as cli_main

REPO_ROOT = Path(langmatrix.__file__).resolve().parents[1]


def test_matrix_rows_engine_union_extra():
    rows = langmatrix.matrix()
    langs = [r["language"] for r in rows]
    # 引擎行在前（升序）+ 认知无引擎行在后（升序）——既序
    assert langs == sorted(assess._s7_engine_suffixes().keys()) + langmatrix.extra_indexed_languages()
    assert len([r for r in rows if r["enforce"] is not None]) == 21
    # 归并不拆行：js 并入 ts 引擎、C++ 并入 c 引擎（后缀包含先例）——无独立行
    extras = {r["language"]: r for r in rows if r["enforce"] is None}
    assert set(extras) == {"GDScript"}  # 唯一 indexed-only 诚实行（#124 assess 先例）
    gd = extras["GDScript"]
    assert gd["astgrep_rules"] == 0 and gd["mutate_engine"] is None and gd["deadcode_p4"] is None


def test_extra_indexed_derived_not_hardcoded():
    """extras 从 EXT_LANGUAGES−引擎后缀实时推导（#153：新语言接引擎即自动出/出清
    extras）；数据/配置格式（非符号语言）分类排除。"""
    engine_exts: set[str] = set()
    for suffixes in assess._s7_engine_suffixes().values():
        engine_exts.update(suffixes)
    lang_exts: dict[str, set[str]] = {}
    for ext, language in evidence.EXT_LANGUAGES.items():
        lang_exts.setdefault(language, set()).add(ext)
    expect = {
        language
        for language, exts in lang_exts.items()
        if language not in langmatrix.DATA_FORMAT_LANGUAGES and not (exts & engine_exts)
    }
    assert set(langmatrix.extra_indexed_languages()) == expect


def test_astgrep_face_counts_match_rule_language_fields():
    """规则 yml language 字段→EXT_LANGUAGES 反查→引擎后缀交集归行：
    js-console-log 落 ts 行（TS_SUFFIXES 含 .js）——js 规则不失踪。"""
    counts = langmatrix.astgrep_rules_by_engine()
    assert counts == {"py": 4, "ts": 5}  # ts=4 ts 规则+1 js 规则归并
    rule_files = sorted((REPO_ROOT / "vrp" / "rules" / "s2").glob("*.yml"))
    assert len(rule_files) == len(s2_astgrep.RULE_META) == sum(counts.values()) == 9


def test_mutate_face_maps_three_engines():
    """行粒度=引擎键：js 归 ts 行（stryker _TS_EXTS 含 .js）。"""
    mutate = langmatrix.mutate_engines_by_engine()
    assert mutate == {"py": "mutmut", "rs": "cargo-mutants", "ts": "stryker"}


def test_deadcode_p4_gate_mirror_anchor_lock():
    """p4 镜像锚点锁：s2_p4 源内两门（package.json/*.py）在→映射有效；
    门被改而镜像未同步=空映射=红（防改门漂移）；行粒度 js 归 ts 行。"""
    p4 = langmatrix.deadcode_p4_by_engine()
    assert p4 == {"py": "vulture", "ts": "knip"}
    source = (REPO_ROOT / "vrp" / "s2_p4.py").read_text(encoding="utf-8")
    assert "package.json" in source and "*.py" in source  # 锚点在码内


def test_testsig_selfbuild_face():
    """行粒度：JS/TS 全归 ts 行（_JS_TS_EXTS ⊆ TS_SUFFIXES）。"""
    selfbuild = langmatrix.testsig_selfbuild_engines()
    assert selfbuild == {"ts"}
    assert testsig._JS_TS_EXTS


def test_face_summary_gap_counts():
    summary = langmatrix.face_summary()
    assert summary["enforce"] == {"covered": 21, "total": 21}
    assert summary["astgrep"]["rules"] == 9  # 4 py+4 ts+1 js 全落行（js 归 ts）
    assert summary["astgrep"]["covered"] == 2 and summary["astgrep"]["total"] == 21  # py/ts
    assert summary["mutate"]["covered"] == 3  # py/ts/rs（js 归 ts）
    assert summary["deadcode_p4"]["covered"] == 2  # ts/py（js 归 ts）
    assert summary["testsig_selfbuild"]["covered"] == 1  # ts（js 归 ts）


def test_matrix_deterministic():
    assert langmatrix.matrix() == langmatrix.matrix()
    assert langmatrix.face_summary() == langmatrix.face_summary()


def test_render_text_shape():
    text = langmatrix.render_text()
    assert "vrp langmatrix" in text
    assert "GDScript" in text  # indexed-only 诚实行
    assert "mutmut" in text and "stryker" in text and "cargo-mutants" in text
    assert "knip" in text and "vulture" in text
    assert "缺口 19 语言" in text  # astgrep 面 21-2（py/ts）
    assert "语言无关面" in text  # jscpd/S3/S5 注记
    assert "并入" in text  # js→ts/C++→c 归并注记
    assert langmatrix.matrix_rc() == 0


def test_cli_end_to_end(capsys):
    rc = cli_main(["langmatrix", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(out["rows"]) == 21 + len(langmatrix.extra_indexed_languages())
    assert out["summary"]["astgrep"]["rules"] == 9
    rc = cli_main(["langmatrix"])
    text = capsys.readouterr().out
    assert rc == 0 and "vrp langmatrix" in text
