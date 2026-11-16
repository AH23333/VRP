"""#148 governance meta-guard：守卫自检（学 TSA RFC-0028 gate --self-check）。

fail-silent AST 守卫（``assert not violations`` 形态）的两类死形态无防：
- 形态 A 扫描面空：glob 模式/目录定位退化→零文件→循环零次→静默绿；
- 形态 B 检测器死：AST 匹配逻辑坏/正则过期→零违规→静默绿
  （R-AD 批 R-X 链式守卫盲区先例 = TSA codemap-sync "the gate itself
  died ... while every test stayed green" 同型教训）。

本文件把手工"红面自证"纪律（git stash push → 精确红 → pop → 绿）自动化：
每个 fail-silent 守卫配罐头违例（批历史真实红面取材），monkeypatch 守卫
模块级扫描常量 → tmp 树 → **直调守卫函数本体** → 必红（同源铁律：罐头
测的就是守卫本体，不是检测逻辑副本——TSA "exact set equality" 哲学）。

fail-loud 守卫（断言形态"解析值必须等于/包含 X"——匹配不到即红）天然
自检，豁免罐头，豁免清单记实如下：
- test_cli_census.test_cbm_name_flag_vocabulary_closed（断言 len(sites)==8）
- test_sqlite_boundary 两 AST 守卫（断言 in names）
- test_docs_drift Face A/D/E（计数对账：regex 解析失败→assert not missing 红）
- test_mcp_server TOOL_NAMES 锁（集合相等断言）
- 各守卫文件内的行为测试（非扫描守卫，不在登记面）
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent


@dataclass
class Canary:
    """一条守卫罐头：违例源码 + 守卫定位 + patch 面。

    patch：守卫模块属性 -> tmp repo 内相对路径（monkeypatch 为 Path）。
    raw_patch：守卫模块属性 -> 原值直设（如 docs_drift.README 的 str）。
    files：tmp repo 内相对路径 -> 罐头违例源码（必可 ast.parse/compile）。
    """

    module: str
    func: str
    patch: dict[str, str] = field(default_factory=dict)
    raw_patch: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)


# ---- 罐头违例源码（批历史真实红面取材）----

_ANYALL_D = "def f(s):\n    return any('abc')\n"
_ANYALL_SHADOW = "all = 5\n"
_CLI_PRINT = 'print("stdout leak")\n'
_CLI_DUMPS = "import json\nout = json.dumps({'k': 1})\n"
_FACE_B_README = "## 重构事务与闸门\n\n| G4 | 无码表 |\n\n## tail\n"
_FACE_B_CODE = 'CODE = "gate/G0/leak"\n'
_FACE_C_README = "此 README 仅提及已有文档，未列新文件。\n"
_EXC_ASCII = 'def f(x):\n    raise RuntimeError("失败: 输入非法")\n'
_SILENT_EXIT = "import sys\nsys.exit(0)\ndef g():\n    raise SystemExit\n"
_GIT_LITERAL = (
    "import subprocess\n"
    "def head():\n"
    "    return subprocess.run(['git', 'rev-parse', 'HEAD'], text=True)\n"
)
_GIT_TEXT = (
    "import subprocess\n"
    "def probe(cmd):\n"
    "    return subprocess.run(cmd, text=True)\n"
)
_IO_ENCODING = (
    "def save(p, data):\n"
    "    with open(p, 'w') as fh:\n"
    "        fh.write(data)\n"
)
# docstring 内单反斜杠无效转义（\z）——compile 发 SyntaxWarning（R-M 已修形态）
_IO_SYNTAX_WARN = '"""docstring bad \\z escape"""\n'
_MOD_SET = "LOOKUP = {1, 2, 3}\n"
_MOD_DICT = "TABLE = {}\ndef reg(x):\n    TABLE.update(x)\n"
_CHAINED_LOADS = "import json\ndef cfg(s):\n    return json.loads(s).get('k')\n"
_VAR_LOADS = (
    "import json\n"
    "def cfg(s):\n"
    "    raw = json.loads(s)\n"
    "    return raw.get('k')\n"
)
_SQL_URI = (
    "import sqlite3\n"
    "def open_ro(p):\n"
    "    return sqlite3.connect(f'file:{p}?mode=ro')\n"
)
_SQL_CONCAT = "def q(con, col):\n    return con.execute('SELECT ' + col)\n"
_SQL_PERCENT = "def q(con, table):\n    return con.execute('SELECT 1 FROM %s' % table)\n"
_SQL_FSTRING = "def q(con, name):\n    return con.execute(f'SELECT * FROM {name}')\n"
_SUB_RUN = "import subprocess\ndef ls(p):\n    return subprocess.run(['ls', p])\n"
_SUB_POPEN_TEXT = "import subprocess\ndef spawn(cmd):\n    return subprocess.Popen(cmd, text=True)\n"
_SUB_SHELL = "import subprocess\ndef sh(cmd):\n    return subprocess.Popen(cmd, shell=True)\n"
_SLEEP = "import time\ndef slow():\n    time.sleep(1)\n"
_WALL_CLOCK = (
    "import time\n"
    "from datetime import datetime\n"
    "def stamp():\n"
    "    return datetime.now(), time.time()\n"
)
_WRITE_NEWLINE = "def save(p, s):\n    p.write_text(s)\n"
_BARE_DUMPS = "import json\ndef ser(x):\n    return json.dumps(x)\n"

# ---- fail-silent 守卫登记表（新增 fail-silent 守卫必须登记；fail-loud 豁免见模块 docstring）----

_CANARIES = [
    # R-R anyall
    Canary("tests.test_anyall_census", "test_any_all_no_iterated_semantics_literals",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _ANYALL_D}),
    Canary("tests.test_anyall_census", "test_any_all_not_shadowed",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _ANYALL_SHADOW}),
    # R-U CLI census（cbm_name 词汇守卫 fail-loud 豁免）
    Canary("tests.test_cli_census", "test_cli_prints_are_step_helpers_or_stderr",
           patch={"CLI_PATH": "vrp/cli.py"}, files={"vrp/cli.py": _CLI_PRINT}),
    Canary("tests.test_cli_census", "test_cli_json_dumps_canonical_form",
           patch={"CLI_PATH": "vrp/cli.py"}, files={"vrp/cli.py": _CLI_DUMPS}),
    # R-AA docs drift（Face B 双集合相等：双空==绿；Face C rglob 空循环绿；
    # Face A/D/E fail-loud 豁免）
    Canary("tests.test_docs_drift", "test_readme_gate_codes_match_code",
           patch={"ROOT": "."}, raw_patch={"README": _FACE_B_README},
           files={"vrp/s6_transact.py": _FACE_B_CODE}),
    Canary("tests.test_docs_drift", "test_readme_docs_list_complete",
           patch={"ROOT": "."}, raw_patch={"README": _FACE_C_README},
           files={"docs/unmentioned.md": "# 新文档\n"}),
    # R-T exception msg
    Canary("tests.test_exception_msg_census", "test_exception_message_literals_ascii",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _EXC_ASCII}),
    Canary("tests.test_exception_msg_census", "test_no_silent_systemexit",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _SILENT_EXIT}),
    # R-N git census
    Canary("tests.test_git_census", "test_git_literal_subprocess_discipline",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _GIT_LITERAL}),
    Canary("tests.test_git_census", "test_subprocess_run_text_mode_pins_encoding",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _GIT_TEXT}),
    # R-M IO census
    Canary("tests.test_io_census", "test_text_io_encoding_pinned",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _IO_ENCODING}),
    Canary("tests.test_io_census", "test_vrp_sources_compile_without_syntax_warnings",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _IO_SYNTAX_WARN}),
    # R-V module scope
    Canary("tests.test_module_scope", "test_module_level_sets_are_frozen",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _MOD_SET}),
    Canary("tests.test_module_scope", "test_module_level_dicts_never_directly_mutated",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _MOD_DICT}),
    # R-X/R-AD payload
    Canary("tests.test_payload_discipline", "test_no_chained_json_loads_get_in_vrp",
           patch={"ROOT": ".", "VRP": "vrp"}, files={"vrp/bad.py": _CHAINED_LOADS}),
    Canary("tests.test_payload_discipline", "test_no_unguarded_variable_mediated_loads_access",
           patch={"ROOT": ".", "VRP": "vrp"}, files={"vrp/bad.py": _VAR_LOADS}),
    # R-S SQL census
    Canary("tests.test_sql_census", "test_no_raw_file_uri_connects",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _SQL_URI}),
    Canary("tests.test_sql_census", "test_no_sql_concat_or_format",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _SQL_CONCAT}),
    Canary("tests.test_sql_census", "test_sql_percent_format_only_placeholder_generation",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _SQL_PERCENT}),
    Canary("tests.test_sql_census", "test_sql_fstring_interpolations_audited",
           patch={"VRP_DIR": "vrp"}, files={"vrp/bad.py": _SQL_FSTRING}),
    # R-Y subprocess discipline
    Canary("tests.test_subprocess_discipline", "test_every_subprocess_run_pins_timeout",
           patch={"VRP": "vrp"}, files={"vrp/bad.py": _SUB_RUN}),
    Canary("tests.test_subprocess_discipline", "test_popen_text_mode_pins_encoding_and_errors",
           patch={"VRP": "vrp"}, files={"vrp/bad.py": _SUB_POPEN_TEXT}),
    Canary("tests.test_subprocess_discipline", "test_shell_true_only_in_s4_freeze",
           patch={"VRP": "vrp"}, files={"vrp/bad.py": _SUB_SHELL}),
    Canary("tests.test_subprocess_discipline", "test_run_call_sites_confined_to_procs",
           patch={"VRP": "vrp"}, files={"vrp/bad.py": _SUB_RUN}),
    # R-W/R-AC timing
    Canary("tests.test_timing_discipline", "test_no_real_sleep_call_sites_in_tests",
           patch={"TESTS_ROOT": "tests"}, files={"tests/bad.py": _SLEEP}),
    Canary("tests.test_timing_discipline", "test_no_bom_in_test_sources",
           patch={"TESTS_ROOT": "tests"}, files={"tests/bad.py": "\ufeffimport os\n"}),
    Canary("tests.test_timing_discipline", "test_vrp_wall_clocks_utc_and_elapsed_monotonic",
           patch={"VRP_ROOT": "vrp"}, files={"vrp/bad.py": _WALL_CLOCK}),
    # R-Z write discipline
    Canary("tests.test_write_discipline", "test_every_text_write_pins_newline",
           patch={"VRP": "vrp"}, files={"vrp/bad.py": _WRITE_NEWLINE}),
    Canary("tests.test_write_discipline", "test_bare_json_dumps_allowlist_closed",
           patch={"VRP": "vrp"}, files={"vrp/bad.py": _BARE_DUMPS}),
]

_IDS = [f"{c.module.rsplit('.', 1)[-1]}::{c.func}" for c in _CANARIES]


@pytest.mark.parametrize("canary", _CANARIES, ids=_IDS)
def test_fail_silent_guard_can_fire(canary: Canary, tmp_path: Path, monkeypatch) -> None:
    """罐头红面注入（canary）：直调守卫函数本体，已知违例必红。

    守卫全绿 ≠ 守卫活着：R-AD 批实锤链式守卫盲区（内联形态可见、变量
    中转不可见），TSA codemap-sync 实锤 gate 死而全绿——本测试让每个
    fail-silent 守卫证明自己能红（unwind 01b "prove the checker can
    fail"）。同源铁律：经 monkeypatch 直调守卫本体，罐头测的检测器就
    是守卫用的检测器，不是副本。
    """
    mod = importlib.import_module(canary.module)
    base = tmp_path / "repo"
    for rel, src in canary.files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src, encoding="utf-8", newline="\n")
    for attr, rel in canary.patch.items():
        monkeypatch.setattr(mod, attr, base / rel)
    for attr, value in canary.raw_patch.items():
        monkeypatch.setattr(mod, attr, value)
    guard = getattr(mod, canary.func)
    with pytest.raises(AssertionError):
        guard()


def test_guard_scan_surfaces_nonempty() -> None:
    """形态 A 防线：登记守卫的真实扫描面非空+下限锁。

    glob 模式/目录定位退化到空集 = 零迭代 = 静默绿（TSA cwd 缺陷同型：
    两 gate 曾按 caller cwd 解析 watch base，从 scripts/ 扫描空集报干净
    退 0）。下限锁取宽松值（防退化到空集即可，不随常规增删文件抖动）。
    """
    vrp_files = list((PROJECT_ROOT / "vrp").glob("*.py"))
    test_files = list(TESTS_ROOT.rglob("*.py"))
    assert len(vrp_files) >= 40, f"vrp/ 扫描面退化：{len(vrp_files)} 文件 < 40 下限"
    assert len(test_files) >= 100, f"tests/ 扫描面退化：{len(test_files)} 文件 < 100 下限"
    # CLI_PATH 守卫面：cli.py 单文件在位
    assert (PROJECT_ROOT / "vrp" / "cli.py").is_file(), "cli.py 守卫面缺失"


def test_guards_cwd_invariant(tmp_path: Path, monkeypatch) -> None:
    """TSA cwd 缺陷形态免疫锁：chdir 任意目录后守卫仍绿。

    TSA 两 gate 曾把 watch base 按 caller cwd 解析——从 scripts/ 运行
    即扫描空集报干净退 0。VRP 守卫全部经 ``Path(__file__)`` 定位（cwd
    无关），本测试锁定该性质：chdir tmp 后直调三个代表形态（VRP_DIR/
    VRP/TESTS_ROOT 各一）守卫仍按真实树全绿。
    """
    monkeypatch.chdir(tmp_path)
    for mod_name, func in [
        ("tests.test_anyall_census", "test_any_all_not_shadowed"),
        ("tests.test_subprocess_discipline", "test_every_subprocess_run_pins_timeout"),
        ("tests.test_timing_discipline", "test_no_real_sleep_call_sites_in_tests"),
    ]:
        getattr(importlib.import_module(mod_name), func)()
