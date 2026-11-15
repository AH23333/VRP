# -*- coding: utf-8 -*-
"""render_fusion —— V-2 融合报告生成器（构想v9 定稿，并入 render 链）。

六风格融合（P1 叙事/P2 对比/P4 下钻/P5 力导向/P6 仪表盘/P11 切片）单
HTML 静态产物，report 根 fusion.html；细节层联动 chain/（同批产出——
file_pages/file_caps 内存直传，零页面扫描）。

三红线：静态产物/无服务端/无前端构建链（JS 允许——红线是"无构建链"）。
复现性（构想v9 §6）：同 facts.db → 字节级同 fusion.html——sort_keys JSON、
无时间戳、无随机数（FR 布局确定性迭代）、ECharts vendored 钉版本
（vrp/assets/echarts.min.js 5.6.0）。

零第三方依赖：布局为纯 stdlib FR（软墙力——V-9.2 教训：逐轮硬 clamp 致
节点贴壁矩形分布；墙用回复力不用截断）。

数据预算（§4）：切片全量嵌入至上限 ROWS_EMBED_CAP（超大仓降级路径，
按 id 序截断无病理偏置——V-9.1 cap 教训，行内诚实标注）。
"""
from __future__ import annotations

import html as H
import json
import math
import re
import sqlite3
from pathlib import Path

from . import dbpath
from . import render_theme

# 目录图节点：size=符号数；大仓阈值 100，小仓按总量 0.5% 自适应（防全灭）
GRAPH_NODE_CAP = 60
GRAPH_LINK_CAP = 400
LAYOUT_W, LAYOUT_H = 1060.0, 660.0
FR_ITERS = 300
PUSH_ROUNDS = 240
ROW_ID_CAP = 20000  # 切片嵌入上限（超大仓降级；按 id 序截断无病理偏置）

# 影响面（交接 #142，学 TSA nav action=impact）：调用图 BFS 只走调用语义边
_IMPACT_EDGE_TYPES = ("CALLS", "CALL_REFERENCE")
IMPACT_BFS_CAP = 4000  # 传递可达 BFS 帽（防枢纽符号全图扩散；截断=下界诚实注记）
IMPACT_TOP_N = 3  # top 调用方/被调名单长度（平行边数降序+名字升序确定性）

_ECHARTS = Path(__file__).resolve().parent / "assets" / "echarts.min.js"

DIR_EXPR = """
CASE WHEN file_path NOT LIKE '%/%' THEN '(root)'
     ELSE substr(file_path,1,instr(file_path||'/','/')-1) ||
          CASE WHEN (length(file_path)-length(replace(file_path,'/',''))) >= 2
               THEN '/' || substr(substr(file_path, instr(file_path,'/')+1), 1,
                      CASE WHEN instr(substr(file_path, instr(file_path,'/')+1)||'/','/') > 0
                           THEN instr(substr(file_path, instr(file_path,'/')+1)||'/','/')-1
                           ELSE length(substr(file_path, instr(file_path,'/')+1)) END)
               ELSE '' END
END"""


