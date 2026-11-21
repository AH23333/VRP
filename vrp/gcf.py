"""GCF 压缩编码（AA3，构想v13 §AA3）——CGC gcf_encoder 语义：键声明一次+值管道分隔。

opt-in：VRP_OUTPUT_FORMAT=gcf（CGC_OUTPUT_FORMAT=gcf 同律）；**无解码器**——
前沿模型直读（CGC 实测 100% 可读），回退=不设 env 即 JSON（默认面零动）。

编码形状（确定性，同输入同输出）：
- dict：首行=键管道串，次行=值管道串；
- 全等键 list[dict]：首行=键声明一次，随后每行一条记录（token 主要收益面）；
- 其余（标量/混合 list/空容器）诚实回退紧凑 JSON——GCF 只编码统一形状，
  混合形状强编码=模型侧断列歧义，宁缺勿错。

值内联：嵌套 list/dict → 单行紧凑 JSON；值内 "\\"→"\\\\"、换行→"\\n"、
"|"→"\\|" 防断列（模型侧可读转义，无解码器纪律内的诚实处理）。

超帽回退（mcp_budget.emit 消费侧）：GCF 面不收缩——JSON 感知收缩算法锚定
JSON 结构（最长列表对半截），GCF 扁平结构无此锚点语义；超帽 payload 回退
JSON 收缩链（截断永远可见律保持）。
"""

from __future__ import annotations

import json

SEP = "|"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("|", "\\|")


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _escape(value)
    return _escape(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _uniform_rows(payload: list) -> list[dict] | None:
    """全等键 list[dict] 判定（空键 dict 不算——空键无列可声明）。"""
    if not payload:
        return None
    if not all(isinstance(r, dict) for r in payload):
        return None
    first_keys = list(payload[0].keys())
    if not first_keys:
        return None
    for r in payload[1:]:
        if list(r.keys()) != first_keys:
            return None
    return payload


def dumps(payload) -> str:
    """GCF 编码主入口；非统一形状回退紧凑 JSON（json.loads 可还原）。"""
    if isinstance(payload, dict):
        if not payload:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        keys = list(payload.keys())
        return (
            SEP.join(_escape(str(k)) for k in keys)
            + "\n"
            + SEP.join(_scalar(payload[k]) for k in keys)
        )
    if isinstance(payload, list):
        rows = _uniform_rows(payload)
        if rows is not None:
            keys = list(rows[0].keys())
            lines = [SEP.join(_escape(str(k)) for k in keys)]
            for row in rows:
                lines.append(SEP.join(_scalar(row[k]) for k in keys))
            return "\n".join(lines)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
