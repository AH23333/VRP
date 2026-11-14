"""R7'(2) 模块交互图链：紧凑层次化可下钻页面链（替代 Mermaid L1/L2）。

  架构（探针 15 移植 + 生产化守卫）：
  index.html 总入口（v8 反馈轮：按层分组 + 业务线区 + layered
  architecture 入口）→ arch.html 全局分层总览（层=边界横排模块、
  跨模块连线；点模块进树页）→ 每模块一条链，按数据形状分流入口：
    2-8 文件且有真实符号 → 模块 dataflow（s1-{模块}-dataflow.html）；
    多目录 → 目录总览树（s2a-{模块}-overview.html）；
    其余 → 目录页 / 单文件符号网格（s3-{模块}-{文件}.html）。
  代码层（唯一代码视图）= 整文件源码页 s4f-*.html + 符号锚点：
    符号行区间 → #sym-{slug} 锚点区块（CSS :target 高亮，零 JS）；
    符号起始处关系条（调用/被调/内含 chips）；页首大纲 chips；
    代码内同文件唯一符号名 → 页内锚点链接；
    嵌套符号（类→方法）按行区间栈式嵌套渲染。
  业务线（slices，v8）：图页 chips 可点高亮（?slice=，命中橙底辉光+
    非命中暗化）+ ↗ 专页；sl-{slice}.html 业务线专页（步骤序列一列
    到底，每步直达代码锚点）；代码页 ?slice= 常驻条（首命中/专页/
    清除）。
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
from collections import defaultdict
from pathlib import Path, PurePosixPath
from . import dbpath
from . import procs

ARCHIFY_TIMEOUT = 300
ARCHIFY_RENDER_TIMEOUT = 900
FID_CHUNK = 500
CHAIN_MODULES_CAP = 30
CHAIN_FILE_PAGES_CAP = 400
CHAIN_FILE_LINES_CAP = 4000
CHAIN_DIAGRAMS_GLOBAL_CAP = 240
REL_EXT_CAP = 8  # rel 条链外对端 chips 封顶（超出 +N more）

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


def slice_page_stem(sid: str) -> str:
    """业务线专页 stem：sl-{slice id slug}（journey 页，v8 反馈轮）。"""
    return f"sl-{slug(sid)}"


# ---------------------------------------------------------------- 数据加载


def _norm(fp: str) -> str:
    return str(fp).replace("\\", "/")


def _load_structure(con: sqlite3.Connection):
    """structure（modules, layers, stale）（active 优先；无 active 退最新
    stale 保底——v8 裁决：stale 结构仍可下钻，诚实注记随链透出）；
    缺表（pre-R2' 旧库）/空 modules → None（调用方降级 L1/L2）。

    返回 layers 一并透出（index 按层序排序/层徽标）；模块 dict 自带
    responsibility/layer 等 AI 梳理字段，调用方负责透传到用户产物。
    """
    try:
        row = con.execute(
            "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stale = False
        if row is None:
            row = con.execute(
                "SELECT payload_json FROM structure WHERE status = 'stale' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            stale = row is not None
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    mods = payload.get("modules") if isinstance(payload, dict) else None
    if not mods:
        return None
    layers = [l for l in (payload.get("layers") if isinstance(payload, dict) else []) or []
              if isinstance(l, dict) and l.get("name")]
    return mods, layers, stale


def _load_symbol_rows(con: sqlite3.Connection, fids: list[str],
                      rows: dict[str, sqlite3.Row]) -> None:
    """按 fid 批量补载符号行（原地并入 rows；FID_CHUNK 分块）。"""
    for i in range(0, len(fids), FID_CHUNK):
        chunk = fids[i:i + FID_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in con.execute(
            f"SELECT fid, name, label, file_path, start_line, end_line FROM symbols WHERE fid IN ({marks})",
            chunk,
        ):
            rows[r["fid"]] = r


def _load_full_file_fids(con: sqlite3.Connection,
                         fpaths: list[str]) -> list[str]:
    """链内文件的 DB 全量符号 fid（v8 用户反馈轮）。

    curated structure.symbol_fids 是 curate 为图挑的代表子集（本仓实测
    s0_precheck.py 只分到 1 个非 File 符号）——代码页锚点/大纲/调用关系
    需要整文件符号覆盖，否则函数不显示（用户实测 rel 条全空）。ORDER BY
    fid 保锚点分配顺序确定（复现契约）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(fpaths), FID_CHUNK):
        chunk = fpaths[i:i + FID_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in con.execute(
            f"SELECT fid FROM symbols WHERE file_path IN ({marks}) ORDER BY fid",
            chunk,
        ):
            if r["fid"] not in seen:
                seen.add(r["fid"])
                out.append(r["fid"])
    return out


def _load_rows_edges(con: sqlite3.Connection, all_fids: list[str],
                     any_endpoint: bool = False
                     ) -> tuple[dict[str, sqlite3.Row], list[tuple[str, str, str]]]:
    """全局符号行 + 模块内边（fid/边按 FID_CHUNK 分块查询）。

    any_endpoint（代码页调用关系用）：额外收录单端在集合内的
    CALLS/CALL_REFERENCE 边（对端可为链外符号，如测试文件/外部桩），
    供代码页 rel 条「调用/被调」外部命名展示；模块图/跨模块聚合等
    消费方自带双端过滤，语义不变。
    """
    rows: dict[str, sqlite3.Row] = {}
    _load_symbol_rows(con, all_fids, rows)
    fid_set = set(all_fids)
    edges: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for i in range(0, len(all_fids), FID_CHUNK):
        chunk = all_fids[i:i + FID_CHUNK]
        marks = ",".join("?" * len(chunk))
        params: list[str] = list(chunk)
        q = f"SELECT src_fid, dst_fid, type FROM edges WHERE src_fid IN ({marks})"
        if any_endpoint:
            q += (f" OR (dst_fid IN ({marks})"
                  " AND type IN ('CALLS','CALL_REFERENCE'))")
            params += chunk
        for a, b, t in con.execute(q, params):
            key = (a, b, t)
            if key in seen:
                continue
            seen.add(key)
            if a in fid_set and b in fid_set:
                edges.append(key)
            elif (any_endpoint and t in ("CALLS", "CALL_REFERENCE")
                    and (a in fid_set or b in fid_set)):
                edges.append(key)
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


def full_symbols_by_file(rows: dict[str, sqlite3.Row],
                         paths: set[str]) -> dict[str, list[str]]:
    """整文件 DB 全量真实符号按文件分组（v8 用户反馈二轮）。

    curated structure 是图为视角挑选的代表子集（实测 s0_precheck.py 只
    分到 1 个非 File 符号）；文件符号浏览器（网格节点/树与目录页计数）
    与文件内调用边推导需要全量，否则网格 1/13 符号与代码页矛盾。
    排除 File/Module 伪符号与无行号符号（与 real_by_file 同口径）；
    每文件序=(start_line, fid)，确定性（复现契约）。
    """
    out: dict[str, list[str]] = defaultdict(list)
    for f, r in rows.items():
        if (r["label"] in ("File", "Module") or not r["start_line"]
                or not r["file_path"]):
            continue
        fp = _norm(r["file_path"])
        if fp in paths:
            out[fp].append(f)
    for fp in out:
        out[fp] = [f for _sl, f in sorted(
            (int(rows[f]["start_line"]), f) for f in out[fp])]
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
        "layout": {"direction": "right", "gapX": 26, "gapY": 10,
                    "nodeW": 84, "nodeMaxW": 180},
        "nodes": nodes,
    }
    return ir, clicks


