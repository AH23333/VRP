"""MCP 消费面预算层（AA2，设计稿-AA1AA2-MCP消费面改造.md §4）——纯 stdlib。

被 mcp_server 消费，可独立单测。预算魔数全部由测试钉死（codegraph ITER1/2/3
预算反复横跳教训——回归测试是唯一防漂移手段）。

分层：
- L1 输出硬顶：MAX_TOOL_OUTPUT_CHARS=24000（~24K 内联上限——宿主 ~25K 以上把
  结果外置成文件再让 agent Read 回来，重引入一次读+缓存写成本，codegraph n=4
  A/B 实录）。超帽做 **JSON 感知确定性收缩**而非砍字节（砍 JSON 中段=agent 收到
  语法破碎垃圾）：最长列表对半截（保头部）→最长字符串截头（...[truncated] 尾）
  →整体信封（兜底，实践不可达）。截断永远可见（"_truncated" 备注）。
- L2 行数限额中央表：收编散在签名的默认值；VRP_TOOL_RESULT_LIMITS env JSON 覆盖
  （CGC tool_limits.py 律：env 条目→default 实参→内置表；非法值静默回落）。
- L5 输入上界：JSON 大载荷（sql/payload_json/tx_json/options_json）≤1 MiB；
  自由文本（query/target）≤10K（codegraph MAX_INPUT_LENGTH 律）。
"""

from __future__ import annotations

import copy
import json
import os

from . import gcf

MAX_TOOL_OUTPUT_CHARS = 24000  # ~24K 内联硬顶（设计稿 D5；测试钉死）
MAX_JSON_INPUT_CHARS = 1 << 20  # 1 MiB：JSON 大载荷上界（设计稿 L5）
MAX_QUERY_INPUT_CHARS = 10_000  # 自由文本上界（设计稿 L5）
VALUE_HEAD_CHARS = 2000  # 巨型标量收缩后的头部长度
TRUNCATED_MARKER = "...[truncated]"

# L2 行数限额中央表：收编自散在签名的默认值（设计稿 §4 L2）；测试钉死。
TOOL_RESULT_LIMITS: dict[str, int] = {
    "nav_search": 20,
    "nav_module": 200,
    "nav_callers": 50,
    "nav_callees": 50,
    "query_facts": 100,  # max_rows
}


def dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def get_tool_result_limit(name: str, default: int | None = None) -> int | None:
    """解析顺序（CGC tool_limits.py 律）：env 覆盖 → default 实参 → 内置表。

    env 非法/缺项静默回落（现行为保持）；env 值 max(1, int)。
    """
    raw = os.environ.get("VRP_TOOL_RESULT_LIMITS", "")
    if raw:
        try:
            overrides = json.loads(raw)
        except ValueError:
            overrides = {}
        if isinstance(overrides, dict) and name in overrides:
            try:
                return max(1, int(overrides[name]))
            except (TypeError, ValueError):
                pass
    if default is not None:
        return default
    return TOOL_RESULT_LIMITS.get(name)


def check_input(name: str, value, limit: int) -> None:
    """L5 输入上界：超界抛 ValueError（工具层统一转成功形状 {"error"}）。"""
    if value is None:
        return
    if len(value) > limit:
        raise ValueError(f"{name} is {len(value)} chars, max {limit}")


def _walk_lists(node, path=()):
    if isinstance(node, list):
        yield len(node), path
        for i, item in enumerate(node):
            yield from _walk_lists(item, path + (i,))
    elif isinstance(node, dict):
        for key, val in node.items():
            yield from _walk_lists(val, path + (key,))


def _walk_strings(node, path=()):
    if isinstance(node, str):
        yield len(node), path
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_strings(item, path + (i,))
    elif isinstance(node, dict):
        for key, val in node.items():
            yield from _walk_strings(val, path + (key,))


def _get_at(node, path):
    for part in path:
        node = node[part]
    return node


def _set_at(node, path, value):
    parent = _get_at(node, path[:-1])
    parent[path[-1]] = value


def _fmt_path(path) -> str:
    return ".".join(str(p) for p in path)


def emit(payload, cap: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """序列化+输出预算（设计稿 D5）。

    收缩序确定性：先序遍历取最长者（并列取先见者）；同输入同输出。
    AA3：VRP_OUTPUT_FORMAT=gcf → GCF 紧凑编码（键声明一次+值管道分隔）；
    超帽回退 JSON 收缩链（GCF 面不收缩——收缩算法锚定 JSON 结构，诚实边界）。
    """
    if os.environ.get("VRP_OUTPUT_FORMAT", "").strip().lower() == "gcf":
        gcf_text = gcf.dumps(payload)
        if len(gcf_text) <= cap:
            return gcf_text
    text = dumps(payload)
    if len(text) <= cap:
        return text
    shrunk = copy.deepcopy(payload)
    notes: list[str] = []
    while True:
        text = dumps(shrunk)
        if len(text) <= cap:
            break
        best_len, best_path = -1, None
        for length, path in _walk_lists(shrunk):
            if length >= 2 and length > best_len:
                best_len, best_path = length, path
        if best_path is not None:
            keep = max(1, best_len // 2)
            if best_path:
                _set_at(shrunk, best_path, _get_at(shrunk, best_path)[:keep])
            else:  # 根级列表：路径为空，_set_at 无父可落——重绑局部
                shrunk = shrunk[:keep]
            notes.append(f"{_fmt_path(best_path)}: {best_len}->{keep} items")
            continue
        str_len, str_path = -1, None
        for length, path in _walk_strings(shrunk):
            if length > VALUE_HEAD_CHARS and length > str_len:
                str_len, str_path = length, path
        if str_path is not None:
            # 截后总长必须恰为 VALUE_HEAD_CHARS（头长扣除 marker）：若头长仍取满
            # VALUE_HEAD_CHARS，截后串（2000+14）> VALUE_HEAD_CHARS 仍是候选，
            # 再截产出同一字符串 → 死循环（回归测试钉死）。
            head = _get_at(shrunk, str_path)[:VALUE_HEAD_CHARS - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER
            if str_path:
                _set_at(shrunk, str_path, head)
            else:  # 根级字符串：同列表理，重绑局部
                shrunk = head
            notes.append(f"{_fmt_path(str_path)}: {str_len}->{VALUE_HEAD_CHARS} chars (marker appended)")
            continue
        break  # 列表全 ≤1 且字符串全短仍超帽 → 信封兜底
    if notes:
        if isinstance(shrunk, dict):
            shrunk["_truncated"] = notes
        else:
            shrunk = {"_truncated": notes, "value": shrunk}
    text = dumps(shrunk)
    if len(text) <= cap:
        return text
    return dumps({
        "truncated": True,
        "original_chars": len(dumps(payload)),
        "value_head": dumps(payload)[:VALUE_HEAD_CHARS],
        "hint": "output exceeded the hard cap after deterministic shrink - "
                "narrow the query or lower limit",
    })
