"""vrp MCP server（AI 接口层，构想v7 §1.4.1）——官方 MCP Python SDK，optional extra。

- 启动：`vrp mcp <repo>`（stdio transport，per-repo 绑定，工具不带 repo 参数）
- 依赖：`pip install vrp[mcp]`（mcp>=2.0）；核心管道零第三方依赖红线不动
- 安全闸：tx_run 不暴露 confirm 参数且恒以 confirm=False 调用 s6 层——
  T4 放行只能 CLI 人工 `vrp transact --confirm`，agent 不可绕过（构想v7 §3 决策点矩阵）
- 错误语义：nav/transact 层的 SystemExit/ValueError 统一转 {"error": ...} JSON
  （SystemExit 是 BaseException，裸抛会炸穿 server task）
- 双 server 组合：本 server 之外推荐 bytebase/dbhub（SQLite 只读）自由浏览 facts.db
"""

from __future__ import annotations

import functools
import json
import tempfile
from pathlib import Path

from . import nav, s6_transact

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError:  # optional extra guard
    MCPServer = None  # type: ignore[assignment]

_VERSION = "0.1.1"
_INSTRUCTIONS = (
    "vrp 重构循环：nav_search 定位 -> nav_module/nav_symbol 下钻核实 -> "
    "tx_preview 自查 diff -> tx_run 执行 -> tx_verdicts 读回执；"
    "失败读 failed_codes 修正后重试（每事务 <=3 次）。"
    "生产规则见 docs/契约/事务契约.md。"
)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _guard(fn):
    """nav/s6 层 SystemExit/ValueError -> {"error": ...}，不炸 server。

    functools.wraps 保住原函数签名（inspect.signature 跟 __wrapped__），
    否则 schema 提取器看到 (*args, **kwargs) 会生成 args/kwargs 两个参数。
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SystemExit, ValueError) as exc:
            return _dumps({"error": str(exc) or exc.__class__.__name__})

    return wrapped


def _write_temp(tx_json: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", text=True)
    tmp = Path(name)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(tx_json)
    return tmp


def build_server(repo: Path) -> "MCPServer":
    """构造绑定单个 repo 的 MCP server（in-process 可测）。"""
    if MCPServer is None:
        raise SystemExit("mcp package missing - install with: pip install vrp[mcp]")
    repo = Path(repo)
    server = MCPServer(name="vrp", title="vrp", version=_VERSION, instructions=_INSTRUCTIONS)

    def nav_search(query: str, limit: int = 20) -> str:
        return _dumps(nav.nav_search(repo, query, limit))

    def nav_architecture() -> str:
        return _dumps(nav.nav_architecture(repo))

    def nav_module(module_id: str, limit: int = 200) -> str:
        return _dumps(nav.nav_module(repo, module_id, limit))

    def nav_symbol(fid: str) -> str:
        return _dumps(nav.nav_symbol(repo, fid))

    def query_facts(sql: str, params: str = "", max_rows: int = 100) -> str:
        return _dumps(nav.query_facts(repo, sql, params, max_rows))

    def tx_preview(tx_json: str) -> str:
        tmp = _write_temp(tx_json)
        try:
            return _dumps(s6_transact.preview_transaction(repo, tmp))
        finally:
            tmp.unlink(missing_ok=True)

    def tx_submit(tx_json: str, overwrite: bool = False) -> str:
        tmp = _write_temp(tx_json)
        try:
            payload = s6_transact.load_transaction(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        tx_dir = repo / ".vrp" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        path = tx_dir / f"{payload['id']}.json"
        if path.exists() and not overwrite:
            raise SystemExit(
                f"tx_submit: transaction exists (overwrite=True to replace): {path.name}"
            )
        path.write_text(tx_json, encoding="utf-8")
        return _dumps(
            {"submitted": True, "path": str(path), "id": payload["id"], "type": payload["type"]}
        )

    def tx_run(tx_json: str, name: str = "") -> str:
        tmp = _write_temp(tx_json)
        try:
            payload = s6_transact.load_transaction(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        tx_dir = repo / ".vrp" / "transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        path = tx_dir / f"{payload['id']}.json"
        path.write_text(tx_json, encoding="utf-8")
        verdict = s6_transact.transact(repo, path, name or repo.name, confirm=False)
        return _dumps(verdict)

    def tx_verdicts(tid: str = "") -> str:
        verdicts = s6_transact.load_verdicts(repo)
        if tid:
            match = [v for v in verdicts if v.get("tid") == tid]
            if not match:
                raise SystemExit(f"tx_verdicts: no verdict for tid: {tid}")
            return _dumps(match)
        return _dumps({"count": len(verdicts), "verdicts": verdicts})

    server.add_tool(
        _guard(nav_search),
        name="nav_search",
        description="符号搜索（AI 入口）：name/qualified_name 匹配，精确命中优先；返回 fid 句柄供下钻",
    )
    server.add_tool(
        _guard(nav_architecture),
        name="nav_architecture",
        description="L0 全局地图：目录级聚合（文件数/符号数/病理数），下钻句柄=module_id(file_path)；"
        "structure 表（R2' curate 入库）存在时优先",
    )
    server.add_tool(
        _guard(nav_module),
        name="nav_module",
        description="L1 文件级下钻：module_id=file_path，返回符号清单+病理摘要+fid 句柄；"
        "先 nav_search / nav_architecture 拿路径",
    )
    server.add_tool(
        _guard(nav_symbol),
        name="nav_symbol",
        description="L2 符号级下钻：签名/行号/callers/callees/关联病理；"
        "caller/callee 的 fid 可继续下钻（全链可查防浅层）",
    )
    server.add_tool(
        _guard(query_facts),
        name="query_facts",
        description="轻量 SQL 直查 facts.db（只读 SELECT/WITH，单语句）；params 为 JSON 数组字符串做位置绑定；"
        "schema 见 vrp/schema/facts_schema.sql（五视图 v_hotspots/v_cycles/v_dead_code/v_dup_clusters/v_coupling）",
    )
    server.add_tool(
        _guard(tx_preview),
        name="tx_preview",
        description="事务提交前自查：per-edit unified diff + difftastic 结构信号；"
        "无闸门不改树不写 verdict；前置失败=目标缺失/已存在（G0 会拒的预检）",
    )
    server.add_tool(
        _guard(tx_submit),
        name="tx_submit",
        description="事务落盘 .vrp/transactions/<id>.json（schema 强校验，不执行）；"
        "大重构拆事务序列供 CLI 批处理（vrp transact-batch）",
    )
    server.add_tool(
        _guard(tx_run),
        name="tx_run",
        description="执行事务：T1-T3 直接跑闸门（G0-G5）出 verdict；"
        "T4 一律返回 needs_confirmation——放行只能人工 CLI `vrp transact --confirm`，agent 不可绕过；"
        "执行前建议先 tx_preview 自查 diff",
    )
    server.add_tool(
        _guard(tx_verdicts),
        name="tx_verdicts",
        description="读事务回执（repair receipt）：tid 给单条全文，不给列全部；"
        "open（非 committed）项自动回馈进 dossier evidence 节",
    )
    return server


def run_stdio(repo: Path) -> None:
    server = build_server(repo)
    server.run("stdio")


def main(argv: list[str] | None = None) -> int:
    import sys

    repo = Path(argv[0]) if argv else Path.cwd()
    run_stdio(repo)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main(sys.argv[1:]))
