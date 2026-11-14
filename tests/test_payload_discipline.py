"""R-X 批守卫：JSON payload 消费纪律。

census（work/probe_rx_census.py）裁决的落地：
- Face A 异常处理族：0 裸 except；5 处 (Exception, KeyboardInterrupt)
  全为清理后重抛边界（KI 非 Exception 子类教训已注释）；3 处宽降级
  全审=诚实（llm_client HTTPError 读体降级 / render 链失败降级 L1L2
  有注释 / s3 逐 claim 隔离 by design）；16 处吞点（pass/continue）逐站
  有据。纪律面绿，本批不改。
- Face B 活缺陷类：json.loads 结果直接 .get/["key"] 而无 isinstance dict
  守卫的消费点——合法 JSON 非 dict 的 payload（手改库/异源写入/日志
  标量行）在按设计应"跳过降级/干净报错"的路径上裸抛 AttributeError
  （离因果远、报错怪、可击穿整报告）。修复 6 处（nav._slices_of /
  nav.nav_slice / render.build_sequence_ir / session._read_session /
  session._list_sessions / s5_cabrief._read_response），与库内已有守卫
  先例（render.py 符号页 slices / render_chain.py）同口径。
- 本文件守卫：链式 json.loads(...).get(...) 反模式全域禁绝（AST 检测，
  nav._slices_of 旧形态即此）；s1_cbm._parse_json_stdout 的
  startswith("{") 结构性安全 pin（本批唯一不修的 Face B 站点及其理由）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from vrp import s1_cbm

ROOT = Path(__file__).resolve().parents[1]
VRP = ROOT / "vrp"


def test_no_chained_json_loads_get_in_vrp():
    """反模式禁绝：vrp/ 全域禁 `json.loads(...).get(...)` 链式形态。

    链式形态 = 解析结果未经 isinstance 守卫直达属性访问——合法 JSON
    非 dict 输入裸抛 AttributeError（R-X 修的 nav._slices_of 旧形态）。
    """
    offenders: list[str] = []
    for path in sorted(VRP.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            # 形态一：json.loads(...).get(...) —— Attribute.get 的 value 是 loads 调用
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "get"
                and isinstance(node.value, ast.Call)
            ):
                call = node.value
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "loads"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "json"
                ):
                    offenders.append(f"{rel}:{node.lineno} chained json.loads(...).get(...)")
    assert not offenders, f"chained json.loads access in vrp/ (R-X census Face B): {offenders}"


def test_parse_json_stdout_pins_object_only():
    """s1_cbm 起点守卫 pin：startswith("{") 保证返回 dict（结构性安全，R-X 不修的理由）。"""
    parsed = s1_cbm._parse_json_stdout('log noise\n{"ok": 1, "n": [1, 2]}')
    assert parsed == {"ok": 1, "n": [1, 2]}
    assert isinstance(parsed, dict)
    with pytest.raises(RuntimeError, match="no JSON object"):
        s1_cbm._parse_json_stdout("scalar only\n42")
    with pytest.raises(RuntimeError, match="no JSON object"):
        s1_cbm._parse_json_stdout("")


def test_no_unguarded_variable_mediated_loads_access():
    """R-AD 守卫：变量中转型 loads 消费纪律（R-X 链式守卫盲区收口）。

    `raw = json.loads(...)` 存入变量后 `raw.get(...)`/`raw[...]` 访问，函数内
    必须存在 isinstance(<名>, dict) 守卫——否则合法 JSON 非 dict payload 在
    按设计应"跳过降级/诚实报错"的路径上裸抛 AttributeError（R-AD 实锤
    curate._revalidate_ingested/_drift_slice/_drift_structure、
    nav._architecture_from_structure、render.build_architecture_ir/
    _iter_modules、s2_p4.run_knip、s2_cargo_mutants.parse_outcomes、
    s2_stryker.parse_report、s7_enforce_ts.parse_violations，共 12 站已修）。
    豁免三函数（绿记实，census work/census_rad.txt）：
    - llm_client._parse_response：try/except (KeyError, IndexError,
      TypeError) 诚实转 ValueError，非 dict 已被 TypeError 分支捕获；
    - render / render_chain 的 _archify_error_text：自家 vendored
      archify --json 输出契约钉死对象形状。
    """
    allowlist: set[tuple[str, str]] = {
        ("llm_client.py", "_parse_response"),
        ("render.py", "_archify_error_text"),
        ("render_chain.py", "_archify_error_text"),
    }
    offenders: list[str] = []
    for path in sorted(VRP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (path.name, func.name) in allowlist:
                continue
            loads_names: set[str] = set()
            guarded_names: set[str] = set()
            accessed_names: set[str] = set()
            for node in ast.walk(func):
                # raw = json.loads(...)
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "loads"
                ):
                    loads_names.add(node.targets[0].id)
                # isinstance(x, dict)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "isinstance"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Name)
                    and isinstance(node.args[1], ast.Name)
                    and node.args[1].id == "dict"
                ):
                    guarded_names.add(node.args[0].id)
                # raw.get(...) / raw[...]
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "get"
                    and isinstance(node.value, ast.Name)
                ):
                    accessed_names.add(node.value.id)
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                    accessed_names.add(node.value.id)
            for name in sorted(loads_names & accessed_names - guarded_names):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{func.lineno} ({func.name}) unguarded loads var '{name}'")
    assert not offenders, (
        "variable-mediated json.loads access without isinstance dict guard "
        f"(R-AD census, allowlist {sorted(allowlist)}): {offenders}"
    )
