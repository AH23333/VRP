"""R7'(2) 模块交互图链：紧凑层次化可下钻页面链（替代 Mermaid L1/L2）。

架构（探针 15 移植 + 生产化守卫）：
  index.html 总入口 → 每模块一条链，按数据形状分流入口：
    2-8 文件且有真实符号 → 模块 dataflow（s1-{模块}-dataflow.html）；
    多目录 → 目录总览树（s2a-{模块}-overview.html）；
    其余 → 目录页 / 单文件符号网格（s3-{模块}-{文件}.html）。
  代码层（唯一代码视图）= 整文件源码页 s4f-*.html + 符号锚点：
    符号行区间 → #sym-{slug} 锚点区块（CSS :target 高亮，零 JS）；
    符号起始处关系条（调用/被调/内含 chips）；页首大纲 chips；
    代码内同文件唯一符号名 → 页内锚点链接；
    嵌套符号（类→方法）按行区间栈式嵌套渲染。
  chrome（全站融合）：统一顶栏面包屑（首页›模块›…›当前）+ 右侧动作；
    图页单击=选中+底部浮板、再次单击/进入按钮/回车=进入（带 ?from=）；
    代码页 sticky 顶栏 + ?from=「← 返回」。

生产化差异（相对探针）：
  三段式原子提交（杜绝断链）：
    Pass1 逐模块 build IR → archify validate+render 进暂存区（.staging），
          任一图失败 → 丢弃整模块暂存、记 note、模块弃链；
    Pass2 文件页归属 = 首个成功链上该文件的模块；全局符号索引只收录
          「文件页已落盘且起始行 ≤ 截断帽」的符号 → 所有 chips/clicks
          天然不指向缺失页/缺失锚点；
    Pass3 index.html（仅成功模块）。
  全局符号索引（跨模块）：同文件被多模块共享时锚点全局去重稳定
    （探针按模块覆盖写，共享文件时先写模块的网格锚点会被后写覆盖掉——
    本仓 fixture m1/m2 共享 pkg/x.py 即触发）。
  帽：CHAIN_MODULES_CAP（模块数）/ CHAIN_FILE_PAGES_CAP（每模块文件页）/
      CHAIN_FILE_LINES_CAP（单文件行数，截断注记）/
      CHAIN_DIAGRAMS_GLOBAL_CAP（全站图预算，超限整模块弃链）。
  守卫：文件路径绝对/含 ':'/'..'/尖括号 → 拒绝注记页；node 缺失快速失败。
  分块：fid/边查询按 FID_CHUNK 分块（防 SQLite 32766 变量上限）。
  去重表为模块级可变状态：build_chain 入口 _reset_state()，
    同进程多次调用（测试）安全。
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

ARCHIFY_TIMEOUT = 300
ARCHIFY_RENDER_TIMEOUT = 900
FID_CHUNK = 500
CHAIN_MODULES_CAP = 30
CHAIN_FILE_PAGES_CAP = 400
CHAIN_FILE_LINES_CAP = 4000
CHAIN_DIAGRAMS_GLOBAL_CAP = 240

# vendored dataflow-compiler 紧凑常量（须与 compiler layout 保持一致）
ROW_Y0, ROW_GAP, NODE_H = 106, 47, 27
STAGE_W, LEFT_X = 120, 70
PAD = 44

# ---------------------------------------------------------------- 去重表（模块级）


_dir_stem_of: dict[tuple[str, str], str] = {}
_dir_stem_used: dict[str, int] = {}
_grid_stem_of: dict[tuple[str, str], str] = {}
_grid_stem_used: dict[str, int] = {}
_s4f_of: dict[tuple[str, str], str] = {}
_s4f_used: dict[str, int] = {}


def _reset_state() -> None:
    """同进程多次 build_chain（测试）安全：清模块级去重表。"""
    for d in (_dir_stem_of, _dir_stem_used, _grid_stem_of, _grid_stem_used,
              _s4f_of, _s4f_used):
        d.clear()


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def _dedup(used: dict[str, int], base: str) -> str:
    k = used.get(base, 0)
    used[base] = k + 1
    return base if k == 0 else f"{base}-{k + 1}"


def dir_page_stem(module_id: str, d: str) -> str:
    """目录页 stem：s2-{模块}-dir-{末两段路径}，幂等 + 全局去重。"""
    key = (module_id, d)
    if key in _dir_stem_of:
        return _dir_stem_of[key]
    base = f"s2-{slug(module_id)}-dir-{slug('-'.join(d.split('/')[-2:]))}"
    stem = _dedup(_dir_stem_used, base)
    _dir_stem_of[key] = stem
    return stem


def grid_stem_of(module_id: str, fpath: str) -> str:
    """符号网格 stem：s3-{模块}-{文件主干}，幂等 + 全局去重。"""
    key = (module_id, fpath)
    if key in _grid_stem_of:
        return _grid_stem_of[key]
    base = f"s3-{slug(module_id)}-{slug(Path(fpath).stem)}"
    stem = _dedup(_grid_stem_used, base)
    _grid_stem_of[key] = stem
    return stem


def file_page_stem(dir_path: str, fname: str) -> str:
    """整文件源码页 stem：按 (目录, 文件名) 幂等分配；末段目录名 + 文件主干去重。"""
    key = (dir_path, fname)
    if key in _s4f_of:
        return _s4f_of[key]
    parent = dir_path.rstrip("/").rsplit("/", 1)[-1] or "root"
    base = f"s4f-{slug(parent)}-{slug(fname.rsplit('.', 1)[0])}"
    stem = _dedup(_s4f_used, base)
    _s4f_of[key] = stem
    return stem


def file_page_stem_of(fpath: str) -> str:
    """由文件路径取整文件页 stem（与 write_file_page 内部分配一致）。"""
    if "/" in fpath:
        dirp, fname = fpath.rsplit("/", 1)
    else:
        dirp, fname = ".", fpath
    return file_page_stem(dirp, fname)


# ---------------------------------------------------------------- 数据加载


def _norm(fp: str) -> str:
    return str(fp).replace("\\", "/")


def _load_structure_modules(con: sqlite3.Connection) -> list[dict] | None:
    """structure active 模块清单（模块交互图链仅源自 curated structure；
    无 structure/无 active/空 modules → None（调用方降级 L1/L2）。"""
    row = con.execute(
        "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    mods = payload.get("modules") if isinstance(payload, dict) else None
    if not mods:
        return None
    return mods


def _load_rows_edges(con: sqlite3.Connection, all_fids: list[str]
                     ) -> tuple[dict[str, sqlite3.Row], list[tuple[str, str, str]]]:
    """全局符号行 + 模块内边（fid/边按 FID_CHUNK 分块查询）。"""
    rows: dict[str, sqlite3.Row] = {}
    fid_set = set(all_fids)
    for i in range(0, len(all_fids), FID_CHUNK):
        chunk = all_fids[i:i + FID_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in con.execute(
            f"SELECT fid, name, label, file_path, start_line, end_line FROM symbols WHERE fid IN ({marks})",
            chunk,
        ):
            rows[r["fid"]] = r
    edges: list[tuple[str, str, str]] = []
    for i in range(0, len(all_fids), FID_CHUNK):
        chunk = all_fids[i:i + FID_CHUNK]
        marks = ",".join("?" * len(chunk))
        for a, b, t in con.execute(
            f"SELECT src_fid, dst_fid, type FROM edges WHERE src_fid IN ({marks})",
            chunk,
        ):
            if a in fid_set and b in fid_set:
                edges.append((a, b, t))
    return rows, edges


def real_by_file(rows: dict[str, sqlite3.Row], fids: list[str]) -> dict[str, list[str]]:
    """真实符号（排除 File/Module 伪符号与无行号符号）按规范路径分组。"""
    out: dict[str, list[str]] = defaultdict(list)
    for f in fids:
        r = rows.get(f)
        if (r is None or not r["file_path"] or not r["start_line"]
                or r["label"] in ("File", "Module")):
            continue
        out[_norm(r["file_path"])].append(f)
    return out


def module_fs(fids: list[str], rows: dict[str, sqlite3.Row]):
    """模块文件系统视图：目录 → 文件 → 符号 fid 列表。"""
    files_by_dir: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for f in fids:
        r = rows.get(f)
        if r is None or not r["file_path"]:
            continue
        fp = _norm(r["file_path"])
        files_by_dir[str(PurePosixPath(fp).parent)][fp.split("/")[-1]].append(f)
    dirs: set[str] = set()
    for d in files_by_dir:
        if d in (".", ""):
            continue
        parts = d.split("/")
        for k in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:k]))
    subdirs: dict[str, list[str]] = defaultdict(list)
    for d in dirs:
        subdirs["/".join(d.split("/")[:-1])].append(d)
    for k in subdirs:
        subdirs[k].sort()

    def agg(d: str) -> int:
        return len(files_by_dir.get(d, {})) + sum(agg(s) for s in subdirs.get(d, []))

    return files_by_dir, subdirs, agg


def depth_from_source(fids, edges):
    """列内调用深度：入口（列内入度 0）深度 0，沿边递增。环安全。"""
    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {f: 0 for f in fids}
    for a, b, _ in edges:
        succ[a].append(b)
        indeg[b] = indeg.get(b, 0) + 1
    memo: dict[str, int] = {}
    on: set[str] = set()

    def d(f: str) -> int:
        if f in memo:
            return memo[f]
        if f in on:
            return 0
        on.add(f)
        best = 0
        for nxt in succ[f]:
            best = max(best, d(nxt) + 1)
        on.discard(f)
        memo[f] = best
        return memo.setdefault(f, best)

    for f in fids:
        d(f)
    return memo, indeg


# ---------------------------------------------------------------- S1 dataflow


def _fit(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def s1_module_dataflow(fids: list[str], rows: dict[str, sqlite3.Row],
                       edges: list[tuple[str, str, str]], module_id: str) -> tuple[dict, dict[str, str]]:
    """模块 dataflow：列=文件、节点=真实符号（伪符号不进图）。

    返回 (IR, id_of)：id_of 为符号 fid → 节点 id。
    """
    by_file: dict[str, list[str]] = defaultdict(list)
    for f in fids:
        r = rows.get(f)
        if r is not None and r["label"] in ("File", "Module"):
            continue  # File/Module 伪符号与列标签冗余，不进节点
        by_file[r["file_path"] if r is not None and r["file_path"] else "(drifted)"].append(f)
    files = sorted(by_file)
    stage_of = {fp: i for i, fp in enumerate(files)}
    deg: dict[str, int] = defaultdict(int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1

    nodes, id_of, geom = [], {}, {}
    for fp in files:
        ff = by_file[fp]
        intra = [(a, b, t) for a, b, t in edges
                 if rows.get(a) and rows.get(b)
                 and rows[a]["file_path"] == fp and rows[b]["file_path"] == fp]
        memo, indeg = depth_from_source(ff, intra)
        ordered = sorted(ff, key=lambda f: (
            memo.get(f, 0),
            (rows[f]["start_line"] or 0) if f in rows else 0))
        for j, f in enumerate(ordered):
            nid = f"n{len(nodes)}"
            id_of[f] = nid
            r = rows.get(f)
            entry = indeg.get(f, 0) == 0
            hub = deg.get(f, 0) >= 8
            nodes.append({
                "id": nid,
                "type": "frontend" if entry else ("database" if hub else "backend"),
                "label": _fit(r["name"] if r is not None else f"drifted {f[:8]}", 13),
                "sublabel": _fit(f"L{r['start_line']}" if r is not None and r["start_line"] else "", 30),
                "stage": stage_of[fp], "row": j,
            })
            geom[f] = (stage_of[fp], j)
    max_row = max((j for _, j in geom.values()), default=0)
    n_stg = len(files)

    def file_of(fid: str) -> str:
        r = rows.get(fid)
        return r["file_path"] if r is not None and r["file_path"] else "(drifted)"

    inter = []
    seen: set[tuple[str, str]] = set()
    for a, b, t in edges:
        if a in id_of and b in id_of and (a, b) not in seen and file_of(a) != file_of(b):
            seen.add((a, b))
            inter.append((a, b, t))

    # 相邻列对区间着色 → 通道需求 → 走廊宽度自适应（layout.colGap 下发编译器）
    pair_iv: dict = defaultdict(list)
    for i, (a, b, t) in enumerate(inter):
        sa, ra = geom[a]
        sb, rb = geom[b]
        if abs(sa - sb) == 1:
            ya = ROW_Y0 + ra * ROW_GAP + NODE_H // 2
            yb = ROW_Y0 + rb * ROW_GAP + NODE_H // 2
            pair_iv[(min(sa, sb), max(sa, sb))].append((min(ya, yb), max(ya, yb), i))

    def channels(iv):
        chans: list[float] = []
        asg: dict[int, int] = {}
        for lo, hi, i in sorted(iv):
            for k, last in enumerate(chans):
                if lo > last + 8:
                    chans[k] = hi
                    asg[i] = k
                    break
            else:
                chans.append(hi)
                asg[i] = len(chans) - 1
        return asg, len(chans)

    pair_chan = {pr: channels(iv) for pr, iv in pair_iv.items()}
    max_k = max((k for _, k in pair_chan.values()), default=0)
    col_gap = min(320, 120 + max(20, max_k * 5 + 10))

    width = max(360, LEFT_X + (n_stg - 1) * col_gap + STAGE_W // 2 + 24)
    node_bottom = ROW_Y0 + max_row * ROW_GAP + NODE_H
    frame_bottom = node_bottom + PAD
    n_lane = sum(1 for a, b, _ in inter if abs(stage_of[file_of(a)] - stage_of[file_of(b)]) > 1)
    height = max(360, frame_bottom + 14 + n_lane * 6 + 72)  # 底部 66px 为 legend 带

    flows = []
    band_labels: dict[int, list[float]] = defaultdict(list)
    lane = 0

    def place_label(lx: float, ra: int, rb: int) -> float:
        """标签放带（行间 20px 净空带，标签板高 18）：沿线路取空闲带，带内 x 间隔 ≥58px。"""
        lo, hi_r = sorted((ra, rb))
        base = hi_r if lo == hi_r else hi_r - 1
        cand = [base] + [b for b in range(-1, max_row + 1)
                         if b != base and abs(b - base) <= 1]
        cand += [b for b in sorted(range(-1, max_row + 1), key=lambda x: abs(x - base))
                 if b not in cand]
        band = next((b for b in cand
                     if all(abs(lx - ox) >= 58 for ox in band_labels[b])), cand[0])
        band_labels[band].append(lx)
        return ROW_Y0 + band * ROW_GAP + NODE_H + 13

    for i, (a, b, t) in enumerate(inter):
        sa, ra = geom[a]
        sb, rb = geom[b]
        ya = ROW_Y0 + ra * ROW_GAP + NODE_H / 2  # 真中心：奇数高 27 时 //2 会偏 0.5
        yb = ROW_Y0 + rb * ROW_GAP + NODE_H / 2
        flow: dict = {"id": f"e{len(flows)}", "from": id_of[a], "to": id_of[b],
                      "label": _fit(t or "CALLS", 10)}
        if abs(sa - sb) == 1:
            pr = (min(sa, sb), max(sa, sb))
            asg, K = pair_chan[pr]
            k = asg[i]
            L = LEFT_X + pr[0] * col_gap + STAGE_W // 2
            R = LEFT_X + pr[1] * col_gap - STAGE_W // 2
            gx = round(L + 3 + k * (R - L - 6) / max(1, K - 1))
            flow.update({"fromSide": "right" if sa < sb else "left",
                         "toSide": "left" if sa < sb else "right",
                         "via": [[gx, ya], [gx, yb]],
                         "labelAt": [gx, place_label(gx, ra, rb)]})
        else:
            lane_y = frame_bottom + 10 + lane * 6
            lane += 1
            g_src_side = "right" if sa < n_stg - 1 else "left"
            g_src = (LEFT_X + sa * col_gap + STAGE_W // 2 + 6 if g_src_side == "right"
                     else LEFT_X + sa * col_gap - STAGE_W // 2 - 6)
            g_tgt_side = "left" if sb > 0 else "right"
            g_tgt = (LEFT_X + sb * col_gap - STAGE_W // 2 - 6 if g_tgt_side == "left"
                     else LEFT_X + sb * col_gap + STAGE_W // 2 + 6)
            flow.update({"fromSide": g_src_side, "toSide": g_tgt_side,
                         "via": [[g_src, ya], [g_src, lane_y], [g_tgt, lane_y], [g_tgt, yb]],
                         "labelAt": [g_src, place_label(g_src, ra, rb)]})
        flows.append(flow)

    stages = []
    for fp in files:
        n = len(by_file[fp])
        intra_n = sum(1 for a, b, _ in edges
                      if rows.get(a) and rows.get(b)
                      and rows[a]["file_path"] == fp and rows[b]["file_path"] == fp)
        stem = fp.split("/")[-1].rsplit(".", 1)[0]
        stages.append({"label": _fit(f"{stem} · {n}/{intra_n} edges", 17)})
    ir = {
        "schema_version": 1, "diagram_type": "dataflow",
        "meta": {"title": f"{module_id} module · columns=files (compact)", "output": "s1.html",
                 "viewBox": [int(width), int(height)]},
        "layout": {"colGap": col_gap},
        "stages": stages, "nodes": nodes, "flows": flows,
    }
    return ir, id_of


# ---------------------------------------------------------------- S2 目录树


def s2a_overview(fs_view, module_id: str, title: str):
    """目录总览树：根 → 全部目录（按路径嵌套），子标签=递归文件数。"""
    files_by_dir, subdirs, agg = fs_view
    all_dirs = sorted({d for ds in subdirs.values() for d in ds})
    total = sum(len(v) for v in files_by_dir.values())
    nodes: list[dict] = [{"id": "root", "label": f"{title} · {total} files"}]
    clicks: dict[str, str] = {}
    id_of_dir: dict[str, str] = {}
    for i, d in enumerate(all_dirs):
        nid = f"d{i}"
        id_of_dir[d] = nid
        nodes.append({
            "id": nid,
            "label": _fit(d.split("/")[-1], 16),
            "sublabel": f"{agg(d)} files",
            "parent": id_of_dir.get("/".join(d.split("/")[:-1]), "root"),
        })
        clicks[nid] = dir_page_stem(module_id, d) + ".html"
    ir = {
        "schema_version": 1, "diagram_type": "tree",
        "meta": {"title": f"{title} directory overview", "output": "s2a.html"},
        "layout": {"direction": "right", "gapX": 32, "gapY": 10,
                   "nodeW": 90, "nodeMaxW": 200},
        "nodes": nodes,
    }
    return ir, clicks


def build_dir_tree(fs_view, module_id: str, dir_path: str, file_page=None):
    """目录内部树下钻页：根=该目录，子节点=子目录（递归文件数）+ 文件。

    direction down：>=3 连续叶子由渲染器折为缩进列表（巨目录可读）。
    子目录节点点击 → 其内部树页；文件节点点击 → file_page（符号页）。
    """
    files_by_dir, subdirs, agg = fs_view
    subs = subdirs.get(dir_path, [])
    files = files_by_dir.get(dir_path, {})
    nodes: list[dict] = [{
        "id": "root",
        "label": f"{dir_path.split('/')[-1]} · {agg(dir_path)} files",
    }]
    clicks: dict[str, str] = {}
    for i, sd in enumerate(subs):
        nid = f"d{i}"
        # 目录计数并入标签保持单行：sublabel 会把共享 rowH 抬高（41→27），巨列表被放大 27%
        nodes.append({"id": nid, "label": f"{sd.split('/')[-1]} · {agg(sd)} files", "parent": "root"})
        clicks[nid] = dir_page_stem(module_id, sd) + ".html"
    for j, fname in enumerate(sorted(files)):
        nid = f"f{j}"
        n: dict = {"id": nid, "label": fname, "parent": "root"}
        if len(files[fname]) > 1:
            n["sublabel"] = f"{len(files[fname])} symbols"
        nodes.append(n)
        if file_page is not None:
            target = file_page(dir_path, fname)
            if target:
                clicks[nid] = target
    ir = {
        "schema_version": 1, "diagram_type": "tree",
        "meta": {"title": f"{module_id} · {dir_path}", "output": "dir.html"},
        # nodeMaxW 放宽：长文件名保持单行，堆叠列表行高不被两行标签抬高
        "layout": {"direction": "down", "nodeW": 160, "nodeMaxW": 300},
        "nodes": nodes,
    }
    return ir, clicks


# ---------------------------------------------------------------- chrome 注入


def inject_chrome(html_path: Path, clicks: dict[str, str],
                  crumbs: list[tuple[str | None, str]],
                  action: tuple[str, str] | None = None,
                  page: str = "") -> None:
    """渲染后注入统一 chrome：顶部面包屑条（首页 › 模块 › … › 当前 + 右侧
    上下文动作，archify 令牌）；单击=选中（原生缩放）+ 底部浮板（节点名+
    进入）；再次单击同一节点 / 点「进入」按钮 / 回车 = 进入（导航带 ?from=
    页 stem，代码页据此显「← 返回」；file:// 下 document.referrer 为空
    不可用）；首次访问一次性提示条（sessionStorage）。
    """
    h = html_path.read_text(encoding="utf-8")
    parts: list[str] = []
    cs: list[str] = []
    for i, (href, label) in enumerate(crumbs):
        esc = html.escape(label, quote=True)
        if i == len(crumbs) - 1 or href is None:
            cs.append(f'<span class="pcb{" pcur" if i == len(crumbs) - 1 else ""}">{esc}</span>')
        else:
            cs.append(f'<a class="pcb" href="{html.escape(href, quote=True)}">{esc}</a>')
    act = (f'<a href="{html.escape(action[0], quote=True)}">'
           f'{html.escape(action[1])} &#8594;</a>' if action else "")
    parts.append(
        "<style>"
        ".ptop{position:fixed;top:14px;left:14px;z-index:9999;display:flex;"
        "align-items:center;gap:16px;max-width:calc(100vw - 28px);"
        "background:rgba(10,17,38,.92);border:1px solid #1e293b;border-radius:12px;"
        "padding:8px 14px;font:13px system-ui,sans-serif;color:#e2e8f0;"
        "box-shadow:0 4px 16px rgba(0,0,0,.4);backdrop-filter:blur(6px)}"
        ".pcrumbs{display:flex;align-items:center;gap:6px;overflow:hidden;"
        "white-space:nowrap}"
        ".pcb{color:#94a3b8;text-decoration:none}.pcb:hover{color:#67e8f9}"
        ".pcb.pcur{color:#e2e8f0;font-weight:600}"
        ".psep{color:#334155}"
        ".pact{flex:0 0 auto}.pact a{color:#04121f;background:#22d3ee;"
        "border-radius:999px;padding:5px 12px;text-decoration:none;font-weight:600;"
        "white-space:nowrap}.pact a:hover{background:#67e8f9}"
        + "".join(f"#node-{i}{{cursor:pointer}}" for i in clicks)
        + "</style>"
        '<div class="ptop"><div class="pcrumbs">'
        + '<span class="psep">&#8250;</span>'.join(cs)
        + '</div><div class="pact">' + act + "</div></div>")
    if clicks:
        parts.append(
            "<script>\n"
            "(function(){var M=" + json.dumps(clicks) + ";var PG=" + json.dumps(page) + ";"
            "function W(u){var i=u.indexOf('#');"
            "return i<0?u+'?from='+PG:u.slice(0,i)+'?from='+PG+u.slice(i);}"
            "var selId=null;"
            "var bar=document.createElement('div');"
            "bar.style.cssText='position:fixed;left:50%;bottom:18px;"
            "transform:translateX(-50%);display:flex;align-items:center;gap:10px;"
            "background:rgba(10,17,38,.94);color:#e2e8f0;padding:8px 8px 8px 16px;"
            "border:1px solid #1e293b;border-radius:999px;font:13px/1.4 system-ui,sans-serif;"
            "z-index:9999;opacity:0;transition:opacity .15s;white-space:nowrap;max-width:90vw;"
            "box-shadow:0 4px 16px rgba(0,0,0,.4);pointer-events:none';"
            "var lbl=document.createElement('span');"
            "lbl.style.cssText='overflow:hidden;text-overflow:ellipsis';"
            "var btn=document.createElement('button');btn.type='button';"
            "btn.textContent='Enter →';"
            "btn.style.cssText='border:0;background:#22d3ee;color:#04121f;"
            "padding:6px 14px;border-radius:999px;font:600 13px system-ui,sans-serif;"
            "cursor:pointer;pointer-events:auto';"
            "btn.addEventListener('click',function(ev){ev.stopPropagation();"
            "if(selId&&M[selId])location.href=W(M[selId]);});"
            "bar.appendChild(lbl);bar.appendChild(btn);"
            "document.body.appendChild(bar);"
            "function n(e){return e.target.closest?e.target.closest('g[data-node-id]'):null}"
            "document.addEventListener('click',function(e){"
            "if(bar.contains(e.target))return;"
            "var g=n(e);"
            "if(!g){selId=null;bar.style.opacity='0';return;}"
            "var id=g.getAttribute('data-node-id');"
            "if(id===selId&&M[id]){location.href=W(M[id]);return;}"
            "selId=id;"
            "lbl.textContent=g.getAttribute('data-node-label')||'';"
            "btn.style.display=M[id]?'':'none';"
            "bar.style.opacity='1';},true);"
            "document.addEventListener('keydown',function(e){"
            "if(e.key!=='Enter')return;"
            "var g=n(e);"
            "if(!g||g.getAttribute('data-node-id')!==selId||!M[selId])return;"
            "location.href=W(M[selId]);},true);"
            "})();\n</script>"
            "<script>\n"
            "try{if(!sessionStorage.getItem('archifyHint')){"
            "var h=document.createElement('div');"
            "h.style.cssText='position:fixed;left:50%;bottom:70px;"
            "transform:translateX(-50%);background:rgba(10,17,38,.94);"
            "border:1px solid #1e293b;color:#e2e8f0;padding:8px 16px;"
            "border-radius:999px;font:12.5px system-ui,sans-serif;z-index:9998;"
            "box-shadow:0 4px 16px rgba(0,0,0,.4)';"
            "h.textContent='Click a node to select · click again (or Enter) to drill down';"
            "document.body.appendChild(h);"
            "sessionStorage.setItem('archifyHint','1');"
            "setTimeout(function(){h.remove()},6000);}}catch(e){}\n"
            "</script>")
    inject = "\n".join(parts)
    h = h.replace("</body>", inject + "\n</body>", 1)
    html_path.write_text(h, encoding="utf-8")


# ---------------------------------------------------------------- S3 角色网格


def build_file_grid(rows: dict[str, sqlite3.Row], ffids: list[str],
                    edges: list[tuple[str, str, str]]) -> tuple[dict, dict[str, str]]:
    """单文件符号角色网格：全部角色由图形状推导（通用规则，零个例特调）。

    返回 (IR, sym)：sym 为节点 id → 符号 fid。
    边分拣：DEFINES=文件包含关系（伪符号成员，不进调用图）；
    其余（CALLS/CALL_REFERENCE）构成调用图。
    角色按优先级归属：
      入口   调用图入度 0（仅当调用图有边）
      分发   出度 ≥3 且过半出边为 CALL_REFERENCE（注册/分发特征）
      家族   前缀 token（首个 '_' 前，去前导下划线）相同且成员 ≥3
      枢纽   剩余符号中入度 ≥ max(3, 符号数//8)
      其他   不属于以上
    """
    defs = [f for f in ffids if f in rows and rows[f]["start_line"]
            and rows[f]["label"] not in ("File", "Module")]
    indeg: dict[str, int] = defaultdict(int)
    outdeg: dict[str, int] = defaultdict(int)
    refout: dict[str, int] = defaultdict(int)
    n_call = 0
    for a, b, t in edges:
        if t == "DEFINES":
            continue
        n_call += 1
        outdeg[a] += 1
        indeg[b] += 1
        if t == "CALL_REFERENCE":
            refout[a] += 1
    rest = list(defs)

    def take(pred) -> list[str]:
        nonlocal rest
        got = sorted((f for f in rest if pred(f)), key=lambda f: rows[f]["start_line"])
        rest = [f for f in rest if f not in set(got)]
        return got

    # 0 边文件无调用图：不判入口/分发（全部符号入度 0 会误判全员入口）
    entries = take(lambda f: n_call > 0 and indeg[f] == 0)
    dispatch = take(lambda f: n_call > 0 and outdeg[f] >= 3 and refout[f] * 2 >= outdeg[f])
    fam: dict[str, list[str]] = defaultdict(list)
    for f in rest:
        fam[rows[f]["name"].lstrip("_").split("_")[0]].append(f)
    families = sorted(((tok, mem) for tok, mem in fam.items() if len(mem) >= 3),
                      key=lambda kv: (-len(kv[1]), kv[0]))
    fam_taken = {f for _, mem in families for f in mem}
    rest = [f for f in rest if f not in fam_taken]
    hubs = take(lambda f: n_call > 0 and indeg[f] >= max(3, len(defs) // 8))
    others = rest

    header: list[tuple[str, str, str]] = []
    regions: list[tuple[str, list[str], str]] = []
    if len(entries) == 1:
        f = entries[0]
        header.append((f, "frontend", f"L{rows[f]['start_line']}"))
    elif entries:
        regions.append(("entries", entries, "frontend"))
    if len(dispatch) == 1:
        f = dispatch[0]
        header.append((f, "messagebus",
                       f"L{rows[f]['start_line']} · {refout[f]} refs"))
    elif dispatch:
        regions.append(("dispatch", dispatch, "messagebus"))
    for tok, mem in families:
        regions.append((f"{tok} · {len(mem)}", mem, "backend"))
    if hubs:
        regions.append((f"shared hubs · {len(hubs)}", hubs, "database"))
    if others:
        regions.append((f"others · {len(others)}", others, "backend"))

    n_cols = max(3, min(8, round(math.sqrt(len(defs) * 1.5))))
    size = [130, 44]
    components: list[dict] = []
    boundaries: list[dict] = []
    sym: dict[str, str] = {}

    def add_node(f: str, ctype: str, sub: str, r: int, c: int) -> str:
        nid = f"n{len(components)}"
        sym[nid] = f
        components.append({"id": nid, "type": ctype,
                           "label": _fit(rows[f]["name"], 22), "sublabel": sub,
                           "row": r, "col": c, "size": size})
        return nid

    r = 0
    for f, ctype, sub in header:
        add_node(f, ctype, sub, 0, r % n_cols)
        r += 1
    r = 2
    for label, members, ctype in regions:
        ids = []
        for i, f in enumerate(members):
            sub = (f"L{rows[f]['start_line']} · {indeg[f]} callers"
                   if ctype == "database" else f"L{rows[f]['start_line']}")
            ids.append(add_node(f, ctype, sub, r + i // n_cols, i % n_cols))
        if len(members) >= 2:
            boundaries.append({"kind": "region", "label": label, "wraps": ids})
        r += (len(members) + n_cols - 1) // n_cols + 1

    fpath = rows[defs[0]]["file_path"].replace("\\", "/") if defs else "?"
    ir = {
        "schema_version": 1, "diagram_type": "architecture",
        "meta": {"title": f"{fpath} symbol roles", "output": "s3.html"},
        "layout": {"mode": "grid", "cols": n_cols, "origin": [40, 80],
                   "gapX": 24, "gapY": 18, "cellW": 130, "cellH": 44},
        "components": components,
        "boundaries": boundaries,
        "connections": [],
    }
    return ir, sym


# ---------------------------------------------------------------- 代码页


_PY_KW = frozenset(
    "def class return if elif else for while try except finally with as import from "
    "pass break continue raise yield lambda global nonlocal assert del in is not and or "
    "None True False async await match case".split())


def _hl_segments(line: str, in_tri: str) -> tuple[list[tuple[str, str]], str]:
    """一行 Python → [(text, cls)]；in_tri 为跨行三引号（''' 或 \"\"\"）。"""
    segs: list[tuple[str, str]] = []
    i, n = 0, len(line)
    if in_tri:
        j = line.find(in_tri)
        if j < 0:
            return [(line, "s")], in_tri
        segs.append((line[:j + 3], "s"))
        i = j + 3
        in_tri = ""
    while i < n:
        ch = line[i]
        if line.startswith('"""', i) or line.startswith("'''", i):
            q = line[i:i + 3]
            j = line.find(q, i + 3)
            if j < 0:
                segs.append((line[i:], "s"))
                return segs, q
            segs.append((line[i:j + 3], "s"))
            i = j + 3
        elif ch == "#":
            segs.append((line[i:], "c"))
            i = n
        elif ch in "\"'":
            j = i + 1
            while j < n and line[j] != ch:
                if line[j] == "\\":
                    j += 1
                j += 1
            segs.append((line[i:j + 1], "s"))
            i = j + 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            w = line[i:j]
            prev = next((t.strip() for t, _ in reversed(segs) if t.strip()), "")
            if w in _PY_KW:
                segs.append((w, "k"))
            elif prev in ("def", "class"):
                segs.append((w, "f"))
            elif prev == "@":
                segs.append((w, "d"))
            else:
                segs.append((w, ""))
            i = j
        elif ch.isdigit():
            j = i
            while j < n and (line[j].isalnum() or line[j] == "."):
                j += 1
            segs.append((line[i:j], "n"))
            i = j
        else:
            j = i
            while j < n and not (line[j].isalpha() or line[j] == "_"
                                 or line[j].isdigit() or line[j] in "#'\""):
                j += 1
            segs.append((line[i:j], ""))
            i = j
    return segs, in_tri


_FROM_JS = (
    "<script>(function(){var m=/[?&]from=([A-Za-z0-9-]+)/.exec(location.search);"
    "if(!m)return;var c=document.querySelector('.crumbs');if(!c)return;"
    "var a=document.createElement('a');a.className='cb';a.href=m[1]+'.html';"
    "a.textContent='\\u2190 Back';c.insertBefore(a,c.firstChild);})();</script>")


def _path_ok(fp: str) -> bool:
    """文件路径守卫（normrel 战果 #59/#60 同族）：绝对/含 ':'/'..'/尖括号
    synthetic 全拒。"""
    return not (not fp or ":" in fp or ".." in fp
                or fp.startswith(("/", "<", ">")) or os.path.isabs(fp))


def _code_page_doc(title: str, h1: str, meta: str, rows_html: list[str],
                   chips_html: str, crumbs: list[tuple[str | None, str]],
                   action: tuple[str, str] | None = None) -> str:
    """代码页 HTML 骨架（唯一代码视图 = 整文件源码页）：

    archify 令牌（#020617/#1e293b/#22d3ee，正文无衬线 + 代码区等宽）+
    统一顶部面包屑条（sticky，右侧上下文动作）+ 大纲 chips +
    符号锚点代码区（.sym :target 高亮）+ ?from= 「← 返回」。
    """
    cs: list[str] = []
    for i, (href, label) in enumerate(crumbs):
        esc = html.escape(label, quote=True)
        if i == len(crumbs) - 1 or href is None:
            cs.append(f'<span class="cb{" cur" if i == len(crumbs) - 1 else ""}">{esc}</span>')
        else:
            cs.append(f'<a class="cb" href="{html.escape(href, quote=True)}">{esc}</a>')
    act = (f'<a href="{html.escape(action[0], quote=True)}">'
           f'{html.escape(action[1])} &#8594;</a>' if action else "")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>"
        "body{margin:0;background:#020617;color:#cbd5e1;"
        "font:13px/1.6 system-ui,'Segoe UI',sans-serif}"
        ".topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;"
        "gap:16px;background:rgba(2,6,23,.94);border-bottom:1px solid #1e293b;"
        "padding:10px 20px;backdrop-filter:blur(6px)}"
        ".crumbs{display:flex;align-items:center;gap:6px;overflow:hidden;"
        "white-space:nowrap;font:13px system-ui,sans-serif}"
        ".cb{color:#94a3b8;text-decoration:none}.cb:hover{color:#67e8f9}"
        ".cb.cur{color:#e2e8f0;font-weight:600}"
        ".sep{color:#334155}"
        ".act{flex:1;text-align:right}.act a{color:#04121f;background:#22d3ee;"
        "border-radius:999px;padding:5px 12px;text-decoration:none;font-weight:600;"
        "white-space:nowrap}.act a:hover{background:#67e8f9}"
        "main{padding:14px 20px 60px;max-width:1100px}"
        "h1{margin:8px 0 2px;font:600 15px ui-monospace,'JetBrains Mono',Consolas,monospace;"
        "color:#f1f5f9}"
        ".meta{color:#94a3b8;font:12px system-ui,sans-serif}"
        ".chips{margin-top:8px}"
        ".sec{color:#64748b;font:11px system-ui,sans-serif;margin-right:8px}"
        ".chip{background:#0b1226;border:1px solid #1e293b;color:#67e8f9;padding:3px 10px;"
        "border-radius:999px;font:12px system-ui,sans-serif;text-decoration:none;"
        "margin:0 6px 4px 0;display:inline-block}.chip:hover{border-color:#22d3ee}"
        ".code{margin-top:10px;background:#0b1226;border:1px solid #1e293b;"
        "border-radius:10px;padding:10px 0;font:12.5px/1.62 ui-monospace,"
        "'JetBrains Mono',Consolas,monospace;overflow-x:auto}"
        ".row{display:flex;min-width:max-content}.row:hover{background:#0e1730}"
        ".ln{flex:0 0 56px;text-align:right;padding-right:16px;color:#475569;"
        "user-select:none}.lc{white-space:pre;flex:1}"
        ".k{color:#c678dd}.s{color:#98c379}.c{color:#7f848e;font-style:italic}"
        ".n{color:#d19a66}.f{color:#67e8f9}.d{color:#e5c07b}"
        ".cl{color:#67e8f9;text-decoration:none}.cl:hover{text-decoration:underline;"
        "color:#22d3ee}"
        ".sym{border-left:2px solid #1e293b;padding-left:10px;margin-left:24px;"
        "border-radius:0 8px 8px 0;scroll-margin-top:120px}"
        ".sym:target{background:rgba(34,211,238,.06);border-left-color:#22d3ee}"
        ".rel{display:flex;flex-wrap:wrap;gap:2px 10px;align-items:center;"
        "padding:6px 10px 6px 0;margin:4px 0}"
        "</style></head><body>"
        '<div class="topbar"><div class="crumbs">'
        + '<span class="sep">&#8250;</span>'.join(cs)
        + '</div><div class="act">' + act + "</div></div>"
        "<main>"
        f"<h1>{html.escape(h1)}</h1>"
        f'<div class="meta">{html.escape(meta)}</div>'
        f"{chips_html}"
        '<div class="code">' + "".join(rows_html) + "</div>"
        + _FROM_JS +
        "</main></body></html>")