def build_dir_tree(fs_view, module_id: str, dir_path: str, file_page=None,
                   sym_counts: dict[str, int] | None = None):
    """目录内部树下钻页：根=该目录，子节点=子目录（递归文件数）+ 文件。

    direction down：>=3 连续叶子由渲染器折为缩进列表（巨目录可读）。
    子目录节点点击 → 其内部树页；文件节点点击 → file_page（符号页）。
    sym_counts（v8 用户反馈二轮）：文件名 → 整文件全量真实符号数；
    给定时 "N symbols" 用它（curated 计数与网格/代码页矛盾），缺省回退
    原 curated fid 计数。
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
        n_syms = (sym_counts.get(fname, 0) if sym_counts is not None
                  else len(files[fname]))
        if n_syms > 1:
            n["sublabel"] = f"{n_syms} symbols"
        nodes.append(n)
        if file_page is not None:
            target = file_page(dir_path, fname)
            if target:
                clicks[nid] = target
    ir = {
        "schema_version": 1, "diagram_type": "tree",
        "meta": {"title": f"{module_id} · {dir_path}", "output": "dir.html"},
        # nodeMaxW 放宽：长文件名保持单行，堆叠列表行高不被两行标签抬高
        "layout": {"direction": "down", "nodeW": 150, "nodeMaxW": 270},
        "nodes": nodes,
    }
    return ir, clicks


def module_overview_tree(fs_view, module_id: str, real: dict[str, list[str]],
                         file_target, root_target: str | None = None):
    """模块文件总览树（v8 模块入口）：根=模块 · 文件数，子=文件（符号数）。

    学 s2a_overview（direction=right，文件竖排）；文件点击 → file_target
    （真实符号文件 → 符号网格，否则整文件页）；根点击 → root_target
    （模块 dataflow，v8：树根下钻进模块交互图）。
    返回 (IR, clicks, file_of)：file_of 为节点 id → 文件路径（业务线文件投影）。
    """
    files_by_dir, _subdirs, _agg = fs_view
    files = sorted({f"{d}/{fn}" if d not in (".", "") else fn
                    for d in files_by_dir for fn in files_by_dir[d]})
    nodes: list[dict] = [{
        "id": "root",
        "label": _fit(f"{module_id} · {len(files)} files", 24),
    }]
    clicks: dict[str, str] = {}
    file_of: dict[str, str] = {}
    for i, fp in enumerate(files):
        nid = f"f{i}"
        file_of[nid] = fp
        n: dict = {"id": nid, "label": _fit(fp.split("/")[-1], 18), "parent": "root"}
        n_syms = len(real.get(fp, []))
        if n_syms:
            n["sublabel"] = f"{n_syms} symbols"
        nodes.append(n)
        target = file_target(fp)
        if target:
            clicks[nid] = target
    if root_target:
        clicks["root"] = root_target
    ir = {
        "schema_version": 1, "diagram_type": "tree",
        "meta": {"title": f"{module_id} file overview", "output": "t.html"},
        "layout": {"direction": "right", "gapX": 26, "gapY": 10,
                    "nodeW": 84, "nodeMaxW": 180},
        "nodes": nodes,
    }
    return ir, clicks, file_of


def _load_slices(con: sqlite3.Connection,
                 rows: dict[str, sqlite3.Row]) -> dict[str, dict]:
    """业务线（slices 表，status 不设限——全量诚实呈现）：steps 的 fid 解析到
    链内已知符号才收录。返回 {slice_id: {...}}（id 序）。

    v8 反馈轮：透出 summary/status/steps/acts（业务线专页 journey 用）；
    steps 原样保留（含链外 fid，专页灰显诚实注记），fids 为链内可解析子集。
    """
    try:
        cur = con.execute(
            "SELECT slice_id, name, summary, payload_json, status"
            " FROM slices ORDER BY slice_id")
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict] = {}
    for sid, name, summary, pj, status in cur:
        try:
            payload = json.loads(pj) if pj else None
        except (json.JSONDecodeError, TypeError):
            continue
        steps = payload.get("steps") if isinstance(payload, dict) else None
        steps = [st for st in (steps or []) if isinstance(st, dict)]
        fids: list[str] = []
        for st in steps:
            fid = st.get("fid")
            if isinstance(fid, str) and fid in rows and fid not in fids:
                fids.append(fid)
        if not fids:
            continue
        acts = payload.get("acts") if isinstance(payload, dict) else None
        out[str(sid)] = {
            "name": str(name),
            "summary": str(summary or ""),
            "status": str(status or ""),
            "steps": steps,
            "acts": [a for a in (acts or []) if isinstance(a, dict)],
            "fids": fids,
        }
    return out


# ---------------------------------------------------------------- chrome 注入


def inject_chrome(html_path: Path, clicks: dict[str, str],
                  crumbs: list[tuple[str | None, str]],
                  action: tuple[str, str] | None = None,
                  page: str = "",
                  slices: list[dict] | None = None,
                  slice_nodes: dict[str, list[str]] | None = None) -> None:
    """渲染后注入统一 chrome（v8 交互版）。

    顶部面包屑条（首页 › 模块 › … › 当前 + 业务线 chips + 右侧上下文
    动作，archify 令牌）。交互（v8 裁决，学参考仓「点选再点下钻」但去
    缩放）：单击节点 = 选中（Archify.focus.set 邻域高亮，捕获阶段拦掉
    archify 自带 svg click 的 set+view.reveal 缩放）；下钻只走按钮——
    passport 卡片 CTA（Drill →）+ 节点旁 SVG 按钮（Drill ↘，Enter 可达）；
    无下级的节点选中只给提示。导航带 ?from= 页 stem（代码页据此显
    「← Back」；file:// 下 document.referrer 为空不可用）。

    slices: [{sid, name, applicable}] 业务线 chips（applicable 者可点，
    其余灰显）；slice_nodes: {sid: [节点 id]} 业务线在本页的命中——
    ?slice= 时节点/边橙色高亮 + chip 选中态。首次访问一次性提示条
    （sessionStorage）。
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
    chips: list[str] = []
    for s in slices or []:
        sid = html.escape(str(s["sid"]), quote=True)
        name = html.escape(str(s["name"]))
        jpage = slice_page_stem(str(s["sid"])) + ".html"
        if s.get("applicable"):
            chips.append(f'<a class="pchip" data-slice="{sid}" href="?slice={sid}">{name}</a>')
        else:
            chips.append(f'<span class="pchip poff" title="not on this page">{name}</span>')
        chips.append(f'<a class="pjmp" href="{html.escape(jpage, quote=True)}" '
                     f'title="slice journey">&#8599;</a>')
    if any(s.get("applicable") for s in (slices or [])):
        chips.append('<a class="pchip pclr" href="?">&#10005;</a>')
    parts.append(
        "<style>"
        ".ptop{position:fixed;top:14px;left:14px;z-index:9999;display:flex;"
        "align-items:center;gap:14px;max-width:calc(100vw - 28px);"
        "background:rgba(10,17,38,.92);border:1px solid #1e293b;border-radius:12px;"
        "padding:8px 14px;font:13px system-ui,sans-serif;color:#e2e8f0;"
        "box-shadow:0 4px 16px rgba(0,0,0,.4);backdrop-filter:blur(6px)}"
        ".pcrumbs{display:flex;align-items:center;gap:6px;overflow:hidden;"
        "white-space:nowrap}"
        ".pcb{color:#94a3b8;text-decoration:none}.pcb:hover{color:#67e8f9}"
        ".pcb.pcur{color:#e2e8f0;font-weight:600}"
        ".psep{color:#334155}"
        ".pchips{display:flex;align-items:center;gap:6px;overflow:hidden;"
        "white-space:nowrap;font-size:12px}"
        ".pchip{color:#67e8f9;background:rgba(34,211,238,.07);"
        "border:1px solid #1e293b;border-radius:999px;padding:2px 10px;"
        "text-decoration:none;white-space:nowrap}"
        ".pchip:hover{border-color:#22d3ee}"
        ".pchip.on{background:#fb923c;border-color:#fb923c;color:#0b1226;"
        "font-weight:600}"
        ".pchip.poff{color:#475569;background:none}"
        ".pchip.pclr{color:#f87171}"
        ".pjmp{color:#64748b;text-decoration:none;font-size:11px;"
        "margin-right:2px}.pjmp:hover{color:#fb923c}"
        ".pact{flex:0 0 auto}.pact a{color:#04121f;background:#22d3ee;"
        "border-radius:999px;padding:5px 12px;text-decoration:none;font-weight:600;"
        "white-space:nowrap}.pact a:hover{background:#67e8f9}"
        # v8 反馈轮：节点盒缩圆角 + 业务线高亮加强（橙底/描边/辉光）+
        # slice 模式非命中节点/边暗化聚焦
        "svg [data-node-id]>rect{rx:5px;ry:5px}"
        "svg [data-node-id]{transition:opacity .2s}"
        "g.v8hl>rect{stroke:#fb923c!important;stroke-width:2.5px!important}"
        "g.v8hl>rect:not(.c-mask){fill:rgba(251,146,60,.22)!important;"
        "filter:drop-shadow(0 0 6px rgba(251,146,60,.5))}"
        "path.v8hl{stroke:#fb923c!important;stroke-width:2.2px!important;"
        "filter:drop-shadow(0 0 4px rgba(251,146,60,.55))}"
        "svg.v8slice [data-node-id]:not(.v8hl){opacity:.35}"
        "svg.v8slice path[data-edge-from]:not(.v8hl){opacity:.14}"
        ".v8cta{color:#04121f;background:#22d3ee;border-radius:999px;"
        "padding:4px 12px;text-decoration:none;font-weight:600;font-size:12px;"
        "margin-right:8px;white-space:nowrap}.v8cta:hover{background:#67e8f9}"
        "g.v8btn{cursor:pointer}g.v8btn rect{fill:#22d3ee}"
        "g.v8btn:hover rect{fill:#67e8f9}"
        "g.v8btn text{fill:#04121f;font:600 11px system-ui,sans-serif;"
        "pointer-events:none}"
        "#v8hint{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);"
        "background:rgba(10,17,38,.94);border:1px solid #1e293b;color:#e2e8f0;"
        "padding:7px 16px;border-radius:999px;font:12.5px/1.4 system-ui,sans-serif;"
        "z-index:9998;box-shadow:0 4px 16px rgba(0,0,0,.4);max-width:90vw;"
        "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
        "pointer-events:none}"
        + "".join(f"#node-{i}{{cursor:pointer}}" for i in clicks)
        + "</style>"
        '<div class="ptop"><div class="pcrumbs">'
        + '<span class="psep">&#8250;</span>'.join(cs)
        + "</div>"
        + (f'<div class="pchips">{"".join(chips)}</div>' if chips else "")
        + '<div class="pact">' + act + "</div></div>")
    parts.append(
        "<script>\n"
        "(function(){"
        "var M=" + json.dumps(clicks) + ";var PG=" + json.dumps(page) + ";"
        "var SL=" + json.dumps(slice_nodes or {}, ensure_ascii=False) + ";"
        "var SQ=(/[?&]slice=([A-Za-z0-9_-]+)/.exec(location.search)||[])[1]||'';"
        "function W(u){var i=u.indexOf('#');var b=i<0?u:u.slice(0,i);"
        "var h2=i<0?'':u.slice(i);"
        "b+=(b.indexOf('?')<0?'?':'&')+'from='+PG;"
        "if(SQ)b+='&slice='+encodeURIComponent(SQ);"
        "return b+h2;}"
        # ---- 业务线高亮（图页）：节点 + 边 + chip 选中态 + 非命中暗化
        "if(SQ&&SL[SQ]){var ids={};SL[SQ].forEach(function(x){ids[x]=1;});"
        "var sv=document.querySelector('.diagram-container svg');"
        "if(sv)sv.classList.add('v8slice');"
        "var ns=document.querySelectorAll('[data-node-id]');"
        "for(var i=0;i<ns.length;i++){"
        "if(ids[ns[i].getAttribute('data-node-id')])ns[i].classList.add('v8hl');}"
        "var es=document.querySelectorAll('path[data-edge-from]');"
        "for(var j=0;j<es.length;j++){var e=es[j];"
        "if(ids[e.getAttribute('data-edge-from')]&&ids[e.getAttribute('data-edge-to')])"
        "e.classList.add('v8hl');}}"
        "var sc=document.querySelector('.pchip[data-slice=\"'+SQ+'\"]');"
        "if(sc)sc.classList.add('on');"
        # ---- v8 交互：单击=选中（邻域高亮无缩放），下钻走按钮
        "var svgEl=document.querySelector('.diagram-container svg');"
        "if(svgEl){"
        "var NS='http://www.w3.org/2000/svg';"
        "var hint=document.createElement('div');hint.id='v8hint';hint.hidden=true;"
        "document.body.appendChild(hint);"
        "function panned(){var c=document.querySelector('.diagram-container');"
        "return !!(c&&c.getAttribute('data-just-panned')==='true');}"
        "function hasFocusApi(){var A=window.Archify;"
        "return !!(A&&A.focus&&typeof A.focus.set==='function');}"
        "function nodeOf(t){return t&&t.closest?t.closest('[data-node-id]'):null;}"
        "function drillable(id){return !!M[id];}"
        "function label(n){return n.getAttribute('data-node-label')||"
        "n.getAttribute('aria-label')||n.getAttribute('data-node-id');}"
        "var btnG=null,cta=null,btnFor=null;"
        "function removeBtn(){if(btnG&&btnG.parentNode){"
        "btnG.parentNode.removeChild(btnG);}btnG=null;btnFor=null;}"
        "function drillGo(id){location.href=W(M[id]);}"
        "function showBtn(node){"
        "var id=node.getAttribute('data-node-id');"
        "if(btnG&&btnFor===id)return;"
        "removeBtn();"
        "if(!drillable(id))return;"
        "var rect=node.querySelector('rect');if(!rect)return;"
        "var x=+rect.getAttribute('x')+ +rect.getAttribute('width')+10;"
        "var y=+rect.getAttribute('y')+(+rect.getAttribute('height')-26)/2;"
        "btnG=document.createElementNS(NS,'g');"
        "btnG.setAttribute('class','v8btn');btnG.setAttribute('tabindex','0');"
        "btnG.setAttribute('role','button');btnG.setAttribute('aria-label','Drill down');"
        "var r=document.createElementNS(NS,'rect');"
        "r.setAttribute('x',x);r.setAttribute('y',y);r.setAttribute('width',56);"
        "r.setAttribute('height',26);r.setAttribute('rx',13);"
        "var t=document.createElementNS(NS,'text');"
        "t.setAttribute('x',x+28);t.setAttribute('y',y+17);"
        "t.setAttribute('text-anchor','middle');t.textContent='Drill \\u2198';"
        "btnG.appendChild(r);btnG.appendChild(t);"
        "btnG.addEventListener('click',function(e){e.stopPropagation();"
        "e.preventDefault();drillGo(id);});"
        "btnG.addEventListener('keydown',function(e){if(e.key==='Enter'){"
        "e.stopPropagation();e.preventDefault();drillGo(id);}});"
        "svgEl.appendChild(btnG);"
        "}"
        "function updateCta(){"
        "var chip=document.getElementById('focus-chip');"
        "if(!chip)return;"
        "if(!cta){"
        "var acts=chip.querySelector('.relationship-lens-actions');"
        "if(!acts)return;"
        "cta=document.createElement('a');cta.className='v8cta';cta.hidden=true;"
        "cta.textContent='Drill \\u2192';"
        "acts.insertBefore(cta,acts.firstChild);"
        "}"
        "var n=svgEl.querySelector('[data-focus-selected]');"
        "var id=n?n.getAttribute('data-node-id'):null;"
        "if(id&&drillable(id)){cta.href=W(M[id]);cta.hidden=false;}"
        "else{cta.hidden=true;}"
        "}"
        "function sync(){"
        "if(!svgEl.hasAttribute('data-focus-active')){"
        "hint.hidden=true;removeBtn();updateCta();return;}"
        "var n=svgEl.querySelector('[data-focus-selected]');"
        "if(n){"
        "var id=n.getAttribute('data-node-id');"
        "showBtn(n);"
        "hint.textContent=label(n)+(drillable(id)"
        "?' \\u2014 press Drill to go deeper'"
        ":' (no deeper page)');"
        "hint.hidden=false;}"
        "updateCta();"
        "}"
        "function activate(n){"
        "var id=n.getAttribute('data-node-id');"
        "if(hasFocusApi()){window.Archify.focus.set(id,{updateUrl:false});}"
        "else{n.classList.add('v8hl');}"
        "sync();"
        "}"
        "document.addEventListener('click',function(e){"
        "if(panned())return;"
        "var n=nodeOf(e.target);"
        "if(!n)return;"
        "e.stopPropagation();e.preventDefault();"
        "activate(n);"
        "},true);"
        "document.addEventListener('keydown',function(e){"
        "if(e.key!=='Enter'&&e.key!==' ')return;"
        "var n=nodeOf(e.target);"
        "if(!n)return;"
        "e.stopPropagation();e.preventDefault();"
        "activate(n);"
        "},true);"
        "if(window.MutationObserver){"
        "new MutationObserver(function(){sync();}).observe(svgEl.parentNode,"
        "{attributes:true,subtree:true,"
        "attributeFilter:['data-focus-active','data-focus-selected']});"
        "}"
        "}"
        "})();\n</script>"
        "<script>\n"
        "try{if(!sessionStorage.getItem('archifyHint')){"
        "var h=document.createElement('div');"
        "h.style.cssText='position:fixed;left:50%;bottom:70px;"
        "transform:translateX(-50%);background:rgba(10,17,38,.94);"
        "border:1px solid #1e293b;color:#e2e8f0;padding:8px 16px;"
        "border-radius:999px;font:12.5px system-ui,sans-serif;z-index:9998;"
        "box-shadow:0 4px 16px rgba(0,0,0,.4)';"
        "h.textContent='Click a node to select \\u00b7 Drill button goes one level deeper';"
        "document.body.appendChild(h);"
        "sessionStorage.setItem('archifyHint','1');"
        "setTimeout(function(){h.remove()},6000);}}catch(e){}\n"
        "</script>")
    inject = "\n".join(parts)
    h = h.replace("</body>", inject + "\n</body>", 1)
    html_path.write_text(h, encoding="utf-8", newline="\n")


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
    # v8 反馈轮：节点盒收紧（130x44 → 112x38，圆角走注入 CSS rx=5）
    size = [112, 38]
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
        "layout": {"mode": "grid", "cols": n_cols, "origin": [36, 72],
                    "gapX": 20, "gapY": 14, "cellW": 112, "cellH": 38},
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
                   action: tuple[str, str] | None = None,
                   slices_html: str = "", extra_js: str = "") -> str:
    """代码页 HTML 骨架（唯一代码视图 = 整文件源码页）：

    archify 令牌（#020617/#1e293b/#22d3ee，正文无衬线 + 代码区等宽）+
    统一顶部面包屑条（sticky，右侧上下文动作）+ 大纲 chips + 业务线
    chips（slices_html，v8）+ 符号锚点代码区（.sym :target 高亮；业务线
    命中 .sym.v8hl 橙标，extra_js）+ ?from= 「← 返回」。
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
        ".chip.on{background:rgba(251,146,60,.15);border-color:#fb923c;"
        "color:#fb923c;font-weight:600}"
        ".chip.dim{opacity:.45}"
        # v8 反馈轮：业务线常驻条（?slice= 时 sticky 顶于代码区上方）
        ".v8band{position:sticky;top:49px;z-index:9;display:flex;align-items:center;"
        "gap:10px;background:rgba(251,146,60,.1);border:1px solid #fb923c;"
        "border-radius:8px;padding:6px 12px;margin:10px 0 0;"
        "font:12.5px system-ui,sans-serif;color:#fdba74}"
        ".v8band a{color:#04121f;background:#fb923c;border-radius:999px;"
        "padding:2px 10px;text-decoration:none;font-weight:600;white-space:nowrap}"
        ".v8band a:hover{background:#fdba74}"
        ".v8band a.v8x{background:none;color:#f87171;padding:2px 4px}"
        ".v8band .v8n{color:#e2e8f0;font-weight:600}"
        ".v8band .v8m{color:#94a3b8;margin-right:auto}"
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
        ".sym.v8hl{background:rgba(251,146,60,.16);border-left-color:#fb923c;"
        "border-left-width:3px}"
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
        f"{slices_html}"
        '<div class="code">' + "".join(rows_html) + "</div>"
        + _FROM_JS
        + extra_js +
        "</main></body></html>")


def write_file_page(out_dir: Path, lines: list[str], fpath: str,
                    crumbs: list[tuple[str | None, str]],
                    action: tuple[str, str] | None = None,
                    sindex: "SymbolIndex | None" = None,
                    notes: list[str] | None = None,
                    slices: list[dict] | None = None) -> str:
    """整文件源码页（唯一代码视图）：行号 + 高亮 + 符号锚点。

    lines 为已读入的源码行（build_chain 统一预读，含守卫/缺失处理）；
    超过 CHAIN_FILE_LINES_CAP 截断并注记。有符号索引时：符号行区间包裹
    为锚点区块（#sym-*，CSS :target 高亮），符号起始处插关系条
    （调用/被调/内含 chips），页首大纲 chips，代码内同文件唯一符号名 →
    页内锚点链接；嵌套符号按行区间栈式嵌套渲染。
    slices（v8 业务线）：[{sid, name, anchors}]——页首业务线 chips +
    ?slice= 时 .sym 命中橙标高亮。返回输出文件名。
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
        for label, ids, ext_ids in (
                ("calls", sindex.callees.get(fid, []),
                 sindex.ext_callees.get(fid, [])),
                ("called by", sindex.callers.get(fid, []),
                 sindex.ext_callers.get(fid, [])),
                ("contains", sindex.children.get(fid, []), [])):
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
            # 链外对端（测试文件/外部桩，无页可下钻）：灰显命名 chips，
            # REL_EXT_CAP 封顶防噪音（超出 +N more 诚实标注）
            seen_ext: set[str] = set()
            n_ext = 0
            for x in ext_ids:
                if x in seen_ext:
                    continue
                seen_ext.add(x)
                if n_ext < REL_EXT_CAP:
                    r = sindex.rows[x]
                    nm = str(r["name"])
                    fp = str(r["file_path"] or "")
                    tip = f"{nm} @ {fp}" if fp else nm
                    chips.append(f'<span class="chip dim" '
                                 f'title="{html.escape(tip, quote=True)}">'
                                 f'{html.escape(nm)}</span>')
                n_ext += 1
            if n_ext > REL_EXT_CAP:
                chips.append(f'<span class="chip dim">+{n_ext - REL_EXT_CAP} more</span>')
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
    slices_html = ""
    slice_js = ""
    if slices:
        ch: list[str] = []
        for s in slices:
            sid = html.escape(str(s["sid"]), quote=True)
            name = html.escape(str(s["name"]))
            if s["anchors"]:
                ch.append(f'<a class="chip" data-slice="{sid}" href="?slice={sid}">{name}</a>')
            else:
                ch.append(f'<span class="chip dim" title="not on this page">{name}</span>')
        if any(s["anchors"] for s in slices):
            ch.append('<a class="chip" href="?">clear</a>')
        slices_html = ('<div class="chips"><span class="sec">Slices</span>'
                       + "".join(ch) + "</div>")
        sl_map = {str(s["sid"]): list(s["anchors"]) for s in slices}
        sl_name = {str(s["sid"]): str(s["name"]) for s in slices}
        sl_href = {str(s["sid"]): slice_page_stem(str(s["sid"])) + ".html"
                   for s in slices}
        slice_js = (
            "<script>(function(){"
            "var SQ=(/[?&]slice=([A-Za-z0-9_-]+)/.exec(location.search)||[])[1]||'';"
            "if(!SQ)return;"
            "var A=" + json.dumps(sl_map, ensure_ascii=False) + ";"
            "var NM=" + json.dumps(sl_name, ensure_ascii=False) + ";"
            "var JH=" + json.dumps(sl_href, ensure_ascii=False) + ";"
            "var S=A[SQ]||[];"
            "if(S.length){var ids={};S.forEach(function(x){ids[x]=1;});"
            "var syms=document.querySelectorAll('.sym');"
            "for(var i=0;i<syms.length;i++){"
            "if(ids[syms[i].id])syms[i].classList.add('v8hl');}"
            "var chips=document.querySelectorAll('.chips .chip');"
            "for(var j=0;j<chips.length;j++){"
            "var h=chips[j].getAttribute('href')||'';"
            "if(h.charAt(0)==='#'&&ids[h.slice(1)])chips[j].classList.add('on');}}"
            "var sc=document.querySelector('.chip[data-slice=\"'+SQ+'\"]');"
            "if(sc)sc.classList.add('on');"
            # v8 反馈轮：常驻业务线条（名称 + 命中数 + 首命中/专页/清除）。
            # DOM 构建（非 innerHTML）：href 不落字面量，链接完整性测试
            # 的 href=" 抓取不会把 JS 拼接误当真链接；名称走 textContent
            # 天然转义。
            "if(NM[SQ]){var b=document.createElement('div');b.className='v8band';"
            "var n=S.length;"
            "var sp=document.createElement('span');sp.className='v8n';"
            "sp.textContent='Slice \\u00b7 '+NM[SQ];b.appendChild(sp);"
            "var m=document.createElement('span');m.className='v8m';"
            "m.textContent=n?n+' hit'+(n>1?'s':'')+' in this file'"
            ":'not on this page';b.appendChild(m);"
            "if(n){var f=document.createElement('a');f.href='#'+S[0];"
            "f.textContent='first hit \\u25b8';b.appendChild(f);}"
            "var j=document.createElement('a');j.href=JH[SQ];"
            "j.textContent='journey \\u2197';b.appendChild(j);"
            "var x=document.createElement('a');x.className='v8x';x.href='?';"
            "x.textContent='\\u00d7';b.appendChild(x);"
            "var code=document.querySelector('.code');"
            "if(code)code.parentNode.insertBefore(b,code);}"
            "})();</script>")
    meta_bits = [f"L1-L{n_lines} · {len(lines)} lines"]
    if lines_truncated:
        meta_bits.append(f"truncated at {CHAIN_FILE_LINES_CAP} lines")
    if syms:
        meta_bits.append(f"{len(syms)} symbols")
    if notes:
        meta_bits.extend(notes)
    meta = " · ".join(meta_bits)
    doc = _code_page_doc(fpath, fpath, meta, rows_html, chips_html, crumbs, action,
                         slices_html=slices_html, extra_js=slice_js)
    (out_dir / f"{file_page_stem_of(fpath)}.html").write_text(doc, encoding="utf-8", newline="\n")
    return this_page


