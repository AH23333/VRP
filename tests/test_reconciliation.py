"""S 阶段产物对账锁定（reconciliation）：report/chain/SARIF 数字必须可从 facts.db 复算。

背景：产物自检曾抓到 s1 fid_clicks 方向 bug（#88）——产物↔库对账是高价值面。
本文件锁「用户产物数字 = SQL 直查结果」：体检计数 / findings 索引 / 总览计数 /
SARIF / modules-index / chain 卡片（n_files/n_syms/n_dirs/depends_on）全部对账
facts.db，DB 侧真值一律独立 SQL 复算，不调用生产 helper（防循环论证）。
"""

import json
import re
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from vrp import render

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")
    con.commit()
    con.close()
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _seed_rich(repo: Path) -> None:
    """四符号双模块 fixture：跨模块边 + 三码七 findings + active/stale slices + structure。"""
    con = _con(repo)
    for fid, qn, name, fp, lo in (
        ("fid-a", "pkg.a", "a", "pkg/x.py", 1),
        ("fid-b", "pkg.b", "b", "pkg/x.py", 5),
        ("fid-c", "pkg.c", "c", "pkg/x.py", 9),
        ("fid-d", "pkg.d", "d", "pkg/y.py", 1),
    ):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, ?, ?, ?, 'h')",
            (fid, qn, name, fp, lo, lo + 2),
        )
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-a', 'fid-b', 'CALLS', 'structural')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-b', 'fid-c', 'CALLS', 'structural')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-c', 'fid-d', 'CALLS', 'structural')")
    findings = (
        # (pathology, kind, source, file_path, start_line, anchor_fid)
        ("P4", "dead-code", "s2-vulture", "pkg/x.py", 3, "fid-a"),
        ("P4", "dead-code", "s2-vulture", "pkg/x.py", 7, "fid-b"),
        ("P4", "unused", "s2-deadcode", "pkg/y.py", 2, "fid-d"),
        ("P1", "debug-residue", "s2-astgrep", "pkg/x.py", 2, "fid-a"),
        ("P1", "todo-residue", "s2-vulture", "pkg/x.py", 10, "fid-c"),
        ("P2", "dup-cluster", "etl", "pkg/x.py", 1, None),
        ("P3", "long-fn", "s2-jscpd", "", None, None),  # 无文件路径：进体检/索引，不进 SARIF
    )
    for pathology, kind, source, fp, line, anchor in findings:
        con.execute(
            "INSERT INTO findings (pathology, kind, source, file_path, start_line, anchor_fid, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (pathology, kind, source, fp, line, anchor),
        )
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status) VALUES ('flow', '主线', 's', ?, 'active')",
        (json.dumps({
            "steps": [{"fid": "fid-a", "role": "entry"}, {"fid": "fid-b"}, {"fid": "fid-c", "role": "persist"}],
            "acts": [{"name": "act-one", "from_step": 0, "to_step": 1}],
        }),),
    )
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status) VALUES ('old', '旧线', 's', ?, 'stale')",
        (json.dumps({"steps": [{"fid": "fid-a"}, {"fid": "fid-b"}]}),),
    )
    con.execute(
        "INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
        (json.dumps({
            "modules": [
                {"module_id": "m1", "name": "甲", "responsibility": "负责一", "symbol_fids": ["fid-a", "fid-b"], "layer": "domain"},
                {"module_id": "m2", "name": "乙", "responsibility": "负责二", "symbol_fids": ["fid-c", "fid-d"], "layer": "service"},
            ],
            "layers": [
                {"name": "domain", "may_depend_on": []},
                {"name": "service", "may_depend_on": ["domain"]},
            ],
        }),),
    )
    con.commit()
    con.close()
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / "pkg" / "x.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n", encoding="utf-8")
    (repo / "pkg" / "y.py").write_text("def d():\n    return 4\n", encoding="utf-8")