def write_file_page(out_dir: Path, lines: list[str], fpath: str,
                    crumbs: list[tuple[str | None, str]],
                    action: tuple[str, str] | None = None,
                    sindex: "SymbolIndex | None" = None,
                    notes: list[str] | None = None) -> str:
    """整文件源码页（唯一代码视图）：行号 + 高亮 + 符号锚点。

    lines 为已读入的源码行（build_chain 统一预读，含守卫/缺失处理）；
    超过 CHAIN_FILE_LINES_CAP 截断并注记。有符号索引时：符号行区间包裹
    为锚点区块（#sym-*，CSS :target 高亮），符号起始处插关系条
    （调用/被调/内含 chips），页首大纲 chips，代码内同文件唯一符号名 →
    页内锚点链接；嵌套符号按行区间栈式嵌套渲染。返回输出文件名。
    """
    lines_truncated = len(lines) > CHAIN_FILE_LINES_CAP
    src = lines[:CHAIN_FILE_LINES_CAP]
    while src and src[-1] == "":
        src.pop()
    n_lines = len(src)

    syms: list[tuple[int, int, str]] = []
    if sindex is not None:
        for fid in sindex.by_file.get(fpath, []):
            r = sindex.rows[fid]
            a = int(r["start_line"] or 0)
            b = int(r["end_line"] or 0) or a
            if 1 <= a <= n_lines:
                syms.append((a, max(b, a), fid))
        syms.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    sym_at: dict[int, list[str]] = defaultdict(list)
    end_of: dict[str, int] = {}
    for a, _b, fid in syms:
        sym_at[a].append(fid)
        end_of[fid] = _b

    this_page = file_page_stem_of(fpath) + ".html"

    def rel_bar(fid: str) -> str:
        secs: list[str] = []
        for label, ids in (("calls", sindex.callees.get(fid, [])),
                           ("called by", sindex.callers.get(fid, [])),
                           ("contains", sindex.children.get(fid, []))):
            chips: list[str] = []
            seen: set[str] = set()
            for x in ids:
                if x in seen or x not in sindex.href:
                    continue
                seen.add(x)
                href = sindex.href[x]
                if href.startswith(this_page + "#"):  # 同页 → 页内锚点，避免 URL 抖动
                    href = href[len(this_page):]
                chips.append(f'<a class="chip" href="{html.escape(href, quote=True)}">'
                             f'{html.escape(str(sindex.rows[x]["name"]))}</a>')
            if chips:
                secs.append(f'<span class="sec">{label}</span>' + "".join(chips))
        return f'<div class="rel">{"".join(secs)}</div>' if secs else ""

    in_tri = ""
    rows_html: list[str] = []
    stack: list[str] = []
    for off, line in enumerate(src, 1):
        while stack and end_of[stack[-1]] < off:
            stack.pop()
            rows_html.append("</div>")
        for fid in sym_at.get(off, []):
            rows_html.append(f'<div class="sym" id="{html.escape(sindex.anchor[fid], quote=True)}">')
            rb = rel_bar(fid)
            if rb:
                rows_html.append(rb)
            stack.append(fid)
        segs, in_tri = _hl_segments(line, in_tri)
        parts: list[str] = []
        for t, c in segs:
            esc = html.escape(t)
            link = (sindex.name_link.get((fpath, t))
                    if (sindex is not None and c == "") else None)
            if link is not None:
                parts.append(f'<a class="cl" href="#{html.escape(sindex.anchor[link], quote=True)}">'
                             f"{esc}</a>")
            elif c:
                parts.append(f'<span class="{c}">{esc}</span>')
            else:
                parts.append(esc)
        rows_html.append(f'<div class="row"><span class="ln">{off}</span>'
                         f'<span class="lc">{"".join(parts) or "&nbsp;"}</span></div>')
    while stack:
        stack.pop()
        rows_html.append("</div>")

    chips_html = ""
    if syms:
        chips = [f'<a class="chip" href="#{html.escape(sindex.anchor[fid], quote=True)}">'
                 f'{html.escape(str(sindex.rows[fid]["name"]))}</a>'
                 for _a, _b, fid in syms]
        chips_html = ('<div class="chips"><span class="sec">Outline</span>'
                      + "".join(chips) + "</div>")
    meta_bits = [f"L1-L{n_lines} · {len(lines)} lines"]
    if lines_truncated:
        meta_bits.append(f"truncated at {CHAIN_FILE_LINES_CAP} lines")
    if syms:
        meta_bits.append(f"{len(syms)} symbols")
    if notes:
        meta_bits.extend(notes)
    meta = " · ".join(meta_bits)
    doc = _code_page_doc(fpath, fpath, meta, rows_html, chips_html, crumbs, action)
    (out_dir / f"{file_page_stem_of(fpath)}.html").write_text(doc, encoding="utf-8")
    return this_page