def _fmt(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------- 布局（纯 stdlib）


def _layout(nodes: list[dict], links: list[dict]) -> None:
    """确定性 FR（软墙力）+ 尺寸感知推开后处理 + 视口适配。

    V-9.2 教训内置：迭代期不硬 clamp（软墙回复力），推开后统一缩放适配
    ——分布有机无贴壁矩形。零随机数、零第三方依赖。
    """
    n = len(nodes)
    if n == 0:
        return
    k = math.sqrt(LAYOUT_W * LAYOUT_H / n)
    for i, nd in enumerate(nodes):
        ang = 2 * math.pi * i / n
        nd["x"] = LAYOUT_W / 2 + (LAYOUT_W / 3.2) * math.cos(ang)
        nd["y"] = LAYOUT_H / 2 + (LAYOUT_H / 3.2) * math.sin(ang)
    by_name = {nd["name"]: nd for nd in nodes}
    m_x, m_y = 70.0, 50.0
    for _it in range(FR_ITERS):
        temp = (LAYOUT_W / 9.0) * (1 - _it / FR_ITERS) + 0.5
        for nd in nodes:
            nd["dx"] = 0.0
            nd["dy"] = 0.0
        for i in range(n):
            a = nodes[i]
            for j in range(i + 1, n):
                b = nodes[j]
                dx = b["x"] - a["x"]
                dy = b["y"] - a["y"]
                d = math.sqrt(dx * dx + dy * dy)
                if d < 0.01:
                    dx, dy, d = 0.1, 0.07, 0.122
                boost = 1 + (a["sz"] + b["sz"]) / 40
                f = (k * k / d) * boost
                ux, uy = dx / d, dy / d
                a["dx"] -= ux * f
                a["dy"] -= uy * f
                b["dx"] += ux * f
                b["dy"] += uy * f
        for lk in links:
            a, b = by_name[lk["s"]], by_name[lk["t"]]
            dx = b["x"] - a["x"]
            dy = b["y"] - a["y"]
            d = math.sqrt(dx * dx + dy * dy) or 0.1
            wfac = min(1.5, 0.5 + math.sqrt(lk["w"]) / 15)
            f = (d * d / k) * wfac
            ux, uy = dx / d, dy / d
            a["dx"] += ux * f
            a["dy"] += uy * f
            b["dx"] -= ux * f
            b["dy"] -= uy * f
        # 软墙（V-9.2 教训：硬 clamp → 贴壁矩形分布；回复力保有机形）
        for nd in nodes:
            if nd["x"] < m_x:
                nd["dx"] += (m_x - nd["x"]) * 0.6
            if nd["x"] > LAYOUT_W - m_x:
                nd["dx"] -= (nd["x"] - (LAYOUT_W - m_x)) * 0.6
            if nd["y"] < m_y:
                nd["dy"] += (m_y - nd["y"]) * 0.6
            if nd["y"] > LAYOUT_H - m_y:
                nd["dy"] -= (nd["y"] - (LAYOUT_H - m_y)) * 0.6
        for nd in nodes:
            disp = math.sqrt(nd["dx"] ** 2 + nd["dy"] ** 2)
            if disp > temp:
                nd["dx"] *= temp / disp
                nd["dy"] *= temp / disp
            nd["x"] += nd["dx"]
            nd["y"] += nd["dy"]
    # 统一缩放适配（保形：非 clamp），再推开后处理
    xs = [nd["x"] for nd in nodes]
    ys = [nd["y"] for nd in nodes]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    if span_x > 1e-6 or span_y > 1e-6:
        box_w = LAYOUT_W - 110
        box_h = LAYOUT_H - 90
        s = min(box_w / max(span_x, 1e-6), box_h / max(span_y, 1e-6), 1.6)
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        for nd in nodes:
            nd["x"] = LAYOUT_W / 2 + (nd["x"] - cx) * s
            nd["y"] = LAYOUT_H / 2 + (nd["y"] - cy) * s
    for _ in range(PUSH_ROUNDS):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                a, b = nodes[i], nodes[j]
                min_d = (a["sz"] + b["sz"]) / 2 + 9
                dx = b["x"] - a["x"]
                dy = b["y"] - a["y"]
                d = math.sqrt(dx * dx + dy * dy)
                if d < 0.01:
                    dx, dy, d = 0.1, 0.07, 0.122
                if d < min_d:
                    push = (min_d - d) / 2
                    ux, uy = dx / d, dy / d
                    a["x"] -= ux * push
                    a["y"] -= uy * push
                    b["x"] += ux * push
                    b["y"] += uy * push
                    moved = True
        for nd in nodes:
            nd["x"] = min(LAYOUT_W - 44, max(44.0, nd["x"]))
            nd["y"] = min(LAYOUT_H - 34, max(30.0, nd["y"]))
        if not moved:
            break


# ---------------------------------------------------------------- 数据装配


def _load_impacts(con: sqlite3.Connection, anchor_fids: list[str]) -> dict:
    """影响面（交接 #142，学 TSA nav action=impact）：锚定符号调用图 BFS。

    每锚算：直达（dc 调用方数/dx 被调数）+传递可达（uc 上游/xc 下游
    BFS，帽 IMPACT_BFS_CAP 截断 cap=true——计数为下界）+top-N 名单
    （平行边数降序+名字升序，确定性）。只走调用语义边
    （_IMPACT_EDGE_TYPES），DEFINES/WRITES 等结构边不算调用流。
    无调用边的锚也进表（全零——dead-code 类 findings 的佐证信号）。
    返回按锚 fid 排序的列表 [{dc, dx, uc, xc, cap, up: [...], down: [...]}]
    （调用方以 sorted fids 序配索引——行 a 字段引用下标）。
    """
    if not anchor_fids:
        return []
    # top 名单用 name（真实符号名——label 是种类 'Function'/'Method'，
    # sym 列语义才是 label；此处给读者看必须用 name）
    edge_rows = con.execute(
        f"SELECT e.src_fid, e.dst_fid, s.name, d.name FROM edges e "
        f"LEFT JOIN symbols s ON s.fid = e.src_fid "
        f"LEFT JOIN symbols d ON d.fid = e.dst_fid "
        f"WHERE e.type IN ({','.join('?' * len(_IMPACT_EDGE_TYPES))})",
        _IMPACT_EDGE_TYPES).fetchall()
    out_adj: dict[str, list[str]] = {}
    in_adj: dict[str, list[str]] = {}
    out_lab: dict[str, list[str]] = {}
    in_lab: dict[str, list[str]] = {}
    for src, dst, sl, dl in edge_rows:
        out_adj.setdefault(src, []).append(dst)
        in_adj.setdefault(dst, []).append(src)
        out_lab.setdefault(src, []).append(dl or "")
        in_lab.setdefault(dst, []).append(sl or "")

    def _bfs(adj: dict[str, list[str]], start: str) -> tuple[set[str], bool]:
        seen = {start}
        frontier = [start]
        while frontier:
            nxt: list[str] = []
            for f in frontier:
                for t in adj.get(f, ()):
                    if t not in seen:
                        seen.add(t)
                        if len(seen) >= IMPACT_BFS_CAP:
                            return seen, True
                        nxt.append(t)
            frontier = nxt
        return seen, False

    def _top(labels: list[str]) -> list[str]:
        cnt: dict[str, int] = {}
        for lb in labels:
            if lb:
                cnt[lb] = cnt.get(lb, 0) + 1
        return [lb for lb, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:IMPACT_TOP_N]]

    impacts: list = []
    for fid in sorted(set(anchor_fids)):
        up_seen, up_cap = _bfs(in_adj, fid)
        dn_seen, dn_cap = _bfs(out_adj, fid)
        impacts.append({
            "dc": len(set(in_adj.get(fid, ()))), "dx": len(set(out_adj.get(fid, ()))),
            "uc": len(up_seen) - 1, "xc": len(dn_seen) - 1,
            "cap": up_cap or dn_cap,
            "up": _top(in_lab.get(fid, [])), "down": _top(out_lab.get(fid, [])),
        })
    return impacts