def _mk_verdict(repo: Path, tid: str) -> None:
    d = repo / ".vrp" / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({
        "tid": tid, "type": "T2-rename", "summary": "s", "status": "committed",
        "verdict_at": "2026-11-06T10:00:00+00:00", "gates": [], "confidence": 0.9,
    }), encoding="utf-8")


def _section(html_text: str, title: str) -> str:
    """按 <h2> 切节，返回 title 节正文（含下一 <h2> 前全部内容）。"""
    for part in html_text.split("<h2>")[1:]:
        if part.startswith(f"{title}</h2>"):
            return part
    raise AssertionError(f"section not found: {title}")


def _need_archify():
    if render.find_archify_mjs() is None:
        pytest.skip("archify not available")
    if shutil.which("node") is None:
        pytest.skip("node not available")


# --------------------------------------------------------------- 对账锁定


def test_checkup_counts_reconcile_db(tmp_path, monkeypatch):
    """体检节：每码计数 + 每码来源细分 = findings 表 GROUP BY 直查。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    render.render_report(repo)
    t = (repo / ".vrp" / "report" / "report.html").read_text(encoding="utf-8")
    sec = _section(t, "Pathology checkup")
    # 产物侧：每码 (code, count)
    shown = re.findall(r"<h3>(P\d+) - [^<]+</h3><p><b>(\d+) finding\(s\)</b>", sec)
    shown_codes = [(c, int(n)) for c, n in shown]
    # DB 侧真值（同序：count DESC, pathology ASC）
    con = _con(repo)
    try:
        db_codes = [(r[0], r[1]) for r in con.execute(
            "SELECT pathology, COUNT(*) AS c FROM findings GROUP BY pathology ORDER BY c DESC, pathology"
        ).fetchall()]
        db_src = {}
        for pathology, source, c in con.execute(
            "SELECT pathology, source, COUNT(*) FROM findings GROUP BY pathology, source"
        ):
            db_src.setdefault(pathology, {})[source] = c
    finally:
        con.close()
    assert shown_codes == db_codes
    # 产物侧：来源细分（每码段落里 "src: N" 列表）
    for code, src_bits in re.findall(r"<h3>(P\d+) - [^<]+</h3><p><b>\d+ finding\(s\)</b>(?: - ([^<]*))?</p>", sec):
        shown_src = {}
        for src, n in re.findall(r"([A-Za-z0-9_-]+): (\d+)", src_bits or ""):
            shown_src[src] = int(n)
        assert shown_src == db_src[code], f"source breakdown mismatch for {code}"


def test_findings_index_reconcile_db(tmp_path, monkeypatch):
    """findings 全索引：条目数 = min(总数, cap)；每码条目数 = DB 每码计数；锚点链接可达。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    render.render_report(repo)
    out = repo / ".vrp" / "report"
    t = (out / "report.html").read_text(encoding="utf-8")
    sec = _section(t, "Pathology findings index")
    items = re.findall(r"<li><span class=\"badge\"[^>]*>(P\d+)</span>", sec)
    con = _con(repo)
    try:
        total = con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        db_per_code = Counter(r[0] for r in con.execute("SELECT pathology FROM findings"))
    finally:
        con.close()
    assert total == 7
    assert len(items) == min(total, render.FINDINGS_INDEX_CAP)
    assert Counter(items) == db_per_code
    assert "truncated" not in sec  # 7 < 200：无截断注记
    # 锚点链接完整性：href 指向的符号页必须存在
    for fid in re.findall(r'href="symbols/([^"]+)\.html"', sec):
        assert (out / "symbols" / f"{fid}.html").is_file(), f"broken symbol link: {fid}"


