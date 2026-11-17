"""I5 explain: 错误码/收据码人话速查（构想v11 甜点，cargo explain 形态）。

设计三要点（#169）：
- 码表单源抽取：gate 码直引 s6_transact.RULE_CODES；reject 码（SLICE_*/
  STRUCT_*/FILE_*）AST 扫 curate/auto_curate 源码收全串常量——新码落源
  即入表，不建第二登记处；P 码直引 render.PATHOLOGY_LEGEND（零复制）。
- 描述面守卫：GATE/REJECT_EXPLANATIONS 的键与抽取集合**双向相等**——
  新码不写解释 → 守卫红 → 强制文档化（防"码表有了人话没有"）。
- 查询面：无参=三族总览；有参=大小写不敏感前缀匹配（"G4"→五子码，
  "G4/edge-mismatch"→单码，"slice_"→全 SLICE 族）；无命中 rc=1 附族级建议。
"""

from __future__ import annotations

import ast
import re

from . import auto_curate, curate, render, s6_transact

FAMILIES = ("gate", "reject", "pathology")

_REJECT_RE = re.compile(r"(SLICE|STRUCT|FILE)_[A-Z_0-9]+")


def _extract_reject_codes() -> list[str]:
    """AST 扫 curate/auto_curate 全串常量，收 SLICE_/STRUCT_/FILE_ 前缀全匹配码。

    只认 whole-constant 全匹配：prompt 散文里的码（长串的子串）不收——
    收录标准=源码里出现过独立字符串形态的码（_err 直参/循环元组皆中）。
    """
    codes: set[str] = set()
    for module in (curate, auto_curate):
        path = module.__file__
        if not path:
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _REJECT_RE.fullmatch(node.value):
                    codes.add(node.value)
    return sorted(codes)


GATE_EXPLANATIONS: dict[str, str] = {
    "gate/G0/apply-edits": "apply 前置校验失败——事务编辑应用前校验不过（文件/形状级），事务拒绝",
    "gate/G1/parse-errors": "提交后重索引解析错误——旁路 ETL 重提取阶段符号/边解析失败，事务回滚",
    "gate/G2/static-check": "lint/typecheck 门失败——目标仓静态检查未通过（发现仓命令面 s0 commands 槽位）",
    "gate/G3/test": "测试门失败——目标仓测试套件未通过，事务回滚",
    "gate/G4/undeclared-change": "声明等价门·符号集信号——改动符号未在 edits/declared_moves 声明",
    "gate/G4/edge-mismatch": "声明等价门·使用边信号——使用边与声明 moves 不符（丢边/加边必抓）",
    "gate/G4/dangling-reference": "声明等价门·悬垂引用信号——改动后残留指向已删/已移符号的引用",
    "gate/G4/verify-broken": "声明等价门·verify_changes 信号——verify 通道返回损坏/不一致",
    "gate/G4/structural-change": "声明等价门·结构 diff 信号——difftastic 同符号体内偷改被抓（结构变更未走声明通道）",
    "gate/G5/dead-code-increase": "死代码单调性门——事务后死码计数高于基线，事务拒绝",
    "gate/G6/commit-failed": "git 提交失败——回滚树+诚实 aborted 裁决（提交先于记账，绝不留 DB=committed 却无 git 提交的假象）",
}