_ROLE_LABEL = {"entry": "entry", "process": "process", "persist": "persist"}


def write_slice_page(out_dir: Path, sid: str, sl: dict,
                     sindex: "SymbolIndex | None",
                     fid_to_mid: dict[str, str],
                     entry_of: dict[str, str]) -> str:
    """业务线专页（v8 反馈轮）：步骤序列一列到底，每步直达代码锚点。

    sl 为 _load_slices 的完整条目（name/summary/status/steps/acts/fids）。
    链外 fid / 无代码页的步骤灰显诚实注记（不可点）；acts → 段落 chips。
    返回输出文件名。
    """
    esc_sid = html.escape(sid, quote=True)
    cs = [
        ('<a class="cb" href="index.html">home</a>'),
        (f'<span class="cb cur">{html.escape(sl["name"])}</span>'),
    ]
    steps_html: list[str] = []
    n_ext = 0
    n_nolink = 0
    for i, st in enumerate(sl["steps"], 1):
        fid = st.get("fid")
        role = _ROLE_LABEL.get(str(st.get("role") or ""), "")
        role_html = (f'<span class="role">{html.escape(role)}</span>' if role else "")
        note = str(st.get("note") or "")
        note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
        r = sindex.rows.get(fid) if (sindex is not None and isinstance(fid, str)) else None
        if r is None:
            n_ext += 1
            steps_html.append(
                '<div class="step ext" title="symbol outside the chained modules">'
                f'<span class="no">{i}</span><div class="body">'
                f'<div class="nm">unknown symbol</div>'
                '<div class="fp">outside the chained modules</div>'
                f"{note_html}</div>{role_html}</div>")
            continue
        nm = html.escape(str(r["name"]))
        fp = html.escape(str(r["file_path"]).replace("\\", "/"))
        ln = f"L{r['start_line']}" if r["start_line"] else ""
        href = sindex.href.get(fid) if sindex is not None else None
        mid = fid_to_mid.get(str(fid) or "", "")
        mod_html = ""
        if mid and mid in entry_of:
            mod_html = (f'<a class="mod" href="{html.escape(entry_of[mid], quote=True)}'
                        f'?slice={esc_sid}">{html.escape(mid)}</a>')
        if href:
            page, _, frag = href.partition("#")
            target = f"{page}?slice={esc_sid}#{html.escape(frag, quote=True)}"
            steps_html.append(
                f'<a class="step" href="{target}">'
                f'<span class="no">{i}</span><div class="body">'
                f'<div class="nm">{nm} <span class="fp">{fp} {ln}</span></div>'
                f"{note_html}</div>{role_html}{mod_html}</a>")
        else:
            n_nolink += 1
            steps_html.append(
                '<div class="step off" title="code page not in chain (cap/path guard)">'
                f'<span class="no">{i}</span><div class="body">'
                f'<div class="nm">{nm} <span class="fp">{fp} {ln}</span></div>'
                f"{note_html}</div>{role_html}{mod_html}</div>")
    acts_html = ""
    if sl["acts"]:
        chips = []
        for a in sl["acts"]:
            name = str(a.get("name") or "")
            fs, ts = a.get("from_step"), a.get("to_step")
            span = (f" (step {fs + 1} &#8594; {ts + 1})"
                    if isinstance(fs, int) and isinstance(ts, int) else "")
            chips.append(f'<span class="actc">{html.escape(name)}'
                         f"{html.escape(span)}</span>")
        acts_html = ('<div class="acts"><span class="sec">Acts</span>'
                     + "".join(chips) + "</div>")
    status = str(sl.get("status") or "")
    stale_html = ""
    if status and status != "active":
        stale_html = (f'<div class="stale">status: {html.escape(status)} '
                      "&mdash; facts drifted since curated, shown as-is</div>")
    notes_bits = []
    if n_ext:
        notes_bits.append(f"{n_ext} step(s) reference symbols outside the chained modules")
    if n_nolink:
        notes_bits.append(f"{n_nolink} step(s) have no code page in chain (cap/path guard)")
    notes_html = (f'<div class="snote">{html.escape("; ".join(notes_bits))}</div>'
                  if notes_bits else "")
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>slice {html.escape(sid)}</title><style>"
        "body{margin:0;background:#020617;color:#cbd5e1;"
        "font:13px/1.6 system-ui,'Segoe UI',sans-serif}"
        ".topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;"
        "gap:16px;background:rgba(2,6,23,.94);border-bottom:1px solid #1e293b;"
        "padding:10px 20px;backdrop-filter:blur(6px)}"
        ".crumbs{display:flex;align-items:center;gap:6px;font:13px system-ui,sans-serif}"
        ".cb{color:#94a3b8;text-decoration:none}.cb:hover{color:#67e8f9}"
        ".cb.cur{color:#e2e8f0;font-weight:600}"
        "main{padding:18px 20px 60px;max-width:920px}"
        "h1{margin:6px 0 2px;font:600 18px system-ui,sans-serif;color:#f1f5f9}"
        ".meta{color:#94a3b8;font:12.5px system-ui,sans-serif}"
        ".stale{margin-top:6px;color:#fb923c;font:12px system-ui,sans-serif}"
        ".steps{margin-top:14px}"
        ".step{display:flex;gap:12px;align-items:flex-start;background:#0b1226;"
        "border:1px solid #1e293b;border-radius:8px;padding:10px 14px;margin:8px 0;"
        "text-decoration:none;color:#e2e8f0}"
        "a.step:hover{border-color:#fb923c}"
        ".step .no{flex:0 0 22px;height:22px;border-radius:999px;background:#1e293b;"
        "color:#67e8f9;font:600 12px system-ui,sans-serif;display:flex;"
        "align-items:center;justify-content:center}"
        ".step .body{min-width:0}"
        ".step .nm{font:600 13px ui-monospace,Consolas,monospace;color:#f1f5f9;"
        "word-break:break-all}"
        ".step .fp{font:11px ui-monospace,Consolas,monospace;color:#64748b}"
        ".step .note{color:#94a3b8;font:12px system-ui,sans-serif;margin-top:2px}"
        ".role{flex:0 0 auto;color:#67e8f9;background:rgba(34,211,238,.07);"
        "border:1px solid #1e293b;border-radius:999px;padding:1px 8px;"
        "font:11px system-ui,sans-serif}"
        ".mod{flex:0 0 auto;margin-left:auto;color:#c4b5fd;"
        "background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.3);"
        "border-radius:999px;padding:1px 8px;font:11px system-ui,sans-serif;"
        "text-decoration:none}.mod:hover{color:#a78bda}"
        ".step.off{opacity:.55}"
        ".step.ext{opacity:.55;border-style:dashed}"
        ".acts{margin-top:14px}"
        ".sec{color:#64748b;font:11px system-ui,sans-serif;margin-right:8px}"
        ".actc{display:inline-block;background:#0b1226;border:1px solid #1e293b;"
        "color:#fdba74;padding:3px 10px;border-radius:999px;font:12px system-ui,"
        "sans-serif;margin:0 6px 4px 0}"
        ".snote{margin-top:14px;color:#64748b;font:11.5px system-ui,sans-serif}"
        "</style></head><body>"
        '<div class="topbar"><div class="crumbs">'
        + '<span class="sep">&#8250;</span>'.join(cs)
        + "</div></div>"
        "<main>"
        f"<h1>{html.escape(sl['name'])}</h1>"
        f'<div class="meta">{html.escape(sl["summary"])}</div>'
        f"{stale_html}"
        f'<div class="steps">{"".join(steps_html)}</div>'
        f"{acts_html}"
        f"{notes_html}"
        "</main></body></html>")
    this_page = slice_page_stem(sid) + ".html"
    (out_dir / this_page).write_text(doc, encoding="utf-8", newline="\n")
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
        # 链外对端（无页可下钻，仅命名展示）：calls 出链/入链的 CALLS 边。
        # 过滤合成桩（<python-builtins> 等 < 路径与无路径）：append/len/
        # str 类内置命名纯噪音，不进 rel 条。
        def _named(r: sqlite3.Row) -> bool:
            fp = str(r["file_path"] or "")
            return bool(fp) and not fp.startswith("<")
        self.ext_callees: dict[str, list[str]] = defaultdict(list)
        self.ext_callers: dict[str, list[str]] = defaultdict(list)
        for a, b, t in edges:
            if t not in ("CALLS", "CALL_REFERENCE"):
                continue
            if a in self.anchor and b in self.anchor:
                self.callees[a].append(b)
                self.callers[b].append(a)
            elif a in self.anchor and b in rows and _named(rows[b]):
                self.ext_callees[a].append(b)
            elif b in self.anchor and a in rows and _named(rows[a]):
                self.ext_callers[b].append(a)
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


