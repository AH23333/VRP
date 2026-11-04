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
import sqlite3
import tempfile
from pathlib import Path

from . import nav, s6_transact
from . import curate as curate_mod
from . import etl as etl_mod
from . import render as render_mod
from . import s7_enforce
from . import session as session_mod

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError:  # optional extra guard
    MCPServer = None  # type: ignore[assignment]

_VERSION = "0.1.1"
_INSTRUCTIONS = (
    "vrp 重构循环：nav_search 定位 -> nav_module/nav_symbol 下钻核实 -> "
    "tx_preview 自查 diff -> tx_run 执行 -> tx_verdicts 读回执；"
    "失败读 failed_codes 修正后重试（每事务 <=3 次）。"
    "梳理环节：curate_submit 提交 slices/structure（确定性验证，拒绝读收据修正重提），"
    "nav_slice 按业务线下钻；"
    "执法环节：curate_structure 入库后 enforce 跑架构执法（P9 findings，幂等）。"
    "汇报环节：render_verdicts/render_slice 出决策点快材料，render_report 出全量静态报告；"
    "全信任模式（构想v7 §3.3）：session_begin 开场 -> 多轮循环 -> session_end -> "
    "session_report 交付初末对比（计数 diff + 双 L0 图并排 + verdict 时间线）。"
    "生产规则见 docs/契约/事务契约.md 与 docs/契约/梳理工件契约.md。"
)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _guard(fn):
    """nav/s6/curate 层 SystemExit/ValueError/RuntimeError -> {"error": ...}，不炸 server。

    functools.wraps 保住原函数签名（inspect.signature 跟 __wrapped__），
    否则 schema 提取器看到 (*args, **kwargs) 会生成 args/kwargs 两个参数。
    RuntimeError 含 M4 freshness 硬拒（etl.check_facts_freshness）。
    """

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SystemExit, ValueError, RuntimeError) as exc:
            return _dumps({"error": str(exc) or exc.__class__.__name__})

    return wrapped


def _write_temp(tx_json: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".json", text=True)
    tmp = Path(name)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(tx_json)
    return tmp