REJECT_EXPLANATIONS: dict[str, str] = {
    "FILE_NOT_JSON": "curate 输入文件不可读或非合法 JSON——检查文件编码与 JSON 语法",
    "FILE_WRONG_SHAPE": "curate 输入文件形状不对——slices.json 须为 JSON 数组、structure 须为 JSON 对象",
    "SLICE_SCHEMA": "slice 对象模式不符——slice_id/name/summary/steps 字段类型或形状校验失败（详见 auto_curate prompt 契约）",
    "SLICE_EMPTY_NAME": "slice.name 为空——name 必须非空字符串",
    "SLICE_EMPTY_SUMMARY": "slice.summary 为空——summary 必须非空字符串",
    "SLICE_UNKNOWN_FID": "steps[].fid 在 facts.db symbols 中不存在——LLM 编造符号的典型拒（sql_views 只暴露少数 fid）",
    "SLICE_MISSING_EDGE": "steps 相邻对之间在 facts.db 无对应边（CALLS/CALL_REFERENCE/HTTP_CALLS）——编造调用链必拒",
    "SLICE_ACT_BOUNDS": "acts 数组与 steps 不对齐——acts 须覆盖全部 steps 且不越界",
    "STRUCT_SCHEMA": "structure 对象模式不符——module/layer/edges 字段类型或形状校验失败",
    "STRUCT_EMPTY_NAME": "structure module name 为空——name 必须非空字符串",
    "STRUCT_EMPTY_RESPONSIBILITY": "structure module responsibility 为空——responsibility 必须非空字符串",
    "STRUCT_UNKNOWN_FID": "structure 中引用的 fid 在 facts.db symbols 中不存在",
    "STRUCT_FID_OVERLAP": "同一 fid 被分配到多个 module——模块划分须互斥",
    "STRUCT_LAYER_CYCLE": "层 DAG 含环——分层必须无环",
    "STRUCT_UNKNOWN_LAYER": "引用了未定义的层名——edges 里的层必须都在 layers 声明中",
    "STRUCT_RULE_LAYER": "rules 中引用了未定义的层名——forbidden/allowed 规则里的层必须都在 layers 声明中",
}


def _short_form(code: str) -> str:
    """展示/匹配短形：gate/G4/edge-mismatch → G4/edge-mismatch；其余原样。"""
    return code.removeprefix("gate/")


def _all_codes() -> dict[str, tuple[str, str]]:
    """全码表：{短形: (族, 描述)}——单源抽取+守卫注册表合成。"""
    table: dict[str, tuple[str, str]] = {}
    for canonical, desc in GATE_EXPLANATIONS.items():
        table[_short_form(canonical)] = ("gate", desc)
    for code, desc in REJECT_EXPLANATIONS.items():
        table[code] = ("reject", desc)
    for code, desc in render.PATHOLOGY_LEGEND.items():
        table[code] = ("pathology", desc)
    return table


def run_explain(query: str | None) -> dict:
    """无参=三族总览；有参=前缀匹配（大小写不敏感）。无命中 matches 空。"""
    table = _all_codes()
    if not query:
        by_family: dict[str, list[str]] = {f: [] for f in FAMILIES}
        for code, (family, _) in table.items():
            by_family[family].append(code)
        return {
            "query": None,
            "families": {f: sorted(cs) for f, cs in by_family.items()},
            "counts": {f: len(by_family[f]) for f in FAMILIES},
            "matches": [],
        }
    q = query.strip().removeprefix("gate/")
    ql = q.lower()
    matches = [
        {"code": code, "family": family, "description": desc}
        for code, (family, desc) in table.items()
        if code.lower().startswith(ql)
    ]
    matches.sort(key=lambda m: (FAMILIES.index(m["family"]), m["code"]))
    return {"query": query, "families": None, "counts": None, "matches": matches}


def explain_rc(result: dict) -> int:
    if result["query"] is None:
        return 0
    return 0 if result["matches"] else 1


def render_text(result: dict) -> str:
    lines: list[str] = []
    if result["query"] is None:
        lines.append("vrp explain - 错误码速查（用法: vrp explain <code>）")
        for family, title in (
            ("gate", "gate（重构事务闸门码）"),
            ("reject", "reject（curate 收据拒码）"),
            ("pathology", "pathology（病理分级码）"),
        ):
            codes = result["families"][family]
            lines.append(f"{title} [{result['counts'][family]}]")
            lines.append(f"  {', '.join(codes)}")
        return "\n".join(lines)
    if not result["matches"]:
        lines.append(f"vrp explain: no code matches {result['query']!r}")
        lines.append("families: gate/G0-G6 | reject SLICE_*/STRUCT_*/FILE_* | pathology P1-P9")
        lines.append("overview: vrp explain")
        return "\n".join(lines)
    for m in result["matches"]:
        lines.append(f"{m['code']} [{m['family']}]")
        lines.append(f"  {m['description']}")
    return "\n".join(lines)