def _archify_error_text(res: dict, limit: int = 160) -> str:
    """validate --json 的错误在 stdout JSON 的 error 字段（stderr 恒空）；两通道都兜。

    R-AO：入参随 _render_diagram 收编 procs.run_tree 由 CompletedProcess 改 dict。
    """
    out = (res.get("stdout") or "").strip()
    if out:
        try:
            doc = json.loads(out)
            err = doc.get("error")
            if err:
                return str(err).strip()[:limit]
        except json.JSONDecodeError:
            pass
        return out[:limit]
    return (res.get("stderr") or "").strip()[:limit]


def _render_diagram(mjs: Path, mod_staging: Path, ir: dict,
                    name: str, dtype: str) -> tuple[Path | None, str | None]:
    """单图：写 IR → validate → render 进模块暂存区。返回 (html, 错误)。"""
    ir_dir = mod_staging / "ir"
    ir_path = ir_dir / f"{name}.json"
    ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    try:
        res = procs.run_tree(
            ["node", str(mjs), "validate", dtype, str(ir_path), "--json"],
            timeout=ARCHIFY_TIMEOUT)
    except OSError as exc:
        return None, f"archify validate error: {exc}"
    if res["timed_out"]:
        return None, f"archify validate error: timed out after {ARCHIFY_TIMEOUT}s"
    if res["returncode"] != 0:
        return None, (f"archify validate failed: {name} rc={res['returncode']}"
                      f" {_archify_error_text(res)}")
    out_html = mod_staging / f"{name}.html"
    try:
        res = procs.run_tree(
            ["node", str(mjs), "render", dtype, str(ir_path), str(out_html)],
            timeout=ARCHIFY_RENDER_TIMEOUT)
    except OSError as exc:
        return None, f"archify render error: {exc}"
    if res["timed_out"]:
        return None, f"archify render error: timed out after {ARCHIFY_RENDER_TIMEOUT}s"
    if res["returncode"] != 0 or not out_html.is_file():
        return None, (f"archify render failed: {name} rc={res['returncode']}"
                      f" {res['stderr'].strip()[-160:]}")
    return out_html, None