def _load_data(con: sqlite3.Connection, chain: dict | None) -> dict:
    q = lambda s, p=(): con.execute(s, p).fetchall()  # noqa: E731
    meta = {k: v for k, v in q("SELECT key, value FROM meta")}
    gi = lambda k, d="": meta.get(k, d)  # noqa: E731

    syms = int(gi("count_symbols", q("SELECT COUNT(*) FROM symbols")[0][0]))
    edges = int(gi("count_edges", q("SELECT COUNT(*) FROM edges")[0][0]))
    fin_total = q("SELECT COUNT(*) FROM findings")[0][0]
    gaps = int(gi("count_coverage_gaps", q("SELECT COUNT(*) FROM coverage_gaps")[0][0]))
    files_n = q("SELECT COUNT(*) FROM file_hashes")[0][0]

    path_counts = dict(q("SELECT pathology, COUNT(*) FROM findings GROUP BY pathology"))
    kinds = q("SELECT kind, COUNT(*) c FROM findings GROUP BY kind ORDER BY c DESC LIMIT 10")
    edge_types = q("SELECT type, COUNT(*) c FROM edges GROUP BY type ORDER BY c DESC LIMIT 12")
    conf_hist = q("SELECT round(confidence,1), COUNT(*) c FROM edges WHERE type='CALLS' GROUP BY 1 ORDER BY 1")
    calls_avg = q("SELECT AVG(confidence) FROM edges WHERE type='CALLS'")[0][0]
    slices = q("SELECT slice_id, name FROM slices ORDER BY slice_id") if _has(con, "slices") else []

    # 双引擎对账（xcheck 未跑的仓 cmp=None，对比节诚实隐藏）
    cmp_data = None
    if gi("xcheck_agreement_pct"):
        cmp_data = {
            "cbm": int(gi("xcheck_cbm_calls", "0")), "ca": int(gi("xcheck_ca_calls", "0")),
            "agr": int(gi("xcheck_agreement", "0")), "agrpct": gi("xcheck_agreement_pct"),
            "caonly": int(gi("xcheck_ca_only", "0")),
            "cabuiltin": int(gi("xcheck_ca_only_builtin_target", "0")),
            "cbmonly": int(gi("xcheck_cbm_only", "0")),
            "cbmtest": int(gi("xcheck_cbm_only_test_source", "0")),
            "ledger": int(gi("xcheck_ledger_rows", "0")),
        }

    # 目录图节点（大仓阈值 100 / 小仓 0.5% 自适应）
    min_syms = 100 if syms >= 20000 else max(1, syms // 200)
    node_rows = q(f"SELECT {DIR_EXPR} d, COUNT(*) c FROM symbols GROUP BY d "
                  f"HAVING c >= {min_syms} ORDER BY c DESC LIMIT {GRAPH_NODE_CAP}",
                  )
    node_names = [r[0] for r in node_rows]
    node_set = set(node_names)
    node_syms = {r[0]: r[1] for r in node_rows}

    link_rows = q(f"WITH sd AS (SELECT fid, {DIR_EXPR} d FROM symbols) "
                  "SELECT a.d, b.d, COUNT(*) c FROM edges e "
                  "JOIN sd a ON e.src_fid = a.fid JOIN sd b ON e.dst_fid = b.fid "
                  "WHERE e.type = 'CALLS' AND a.d <> b.d GROUP BY 1, 2 ORDER BY c DESC")
    links = []
    for s, t, c in link_rows:
        if s in node_set and t in node_set and len(links) < GRAPH_LINK_CAP:
            links.append({"s": s, "t": t, "w": c,
                          "lw": round(min(6.0, max(0.8, 0.6 * math.sqrt(c))), 2)})

    fin_by_dir: dict[str, dict[str, int]] = {}
    for d, p, c in q(f"SELECT {DIR_EXPR}, pathology, COUNT(*) FROM findings GROUP BY 1, 2"):
        if d in node_set:
            fin_by_dir.setdefault(d, {})[p] = c
    files_by_dir: dict[str, list] = {}
    for d, f, c in q(f"SELECT {DIR_EXPR}, file_path, COUNT(*) c FROM symbols "
                     "GROUP BY 1, 2 ORDER BY c DESC"):
        if d in node_set:
            fl = files_by_dir.setdefault(d, [])
            if len(fl) < 6:
                fl.append([f, c])

    nodes: list[dict] = []
    densities = sorted(sum(fin_by_dir.get(nm, {}).values()) / node_syms[nm]
                       for nm in node_names) or [0.0]
    q1 = densities[len(densities) // 4]
    q2 = densities[len(densities) // 2]
    q3 = densities[3 * len(densities) // 4]
    panels = []
    for i, nm in enumerate(node_names):
        bp = fin_by_dir.get(nm, {})
        fin_n = sum(bp.values())
        dens = fin_n / node_syms[nm]
        cat = 0 if dens <= q1 else 1 if dens <= q2 else 2 if dens <= q3 else 3
        out_l = sorted([(l["t"], l["w"]) for l in links if l["s"] == nm], key=lambda x: -x[1])[:3]
        in_l = sorted([(l["s"], l["w"]) for l in links if l["t"] == nm], key=lambda x: -x[1])[:3]
        sz = round(min(66, 14 + 12 * math.log10(max(node_syms[nm], 1))), 1)
        nodes.append({"name": nm, "value": node_syms[nm], "symbolSize": sz, "sz": sz,
                      "category": cat, "idx": i})
        panels.append({"name": nm, "syms": node_syms[nm], "fin": fin_n,
                       "dens": round(dens, 4), "bypath": bp,
                       "files": files_by_dir.get(nm, []), "in": in_l, "out": out_l})
    _layout(nodes, links)

    # 切片行（cap 降级：按 id 序无病理偏置——V-9.1 cap 教训）。
    # #139 覆盖面：行位置 COALESCE 锚点符号兜底——旧库 similar_to 漏填
    # （etl 已修）仍可定位到锚点文件/行
    rows_raw = q("SELECT f.id, f.pathology, f.kind, f.source,"
                 " COALESCE(f.file_path, s.file_path), COALESCE(f.start_line, s.start_line), s.label, f.anchor_fid "
                 "FROM findings f LEFT JOIN symbols s ON f.anchor_fid = s.fid "
                 "ORDER BY f.id LIMIT ?", (ROW_ID_CAP,))
    truncated = fin_total > len(rows_raw)
    file_pages = (chain or {}).get("file_pages") or {}
    file_caps = (chain or {}).get("file_caps") or {}
    # 影响面（交接 #142）：按锚 fid 去重计算（tsa 实测 15741 行 → 7737 锚），
    # D.impacts 为列表（锚序=sorted fids 确定性），行只带 int 索引 a
    # （64 字符 fid 逐行重复会膨胀 ~1.5MB——列表+索引压至 ~1MB）；
    # 无锚行 a=-1（面板诚实注记不可算）
    anchors = sorted({r[7] for r in rows_raw if r[7]})
    impacts = _load_impacts(con, anchors)
    anchor_idx = {fid: i for i, fid in enumerate(anchors)}
    rows = []
    for r in rows_raw:
        fp = r[4] or ""
        ln = r[5] or 0
        page = file_pages.get(fp)
        href = ""
        if page:
            href = f"chain/{page}"
            cap = file_caps.get(fp)
            if ln and (cap is None or 1 <= ln <= cap):
                href += f"#L{ln}"
        rows.append({"i": r[0], "p": r[1], "k": r[2], "s": r[3], "f": fp,
                     "ln": ln, "sym": r[6] or "", "h": href,
                     "a": anchor_idx.get(r[7], -1)})
    pages = {}
    for r in rows:
        if r["f"] and r["f"] not in pages:
            pg = file_pages.get(r["f"])
            pages[r["f"]] = f"chain/{pg}" if pg else ""
    for pn in panels:
        for f, _c in pn["files"]:
            if f not in pages:
                pg = file_pages.get(f)
                pages[f] = f"chain/{pg}" if pg else ""

    return {
        "meta": {"vrp": gi("vrp_version", ""), "head": gi("git_head", ""),
                 "etl": gi("etl_at", "")},
        "cards": [
            ["符号", _fmt(syms), "facts.db 真值"],
            ["边", _fmt(edges), "16 型关系"],
            ["findings", _fmt(fin_total), "行级锚定"],
        ] + ([["对账一致率", cmp_data["agrpct"] + "%", "xcheck 双引擎"]] if cmp_data else []) + [
            ["coverage gaps", str(gaps), "未覆盖面"],
            ["业务线", str(len(slices)), "AI 梳理锚定"],
        ],
        "path": [[p, path_counts.get(p, 0)] for p in ("P1", "P2", "P3", "P4", "P9")],
        "kinds": [r[0] for r in q("SELECT kind FROM findings GROUP BY kind ORDER BY kind")],
        "kindTop": [[r[0], r[1]] for r in kinds],
        "kindMap": {r[0]: r[1] for r in q("SELECT kind, COUNT(*) c FROM findings GROUP BY kind")},
        "edgeTypes": [[r[0], r[1]] for r in edge_types],
        "conf": [[str(r[0]) if r[0] is not None else "null", r[1]] for r in conf_hist],
        "graph": {"nodes": nodes,
                  "links": [{"source": l["s"], "target": l["t"],
                             "lineStyle": {"width": l["lw"]}} for l in links]},
        "panels": panels,
        "rows": rows,
        "impacts": impacts,
        "rowsTruncated": truncated,
        "pages": pages,
        "cmp": cmp_data,
        "finTotal": fin_total,
        "slices": [[r[0], r[1]] for r in slices],
        "_stats": {
            "syms": syms, "edges": edges, "files": files_n, "gaps": gaps,
            "calls_avg": calls_avg, "path_counts": path_counts,
            "tests_syms": q("SELECT COUNT(*) FROM symbols WHERE file_path LIKE 'tests/%'")[0][0],
        },
    }


def _has(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


# ---------------------------------------------------------------- 叙事（P1 范式）


def _narr(D: dict, repo_name: str) -> str:
    st = D["_stats"]
    esc = H.escape
    syms = st["syms"]

    def chip(text, k=None, p=None):
        da = f' data-k="{esc(k)}"' if k else ""
        da += f' data-p="{esc(p)}"' if p else ""
        return f'<span class="chip"{da}>{esc(text)}</span>'

    def em(text):
        return f"<b>{esc(text)}</b>"

    tests_pct = st["tests_syms"] * 100 // syms if syms else 0
    calls_avg = st["calls_avg"]
    head = D["meta"]["head"]
    total_fin = D["finTotal"]
    top_kinds = D["kindTop"][:3]
    p_counts = st["path_counts"]
    p1 = p_counts.get("P1", 0)
    p2 = p_counts.get("P2", 0)
    p3 = p_counts.get("P3", 0)
    p4 = p_counts.get("P4", 0)
    kinds_txt = "、".join(chip(f"{k} {_fmt(c)} 条", k=k) for k, c in top_kinds) if top_kinds else "无"
    narr = f"""
<article><h3>① 总览</h3>
<p>本仓 <b>{esc(repo_name)}</b>：{_fmt(syms)} 符号 / {_fmt(st['edges'])} 边 / {_fmt(st['files'])} 文件（git <code>{esc(head[:7] if head else '')}</code>，etl {esc(D['meta']['etl'][:10])}）。{'测试目录符号占 ' + em(f'{tests_pct}%') + '。' if st['tests_syms'] else ''}{'调用图 CALLS 均置信 ' + em(f'{calls_avg:.2f}') + '。' if calls_avg else ''}{'双引擎对账一致率 ' + em(D['cmp']['agrpct'] + '%') + '。' if D['cmp'] else ''}</p></article>
<article><h3>② 异常</h3>
<p>{_fmt(total_fin)} findings：{kinds_txt}。病理分布 {'、'.join(esc(f'{k} {_fmt(v)}') for k, v in sorted(p_counts.items()))}。</p></article>
<article><h3>③ 证据</h3>
<p>每条 finding 行级锚定（file:start-line + window_hash 溯源，切片详情可跳转 chain/ 源码行）。{'交叉证据：xcheck 双引擎对账 ledger ' + _fmt(D['cmp']['ledger']) + ' 行。' if D['cmp'] else ''}coverage gaps {st['gaps']} 处；AI 梳理业务线 {len(D['slices'])} 条{'——' + '、'.join(esc(n) for _i, n in D['slices']) if D['slices'] else ''}。</p></article>
<article><h3>④ 建议</h3>
<p>{f'P1 病理 {p1} 处红线级优先清理（可脚本化批量核除）。' if p1 else ''}{f'P2 语义病灶 {p2} 处逐条复核。' if p2 else ''}{f'P3 重复聚类 {p3} 处收敛 top 文件。' if p3 else ''}{f'P4 死代码 {p4} 处走 T4 人工确认通道后再删。' if p4 else ''}全部处置过 transact 事务留痕（G 码收据）。</p></article>
"""
    return narr


# ---------------------------------------------------------------- 模板


def _template() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>document.documentElement.dataset.theme=(location.search.match(/theme=(\\w+)/)||[])[1]||localStorage.getItem('v9theme')||'archify';</script>
<title>VRP 融合报告 · __REPO__</title>
<style>
__THEMES__
*{box-sizing:border-box}
body{background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;line-height:1.55}
a{color:var(--accent);text-decoration:none}
code{color:var(--code-fg);background:var(--code-bg);padding:1px 5px;border-radius:4px;font-size:.88em}
header{position:sticky;top:0;z-index:50;background:var(--header);backdrop-filter:blur(6px);border-bottom:1px solid var(--border);padding:14px 22px 10px}
header h1{margin:0;font-size:18px;font-family:var(--h1font)}
.themes{position:absolute;top:12px;right:20px;display:flex;gap:6px}
.themes button{background:var(--card);border:1px solid var(--border);color:var(--muted);border-radius:999px;padding:3px 11px;font-size:11.5px;cursor:pointer;font-family:var(--h1font)}
.themes button:hover{color:var(--txt);border-color:var(--accent)}
.themes button.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
header .sub{color:var(--muted);font-size:12px;margin-top:2px}
nav{display:flex;gap:16px;margin-top:9px;font-size:13px;flex-wrap:wrap}
nav a{color:var(--muted)}
nav a:hover{color:var(--accent)}
main{max-width:1180px;margin:0 auto;padding:20px 22px 80px}
section{margin-top:34px}
h2{font-size:16px;color:var(--accent);border-bottom:1px solid var(--border);padding-bottom:6px;margin:0 0 14px}
h2 .src{color:var(--muted);font-size:11px;font-weight:400;margin-left:10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.card .t{color:var(--muted);font-size:12px}
.card .v{font-size:26px;font-weight:600;margin-top:3px}
.card .s{color:var(--muted);font-size:11px;margin-top:2px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.chartbox{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px 4px}
.chartbox .ct{color:var(--muted);font-size:12px;padding:2px 4px 6px}
.chart{width:100%;height:230px}
#graph{width:100%;height:560px}
article{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:12px}
article h3{margin:0 0 6px;font-size:14px;color:var(--h3)}
article p{margin:0;font-size:13.5px}
.chip{display:inline-block;background:var(--chip-bg);border:1px solid var(--chip-bd);color:var(--accent);border-radius:999px;padding:0 10px;font-size:12.5px;margin:0 2px}
.chip[data-k],.chip[data-p],#reset{cursor:pointer}
.chip[data-k]:hover,.chip[data-p]:hover{background:var(--chip-hover)}
td a,#panel a{color:var(--code-fg);text-decoration:none;word-break:break-all}
td a:hover,#panel a:hover{text-decoration:underline}
.hint{color:var(--muted);font-size:12px;margin:6px 2px 0}
.cmpbox{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:900px){.cmpbox{grid-template-columns:1fr}}
.cmpside{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
.cmpside .eng{color:var(--muted);font-size:12px}
.cmpside .num{font-size:30px;font-weight:600}
.agr{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-top:12px}
.bar{display:flex;height:26px;border-radius:6px;overflow:hidden;margin-top:8px;font-size:11px}
.bar div{display:flex;align-items:center;justify-content:center;color:var(--bar-ink);white-space:nowrap;overflow:hidden}
.dl{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:12.5px;color:var(--muted);margin-top:10px}
.dl b{color:var(--txt);font-weight:600}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.controls input,.controls select{background:var(--panel);border:1px solid var(--border);color:var(--txt);border-radius:8px;padding:7px 10px;font-size:13px}
.controls input{width:240px}
.count{color:var(--muted);font-size:12px;margin-left:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--muted);text-align:left;font-weight:500;border-bottom:1px solid var(--border);padding:7px 8px;position:sticky;top:0;background:var(--bg)}
td{border-bottom:1px solid var(--tdb);padding:6px 8px;cursor:pointer}
tr:hover td{background:var(--hover)}
.badge{display:inline-block;border-radius:5px;padding:0 7px;font-size:11px;font-weight:600;color:var(--bar-ink)}
.pP1{background:#f87171}.pP2{background:#fbbf24}.pP3{background:#a78bfa}.pP4{background:#34d399}.pP9{background:#f472b6}
.fp{color:var(--muted)}
.pager{display:flex;gap:10px;align-items:center;justify-content:center;margin-top:12px;font-size:13px}
.pager button{background:var(--panel);border:1px solid var(--border);color:var(--txt);border-radius:8px;padding:6px 14px;cursor:pointer}
.pager button:disabled{opacity:.35;cursor:default}
.tblwrap{max-height:560px;overflow:auto;border:1px solid var(--border);border-radius:10px;background:var(--card)}
#panel{position:fixed;top:0;right:0;bottom:0;width:min(420px,92vw);background:var(--panel);border-left:1px solid var(--border);z-index:100;transform:translateX(102%);transition:transform .22s;overflow-y:auto;padding:18px 20px 40px}
#panel.open{transform:none}
#panel h3{margin:4px 0 4px;font-size:15px;color:var(--accent);word-break:break-all}
#panel .x{position:absolute;top:12px;right:14px;cursor:pointer;color:var(--muted);font-size:20px;border:none;background:none}
#panel .kv{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:13px;margin-top:10px}
#panel .kv span{color:var(--muted)}
#panel .sec{margin-top:16px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--border);padding-bottom:4px}
#panel ul{list-style:none;padding:0;margin:8px 0 0;font-size:12.5px}
#panel li{padding:4px 0;border-bottom:1px dashed var(--tdb);word-break:break-all}
#panel .btn{display:inline-block;margin-top:8px;padding:9px 16px;border-radius:9px;background:var(--accent);color:var(--accent-ink);font-weight:700;font-size:13px;text-decoration:none}
#panel .btn:hover{filter:brightness(1.12)}
footer{border-top:1px solid var(--border);color:var(--muted);font-size:11.5px;text-align:center;padding:18px;max-width:1180px;margin:40px auto 0}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
</style>
</head>
<body>
<header>
<h1>VRP 融合报告 · __REPO__</h1>
<div class="themes"><button data-t="archify">archify</button><button data-t="paper">纸面</button><button data-t="ember">ember</button></div>
<div class="sub">六风格融合（仪表盘+叙事+力导向图+下钻+对比+数据切片）· 单 HTML 静态产物 · 总览层——细节下钻联动 chain/ 源码页 · 切片详情行级跳转 · 主题令牌与 chain 同源</div>
<nav>
<a href="#dash">仪表盘</a><a href="#narr">叙事主线</a><a href="#graphsec">结构图</a><a href="#compare">双引擎对账</a><a href="#slice">数据切片</a>
</nav>
</header>
<main>

<section id="dash">
<h2>仪表盘<span class="src">融合自 P6 ECharts 形态</span></h2>
<div class="cards" id="cards"></div>
<div class="grid2">
<div class="chartbox"><div class="ct">findings 病理分布（P1-P9）</div><div id="chPath" class="chart"></div></div>
<div class="chartbox"><div class="ct">findings kind Top10</div><div id="chKind" class="chart"></div></div>
<div class="chartbox"><div class="ct">边类型分布（Top12，log 轴）</div><div id="chEdge" class="chart"></div></div>
<div class="chartbox"><div class="ct">CALLS 置信分布（0.0-1.0）</div><div id="chConf" class="chart"></div></div>
</div>
</section>

<section id="narr">
<h2>叙事主线<span class="src">融合自 P1 archify explainer 形态——数字全部来自 facts.db 实算</span></h2>
__NARR__
</section>

<section id="graphsec">
<h2>结构图<span class="src">融合自 P5 力导向形态——节点=目录（size=符号数，色=病理密度），边=跨目录 CALLS 聚合；点击节点下钻（P4 范式）</span></h2>
<div class="chartbox"><div class="ct">__REPO__ 目录级力导向图 · 可缩放拖拽 · 悬停聚焦邻域</div><div id="graph"></div></div>
<div class="legend">
<span><i style="background:#34d399"></i>病理密度 低</span>
<span><i style="background:#fbbf24"></i>中</span>
<span><i style="background:#fb923c"></i>高</span>
<span><i style="background:#f87171"></i>极高</span>
<span style="margin-left:auto">点击节点 → 右侧下钻面板</span>
</div>
</section>

<section id="compare">
<h2>双引擎对账<span class="src">融合自 P2 compare 形态——CBM vs CA 调用对账（数据源：facts.db meta xcheck_*）</span></h2>
<div class="cmpbox">
<div class="cmpside"><div class="eng">CBM 侧调用数（codebase-memory）</div><div class="num" id="cbmNum"></div><div class="eng" id="cbmSub"></div></div>
<div class="cmpside"><div class="eng">CA 侧调用数（code-atlas）</div><div class="num" id="caNum"></div><div class="eng" id="caSub"></div></div>
</div>
<div class="agr">
<b id="agrT"></b>
<div class="bar" id="agrBar"></div>
<div class="dl" id="cmpDl"></div>
</div>
</section>

<section id="slice">
<h2>数据切片<span class="src">融合自 P11 Streamlit 范式（静态化：客户端过滤/分页，零服务端）</span></h2>
<div class="controls">
<input id="q" type="text" placeholder="搜索 kind / 文件 / 符号…">
<select id="selP"><option value="">全部病理</option></select>
<select id="selK"><option value="">全部 kind</option></select>
<button class="chip" id="reset" style="border-radius:8px">重置</button>
<span class="count" id="cnt"></span>
</div>
<div class="tblwrap">
<table>
<thead><tr><th style="width:44px">P</th><th style="width:150px">kind</th><th>file:line</th><th>符号</th></tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>
<div class="pager"><button id="prev">上一页</button><span id="pg"></span><button id="next">下一页</button></div>
</section>

</main>

<div id="panel"><button class="x" onclick="closePanel()">✕</button><div id="pbody"></div></div>

<footer id="ft"></footer>

<script>__ECHARTS__</script>
<script>
const D = __DATA__;
const PCOL = {P1:'#f87171',P2:'#fbbf24',P3:'#a78bfa',P4:'#34d399',P9:'#f472b6'};
const CATCOL = ['#34d399','#fbbf24','#fb923c','#f87171'];
const fmt = n => n.toLocaleString('en-US');
const $ = id => document.getElementById(id);
// 注入面统一转义：DB 派生串（文件名/符号/目录名）进 innerHTML 前必须过 esc
// （对齐 chain 层 html.escape 全覆盖口径；过滤/搜索仍用原始值，不受影响）
const esc = s => String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

$('cards').innerHTML = D.cards.map(c =>
  '<div class="card"><div class="t">'+esc(c[0])+'</div><div class="v">'+esc(c[1])+'</div><div class="s">'+esc(c[2])+'</div></div>').join('');
$('ft').innerHTML = 'vrp '+esc(D.meta.vrp)+' · facts git '+esc((D.meta.head||'').slice(0,7))+' · etl '+esc(D.meta.etl)+
  ' · findings '+fmt(D.finTotal)+' · 复现性：同 facts.db → 字节级同报告（构想v9 §6）';
if(!D.cmp){ $('compare').style.display='none'; }

const charts = [];
function mk(id, opt){ const c = echarts.init($(id)); c.setOption(opt); charts.push(c); return c; }
function cssVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

function buildCharts(){
const AXC = {axisLine:{lineStyle:{color:cssVar('--axline')}},axisLabel:{color:cssVar('--muted')},splitLine:{lineStyle:{color:cssVar('--split')}}};
const TXT = cssVar('--txt'), MUT = cssVar('--muted'), BGB = cssVar('--bg');

mk('chPath', {tooltip:{trigger:'item'},
  series:[{type:'pie',radius:['42%','70%'],center:['50%','52%'],
    data:D.path.map(x=>({name:x[0],value:x[1],itemStyle:{color:PCOL[x[0]]}})),
    label:{color:TXT,formatter:'{b}\\n{c}'}}]});

mk('chKind', {tooltip:{},grid:{left:10,right:30,top:10,bottom:6,containLabel:true},
  xAxis:{type:'value',...AXC},
  yAxis:{type:'category',...AXC,axisLabel:{color:MUT,fontSize:10},data:D.kindTop.map(x=>x[0]).reverse()},
  series:[{type:'bar',data:D.kindTop.map(x=>x[1]).reverse(),itemStyle:{color:'#a78bfa'},barWidth:11,
    label:{show:true,position:'right',color:MUT,fontSize:10}}]});

mk('chEdge', {tooltip:{},grid:{left:10,right:16,top:26,bottom:6,containLabel:true},
  xAxis:{type:'category',...AXC,axisLabel:{color:MUT,fontSize:9,rotate:40},data:D.edgeTypes.map(x=>x[0])},
  yAxis:{type:'log',...AXC},
  series:[{type:'bar',data:D.edgeTypes.map(x=>x[1]),itemStyle:{color:'#60a5fa'}}]});

mk('chConf', {tooltip:{},grid:{left:10,right:16,top:26,bottom:6,containLabel:true},
  xAxis:{type:'category',...AXC,axisLabel:{color:MUT,fontSize:10},data:D.conf.map(x=>x[0])},
  yAxis:{...AXC},
  series:[{type:'bar',data:D.conf.map(x=>x[1]),itemStyle:{color:cssVar('--accent')}}]});

const graphC = mk('graph', {tooltip:{formatter:p=>p.dataType==='node'? esc(p.name)+'<br>符号 '+fmt(p.value)+(p.data.fin!==undefined?'<br>findings '+p.data.fin:'') : esc(p.data.source)+' → '+esc(p.data.target)},
  legend:{show:false},
  series:[{type:'graph',layout:'none',roam:true,draggable:true,
    categories:[{name:'低'},{name:'中'},{name:'高'},{name:'极高'}],
    color:CATCOL,
    label:{show:true,position:'bottom',fontSize:10.5,color:TXT,
      textBorderColor:BGB,textBorderWidth:2,formatter:p=>p.name.length>24?p.name.slice(0,23)+'…':p.name},
    emphasis:{focus:'adjacency',label:{show:true,fontSize:12,fontWeight:600}},
    lineStyle:{color:cssVar('--axline'),opacity:.55,curveness:.12},
    data:D.graph.nodes.map(n=>({name:n.name,value:n.value,symbolSize:n.symbolSize,category:n.category,idx:n.idx,x:n.x,y:n.y})),
    links:D.graph.links}]});
graphC.on('click',p=>{ if(p.dataType==='node') openDir(p.data.idx); });
}

function setTheme(t){
  document.documentElement.dataset.theme = t;
  try{ localStorage.setItem('v9theme',t); }catch(e){}
  document.querySelectorAll('.themes button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
  charts.forEach(c=>c.dispose()); charts.length=0;
  buildCharts();
}
document.querySelectorAll('.themes button').forEach(b=>b.onclick=()=>setTheme(b.dataset.t));
document.querySelectorAll('.themes button').forEach(b=>b.classList.toggle('on',b.dataset.t===document.documentElement.dataset.theme));
buildCharts();

if(D.cmp){
  $('cbmNum').textContent = fmt(D.cmp.cbm);
  $('caNum').textContent = fmt(D.cmp.ca);
  $('cbmSub').textContent = 'codebase-memory 提取调用';
  $('caSub').textContent = 'code-atlas 提取调用';
  $('agrT').textContent = '一致 '+fmt(D.cmp.agr)+' 调用（占 CA 侧 '+D.cmp.agrpct+'%）';
  const tot = D.cmp.agr + D.cmp.caonly + D.cmp.cbmonly;
  $('agrBar').innerHTML =
    '<div style="flex:'+D.cmp.agr+';background:#34d399">一致 '+fmt(D.cmp.agr)+'</div>'+
    '<div style="flex:'+D.cmp.caonly+';background:#a78bfa;color:#fff">CA 独有 '+fmt(D.cmp.caonly)+'</div>'+
    '<div style="flex:'+D.cmp.cbmonly+';background:#fbbf24">CBM 独有 '+fmt(D.cmp.cbmonly)+'</div>';
  $('cmpDl').innerHTML =
    '<span>ledger 行</span><b>'+fmt(D.cmp.ledger)+'</b>'+
    '<span>ca_only_builtin_target</span><b>'+fmt(D.cmp.cabuiltin)+'</b>'+
    '<span>cbm_only_test_source</span><b>'+fmt(D.cmp.cbmtest)+'</b>'+
    '<span>口径</span><b>计数口径见 facts.db meta 键名（xcheck_*）</b>';
}

D.path.forEach(x=>{ const o=document.createElement('option'); o.value=x[0]; o.textContent=x[0]+'（'+fmt(x[1])+'）'; $('selP').appendChild(o); });
D.kinds.forEach(k=>{ const o=document.createElement('option'); o.value=k; o.textContent=k+'（'+fmt(D.kindMap[k]||0)+'）'; $('selK').appendChild(o); });

const st = {q:'',p:'',k:'',page:0};
const PER = 50;
function rowsF(){ return D.rows.filter(r=>(!st.p||r.p===st.p)&&(!st.k||r.k===st.k)&&
  (!st.q||(r.f+' '+r.k+' '+r.sym).toLowerCase().includes(st.q.toLowerCase()))); }
function renderTbl(){
  const rs = rowsF(); const pages = Math.max(1,Math.ceil(rs.length/PER));
  if(st.page>=pages) st.page = pages-1;
  const slice = rs.slice(st.page*PER,(st.page+1)*PER);
  $('tbody').innerHTML = slice.map(r=>{
    const loc = r.h ? '<a href="'+r.h+'" target="_blank" title="chain 源码页（行级锚点）">'+esc(r.f)+':'+r.ln+'</a>' : esc(r.f)+':'+r.ln;
    return '<tr data-i="'+r.i+'"><td><span class="badge p'+esc(r.p)+'">'+esc(r.p)+'</span></td><td>'+esc(r.k)+'</td>'+
    '<td class="fp">'+loc+'</td><td>'+esc(r.sym)+'</td></tr>';}).join('');
  $('cnt').textContent = D.rowsTruncated
    ? '命中 '+fmt(rs.length)+' · 嵌入 '+fmt(D.rows.length)+' / 全量 '+fmt(D.finTotal)+'（超过嵌入上限按 id 序截断——构想v9 §4 降级路径）'
    : '命中 '+fmt(rs.length)+' · 全量 '+fmt(D.rows.length)+' 行内嵌（无截断）';
  $('pg').textContent = (st.page+1)+' / '+pages;
  $('prev').disabled = st.page<=0; $('next').disabled = st.page>=pages-1;
}
$('tbody').addEventListener('click',e=>{ const tr=e.target.closest('tr'); if(tr) openFinding(+tr.dataset.i); });
$('prev').onclick=()=>{st.page--;renderTbl();};
$('next').onclick=()=>{st.page++;renderTbl();};
let deb; $('q').oninput=e=>{ clearTimeout(deb); deb=setTimeout(()=>{st.q=e.target.value;st.page=0;renderTbl();},180); };
$('selP').onchange=e=>{st.p=e.target.value;st.page=0;renderTbl();};
$('selK').onchange=e=>{st.k=e.target.value;st.page=0;renderTbl();};
$('reset').onclick=()=>{ st.q='';st.p='';st.k='';st.page=0; $('q').value='';$('selP').value='';$('selK').value=''; renderTbl(); };
document.querySelectorAll('.chip[data-k],.chip[data-p]').forEach(c=>c.onclick=()=>{
  if(c.dataset.k){ st.k=c.dataset.k; $('selK').value=st.k; }
  if(c.dataset.p){ st.p=c.dataset.p; $('selP').value=st.p; }
  if(!c.dataset.k&&!c.dataset.p){ return; }
  st.page=0; renderTbl(); location.hash='slice';
});

function show(html){ $('pbody').innerHTML=html; $('panel').classList.add('open'); }
function closePanel(){ $('panel').classList.remove('open'); history.replaceState(null,'',location.pathname+location.search); }
function openDir(i){
  const p = D.panels[i];
  const bp = Object.keys(p.bypath).sort().map(k=>'<span class="badge p'+esc(k)+'">'+esc(k)+'</span> '+fmt(p.bypath[k])).join('<br>') || '无';
  const fl = p.files.map(f=>{
    const href = D.pages[f[0]];
    const nm = href ? '<a href="'+href+'" target="_blank">'+esc(f[0])+'</a>' : esc(f[0]);
    return '<li>'+nm+' <span class="fp">('+fmt(f[1])+')</span></li>';}).join('');
  show('<h3>'+esc(p.name)+'</h3><div class="kv">'+
    '<span>符号数</span><b>'+fmt(p.syms)+'</b>'+
    '<span>findings</span><b>'+fmt(p.fin)+'（密度 '+(p.dens*100).toFixed(1)+'%）</b></div>'+
    '<div class="sec">findings 病理分布</div>'+bp+
    '<div class="sec">top 文件（按符号数，链接→chain 细节页）</div><ul>'+fl+'</ul>'+
    '<div class="sec">CALLS 出边 top3</div><ul>'+(p.out.map(o=>'<li>'+esc(o[0])+' <span class="fp">('+fmt(o[1])+')</span>').join('')||'<li>无')+'</ul>'+
    '<div class="sec">CALLS 入边 top3</div><ul>'+(p.in.map(o=>'<li>'+esc(o[0])+' <span class="fp">('+fmt(o[1])+')</span>').join('')||'<li>无')+'</ul>'+
    '<div class="sec">archify 细节层</div><ul><li><a href="chain/index.html" target="_blank">chain/ 总入口（模块交互图链 · 文件级源码页）</a></li></ul>');
  history.replaceState(null,'','#d/'+i);
}
function impactHtml(r){
  if(r.a < 0) return '<div class="fp">无锚定符号——影响面不可算（该 finding 未锚定到符号）</div>';
  const m = D.impacts[r.a];
  if(!m) return '<div class="fp">无调用边——直接影响面为零</div>';
  const pre = m.cap ? '≥ ' : '';
  return '<div class="kv">'+
    '<span>直接影响</span><b>被 '+fmt(m.dc)+' 符号调用 · 调用 '+fmt(m.dx)+' 符号</b>'+
    '<span>传递影响</span><b>上游 '+pre+fmt(m.uc)+' · 下游 '+pre+fmt(m.xc)+(m.cap?'（BFS 帽截断，为下界）':'')+'</b>'+
    (m.up.length?'<span>top 调用方</span><b>'+m.up.map(esc).join('、')+'</b>':'')+
    (m.down.length?'<span>top 被调</span><b>'+m.down.map(esc).join('、')+'</b>':'')+
    '</div>';
}
function openFinding(id){
  const r = D.rows.find(x=>x.i===id); if(!r) return;
  const jumpBtn = r.h
    ? '<a class="btn" href="'+r.h+'" target="_blank">跳转源码位置 →'+(r.h.includes('#L')?'（行级锚点）':'（文件级定位）')+'</a>'
    : '<div class="fp">该文件未入 chain 细节层（未入链文件的源码页不在报告产物内）</div>';
  show('<h3>'+esc(r.k)+'</h3><div class="kv">'+
    '<span>病理</span><b><span class="badge p'+esc(r.p)+'">'+esc(r.p)+'</span></b>'+
    '<span>位置</span><b>'+(r.h?'<a href="'+r.h+'" target="_blank">'+esc(r.f)+':'+r.ln+'</a>':esc(r.f)+':'+r.ln)+'</b>'+
    '<span>锚定符号</span><b>'+(r.sym?esc(r.sym):'—')+'</b>'+
    '<span>来源</span><b>'+esc(r.s)+'</b></div>'+
    '<div class="sec">跳转</div>'+jumpBtn+
    '<div class="sec">影响面</div>'+impactHtml(r)+
    '<div class="sec">证据链</div><div class="fp" style="font-size:12px">行级锚定 file:start-end + window_hash 溯源（facts.db findings 表 #'+r.i+'）</div>');
  history.replaceState(null,'','#f/'+id);
}
window.addEventListener('resize',()=>charts.forEach(c=>c.resize()));
renderTbl();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 入口


def build_fusion(repo: Path, out_dir: Path, chain: dict | None) -> dict:
    """融合报告生成（report 根 fusion.html）。

    chain 为 render_chain.build_chain 返回值（同批产出——file_pages/
    file_caps 内存直传，行级跳转 chain/{page}#L{n} 零扫描）；chain 缺失/
    降级时照常生成（无 chain 链接，诚实注记）。
    返回 {ok, fusion, notes}。
    """
    repo = Path(repo)
    out_dir = Path(out_dir)
    notes: list[str] = []
    db = repo / ".vrp" / "facts.db"
    con = dbpath.ro_connect(db)
    con.row_factory = sqlite3.Row
    try:
        D = _load_data(con, chain)
    finally:
        con.close()
    if not (chain or {}).get("ok"):
        notes.append("chain degraded/missing - fusion emitted without code page links")
    if D["rowsTruncated"]:
        notes.append(f"findings {D['finTotal']} over embed cap {ROW_ID_CAP}, "
                     f"rows embedded by id order (truncated)")
    narr = _narr(D, repo.name)
    D.pop("_stats", None)
    data_json = json.dumps(D, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True).replace("</", "<\\/")
    echarts = _ECHARTS.read_text(encoding="utf-8")
    # 单趟占位替换：替换产物（narr/data_json 可含 DB 派生的 "__DATA__" 等
    # 字面量）不再被后续替换重扫——串行 replace 链存在二次注入面
    subs = {
        "__REPO__": H.escape(repo.name),
        "__THEMES__": render_theme.theme_root_css(),
        "__ECHARTS__": echarts,
        "__NARR__": narr,
        "__DATA__": data_json,
    }
    html_out = re.sub(r"__[A-Z]+__",
                      lambda m: subs.get(m.group(0), m.group(0)), _template())
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "fusion.html"
    target.write_bytes(html_out.encode("utf-8"))
    return {"ok": True, "fusion": str(target), "notes": notes}
