"""J8: 文件粒度增量 ETL——在 facts.db 副本上打补丁（设计稿已批准 D1-D6）。

增量是优化，永不改语义：闸门未过（FullFallback）或本模块内部任何异常
都由 etl.run_etl 捕获后自动回落全量重建（D5）。等价判据（D6）：同输入
下增量 vs 全量逐表行集相等（tests/test_etl_incremental.py 对拍钉住；
findings.created_at / fid_ledger.first_seen_at / meta.etl_at 为时点性
元数据列，不参与对拍——全量路径自身也不满足字节自复现）。

检测（D2）：etl_file_state 自持久工作树快照（etl 时点全文件 sha256，
非 CBM file_hashes 转存——CBM 索引可能滞后工作树）。四分类：
unchanged / changed / new / deleted。有节点但不在 CBM 文件集的路径
（合成包节点等）无文件哈希可锚定，每轮重提取——全量路径每轮也重读
它们，两路同构。

崩溃安全（D3）：shutil.copy2 同目录副本 → 副本上事务内 patch →
os.replace——保 H2"中途失败旧库完好"。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import curate as curate_mod
from . import dbpath
from . import etl, s1_cbm, testsig
from . import evidence as ev

_CHUNK = 400  # IN 列表分块，低于 sqlite 变量上限

# 边族表级联删除列（M1 2026-11-03 裁定范围：edges + 边族表，不收窄）
_EDGE_FAMILY = (
    ("edges", "src_fid", "dst_fid"),
    ("usage_hints", "src_fid", "dst_fid"),
    ("history_coupling", "src_fid", "dst_fid"),
    ("test_linkage", "test_fid", "target_fid"),
)

# 行集对拍面（D6）：etl 写入的派生表；OWNED 表两路均零动/等价携带
EQUIV_TABLES = (
    "symbols",
    "edges",
    "usage_hints",
    "history_coupling",
    "test_linkage",
    "coverage_gaps",
    "file_hashes",
    "etl_file_state",
)


class FullFallback(Exception):
    """增量窗口不适用/一致性不满足——回落全量（语义面零变化）。"""


def _chunks(seq: list | set):
    seq = list(seq)
    for i in range(0, len(seq), _CHUNK):
        yield seq[i : i + _CHUNK]


def _read_meta(out_db: Path) -> dict[str, str]:
    con = dbpath.ro_connect(out_db)
    try:
        return {str(k): str(v) for k, v in con.execute("SELECT key, value FROM meta")}
    finally:
        con.close()


def _read_file_state(out_db: Path) -> dict[str, str]:
    con = dbpath.ro_connect(out_db)
    try:
        return {str(p): str(h) for p, h in con.execute("SELECT file_path, wt_hash FROM etl_file_state")}
    except sqlite3.OperationalError:
        raise FullFallback("no etl_file_state table (pre-J8 facts.db)")
    finally:
        con.close()


def _node_paths(src: sqlite3.Connection, project: str) -> set[str]:
    return {etl._norm(r[0]) for r in src.execute("SELECT DISTINCT file_path FROM nodes WHERE project = ?", (project,))}


def _delete_fid_touches(dst: sqlite3.Connection, fids: list[str]) -> None:
    """旧 fid 级联：边族表全删 + findings 仅 etl 源（非 etl findings 非本路职责）。"""
    for table, c0, c1 in _EDGE_FAMILY:
        for chunk in _chunks(fids):
            marks = ",".join("?" * len(chunk))
            dst.execute(
                f"DELETE FROM {table} WHERE {c0} IN ({marks}) OR {c1} IN ({marks})",
                chunk + chunk,
            )
    for chunk in _chunks(fids):
        marks = ",".join("?" * len(chunk))
        dst.execute(
            "DELETE FROM findings WHERE source = 'etl'"
            f" AND (anchor_fid IN ({marks}) OR related_fid IN ({marks}))",
            chunk + chunk,
        )


def run(repo: Path, db_path: Path, out_db: Path, project: str, store_meta: dict) -> dict:
    t0 = time.perf_counter()
    # ---- 闸门（D4）：cbm_version / cbm_db_uid 漂移强制全量；mutation_gen 仅记档不设闸
    meta = _read_meta(out_db)
    if meta.get("cbm_version") != (s1_cbm.cbm_version() or ""):
        raise FullFallback(
            f"cbm_version drift: {meta.get('cbm_version')!r} -> {s1_cbm.cbm_version()!r}"
        )
    if meta.get("cbm_db_uid") != store_meta.get("db_uid", ""):
        raise FullFallback(f"cbm_db_uid drift: {meta.get('cbm_db_uid')!r} -> {store_meta.get('db_uid')!r}")
    old_state = _read_file_state(out_db)
    if not old_state:
        raise FullFallback("empty etl_file_state")

    src = s1_cbm.connect_ro(db_path)
    try:
        t_classify = time.perf_counter()
        cbm_files = etl.cbm_file_set(src, project)
        hasher = etl._ContentHasher(repo)
        changed: set[str] = set()
        new: set[str] = set()
        for path in cbm_files:
            old = old_state.get(path)
            if old is None:
                new.add(path)
            elif old != hasher.wt_hash(path):
                changed.add(path)
        node_paths = _node_paths(src, project)
        synthetic = node_paths - cbm_files  # 无文件哈希可锚定：每轮重提取（与全量同构）
        reextract = changed | new | synthetic
        deleted = set(old_state) - cbm_files - node_paths
        t_classify = time.perf_counter() - t_classify

        # ---- D3：库副本 + patch + os.replace（H2 中途失败旧库完好）
        tmp_db = out_db.with_name(f"{out_db.name}.tmp-{os.getpid()}")
        tmp_db.unlink(missing_ok=True)
        shutil.copy2(out_db, tmp_db)
        dst = sqlite3.connect(tmp_db)
        try:
            dst.executescript(etl.SCHEMA_PATH.read_text(encoding="utf-8"))  # 自愈迁移 etl_file_state
            t_patch = time.perf_counter()
            stats, noise = _patch(
                dst, src, hasher, repo, project,
                reextract=reextract, deleted=deleted,
                changed=changed, new=new, synthetic=synthetic,
                cbm_files=cbm_files,
            )
            stats["t_patch_s"] = round(time.perf_counter() - t_patch, 3)
            stats["t_classify_s"] = round(t_classify, 3)
            stats["t_total_s"] = round(time.perf_counter() - t0, 3)
            etl._write_meta(dst, project, store_meta, repo, stats)
            dst.commit()
            dst.close()
        except (Exception, KeyboardInterrupt):
            # KI 同样走清理边界后重抛（etl.run_etl 全量路径同款纪律）
            dst.close()
            tmp_db.unlink(missing_ok=True)
            raise
        os.replace(tmp_db, out_db)
    finally:
        src.close()

    (out_db.parent / "artifact-hints.json").write_text(
        json.dumps(noise, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    stats["facts_db"] = str(out_db)
    return stats


def _patch(
    dst: sqlite3.Connection,
    src: sqlite3.Connection,
    hasher: etl._ContentHasher,
    repo: Path,
    project: str,
    reextract: set[str],
    deleted: set[str],
    changed: set[str],
    new: set[str],
    synthetic: set[str],
    cbm_files: set[str],
) -> tuple[dict, dict]:
    stats: dict[str, object] = {
        "mode": "incremental",
        "files_unchanged": len(cbm_files - changed - new),
        "files_changed": len(changed),
        "files_new": len(new),
        "files_deleted": len(deleted),
        "files_synthetic": len(synthetic),
        "dangling_edges": 0,
    }
    cleared_paths = reextract | deleted

    # ---- 旧符号锚点（删除前全量读回：交叉边解析 + 携带校验的原料）----
    old_symbols: dict[tuple[str, str], str] = {}  # (project, qn) -> 幸存行 fid
    old_by_file: dict[str, set] = {}  # 未触碰文件携带校验面：(qn, s, e) 集合
    old_fids_by_file: dict[str, list[str]] = {}
    for fid, qn, fpath, s, e in dst.execute(
        "SELECT fid, qualified_name, file_path, start_line, end_line FROM symbols WHERE project = ?",
        (project,),
    ):
        old_symbols[(project, qn)] = fid
        old_fids_by_file.setdefault(fpath, []).append(fid)
        if fpath not in cleared_paths:
            old_by_file.setdefault(fpath, set()).add(((project, qn), s, e))

    touched_fids: list[str] = []
    for path in cleared_paths:
        touched_fids.extend(old_fids_by_file.get(path, []))

    # ---- 清行：cleared 文件符号 + 旧 fid 边族级联 ----
    for chunk in _chunks(cleared_paths):
        marks = ",".join("?" * len(chunk))
        dst.execute(f"DELETE FROM symbols WHERE file_path IN ({marks})", chunk)
    _delete_fid_touches(dst, touched_fids)

    # ---- 提取：单遍扫描项目节点（重提取集全处理，未触碰集只记账零读盘）----
    symbol_rows = []
    fid_by_id: dict[int, str] = {}
    file_by_id: dict[int, str] = {}
    replaced: set[str] = set()
    surviving: dict[tuple[str, str], str] = {}
    u_nodes: dict[tuple[str, str], list[int]] = {}  # 未触碰 qn -> 节点序（扫描序）
    u_range: dict[int, tuple] = {}
    seen_ids: set[int] = set()
    for r in src.execute(
        "SELECT id, label, name, qualified_name, file_path, start_line, end_line, properties"
        " FROM nodes WHERE project = ?",
        (project,),
    ):
        node_id, label, name, qn, file_path, start_line, end_line, props = r
        rel = etl._norm(file_path)
        key = (project, qn or name)
        seen_ids.add(node_id)
        file_by_id[node_id] = rel
        if rel in reextract:
            content_hash = hasher.definition_hash(rel, start_line, end_line)
            fid = etl.cast_fid(project, qn or name, content_hash)
            fid_by_id[node_id] = fid
            prev = surviving.get(key)
            if prev is not None and prev != fid:
                replaced.add(prev)  # M1：同 qn 后扫描者幸存，先者入 replaced
            surviving[key] = fid
            symbol_rows.append(
                (fid, project, qn or name, label, name, rel, start_line, end_line, content_hash, props)
            )
        else:
            u_nodes.setdefault(key, []).append(node_id)
            u_range[node_id] = (start_line, end_line)

    # ---- 闸门 A：重提取 qn 撞未触碰节点——M1 幸存序携带无法复现，回落全量
    for key in surviving:
        if key in u_nodes:
            raise FullFallback(f"qn {key[1]!r} collides across re-extracted and untouched files")

    # ---- 未触碰面携带校验（verify）：每文件旧行 (qn,s,e) 集 == 当前幸存节点集。
    # 不满足=CBM 节点面在 wt 哈希盲区漂移（重排/换锚），携带会偏离全量——回落。
    survivor_set_by_file: dict[str, set] = {}
    untouched_fid: dict[int, str] = {}
    untouched_loser: set[int] = set()
    for key, ids in u_nodes.items():
        last = ids[-1]
        survivor_set_by_file.setdefault(file_by_id[last], set()).add((key, u_range[last][0], u_range[last][1]))
        untouched_fid[last] = old_symbols.get(key, "")
        for nid in ids[:-1]:
            untouched_loser.add(nid)
    for fpath, rows_set in old_by_file.items():
        if survivor_set_by_file.get(fpath, set()) != rows_set:
            raise FullFallback(f"carry check failed: symbol rows on {fpath} drifted from CBM nodes")
    for fpath, sset in survivor_set_by_file.items():
        if old_by_file.get(fpath, set()) != sset:
            raise FullFallback(f"carry check failed: CBM nodes on {fpath} drifted from symbol rows")

    # ---- 符号落库（重提取集）+ F1 ledger 增行 ----
    dst.executemany(
        "INSERT OR REPLACE INTO symbols (fid, project, qualified_name, label, name, file_path,"
        " start_line, end_line, content_hash, properties_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        symbol_rows,
    )
    now = etl._now()
    dst.executemany(
        "INSERT OR IGNORE INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
        " VALUES (?,?,?,?,?)",
        [(r[0], r[1], r[2], r[5], now) for r in symbol_rows],
    )

    # ---- 边重算：触碰重提取节点的边重算，其余携带（原地不动）----
    edge_rows, usage_rows, history_rows, tests_rows, findings_rows = [], [], [], [], []
    seen_js_ts: set[tuple[str, str]] = set()
    seen_rust: set[tuple[str, str]] = set()

    def _resolve(node_id: int) -> str | None:
        fid = fid_by_id.get(node_id)
        if fid is not None:
            return fid
        return untouched_fid.get(node_id)

    for r in src.execute(
        "SELECT source_id, target_id, type, properties FROM edges WHERE project = ?", (project,)
    ):
        src_id, dst_id, etype, props_raw = r
        if src_id not in fid_by_id and dst_id not in fid_by_id:
            continue  # 两端均未触碰：携带行原地保留
        src_fid = _resolve(src_id)
        dst_fid = _resolve(dst_id)
        if not src_fid or not dst_fid:
            if src_id not in seen_ids or dst_id not in seen_ids:
                stats["dangling_edges"] = int(stats["dangling_edges"]) + 1
            continue
        if src_fid in replaced or dst_fid in replaced:
            continue  # M1 级联：被替换符号的边随符号消亡
        props = etl._decode_props(props_raw)
        table, level = ev.route_edge(etype, props.get("strategy"), props.get("confidence"))

        if table == "findings_p2":
            payload = etl._similar_to_payload(props)
            findings_rows.append((src_fid, dst_fid, payload))
        elif table == "history_coupling":
            history_rows.append((src_fid, dst_fid, props_raw))
        elif table == "test_linkage":
            if testsig.is_synthetic_path(file_by_id.get(dst_id, "")):
                continue
            tests_rows.append((src_fid, dst_fid, "cbm", props_raw))
        elif table == "usage_hints":
            usage_rows.append((src_fid, dst_fid, props_raw))
        else:
            edge_rows.append(
                (
                    src_fid,
                    dst_fid,
                    etype,
                    level,
                    props.get("strategy"),
                    props.get("confidence"),
                    props.get("line"),
                    props_raw,
                )
            )

        if etype in ("CALLS", "USAGE"):
            src_file = file_by_id.get(src_id, "")
            dst_file = file_by_id.get(dst_id, "")
            if testsig.should_self_build_tests(src_file, dst_file):
                key = (src_fid, dst_fid)
                if key not in seen_js_ts:
                    seen_js_ts.add(key)
                    tests_rows.append((src_fid, dst_fid, "etl-js-ts", props_raw))
            elif testsig.should_self_build_tests_rust(src_file, dst_file):
                key = (src_fid, dst_fid)
                if key not in seen_rust:
                    seen_rust.add(key)
                    tests_rows.append((src_fid, dst_fid, "etl-rust", props_raw))

    dst.executemany(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence, strategy, confidence, line, properties_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        edge_rows,
    )
    dst.executemany("INSERT INTO usage_hints (src_fid, dst_fid, properties_json) VALUES (?,?,?)", usage_rows)
    dst.executemany("INSERT INTO history_coupling (src_fid, dst_fid, properties_json) VALUES (?,?,?)", history_rows)
    dst.executemany("INSERT INTO test_linkage (test_fid, target_fid, source, properties_json) VALUES (?,?,?,?)", tests_rows)
    dst.executemany(
        "INSERT INTO findings (pathology, kind, source, anchor_fid, related_fid,"
        " file_path, start_line, end_line, payload_json)"
        " SELECT 'P2', 'similar_to', 'etl', ?, ?, s.file_path, s.start_line,"
        " s.end_line, ? FROM symbols s WHERE s.fid = ?",
        [(a, b, p, a) for a, b, p in findings_rows],
    )

    # ---- 聚合面：coverage/file_hashes 全量重算（单表读，廉价）；ledger 与全量同构清空 ----
    dst.execute("DELETE FROM coverage_gaps")
    dst.execute("DELETE FROM file_hashes")
    stats.update(etl._etl_coverage(dst, src, project))
    dst.execute("DELETE FROM discrepancy_ledger")  # 非 OWNED：全量重建每轮清空，增量同构
    stats["file_state"] = etl.write_file_state(dst, hasher, src, project)

    # ---- 计数（与全量同语义：库内总量，非本轮增量）----
    stats["symbols"] = dst.execute(
        "SELECT COUNT(*) FROM symbols WHERE project = ?", (project,)
    ).fetchone()[0]
    for k in ("edges", "usage_hints", "history_coupling"):
        stats[k] = dst.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0]
    for tag, key in (
        ("cbm", "test_linkage_cbm"),
        ("etl-js-ts", "test_linkage_js_ts"),
        ("etl-rust", "test_linkage_rust"),
    ):
        stats[key] = dst.execute("SELECT COUNT(*) FROM test_linkage WHERE source = ?", (tag,)).fetchone()[0]
    stats["findings_p2"] = dst.execute(
        "SELECT COUNT(*) FROM findings WHERE source = 'etl' AND kind = 'similar_to'"
    ).fetchone()[0]

    noise = etl.detect_artifact_noise(dst)
    stats["artifact_hints"] = len(noise["hints"])
    # F2：fid 轮换时刻照跑（git_head=新批次锚，event=etl-incremental——批④ A3 前置历史事件流）
    revalidated = curate_mod._revalidate_ingested(
        dst, git_head=etl.current_git_head(repo), event="etl-incremental"
    )
    stats["curate_stale_slices"] = len(revalidated["slices"])
    stats["curate_stale_structure"] = 1 if revalidated["structure"] else 0

    # meta count_* 卫生：先清陈年计数键，再由 _write_meta 写本轮计数
    dst.execute("DELETE FROM meta WHERE key LIKE 'count_%'")
    return stats, noise