def test_findings_index_truncation_reconcile_db(tmp_path, monkeypatch):
    """205 条 findings：索引截断到 cap + 截断注记诚实；SARIF 不截断（cap 2000）。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    con = _con(repo)
    for i in range(205):
        con.execute(
            "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
            " VALUES ('P2', 'dup', 'etl', 'pkg/x.py', ?, '{}')",
            (i + 1,),
        )
    con.commit()
    con.close()
    render.render_report(repo)
    out = repo / ".vrp" / "report"
    sec = _section((out / "report.html").read_text(encoding="utf-8"), "Pathology findings index")
    items = re.findall(r"<li><span class=\"badge\"[^>]*>(P\d+)</span>", sec)
    assert len(items) == render.FINDINGS_INDEX_CAP == 200
    m = re.search(r"Showing (\d+) of (\d+) findings \(truncated\)", sec)
    assert m and (int(m.group(1)), int(m.group(2))) == (200, 212)  # 205 + seed 7
    sarif = json.loads((out / "findings.sarif").read_text(encoding="utf-8"))
    with_file = _con(repo).execute(
        "SELECT COUNT(*) FROM findings WHERE file_path IS NOT NULL AND file_path != ''"
    ).fetchone()[0]
    assert len(sarif["runs"][0]["results"]) == with_file  # 211（P3 无文件路径者除外）


def test_sarif_reconcile_db(tmp_path, monkeypatch):
    """SARIF results 数 = 有文件路径的 findings 数（cap 2000 内）。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    render.render_report(repo)
    sarif = json.loads((repo / ".vrp" / "report" / "findings.sarif").read_text(encoding="utf-8"))
    con = _con(repo)
    try:
        expected = con.execute(
            "SELECT COUNT(*) FROM findings WHERE file_path IS NOT NULL AND file_path != ''"
        ).fetchone()[0]
    finally:
        con.close()
    assert len(sarif["runs"][0]["results"]) == expected == 6  # P3 无文件路径不入 SARIF