class SymbolIndex:
    """全局符号索引（跨模块；代码层唯一视图的支撑）：

    真实符号 fid → 整文件页（s4f-*.html）/ 页内锚点（#sym-{slug}，页内
    同名按出现序加 -2/-3 后缀去重）/ 完整 href（page#anchor）；
    调用/被调（CALLS/CALL_REFERENCE 边）；内含（行区间包含推导最近
    包围符号，一般仓的类→方法）；同文件唯一名 → fid（代码内标识符
    链接，重名不链接，诚实优先）。

    fid_ok 过滤（生产化）：只收录「文件页已落盘且起始行 ≤ 截断帽」的
    符号 → 下游 chips/clicks 天然不指向缺失页/缺失锚点（三段式 Pass2）。
    """

    def __init__(self, rows: dict[str, sqlite3.Row], fids: list[str],
                 edges: list[tuple[str, str, str]],
                 fid_ok: set[str] | None = None) -> None:
        self.rows = rows
        self.by_file: dict[str, list[str]] = defaultdict(list)
        self.anchor: dict[str, str] = {}
        self.href: dict[str, str] = {}
        used: dict[str, int] = {}
        for fid in fids:
            r = rows.get(fid)
            if r is None or not r["start_line"] or r["label"] in ("File", "Module"):
                continue
            if fid_ok is not None and fid not in fid_ok:
                continue
            fpath = _norm(r["file_path"])
            self.by_file[fpath].append(fid)
            base = f"sym-{slug(str(r['name']))}"
            k = used.get(base, 0)
            used[base] = k + 1
            self.anchor[fid] = base if k == 0 else f"{base}-{k + 1}"
            self.href[fid] = (file_page_stem_of(fpath) + ".html#"
                              + self.anchor[fid])
        for fl in self.by_file.values():
            fl.sort(key=lambda f: (int(rows[f]["start_line"]),
                                   -(int(rows[f]["end_line"] or 0))))
        self.callees: dict[str, list[str]] = defaultdict(list)
        self.callers: dict[str, list[str]] = defaultdict(list)
        for a, b, t in edges:
            if t in ("CALLS", "CALL_REFERENCE") and a in self.anchor and b in self.anchor:
                self.callees[a].append(b)
                self.callers[b].append(a)
        self.children: dict[str, list[str]] = defaultdict(list)
        for fl in self.by_file.values():
            for g in fl:
                ga, gb = int(rows[g]["start_line"]), int(rows[g]["end_line"] or 0)
                if not gb:
                    continue
                best, best_span = None, None
                for f in fl:
                    if f == g:
                        continue
                    fa, fb = int(rows[f]["start_line"]), int(rows[f]["end_line"] or 0)
                    if not fb or fa > ga or gb > fb:
                        continue
                    span = fb - fa
                    if best is None or span < best_span:
                        best, best_span = f, span
                if best is not None:
                    self.children[best].append(g)
            for v in self.children.values():
                v.sort(key=lambda f: int(rows[f]["start_line"]))
        # 同文件唯一名 → fid：代码内标识符链接（重名不链接，诚实优先）
        self.name_link: dict[tuple[str, str], str] = {}
        for fp, fl in self.by_file.items():
            cnt: dict[str, list[str]] = defaultdict(list)
            for fid in fl:
                cnt[str(rows[fid]["name"])].append(fid)
            for name, fl2 in cnt.items():
                if len(fl2) == 1:
                    self.name_link[(fp, name)] = fl2[0]