# ---------------------------------------------------------------- 模块链规划


def _plan_module(module_id: str, fids: list[str], rows: dict[str, sqlite3.Row],
                 edges: list[tuple[str, str, str]],
                 slices: dict[str, dict] | None = None,
                 full_by_file: dict[str, list[str]] | None = None) -> dict | None:
    """模块链规划（Pass1a）：入口分流 + 页清单 + 每页 IR/crumbs 元数据。

    v8：模块入口 = 文件总览树（t-*-overview，archify tree）——文件 →
    符号网格/整文件页，树根 → 模块 dataflow；s1/s2a/s2/s3 保留为更深层。
    slices（v8 业务线）：{sid: {name, fids}}——每图算 slice_nodes
    （{sid: [节点 id]}，s1/s3 按 fid、t 按文件路径投影）。
    full_by_file（v8 用户反馈二轮）：build_chain 统一算的整文件全量真实
    符号（与 mod_edges 端点同源）——网格节点/树与目录页计数用。
    返回 None = 模块无内容（0 fid / 无文件视图），调用方弃链。
    diagrams: [{name, dtype, ir, crumbs, action, clicks | fid_clicks,
    slice_nodes}]
    """
    fs_view = module_fs(fids, rows)
    files_by_dir, subdirs, agg = fs_view
    real = real_by_file(rows, fids)
    # v8 用户反馈二轮：文件符号浏览器（总览树/目录页计数、符号网格节点）
    # 用整文件 DB 全量真实符号；dataflow/结构页仍 curated（curate 语义
    # = 为图挑代表）。未传时回退 curated（独立调用健壮性）。
    full_real: dict[str, list[str]] = (
        dict(full_by_file) if full_by_file is not None
        else {fp: list(v) for fp, v in real.items()})
    for fp in real:  # 全量缺失时回退 curated；键序随 real（确定性）
        full_real.setdefault(fp, list(real[fp]))
    all_dirs = sorted({d for ds in subdirs.values() for d in ds})
    n_files = sum(len(v) for v in files_by_dir.values())
    if not fids or n_files == 0:
        return None
    ms = slug(module_id)
    t_stem = f"t-{ms}-overview"
    s1_stem = f"s1-{ms}-dataflow"
    s2a_stem = f"s2a-{ms}-overview"
    have_s1 = 2 <= n_files <= 8 and bool(real)
    have_s2a = len(all_dirs) > 1
    dir_pages = set(all_dirs)
    if (len(all_dirs) == 1 and len(files_by_dir.get(all_dirs[0], {})) == 1
            and not subdirs.get(all_dirs[0])):
        dir_pages = set()
    entry_stem, kind = t_stem, "file overview"

    # 业务线投影辅助（v8）
    slice_fid_sets: dict[str, set[str]] = {}
    slice_file_sets: dict[str, set[str]] = {}
    if slices:
        for sid, sl in slices.items():
            fs_ = {f for f in sl["fids"] if f in rows}
            slice_fid_sets[sid] = fs_
            slice_file_sets[sid] = {_norm(rows[f]["file_path"])
                                    for f in fs_ if rows[f]["file_path"]}

    def sn_of_fid(nid_of_fid: dict[str, str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for sid in slice_fid_sets:
            hits = [nid_of_fid[f] for f in slice_fid_sets[sid] if f in nid_of_fid]
            if hits:
                out[sid] = hits
        return out

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

    # T 模块文件总览树（v8 入口）：文件 → 网格/整文件页；树根 → dataflow
    # 计数传 full_real（v8 用户反馈二轮：与代码页/网格全量口径一致）
    ir_t, clicks_t, file_of_t = module_overview_tree(
        fs_view, module_id, full_real,
        file_target=lambda fp: (
            (grid_stem_of(module_id, fp) + ".html")
            if fp in real
            else (file_page_stem_of(fp) + ".html")),
        root_target=(s1_stem + ".html") if have_s1 else None)
    sn_t: dict[str, list[str]] = {}
    for sid in slice_file_sets:
        hits = [nid for nid, fp in file_of_t.items() if fp in slice_file_sets[sid]]
        if hits:
            sn_t[sid] = hits
    diagrams.append({"name": t_stem, "dtype": "tree", "ir": ir_t,
                     "crumbs": [("index.html", "home"), (None, module_id)],
                     "action": ((s1_stem + ".html", "module dataflow")
                                if have_s1 else None),
                     "clicks": clicks_t, "slice_nodes": sn_t})

    # S1 模块 dataflow：节点 → 代码页锚点；动作 → 文件总览树
    if have_s1:
        ir, id_of = s1_module_dataflow(fids, rows, edges, module_id)
        act = (t_stem + ".html", "file overview")
        diagrams.append({"name": s1_stem, "dtype": "dataflow", "ir": ir,
                         "crumbs": base_crumbs() + [(None, "dataflow")],
                         "action": act,
                         "fid_clicks": {nid: fid for fid, nid in id_of.items()},
                         "slice_nodes": sn_of_fid(id_of)})

    # S2a 目录总览（多目录）：节点 → 目录页；动作 → 模块数据流
    if have_s2a:
        ir2a, clicks2a = s2a_overview(fs_view, module_id, module_id)
        crumbs = (base_crumbs() + [(None, "directory overview")])
        act = (s1_stem + ".html", "module dataflow") if have_s1 else None
        diagrams.append({"name": s2a_stem, "dtype": "tree", "ir": ir2a,
                         "crumbs": crumbs, "action": act, "clicks": clicks2a,
                         "slice_nodes": {}})

    # S2 目录页：子目录 → 目录页；文件 → 符号网格（真实符号）或整文件页
    for d in sorted(dir_pages):
        cnt = {fp.rsplit("/", 1)[-1]: len(v) for fp, v in full_real.items()
               if "/" in fp and fp.rsplit("/", 1)[0] == d}
        ir, clicks = build_dir_tree(
            fs_view, module_id, d,
            file_page=lambda dd, f_: (
                (grid_stem_of(module_id, f"{dd}/{f_}") + ".html")
                if f"{dd}/{f_}" in real
                else (file_page_stem_of(f"{dd}/{f_}") + ".html")),
            sym_counts=cnt)
        crumbs = base_crumbs() + dir_segs(d)
        act = (s1_stem + ".html", "module dataflow") if have_s1 else None
        diagrams.append({"name": dir_page_stem(module_id, d), "dtype": "tree",
                         "ir": ir, "crumbs": crumbs, "action": act, "clicks": clicks,
                         "slice_nodes": {}})

    # S3 网格（有真实符号的文件）：节点 → 代码页锚点；动作 → 整文件源码
    # v8 用户反馈二轮：节点用整文件全量符号（curated 子集会 1/13 丢符号），
    # mod_edges 已含全量端点边 → 角色（入口/枢纽）推导覆盖非 curated 符号
    for fpath in sorted(real):
        gstem = grid_stem_of(module_id, fpath)
        s4f_html = file_page_stem_of(fpath) + ".html"
        d_ = fpath.rsplit("/", 1)[0] if "/" in fpath else ""
        fname = fpath.rsplit("/", 1)[-1]
        rf = full_real.get(fpath) or real[fpath]
        fs = set(rf)
        ee = [(a, b, t) for a, b, t in edges if a in fs and b in fs]
        ir, syms = build_file_grid(rows, rf, ee)
        diagrams.append({"name": gstem, "dtype": "architecture", "ir": ir,
                         "crumbs": base_crumbs() + dir_segs(d_) + [(None, fname)],
                          "action": (s4f_html, "full file source"),
                          "fid_clicks": {nid: fid for nid, fid in syms.items()},
                          "slice_nodes": sn_of_fid({fid: nid for nid, fid in syms.items()})})

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
        "n_syms": sum(len(v) for v in full_real.values()),
        "n_dirs": len(all_dirs),
        "diagrams": diagrams,
        "file_crumbs": file_crumbs,
    }


_ARCH_HUES = ("frontend", "backend", "messagebus", "database", "security")


def build_arch_ir(mod_entries: list[dict], layers: list[dict],
                  cross_counts: dict[tuple[str, str], int],
                  slices: dict[str, dict] | None = None,
                  fid_to_mid: dict[str, str] | None = None):
    """全局分层总览 IR（v8 反馈轮：补回横向层面）。

    层 = region 边界（横向一行一个层，层序取 layers 数组序，未知层最后）；
    模块 = 节点（label=module_id，sublabel=AI 梳理职责一句话）；
    连线 = 模块间实测边计数（cross_counts 聚合，帽 60）。
    pos/size 布局（与 render.py L0 architecture 同形，validate 已证）。
    返回 (IR, clicks, slice_nodes, slice_chips)：clicks {nid: 模块树页}；
    slice_nodes {sid: [模块 nid]}（业务线在全局架构的模块分布投影）。
    """
    layer_names = [str(l["name"]) for l in (layers or [])
                   if isinstance(l, dict) and l.get("name")]
    rank = {n: i for i, n in enumerate(layer_names)}
    by_layer: dict[str, list[dict]] = defaultdict(list)
    unknown: list[dict] = []
    for m in mod_entries:
        lay = str(m.get("layer") or "")
        (by_layer[lay] if lay in rank else unknown).append(m)
    groups: list[tuple[str, list[dict]]] = []
    for n in layer_names:
        if by_layer.get(n):
            groups.append((n, by_layer[n]))
    if unknown:
        groups.append(("other", unknown))
    components: list[dict] = []
    boundaries: list[dict] = []
    clicks: dict[str, str] = {}
    nid_of: dict[str, str] = {}
    for gi, (lay, members) in enumerate(groups):
        ids: list[str] = []
        for ci, m in enumerate(members):
            nid = f"m{len(components)}"
            nid_of[str(m["module_id"])] = nid
            components.append({
                "id": nid, "type": _ARCH_HUES[gi % len(_ARCH_HUES)],
                "label": _fit(str(m["module_id"]), 20),
                "sublabel": _fit(str(m.get("responsibility") or ""), 30),
                "pos": [40 + ci * 240, 40 + gi * 170],
                "size": [200, 64],
            })
            clicks[nid] = str(m["entry"])
            ids.append(nid)
        boundaries.append({"kind": "region", "label": lay, "wraps": ids})
    connections: list[dict] = []
    for pair in sorted(cross_counts):
        a, b = pair
        if a in nid_of and b in nid_of and len(connections) < 60:
            # 不带 label：archify 连线标签默认落中点，与 pos 布局节点重叠
            # （validate 拒绝）；计数语义由 index 卡片 depends_on 承担
            connections.append({"id": f"e{len(connections)}",
                                "from": nid_of[a], "to": nid_of[b]})
    slice_nodes: dict[str, list[str]] = {}
    slice_chips: list[dict] = []
    for sid, sl in (slices or {}).items():
        hits: list[str] = []
        seen: set[str] = set()
        for f in sl["fids"]:
            mid = (fid_to_mid or {}).get(f)
            if mid and mid in nid_of and mid not in seen:
                seen.add(mid)
                hits.append(nid_of[mid])
        if hits:
            slice_nodes[sid] = hits
        slice_chips.append({"sid": sid, "name": sl["name"],
                            "applicable": bool(hits)})
    ir = {
        "schema_version": 1, "diagram_type": "architecture",
        "meta": {"title": "layered architecture", "output": "arch.html"},
        "components": components,
        "boundaries": boundaries,
        "connections": connections,
    }
    return ir, clicks, slice_nodes, slice_chips


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
    con = dbpath.ro_connect(db)
    con.row_factory = sqlite3.Row
    try:
        structure = _load_structure(con)
        if structure is None:
            notes.append("structure inactive/empty - module chain skipped")
            return result
        mods, layers, struct_stale = structure
        if struct_stale:
            notes.append("structure stale (facts drifted since curated) - "
                         "rendered as-is, re-run curate for a fresh structure")
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
            plans.append((str(m["module_id"]), fids,
                          str(m.get("responsibility") or ""), str(m.get("layer") or "")))
        if not plans:
            notes.append("no chainable modules")
            return result
        all_fids: list[str] = []
        seen: set[str] = set()
        for _mid, fids, _resp, _layer in plans:
            for f in fids:
                if f not in seen:
                    seen.add(f)
                    all_fids.append(f)
        # 代码页全量符号（v8 用户反馈轮）：curated symbol_fids 是图为视角的
        # 代表子集，代码页/rel 条需要链内文件的 DB 全量符号——否则函数不
        # 显示调用/被调（用户实测 s0_precheck 整页仅 1 符号）。
        fpaths: list[str] = []
        seen_fp: set[str] = set()
        for i in range(0, len(all_fids), FID_CHUNK):
            chunk = all_fids[i:i + FID_CHUNK]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT DISTINCT file_path FROM symbols"
                    f" WHERE fid IN ({marks}) ORDER BY file_path", chunk):
                fp = str(r["file_path"] or "")
                if fp and fp not in seen_fp:
                    seen_fp.add(fp)
                    fpaths.append(fp)
        all_fids = list(dict.fromkeys(
            all_fids + _load_full_file_fids(con, fpaths)))
        rows, edges = _load_rows_edges(con, all_fids, any_endpoint=True)
        # CALLS 边链外对端符号行补载：rel 条外部目标可命名（无页，仅名 + @file）
        _load_symbol_rows(
            con,
            sorted({x for a, b, t in edges
                    if t in ("CALLS", "CALL_REFERENCE")
                    for x in (a, b)
                    if x not in rows}),
            rows)
        slices = _load_slices(con, rows)
        mod_data = []
        for mid, fids, resp, layer in plans:
            fset = set(fids)
            # v8 用户反馈二轮：模块内边集端点扩到模块文件的整文件全量真实
            # 符号（网格角色推导需要文件内全量调用边——curated 子集时非
            # curated 符号全 0 度，入口/枢纽误判）；s1 dataflow 节点仍
            # curated（s1 只认 fids，多余边只抬高 deg，hub 阈值更真实）。
            mfps = {_norm(r["file_path"]) for f in fids
                    if (r := rows.get(f)) is not None and r["file_path"]}
            full_by_file = full_symbols_by_file(rows, mfps)
            full_fids = [f for v in full_by_file.values() for f in v]
            eset = fset | set(full_fids)
            mod_edges = [(a, b, t) for a, b, t in edges if a in eset and b in eset]
            mod_data.append((mid, fids, full_by_file, mod_edges, resp, layer))
        # 模块间依赖（index 卡片 "depends on"）：全局 edges 跨模块聚合
        # v8 用户反馈二轮：fid→模块归属扩到全量符号（非 curated 函数的
        # 跨模块调用也计数）。curated 先到先得（共享文件时结构归属优先，
        # 防 m2 curated 符号被 m1 全量集合抢走致跨模块连线消失），全量补位
        fid_to_mid: dict[str, str] = {}
        for mid, fids, _fb, _e, _r, _l in mod_data:
            for f in fids:
                fid_to_mid.setdefault(f, mid)
        for mid, _fids, full_by_file, _e, _r, _l in mod_data:
            for f in [x for v in full_by_file.values() for x in v]:
                fid_to_mid.setdefault(f, mid)
        cross_deps: dict[str, set[str]] = defaultdict(set)
        cross_counts: dict[tuple[str, str], int] = defaultdict(int)
        for a, b, _t in edges:
            ma, mb = fid_to_mid.get(a), fid_to_mid.get(b)
            if ma is not None and mb is not None and ma != mb:
                cross_deps[ma].add(mb)
                cross_counts[(ma, mb)] += 1
    finally:
        con.close()

    staging = out_chain / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    ok_plans: list[tuple[dict, Path]] = []  # (plan, mod_staging)
    try:
        # ---- Pass 1：规划 + 逐模块渲染（模块原子性：任一图失败整模块弃链）
        budget = 0
        for mid, fids, full_by_file, mod_edges, _resp, _layer in mod_data:
            plan = _plan_module(mid, fids, rows, mod_edges,
                                slices=slices or None,
                                full_by_file=full_by_file)
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

        caps: dict[str, int] = {}
        for fpath, lines in written.items():
            caps[fpath] = min(CHAIN_FILE_LINES_CAP,
                              max(0, len(lines) - (1 if lines and lines[-1] == "" else 0)))
        fid_ok: set[str] = set()
        for fid in all_fids:
            r = rows.get(fid)
            if (r is None or r["label"] in ("File", "Module")
                    or not r["start_line"]):
                continue
            cap = caps.get(_norm(r["file_path"]))
            if cap is not None and 1 <= int(r["start_line"]) <= cap:
                fid_ok.add(fid)
        sindex = SymbolIndex(rows, all_fids, edges, fid_ok=fid_ok)

        n_pages = 0
        for fpath in sorted(written):
            page_slices: list[dict] | None = None
            if slices:
                page_slices = []
                for sid, sl in slices.items():
                    anchors = [sindex.anchor[f] for f in sl["fids"]
                               if f in sindex.anchor
                               and _norm(rows[f]["file_path"]) == fpath]
                    page_slices.append({"sid": sid, "name": sl["name"],
                                        "anchors": anchors})
            write_file_page(out_chain, written[fpath], fpath,
                            file_crumbs_of[fpath], sindex=sindex,
                            notes=file_notes[fpath] or None,
                            slices=page_slices)
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
                page_slices = None
                if slices:
                    sn = d.get("slice_nodes") or {}
                    page_slices = [{"sid": sid, "name": sl["name"],
                                    "applicable": sid in sn}
                                   for sid, sl in slices.items()]
                inject_chrome(staged, clicks, d["crumbs"], d.get("action"),
                              page=d["name"], slices=page_slices,
                              slice_nodes=d.get("slice_nodes"))
                shutil.move(str(staged), str(out_chain / f"{d['name']}.html"))
                ir_src = mod_staging / "ir" / f"{d['name']}.json"
                if ir_src.is_file():
                    shutil.move(str(ir_src), str(out_chain / "ir" / f"{d['name']}.json"))
                n_pages += 1
            shutil.rmtree(mod_staging, ignore_errors=True)

        # ---- v8 反馈轮：业务线专页 + 全局分层总览图
        meta_of = {mid: (resp, layer) for mid, _fids, _fb, _edges, resp, layer in mod_data}
        entry_of = {plan["module_id"]: plan["entry_stem"] + ".html"
                    for plan, _stg in ok_plans}
        slice_list: list[dict] = []
        if slices:
            for sid, sl in slices.items():
                write_slice_page(out_chain, sid, sl, sindex, fid_to_mid, entry_of)
                slice_list.append({"sid": sid, "name": sl["name"],
                                   "stale": str(sl.get("status") or "")
                                   not in ("", "active")})
                n_pages += 1
        arch_ok = False
        mod_entries = [{"module_id": plan["module_id"],
                        "entry": entry_of[plan["module_id"]],
                        "layer": meta_of[plan["module_id"]][1],
                        "responsibility": meta_of[plan["module_id"]][0]}
                       for plan, _stg in ok_plans]
        ir_arch, clicks_a, sn_a, chips_a = build_arch_ir(
            mod_entries, layers, dict(cross_counts),
            slices=slices, fid_to_mid=fid_to_mid)
        arch_staging = staging / "arch"
        (arch_staging / "ir").mkdir(parents=True, exist_ok=True)
        html_a, err_a = _render_diagram(mjs, arch_staging, ir_arch,
                                        "arch", "architecture")
        if err_a is None:
            inject_chrome(html_a, clicks_a,
                          [("index.html", "home"), (None, "architecture")],
                          page="arch", slices=chips_a, slice_nodes=sn_a)
            shutil.move(str(html_a), str(out_chain / "arch.html"))
            ir_src = arch_staging / "ir" / "arch.json"
            if ir_src.is_file():
                shutil.move(str(ir_src), str(out_chain / "ir" / "arch.json"))
            n_pages += 1
            arch_ok = True
        else:
            notes.append(f"layered overview dropped: {err_a}")
        shutil.rmtree(arch_staging, ignore_errors=True)

        # ---- Pass 3：index 总入口
        cards = [{
            "module_id": plan["module_id"],
            "entry": plan["entry_stem"] + ".html",
            "kind": plan["kind"],
            "n_files": plan["n_files"],
            "n_syms": plan["n_syms"],
            "n_dirs": plan["n_dirs"],
            "responsibility": meta_of[plan["module_id"]][0],
            "layer": meta_of[plan["module_id"]][1],
            "depends_on": sorted(cross_deps.get(plan["module_id"], ())),
        } for plan, _stg in ok_plans]
        write_index(out_chain, cards, layers, slices=slice_list, arch=arch_ok)
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


