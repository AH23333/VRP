"""R-Z 批守卫：文件写入生命周期与序列化纪律（写面，与 R-X 读面对称）。

census（work/probe_rz_recon.py → work/census_rz.txt）裁决的落地：
- newline 面：42 写站仅 13 pin -> 27 write_text 批量补 pin（codemod
  work/codemod_rz_newline.py）+ mcp_server _write_temp mkstemp(text=True)
  撤 flag+open 补 newline + auto_curate log append 补 newline。理由：
  同一报告目录内混用（render.py module 页 pin LF 而 ir/*.json+mmd 裸奔
  CRLF）=批次累积不一致；统一 LF 后成品字节跨平台确定。
- dumps 面：90 非 CLI 站中裸 dumps（缺 ensure_ascii=）仅 3 函数豁免
  （JS 嵌入语义等价 / ASCII 按构造），xcheck ledger detail 与 llm_client
  请求体已修为 pin（同族一致性，s2_p4 同型站点先例）。
- 原子性面（记实绿）：4 个 os.replace 恰为 4 个状态关键单文件成品
  （etl facts.db / s5_dossier / s5_rawpack / session）；verdicts 直写但
  读侧容错（R-O unreadable 占位）；报告可再生成。
- fsync 面（记实）：0 站；可再生成成品 + 原子改名提供崩溃一致性，
  不承诺 fsync 持久性。
"""

from __future__ import annotations

import ast
from pathlib import Path

VRP = Path(__file__).resolve().parents[1] / "vrp"

# 裸 dumps（无 ensure_ascii=）豁免清单：census 逐站裁决为绿的三函数。
# JS 嵌入（\\uXXXX 与原文在 JS 字符串语义等价）或载荷 ASCII 按构造。
BARE_DUMPS_ALLOWLIST = {
    ("render_chain.py", "inject_chrome"),  # JS var 嵌入：ensure_ascii 两态 JS 解析同值
    ("etl.py", "_etl_edges"),  # {"jaccard": float, "same_file": bool} ASCII 按构造
    ("s2_jscpd.py", "cross_check"),  # {"tool"/"agreed"/"lines"/"side"} ASCII 按构造
}


def _trees():
    for py in sorted(VRP.glob("*.py")):
        yield py, ast.parse(py.read_text(encoding="utf-8-sig"))


def test_every_text_write_pins_newline():
    """newline 守卫：write_text 与文本写/追加模式 open 必 pin newline=；mkstemp 禁 text=True。"""
    violations: list[str] = []
    for py, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "write_text":
                if not any(k.arg == "newline" for k in node.keywords):
                    violations.append(f"{py.name}:{node.lineno} write_text without newline=")
                continue
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "mkstemp"
                and isinstance(func.value, ast.Name)
                and func.value.id == "tempfile"
                and any(k.arg == "text" and isinstance(k.value, ast.Constant) and k.value.value for k in node.keywords)
            ):
                violations.append(f"{py.name}:{node.lineno} mkstemp(text=True) CRT flag banned")
                continue
            if isinstance(func, ast.Attribute) and func.attr == "open":
                mode_arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "mode"), None)
            elif isinstance(func, ast.Name) and func.id == "open":
                mode_arg = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
            else:
                continue
            if not (isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str)):
                continue
            mode = mode_arg.value
            if "b" in mode:
                continue
            if any(c in mode for c in "wax+"):
                if not any(k.arg == "newline" for k in node.keywords):
                    violations.append(f"{py.name}:{node.lineno} open({mode!r}) text-write without newline=")
    assert not violations, "text-write newline discipline regressed: " + "; ".join(violations)


def test_bare_json_dumps_allowlist_closed():
    """dumps 守卫：缺 ensure_ascii= 的 json.dumps 只许出现在 census 豁免的三个函数。"""
    violations: list[str] = []
    for py, tree in _trees():
        parents: dict[int, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for n in ast.walk(fn):
                    parents[id(n)] = fn.name
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            func = node.func
            if not (func.attr == "dumps" and isinstance(func.value, ast.Name) and func.value.id == "json"):
                continue
            if any(k.arg == "ensure_ascii" for k in node.keywords):
                continue
            owner = parents.get(id(node), "<module>")
            if (py.name, owner) not in BARE_DUMPS_ALLOWLIST:
                violations.append(f"{py.name}:{node.lineno} bare dumps in {owner}()")
    assert not violations, "bare json.dumps outside allowlist: " + "; ".join(violations)