def _open_facts_rw(repo: Path) -> sqlite3.Connection:
    """curate 族写 facts.db：ensure_schema 自动迁移新表 + M4 freshness 硬拒。"""
    facts = repo / ".vrp" / "facts.db"
    if not facts.exists():
        raise SystemExit(f"facts.db missing - run 'vrp run' first: {facts}")
    con = sqlite3.connect(facts)
    etl_mod.ensure_schema(con)
    etl_mod.check_facts_freshness(con, repo)
    return con


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

    def nav_callers(fid: str, limit: int = 50) -> str:
        return _dumps(nav.nav_callers(repo, fid, limit))

    def nav_callees(fid: str, limit: int = 50) -> str:
        return _dumps(nav.nav_callees(repo, fid, limit))

    def nav_slice(slice_id: str) -> str:
        return _dumps(nav.nav_slice(repo, slice_id))

    def nav_path(from_fid: str, to_fid: str, max_hops: int = 5) -> str:
        return _dumps(nav.nav_path(repo, from_fid, to_fid, max_hops))

    def curate_submit(kind: str, payload_json: str) -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(curate_mod.curate_payload(con, kind, payload_json))
        finally:
            con.close()

    def curate_slices() -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps({"slices": curate_mod.list_slices(con)})
        finally:
            con.close()

    def curate_structure() -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(curate_mod.get_structure(con) or {"structure": None})
        finally:
            con.close()

    def curate_reject(kind: str, target_id: str, note: str = "") -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(curate_mod.reject(con, kind, target_id, note))
        finally:
            con.close()

    def curate_receipt() -> str:
        return _dumps(curate_mod.read_receipt(repo) or {"receipt": None})

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

    def render_verdicts() -> str:
        return _dumps(render_mod.render_verdicts(repo))

    def render_slice(slice_id: str) -> str:
        return _dumps(render_mod.render_slice(repo, slice_id))

    def render_report() -> str:
        return _dumps(render_mod.render_report(repo))

    def session_begin(note: str = "") -> str:
        return _dumps(session_mod.session_begin(repo, note))

    def session_end(note: str = "") -> str:
        return _dumps(session_mod.session_end(repo, note))

    def session_report(session_id: str = "") -> str:
        return _dumps(session_mod.session_report(repo, session_id))

    def enforce(timeout: int = 600) -> str:
        con = _open_facts_rw(repo)
        try:
            return _dumps(s7_enforce.run(repo, con, timeout))
        finally:
            con.close()

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
        _guard(nav_callers),
        name="nav_callers",
        description="完整调用者列表（分页 limit，nav_symbol 内置 cap 20 的补充）；"
        "truncated=true 时调大 limit；fid 句柄供 nav_symbol 下钻",
    )
    server.add_tool(
        _guard(nav_callees),
        name="nav_callees",
        description="完整被调用者列表（分页 limit，nav_symbol 内置 cap 20 的补充）；"
        "truncated=true 时调大 limit；fid 句柄供 nav_symbol 下钻",
    )
    server.add_tool(
        _guard(nav_slice),
        name="nav_slice",
        description="业务线视角：steps（附符号摘要+fid 句柄）+ acts（分幕）；"
        "slices 由 curate_submit 验收入库；status=stale 表示符号/边已漂移",
    )
    server.add_tool(
        _guard(nav_path),
        name="nav_path",
        description="耦合路径查找：from→to 沿 edges（src→dst）最短路径（WITH RECURSIVE BFS）；"
        "hops_detail 透传每跳 type/evidence/confidence——低置信边需下钻核实；"
        "不可达时 found=false（可调大 max_hops 上限 10 或反向查询）",
    )
    server.add_tool(
        _guard(curate_submit),
        name="curate_submit",
        description="提交 AI 梳理工件验收入库：kind=slices（JSON 数组）|structure（JSON 对象），"
        "payload_json 为工件原文；确定性验证全过才入库，拒绝出收据（errors 逐条含稳定码）；"
        "schema 见 docs/契约/梳理工件契约.md",
    )
    server.add_tool(
        _guard(curate_slices),
        name="curate_slices",
        description="列已入库业务线（slice_id/name/status/steps 计数）；"
        "status=active 可用，stale=符号漂移待复梳理，rejected=用户否决",
    )
    server.add_tool(
        _guard(curate_structure),
        name="curate_structure",
        description="读已入库整体结构（modules/layers/rules 原文+status）；"
        "structure 是 S7 架构执法（远期）与 L0 地图的原料",
    )
    server.add_tool(
        _guard(curate_reject),
        name="curate_reject",
        description="用户否决已入库工件（kind=slice|structure，target_id=slice_id 或 'structure'）："
        "status→rejected + decisions 留痕；否决是保守方向，agent 可代转达用户决定",
    )
    server.add_tool(
        _guard(curate_receipt),
        name="curate_receipt",
        description="读最近一次 curate 收据（.vrp/curate-receipt.json）："
        "被拒时按 errors 码逐条修正工件后重提",
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
    server.add_tool(
        _guard(render_verdicts),
        name="render_verdicts",
        description="T4 决策点快材料：verdict 时间线 + open/T4 清单；"
        "T4 放行只能人工 CLI vrp transact --confirm",
    )
    server.add_tool(
        _guard(render_slice),
        name="render_slice",
        description="单业务线快材料：Mermaid 时序源（steps/acts），可贴 mermaid.live 渲染；"
        "slice_id 从 curate_slices 拿",
    )
    server.add_tool(
        _guard(render_report),
        name="render_report",
        description="S8 全量静态报告 -> .vrp/report/（report.html + IR + SARIF + Mermaid；"
        "archify 缺失自动降级 Mermaid-only）；报告无时间戳，带 facts.git_head 可溯源",
    )
    server.add_tool(
        _guard(session_begin),
        name="session_begin",
        description="全信任模式开场（构想v7 §3.3）：facts.db 快照 + 会话元数据；"
        "同一 repo 同时只允许一个活动会话；之后多轮重构零打扰直至 session_end",
    )
    server.add_tool(
        _guard(session_end),
        name="session_end",
        description="全信任模式收场：末态快照 + ended_at + 初末计数 diff"
        "（节点/边/病理/xcheck 四组）；之后 session_report 出交付",
    )
    server.add_tool(
        _guard(session_report),
        name="session_report",
        description="全信任模式交付：初末计数 diff + 双 L0 架构图并排（archify，缺失降级 Mermaid）"
        "+ 会话窗口内 verdict 时间线 -> 会话目录 report.html；session_id 缺省取最近已结束会话",
    )
    server.add_tool(
        _guard(enforce),
        name="enforce",
        description="S7 架构执法（构想v7 §4）：curate 入库的 structure 经 import-linter 执行，"
        "Broken contracts = P9 findings（source='s7-enforce'，幂等 replace-all）；"
        "无 structure/非 active 诚实 skipped；违例经 dossier 进重构目标——AI 理解，工具执法",
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
