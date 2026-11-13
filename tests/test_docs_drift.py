"""R-AA 批守卫：README 活文档可证伪断言对账（docs 漂移面，与代码真理源双向钉）。

census（work/census_raa.txt）裁决的落地：
- Face A 计数漂移：README 测试计数四处（快速层/深度层/全量/pre-push）曾停在
  402/1410/1812/268 而实值 1288/1523/2811/1288——守卫以 pytest --collect-only
  （实测 ~5s）为真值源对账，后续批次加测试 → collect 数变 → 守卫红 → README
  同批更新，漂移窗口=单批次，自维护。
- Face B 闸门码表：G3/G5 码拼写漂移 + G6 行缺失——守卫= s6_transact.py
  `gate/G…` 码全集与 README 闸门表全集双向相等。
- Face C 文档清单：构想v8 等现存文件未列入——守卫= docs/**/*.md 每文件被
  README 覆盖（点名引用或 构想vN 历史区间线覆盖）。
- Face D 计数型断言：MCP 工具数（中文数字）与 ast-grep 规则文件数对账。
时长类断言（~4-8min 等）机器相关不可守卫，由 census 人工对账。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8-sig")

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(text: str) -> int:
    """中文数字（1-99）转 int：二十七→27、十七→17、十→10、三→3。"""
    if "十" not in text:
        return _CN_DIGITS[text]
    tens_s, _, ones_s = text.partition("十")
    tens = _CN_DIGITS[tens_s] if tens_s else 10
    ones = _CN_DIGITS[ones_s] if ones_s else 0
    return tens * 10 + ones


def test_readme_test_counts_match_collection():
    """Face A：README 四处测试计数 == pytest collect 真值（+内部自洽）。"""
    env = {k: v for k, v in __import__("os").environ.items()}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, timeout=300, env=env,
    )
    out = proc.stdout.decode("utf-8", "replace")
    m = re.search(r"(\d+)/(\d+) tests collected \((\d+) deselected\)", out)
    assert m, f"collect-only summary line not found: {out[-300:]!r}"
    quick, total, deep = int(m.group(1)), int(m.group(2)), int(m.group(3))
    claims = {
        "快速层": re.search(r"快速层 (\d+) 例", README),
        "深度层": re.search(r"深度层 (\d+) 例", README),
        "全量": re.search(r"全量 (\d+) 例", README),
        "pre-push": re.search(r"快速层 (\d+) 例全绿才放行", README),
    }
    missing = [k for k, v in claims.items() if v is None]
    assert not missing, f"README missing test-count claims: {missing}"
    assert int(claims["快速层"].group(1)) == quick, (
        f"README 快速层 {claims['快速层'].group(1)} 例 != collect {quick}"
    )
    assert int(claims["深度层"].group(1)) == deep, (
        f"README 深度层 {claims['深度层'].group(1)} 例 != collect {deep}"
    )
    assert int(claims["全量"].group(1)) == total, (
        f"README 全量 {claims['全量'].group(1)} 例 != collect {total}"
    )
    assert int(claims["pre-push"].group(1)) == quick, "pre-push 钩子跑的就是快速层，计数必须一致"
    assert quick + deep == total, "内部自洽失败：快速层+深度层 != 全量"


def test_readme_gate_codes_match_code():
    """Face B：README 闸门表码全集 == s6_transact.py RULE_CODES 码全集（双向）。"""
    code_src = (ROOT / "vrp" / "s6_transact.py").read_text(encoding="utf-8-sig")
    code_set = set(re.findall(r"gate/G\d+/[a-z-]+", code_src))
    table_m = re.search(r"## 重构事务与闸门(.*?)\n## ", README, re.S)
    assert table_m, "README 闸门表 section not found"
    table = table_m.group(1)
    readme_set = set(re.findall(r"gate/G\d+/[a-z-]+", table))
    g4_row = next((ln for ln in table.splitlines() if ln.startswith("| G4")), "")
    bare = [t for t in re.findall(r"`([a-z][a-z-]+)`", g4_row) if "/" not in t]
    readme_set |= {f"gate/G4/{t}" for t in bare}
    assert readme_set == code_set, (
        f"gate-code drift: README-only={sorted(readme_set - code_set)} "
        f"code-only={sorted(code_set - readme_set)}"
    )


def test_readme_docs_list_complete():
    """Face C：docs/ 每个现存文件被 README 文档节覆盖（点名或 构想vN 历史区间线）。"""
    interval = re.search(r"构想 v(\d+)-v(\d+)", README)
    lo, hi = (int(interval.group(1)), int(interval.group(2))) if interval else (0, -1)
    uncovered = []
    for doc in sorted((ROOT / "docs").rglob("*.md")):
        rel = doc.relative_to(ROOT / "docs")
        if doc.stem in README or str(rel) in README or f"契约/{rel.name}" in README:
            continue
        vm = re.match(r"构想v(\d+)", doc.stem)
        if vm and lo <= int(vm.group(1)) <= hi:
            continue
        uncovered.append(str(rel))
    assert not uncovered, f"docs/ files missing from README 文档节: {uncovered}"


def test_readme_count_claims_match_reality():
    """Face D：计数型断言对账——MCP 工具数（中文数字）与 ast-grep 规则文件数。"""
    mcp_src = (ROOT / "vrp" / "mcp_server.py").read_text(encoding="utf-8-sig")
    tool_count = len(re.findall(r"server\.add_tool\(", mcp_src))
    m = re.search(r"([一二两三四五六七八九十]{1,3})件", README)
    assert m, "README MCP tool-count claim (N件) not found"
    assert _cn_num(m.group(1)) == tool_count, (
        f"README MCP {_cn_num(m.group(1))}件 != add_tool 注册 {tool_count}"
    )
    rule_count = len(list((ROOT / "vrp" / "rules" / "s2").glob("*.yml")))
    m2 = re.search(r"ast-grep (\d+) 规则", README)
    assert m2, "README ast-grep rule-count claim not found"
    assert int(m2.group(1)) == rule_count, (
        f"README ast-grep {m2.group(1)} 规则 != rules/s2 文件 {rule_count}"
    )