# ---------------------------------------------------------------- archify 子进程


def _render_diagram(mjs: Path, mod_staging: Path, ir: dict,
                    name: str, dtype: str) -> tuple[Path | None, str | None]:
    """单图：写 IR → validate → render 进模块暂存区。返回 (html, 错误)。"""
    ir_dir = mod_staging / "ir"
    ir_path = ir_dir / f"{name}.json"
    ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(mjs), "validate", dtype, str(ir_path), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ARCHIFY_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"archify validate error: {exc}"
    if proc.returncode != 0:
        return None, (f"archify validate failed: {name} rc={proc.returncode}"
                      f" {proc.stderr.strip()[:160]}")
    out_html = mod_staging / f"{name}.html"
    try:
        proc = subprocess.run(
            ["node", str(mjs), "render", dtype, str(ir_path), str(out_html)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=ARCHIFY_RENDER_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"archify render error: {exc}"
    if proc.returncode != 0 or not out_html.is_file():
        return None, (f"archify render failed: {name} rc={proc.returncode}"
                      f" {proc.stderr.strip()[-160:]}")
    return out_html, None


# ---------------------------------------------------------------- 模块链规划


def _plan_module(module_id: str, fids: list[str], rows: dict[str, sqlite3.Row],
                 edges: list[tuple[str, str, str]]) -> dict | None:
    """模块链规划（Pass1a）：入口分流 + 页清单 + 每页 IR/crumbs 元数据。

    返回 None = 模块无内容（0 fid / 无文件视图 / 无入口），调用方弃链。
    diagrams: [{name, dtype, ir, crumbs, action, clicks | fid_clicks}]
    """
    fs_view = module_fs(fids, rows)
    files_by_dir, subdirs, agg = fs_view
    real = real_by_file(rows, fids)
    all_dirs = sorted({d for ds in subdirs.values() for d in ds})
    n_files = sum(len(v) for v in files_by_dir.values())
    if not fids or n_files == 0:
        return None
    ms = slug(module_id)
    s1_stem = f"s1-{ms}-dataflow"
    s2a_stem = f"s2a-{ms}-overview"
    have_s1 = 2 <= n_files <= 8 and bool(real)
    have_s2a = len(all_dirs) > 1
    dir_pages = set(all_dirs)
    if (len(all_dirs) == 1 and len(files_by_dir.get(all_dirs[0], {})) == 1
            and not subdirs.get(all_dirs[0])):
        dir_pages = set()
    single_real = next(iter(real)) if len(real) == 1 else None
    if have_s1:
        entry_stem, kind = s1_stem, "dataflow"
    elif have_s2a:
        entry_stem, kind = s2a_stem, "dir overview"
    elif single_real:
        entry_stem, kind = grid_stem_of(module_id, single_real), "symbol grid"
    elif dir_pages:
        entry_stem = dir_page_stem(module_id, sorted(dir_pages)[0])
        kind = "directory"
    else:
        # 单目录单文件无子目录（目录页冗余被跳过）→ 入口 = 该文件整文件页
        the_file = next(f for d in sorted(files_by_dir) for f in sorted(files_by_dir[d]))
        entry_stem, kind = file_page_stem_of(the_file), "source"

    def base_crumbs() -> list[tuple[str | None, str]]:
        return [("index.html", "home"), (entry_stem + ".html", module_id)]

    def dir_segs(d: str) -> list[tuple[str | None, str]]:
        """目录路径 → 逐段面包屑（有目录页的段可点，末段由渲染层置当前）。"""
        out: list[tuple[str | None, str]] = []
        acc = ""
        for p in [x for x in d.split("/") if x]:
            acc = f"{acc}/{p}" if acc else p
            if acc in dir_pages:
                out.append((dir_page_stem(module_id, acc) + ".html", p))
            else:
                out.append((None, p))
        return out

    diagrams: list[dict] = []

    # S1 模块 dataflow（入口）：节点 → 代码页锚点；动作 → 目录下钻
    if have_s1:
        ir, id_of = s1_module_dataflow(fids, rows, edges, module_id)
        if len(dir_pages) == 1:
            act = (dir_page_stem(module_id, next(iter(dir_pages))) + ".html", "drill into dirs")
        elif have_s2a:
            act = (s2a_stem + ".html", "drill into dirs")
        else:
            act = None
        diagrams.append({"name": s1_stem, "dtype": "dataflow", "ir": ir,
                         "crumbs": base_crumbs(), "action": act,
                         "fid_clicks": dict(id_of)})

    # S2a 目录总览（多目录）：节点 → 目录页；动作 → 模块数据流
    if have_s2a:
        ir2a, clicks2a = s2a_overview(fs_view, module_id, module_id)
        crumbs = (base_crumbs() if entry_stem == s2a_stem
                  else base_crumbs() + [(None, "directory overview")])
        act = (s1_stem + ".html", "module dataflow") if have_s1 else None
        diagrams.append({"name": s2a_stem, "dtype": "tree", "ir": ir2a,
                         "crumbs": crumbs, "action": act, "clicks": clicks2a})

    # S2 目录页：子目录 → 目录页；文件 → 符号网格（真实符号）或整文件页
    for d in sorted(dir_pages):
        ir, clicks = build_dir_tree(
            fs_view, module_id, d,
            file_page=lambda dd, f_: (
                (grid_stem_of(module_id, f"{dd}/{f_}") + ".html")
                if f"{dd}/{f_}" in real
                else (file_page_stem_of(f"{dd}/{f_}") + ".html")))
        crumbs = base_crumbs() + dir_segs(d)
        act = (s1_stem + ".html", "module dataflow") if have_s1 else None
        diagrams.append({"name": dir_page_stem(module_id, d), "dtype": "tree",
                         "ir": ir, "crumbs": crumbs, "action": act, "clicks": clicks})

    # S3 网格（有真实符号的文件）：节点 → 代码页锚点；动作 → 整文件源码
    for fpath in sorted(real):
        gstem = grid_stem_of(module_id, fpath)
        s4f_html = file_page_stem_of(fpath) + ".html"
        d_ = fpath.rsplit("/", 1)[0] if "/" in fpath else ""
        fname = fpath.rsplit("/", 1)[-1]
        rf = real[fpath]
        fs = set(rf)
        ee = [(a, b, t) for a, b, t in edges if a in fs and b in fs]
        ir, syms = build_file_grid(rows, rf, ee)
        diagrams.append({"name": gstem, "dtype": "architecture", "ir": ir,
                         "crumbs": base_crumbs() + dir_segs(d_) + [(None, fname)],
                          "action": (s4f_html, "full file source"),
                         "fid_clicks": {nid: fid for nid, fid in syms.items()}})

    # 文件页清单（Pass2 归属去重后写）：真实文件 → 网格段面包屑；其余 → 纯源码
    file_crumbs: dict[str, list[tuple[str | None, str]]] = {}
    for d_ in sorted(files_by_dir):
        for fname in sorted(files_by_dir[d_]):
            fpath = f"{d_}/{fname}" if d_ not in (".", "") else fname
            if fpath in real:
                gstem = grid_stem_of(module_id, fpath)
                file_crumbs[fpath] = (base_crumbs() + dir_segs(d_)
                                      + [(gstem + ".html", fname), (None, "source")])
            else:
                file_crumbs[fpath] = base_crumbs() + dir_segs(d_) + [(None, fname)]

    return {
        "module_id": module_id,
        "fids": fids,
        "edges": edges,
        "entry_stem": entry_stem,
        "kind": kind,
        "n_files": n_files,
        "n_syms": sum(len(v) for v in real.values()),
        "n_dirs": len(all_dirs),
        "diagrams": diagrams,
        "file_crumbs": file_crumbs,
    }


# ---------------------------------------------------------------- build_chain


def build_chain(repo: Path, out_chain: Path, mjs: Path) -> dict:
    """模块交互图链总入口（三段式原子提交）。

    返回 {ok, index, cards, counts, notes, file_pages}：
      ok         至少一个模块全链成功；
      index      chain/index.html 路径（ok=False 时为 None）；
      cards      模块卡片（index.html 数据）；
      counts     {modules, diagrams, file_pages, pages}；
      notes      诚实注记（弃链模块原因、帽截断）；
      file_pages {规范文件路径 → 页文件名}（report findings 链接用）。
    ok=False 时调用方降级 L1/L2（Mermaid），chain/ 已清理。
    """
    repo = Path(repo)
    out_chain = Path(out_chain)
    _reset_state()
    notes: list[str] = []
    result = {"ok": False, "index": None, "cards": [], "file_pages": {},
              "counts": {"modules": 0, "diagrams": 0, "file_pages": 0, "pages": 0},
              "notes": notes}

    if shutil.which("node") is None:
        notes.append("node not found in PATH - module chain skipped")
        return result
    db = repo / ".vrp" / "facts.db"
    if not db.exists():
        notes.append("facts.db missing - module chain skipped")
        return result
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        mods = _load_structure_modules(con)
        if mods is None:
            notes.append("structure inactive/empty - module chain skipped")
            return result
        if len(mods) > CHAIN_MODULES_CAP:
            notes.append(f"module count {len(mods)} over cap {CHAIN_MODULES_CAP}, "
                         f"only first {CHAIN_MODULES_CAP} chained (truncated)")
            mods = mods[:CHAIN_MODULES_CAP]
        plans = []
        for m in mods:
            if not isinstance(m, dict) or not m.get("module_id"):
                continue
            fids = [f for f in m.get("symbol_fids", []) if isinstance(f, str)]
            if not fids:
                notes.append(f"module {m.get('module_id')} has no symbol fids, dropped from chain")
                continue
            plans.append((str(m["module_id"]), fids))
        if not plans:
            notes.append("no chainable modules")
            return result
        all_fids: list[str] = []
        seen: set[str] = set()
        for _mid, fids in plans:
            for f in fids:
                if f not in seen:
                    seen.add(f)
                    all_fids.append(f)
        rows, edges = _load_rows_edges(con, all_fids)
        mod_data = []
        for mid, fids in plans:
            fset = set(fids)
            mod_edges = [(a, b, t) for a, b, t in edges if a in fset and b in fset]
            mod_data.append((mid, fids, mod_edges))
    finally:
        con.close()

    staging = out_chain / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    ok_plans: list[tuple[dict, Path]] = []  # (plan, mod_staging)
    try:
        # ---- Pass 1：规划 + 逐模块渲染（模块原子性：任一图失败整模块弃链）
        budget = 0
        for mid, fids, mod_edges in mod_data:
            plan = _plan_module(mid, fids, rows, mod_edges)
            if plan is None:
                notes.append(f"module {mid} has no file view, dropped from chain")
                continue
            est = len(plan["diagrams"])
            if budget + est > CHAIN_DIAGRAMS_GLOBAL_CAP:
                notes.append(f"module {mid} needs {est} diagrams, site-wide budget "
                             f"{CHAIN_DIAGRAMS_GLOBAL_CAP} with {CHAIN_DIAGRAMS_GLOBAL_CAP - budget} left, dropped from chain")
                continue
            mod_staging = staging / f"m{len(ok_plans)}"
            (mod_staging / "ir").mkdir(parents=True, exist_ok=True)
            failed: str | None = None
            for d in plan["diagrams"]:
                html, err = _render_diagram(mjs, mod_staging, d["ir"], d["name"], d["dtype"])
                if err is not None:
                    failed = err
                    break
            if failed is not None:
                notes.append(f"module {mid} dropped from chain: {failed}")
                shutil.rmtree(mod_staging, ignore_errors=True)
                continue
            budget += est
            ok_plans.append((plan, mod_staging))

        if not ok_plans:
            notes.append("all module diagrams failed to render, module chain degraded")
            return result

        # ---- Pass 2：文件页归属 + 预读源码 + 全局符号索引 + 落盘
        out_chain.mkdir(parents=True, exist_ok=True)
        (out_chain / "ir").mkdir(parents=True, exist_ok=True)
        written: dict[str, list[str]] = {}  # 规范 fpath → 源码行（含守卫/缺失注记）
        file_notes: dict[str, list[str]] = {}
        file_crumbs_of: dict[str, list[tuple[str | None, str]]] = {}
        file_owner: dict[str, str] = {}
        for plan, _stg in ok_plans:
            for fpath in sorted(plan["file_crumbs"]):
                if fpath in written:
                    continue
                if not _path_ok(fpath):
                    lines: list[str] = []
                    fnote = ["path rejected by guard (absolute / .. / synthetic)"]
                else:
                    try:
                        lines = (repo / fpath).read_text(
                            encoding="utf-8", errors="replace").split("\n")
                        fnote = []
                    except OSError:
                        lines = []
                        fnote = ["file missing or unreadable"]
                written[fpath] = lines
                file_notes[fpath] = fnote
                file_crumbs_of[fpath] = plan["file_crumbs"][fpath]
                file_owner[fpath] = plan["module_id"]
                if len(written) > CHAIN_FILE_PAGES_CAP * len(ok_plans):
                    break
        # 每模块文件页帽：归属后再按模块计数裁剪（超出者从 written 剔除）
        per_mod: dict[str, list[str]] = {}
        for fpath in sorted(written):
            per_mod.setdefault(file_owner[fpath], []).append(fpath)
        dropped_files: set[str] = set()
        for owner, fpaths in per_mod.items():
            if len(fpaths) > CHAIN_FILE_PAGES_CAP:
                for fpath in fpaths[CHAIN_FILE_PAGES_CAP:]:
                    dropped_files.add(fpath)
        for fpath in dropped_files:
            del written[fpath]
            del file_notes[fpath]
            del file_crumbs_of[fpath]
            del file_owner[fpath]
        if dropped_files:
            notes.append(f"{len(dropped_files)} files over per-module file page cap "
                         f"{CHAIN_FILE_PAGES_CAP}, no code pages emitted (truncated)")

        fid_ok: set[str] = set()
        for fpath, lines in written.items():
            cap = min(CHAIN_FILE_LINES_CAP, max(0, len(lines) - (1 if lines and lines[-1] == "" else 0)))
            for plan, _stg in ok_plans:
                for fid in plan["fids"]:
                    r = rows.get(fid)
                    if (r is not None and _norm(r["file_path"]) == fpath
                            and r["start_line"] and 1 <= int(r["start_line"]) <= cap
                            and r["label"] not in ("File", "Module")):
                        fid_ok.add(fid)
        sindex = SymbolIndex(rows, all_fids, edges, fid_ok=fid_ok)

        n_pages = 0
        for fpath in sorted(written):
            write_file_page(out_chain, written[fpath], fpath,
                            file_crumbs_of[fpath], sindex=sindex,
                            notes=file_notes[fpath] or None)
            n_pages += 1
        n_file_pages = len(written)

        for plan, mod_staging in ok_plans:
            for d in plan["diagrams"]:
                staged = mod_staging / f"{d['name']}.html"
                if "clicks" in d:
                    clicks = d["clicks"]
                else:
                    clicks = {nid: sindex.href[fid]
                              for nid, fid in d.get("fid_clicks", {}).items()
                              if fid in sindex.href}
                inject_chrome(staged, clicks, d["crumbs"], d.get("action"),
                              page=d["name"])
                shutil.move(str(staged), str(out_chain / f"{d['name']}.html"))
                ir_src = mod_staging / "ir" / f"{d['name']}.json"
                if ir_src.is_file():
                    shutil.move(str(ir_src), str(out_chain / "ir" / f"{d['name']}.json"))
                n_pages += 1
            shutil.rmtree(mod_staging, ignore_errors=True)

        # ---- Pass 3：index 总入口
        cards = [{
            "module_id": plan["module_id"],
            "entry": plan["entry_stem"] + ".html",
            "kind": plan["kind"],
            "n_files": plan["n_files"],
            "n_syms": plan["n_syms"],
            "n_dirs": plan["n_dirs"],
        } for plan, _stg in ok_plans]
        write_index(out_chain, cards)
        n_pages += 1

        result.update({
            "ok": True,
            "index": str(out_chain / "index.html"),
            "cards": cards,
            "file_pages": {fp: file_page_stem_of(fp) + ".html" for fp in written},
            "counts": {
                "modules": len(ok_plans),
                "diagrams": sum(len(p["diagrams"]) for p, _ in ok_plans),
                "file_pages": n_file_pages,
                "pages": n_pages,
            },
        })
        return result
    finally:
        if not result.get("ok"):
            shutil.rmtree(out_chain, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)


def write_index(out_dir: Path, cards: list[dict]) -> None:
    """总入口 index.html：archify 令牌 + 模块卡片（名/类型/规模/入口）。"""
    items = []
    for c in cards:
        items.append(
            f'<a class="card" href="{html.escape(c["entry"], quote=True)}">'
            f'<div class="k">{html.escape(c["kind"])}</div>'
            f'<h2>{html.escape(c["module_id"])}</h2>'
            f'<div class="s">{c["n_files"]} files · {c["n_syms"]} symbols · {c["n_dirs"]} dirs</div>'
            "</a>")
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>Module interaction chain · index</title><style>"
        "body{margin:0;background:#020617;color:#e2e8f0;"
        "font:14px/1.6 system-ui,'Segoe UI',sans-serif;padding:48px 24px}"
        ".wrap{max-width:1080px;margin:0 auto}"
        "h1{font:600 22px system-ui,sans-serif;margin:0 0 4px}"
        ".sub{color:#94a3b8;font:13px system-ui,sans-serif;margin-bottom:28px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}"
        ".card{display:block;background:#0b1226;border:1px solid #1e293b;"
        "border-radius:14px;padding:18px 18px 16px;text-decoration:none;color:#e2e8f0;"
        "transition:border-color .15s}"
        ".card:hover{border-color:#22d3ee}"
        ".k{display:inline-block;color:#67e8f9;background:rgba(34,211,238,.08);"
        "border:1px solid rgba(34,211,238,.25);border-radius:999px;padding:2px 10px;"
        "font:11px system-ui,sans-serif;margin-bottom:10px}"
        "h2{margin:0 0 6px;font:600 16px system-ui,sans-serif;color:#f1f5f9}"
        ".s{color:#94a3b8;font:12px system-ui,sans-serif}"
        "</style></head><body><div class=\"wrap\">"
        "<h1>Module interaction chain</h1>"
        f'<div class="sub">{len(cards)} modules · '
        f'{sum(c["n_files"] for c in cards)} files · '
        "click a module to enter its chain; click a node to select, click again to drill down to code</div>"
        '<div class="grid">' + "".join(items) + "</div></div></body></html>")
    (out_dir / "index.html").write_text(doc, encoding="utf-8")