def test_overview_counts_reconcile_db(tmp_path, monkeypatch):
    """总览：模块/层/业务线/verdict 计数 = structure 载荷 + active slices + verdict 文件数；
    模块表 Symbols 列 = 载荷 symbol_fids 数。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    _mk_verdict(repo, "tx-1")
    _mk_verdict(repo, "tx-2")
    render.render_report(repo)
    t = (repo / ".vrp" / "report" / "report.html").read_text(encoding="utf-8")
    sec = _section(t, "Project overview")
    m = re.search(r"(\d+) modules across (\d+) layers(?:, (\d+) business slice\(s\), (\d+) refactor verdict\(s\))?\.", sec)
    assert m, "overview count sentence missing"
    con = _con(repo)
    try:
        payload = json.loads(con.execute(
            "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        n_active_slices = con.execute(
            "SELECT COUNT(*) FROM slices WHERE status = 'active'"
        ).fetchone()[0]
    finally:
        con.close()
    assert int(m.group(1)) == len(payload["modules"]) == 2
    assert int(m.group(2)) == len(payload["layers"]) == 2
    assert int(m.group(3)) == n_active_slices == 1  # stale 'old' 不入
    assert int(m.group(4)) == 2
    # 模块表 Symbols 列
    rows = re.findall(r"<tr><td>(m\d)</td><td>[^<]*</td><td>[^<]*</td><td>(\d+)</td></tr>", sec)
    db_syms = {mod["module_id"]: len(mod["symbol_fids"]) for mod in payload["modules"]}
    assert {mid: int(n) for mid, n in rows} == db_syms


def test_modules_index_reconcile_db(tmp_path, monkeypatch):
    """降级路径（无 archify）：modules-index.json symbols = structure 载荷每模块 fid 数。"""
    monkeypatch.setattr(render, "find_archify_mjs", lambda: None)
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    render.render_report(repo)
    idx = json.loads((repo / ".vrp" / "report" / "ir" / "modules-index.json").read_text(encoding="utf-8"))
    con = _con(repo)
    try:
        payload = json.loads(con.execute(
            "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    finally:
        con.close()
    db_syms = {mod["module_id"]: len(mod["symbol_fids"]) for mod in payload["modules"]}
    assert {m["module_id"]: m["symbols"] for m in idx["modules"]} == db_syms


def test_chain_cards_reconcile_db(tmp_path):
    """chain 卡片对账：n_files/n_syms/n_dirs/depends_on = symbols/edges SQL 复算；
    index 头部计数与 report 链节 meta = chain-index counts。"""
    _need_archify()
    repo = _mk_repo(tmp_path)
    _seed_rich(repo)
    render.render_report(repo)
    out = repo / ".vrp" / "report"
    idx = json.loads((out / "ir" / "chain-index.json").read_text(encoding="utf-8"))
    con = _con(repo)
    try:
        payload = json.loads(con.execute(
            "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        fid_of = {mod["module_id"]: mod["symbol_fids"] for mod in payload["modules"]}
        for mid, fids in fid_of.items():
            marks = ",".join("?" * len(fids))
            files = [r[0] for r in con.execute(
                f"SELECT DISTINCT file_path FROM symbols WHERE fid IN ({marks}) AND file_path != ''",
                fids,
            )]
            # n_syms（v8 用户反馈二轮起）：模块文件的整文件全量真实符号
            # （curated 只是图为视角子集；卡片口径与网格/代码页一致）
            fmarks = ",".join("?" * len(files))
            n_syms_db = con.execute(
                f"SELECT COUNT(*) FROM symbols WHERE file_path IN ({fmarks})"
                " AND file_path != '' AND start_line > 0 AND label NOT IN ('File', 'Module')",
                files,
            ).fetchone()[0]
            # 目录闭包（module_fs 同义复算：文件父链全部祖先目录）
            dirs: set[str] = set()
            for fp in files:
                parts = fp.replace("\\", "/").split("/")
                for k in range(1, len(parts)):
                    dirs.add("/".join(parts[:k]))
            card = next(c for c in idx["modules"] if c["module_id"] == mid)
            assert card["n_syms"] == n_syms_db, mid
            assert card["n_files"] == len(files), mid
            assert card["n_dirs"] == len(dirs), mid
        # depends_on：跨模块边聚合（edges SQL 复算）
        m1, m2 = fid_of["m1"], fid_of["m2"]
        marks1, marks2 = ",".join("?" * len(m1)), ",".join("?" * len(m2))
        cross_1_2 = con.execute(
            f"SELECT COUNT(*) FROM edges WHERE src_fid IN ({marks1}) AND dst_fid IN ({marks2})",
            (*m1, *m2),
        ).fetchone()[0]
        cross_2_1 = con.execute(
            f"SELECT COUNT(*) FROM edges WHERE src_fid IN ({marks2}) AND dst_fid IN ({marks1})",
            (*m2, *m1),
        ).fetchone()[0]
    finally:
        con.close()
    c1 = next(c for c in idx["modules"] if c["module_id"] == "m1")
    c2 = next(c for c in idx["modules"] if c["module_id"] == "m2")
    assert c1["depends_on"] == (["m2"] if cross_1_2 else [])
    assert c2["depends_on"] == ([] if not cross_2_1 else ["m1"])
    # index.html 头部：模块数 + 文件总数
    idx_html = (out / "chain" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"(\d+) modules · (\d+) files", idx_html)
    assert m and int(m.group(1)) == len(idx["modules"])
    assert int(m.group(2)) == sum(c["n_files"] for c in idx["modules"])
    # report.html 链节 meta = chain-index counts
    sec = _section((out / "report.html").read_text(encoding="utf-8"), "Module interaction chain")
    counts = idx["counts"]
    mm = re.search(
        r"(\d+) modules / (\d+) diagrams / (\d+) code pages / (\d+) pages total",
        sec,
    )
    assert mm
    assert (int(mm.group(1)), int(mm.group(2)), int(mm.group(3)), int(mm.group(4))) == (
        counts["modules"], counts["diagrams"], counts["file_pages"], counts["pages"],
    )