def write_index(out_dir: Path, cards: list[dict], layers: list[dict] | None = None,
                slices: list[dict] | None = None, arch: bool = False) -> None:
    """总入口 index.html：archify 令牌 + 模块卡片（v8 反馈轮：按层分组横排）。

    用户产物定位（AI 梳理呈现）：卡片显示 AI 梳理职责一句话（responsibility）
    + 层徽标（layer）+ 模块间依赖（depends_on），按层序（layers 数组序，
    未知层排最后）分组——每层一个段落头，组内卡片横排网格。
    arch=True 时头部给「layered architecture」总览图入口；
    slices=[{sid,name,stale}] 时页尾业务线区（链接 sl-*.html 专页）。
    """
    layer_names = [l["name"] for l in (layers or []) if isinstance(l, dict) and l.get("name")]
    rank = {name: i for i, name in enumerate(layer_names)}
    ordered = sorted(cards, key=lambda c: (rank.get(c.get("layer", ""), len(rank)),
                                            str(c.get("module_id", ""))))
    groups: list[tuple[str, list[dict]]] = []
    by_layer: dict[str, list[dict]] = defaultdict(list)
    unknown: list[dict] = []
    for c in ordered:
        lay = str(c.get("layer") or "")
        (by_layer[lay] if lay in rank else unknown).append(c)
    for n in layer_names:
        if by_layer.get(n):
            groups.append((n, by_layer[n]))
    if unknown:
        groups.append(("other", unknown))

    def card_html(c: dict) -> str:
        resp = str(c.get("responsibility") or "")
        resp_html = f'<div class="r">{html.escape(resp)}</div>' if resp else ""
        layer = str(c.get("layer") or "")
        lay_html = f'<span class="lay">{html.escape(layer)}</span>' if layer else ""
        deps = [d for d in c.get("depends_on", []) if d != c.get("module_id")]
        deps_html = f'<div class="d">depends on: {html.escape(", ".join(deps))}</div>' if deps else ""
        return (
            f'<a class="card" href="{html.escape(c["entry"], quote=True)}">'
            f'<div class="k">{html.escape(c["kind"])}{lay_html}</div>'
            f'<h2>{html.escape(c["module_id"])}</h2>'
            f"{resp_html}"
            f'<div class="s">{c["n_files"]} files · {c["n_syms"]} symbols · {c["n_dirs"]} dirs</div>'
            f"{deps_html}"
            "</a>")

    secs: list[str] = []
    for lay, members in groups:
        secs.append(
            f'<div class="lhead">{html.escape(lay)}'
            f'<span class="lcount">{len(members)}</span></div>'
            '<div class="grid">' + "".join(card_html(c) for c in members) + "</div>")
    arch_html = ('<a class="archlink" href="arch.html">layered architecture &#8594;</a>'
                 if arch else "")
    slices_html = ""
    if slices:
        ch: list[str] = []
        for s in slices:
            stale = (' <span class="slstale">stale</span>' if s.get("stale") else "")
            ch.append(f'<a class="slchip" href="{slice_page_stem(str(s["sid"]))}.html">'
                      f'{html.escape(str(s["name"]))}{stale}</a>')
        slices_html = ('<div class="slices"><span class="slsec">Business lines</span>'
                       + "".join(ch) + "</div>")
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>Module interaction chain · index</title><style>"
        "body{margin:0;background:#020617;color:#e2e8f0;"
        "font:14px/1.6 system-ui,'Segoe UI',sans-serif;padding:48px 24px}"
        ".wrap{max-width:1080px;margin:0 auto}"
        "h1{font:600 22px system-ui,sans-serif;margin:0 0 4px}"
        ".sub{color:#94a3b8;font:13px system-ui,sans-serif;margin-bottom:10px}"
        ".archlink{display:inline-block;color:#04121f;background:#22d3ee;"
        "border-radius:999px;padding:4px 14px;text-decoration:none;font-weight:600;"
        "font-size:12.5px;margin-bottom:24px}.archlink:hover{background:#67e8f9}"
        ".lhead{color:#67e8f9;font:600 12px system-ui,sans-serif;letter-spacing:.12em;"
        "text-transform:uppercase;margin:22px 0 10px}"
        ".lhead:first-of-type{margin-top:0}"
        ".lcount{color:#475569;margin-left:8px;letter-spacing:0}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}"
        ".card{display:block;background:#0b1226;border:1px solid #1e293b;"
        "border-radius:14px;padding:18px 18px 16px;text-decoration:none;color:#e2e8f0;"
        "transition:border-color .15s}"
        ".card:hover{border-color:#22d3ee}"
        ".k{color:#67e8f9;font:11px system-ui,sans-serif;margin-bottom:10px}"
        ".lay{display:inline-block;margin-left:8px;color:#c4b5fd;"
        "background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.3);"
        "border-radius:999px;padding:1px 8px}"
        "h2{margin:0 0 6px;font:600 16px system-ui,sans-serif;color:#f1f5f9}"
        ".r{color:#cbd5e1;font:12.5px/1.5 system-ui,sans-serif;margin-bottom:8px}"
        ".s{color:#94a3b8;font:12px system-ui,sans-serif}"
        ".d{color:#64748b;font:11px system-ui,sans-serif;margin-top:6px}"
        ".slices{margin-top:30px}"
        ".slsec{color:#fb923c;font:600 12px system-ui,sans-serif;letter-spacing:.12em;"
        "text-transform:uppercase;margin-right:10px}"
        ".slchip{display:inline-block;background:#0b1226;border:1px solid #1e293b;"
        "color:#fdba74;padding:4px 12px;border-radius:999px;font:12.5px system-ui,"
        "sans-serif;text-decoration:none;margin:0 6px 6px 0}"
        ".slchip:hover{border-color:#fb923c}"
        ".slstale{color:#64748b;font-size:10.5px}"
        "</style></head><body><div class=\"wrap\">"
        "<h1>Module interaction chain</h1>"
        f'<div class="sub">{len(cards)} modules · '
        f'{sum(c["n_files"] for c in cards)} files · '
            "each card is one AI-curated module (responsibility in its own words); "
            "click to enter its chain; click a node to select, use the drill button to go deeper</div>"
        + arch_html
        + "".join(secs)
        + slices_html
        + "</div></body></html>")
    (out_dir / "index.html").write_text(doc, encoding="utf-8", newline="\n")
