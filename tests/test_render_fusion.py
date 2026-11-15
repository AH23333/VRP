# -*- coding: utf-8 -*-
"""V-2 融合报告（vrp/render_fusion.py）测试。

覆盖：确定性（两跑字节一致）/ D JSON 结构 / 行级锚点联动 chain 页 /
rowsTruncated cap 逻辑 / 主题令牌同源锁（archify 令牌值出现在 chain 页）/
chain 缺席降级（fusion 仍出、无链接诚实注记）/ render_report 集成。
"""
import json
import re
import sqlite3
import tempfile
from pathlib import Path

import shutil

import pytest

from vrp import render, render_chain, render_fusion, render_theme

SCHEMA = Path(__file__).resolve().parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo():
    tmp = Path(tempfile.mkdtemp(prefix="t_fusion_"))
    repo = tmp / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return tmp, repo, con


def _seed(con):
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'abc1234')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_agreement_pct', '71.2')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_cbm_calls', '120')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_ca_calls', '118')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_agreement', '90')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_ca_only', '12')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_ca_only_builtin_target', '8')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_cbm_only', '5')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_cbm_only_test_source', '2')")
    con.execute("INSERT INTO meta (key, value) VALUES ('xcheck_ledger_rows', '95')")
    for i, name in enumerate(("alpha", "beta", "gamma")):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
            " start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, 'pkg/x.py', ?, ?, 'h')",
            (f"fid-{name}", f"pkg.{name}", name, i * 3 + 1, i * 3 + 3))
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence, confidence)"
                " VALUES ('fid-alpha', 'fid-beta', 'CALLS', 'structural', 1.0)")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence, confidence)"
                " VALUES ('fid-beta', 'fid-gamma', 'CALLS', 'structural', 0.9)")
    con.execute("INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
                " VALUES ('P1', 'kind-a', 's2-astgrep', 'pkg/x.py', 2, '{}')")
    con.execute("INSERT INTO slices (slice_id, name, summary, payload_json, status)"
                " VALUES ('flow', '主线', 's', ?, 'active')",
                (json.dumps({
                    "steps": [{"fid": "fid-alpha", "role": "entry"},
                              {"fid": "fid-beta"}, {"fid": "fid-gamma", "role": "persist"}],
                    "acts": [{"name": "幕一", "from_step": 0, "to_step": 1}]}),))
    con.execute("INSERT INTO structure (payload_json, status) VALUES (?, 'active')",
                (json.dumps({"modules": [
                    {"module_id": "m1", "name": "甲", "responsibility": "负责一",
                     "symbol_fids": ["fid-alpha", "fid-beta"], "layer": "domain"},
                    {"module_id": "m2", "name": "乙", "responsibility": "负责二",
                     "symbol_fids": ["fid-gamma"], "layer": "service"}],
                    "layers": [{"name": "domain", "may_depend_on": []},
                               {"name": "service", "may_depend_on": ["domain"]}]}),))
    con.commit()
    con.close()


def _seed_files(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "x.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n\n\ndef gamma():\n    return 3\n",
        encoding="utf-8")


def _build_chain(repo, out):
    mjs = render.find_archify_mjs()
    if mjs is None or shutil.which("node") is None:
        return None
    return render_chain.build_chain(repo, out / "chain", mjs)


def _extract_D(html):
    m = re.search(r"const D = (\{.*?\});\n", html, re.S)
    assert m, "fusion.html 缺少 const D JSON"
    return json.loads(m.group(1).replace("<\\/", "</"))


# ---------- build_fusion 直接测试（不依赖 archify） ----------

def test_fusion_deterministic_and_structure():
    tmp, repo, con = _mk_repo()
    _seed(con)
    _seed_files(repo)
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    r1 = render_fusion.build_fusion(repo, out, None)
    f = out / "fusion.html"
    assert f.is_file()
    b1 = f.read_bytes()
    r2 = render_fusion.build_fusion(repo, out, None)
    assert f.read_bytes() == b1, "两跑 fusion.html 字节不一致（复现性红线）"
    assert r1 == r2

    t = b1.decode("utf-8")
    assert "__REPO__" not in t and "__DATA__" not in t and "__THEMES__" not in t
    assert "__ECHARTS__" not in t and "__NARR__" not in t
    assert "echarts" in t  # vendored ECharts 已内联
    D = _extract_D(t)
    assert "repo" in t  # 仓名落在页面标题（__REPO__ 占位）
    assert D["cmp"]["agrpct"] == "71.2"
    assert D["rowsTruncated"] is False
    assert len(D["rows"]) == 1
    row = D["rows"][0]
    assert row["f"] == "pkg/x.py" and row["ln"] == 2
    assert row["h"] == "", "无 chain 时行 href 必须为空（诚实降级）"
    assert r1["notes"] == ["chain degraded/missing - fusion emitted without code page links"]


def test_fusion_theme_tokens_and_archify_shared_source():
    """同源锁：render_theme archify 令牌值必须出现在 fusion 页内。"""
    tmp, repo, con = _mk_repo()
    _seed(con)
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    render_fusion.build_fusion(repo, out, None)
    t = (out / "fusion.html").read_text(encoding="utf-8")
    assert render_theme.theme_root_css() in t
    for tok in ("--bg", "--panel", "--accent", "--txt"):
        assert tok in t
    arch = render_theme.THEMES["archify"]
    assert arch["--bg"] in t and arch["--accent"] in t


def test_fusion_row_cap_truncation():
    tmp, repo, con = _mk_repo()
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'h')")
    for i in range(render_fusion.ROW_ID_CAP + 5):
        con.execute(
            "INSERT INTO findings (pathology, kind, source, file_path, start_line, payload_json)"
            " VALUES ('P2', 'k', 's2-astgrep', 'pkg/x.py', ?, '{}')", (i + 1,))
    con.commit()
    con.close()
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    res = render_fusion.build_fusion(repo, out, None)
    D = _extract_D((out / "fusion.html").read_text(encoding="utf-8"))
    assert D["rowsTruncated"] is True
    assert len(D["rows"]) == render_fusion.ROW_ID_CAP
    assert [r["i"] for r in D["rows"][:3]] == [1, 2, 3], "cap 必须按 id 序（无偏置）"
    assert any("over embed cap" in n for n in res["notes"])


# ---------- chain 联动（需 archify mjs） ----------

def test_fusion_chain_line_anchor_linkage():
    if render.find_archify_mjs() is None or shutil.which("node") is None:
        pytest.skip("archify/node not available")
    tmp, repo, con = _mk_repo()
    _seed(con)
    _seed_files(repo)
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    mjs = render.find_archify_mjs()
    chain = render_chain.build_chain(repo, out / "chain", mjs)
    assert chain["ok"]
    render_fusion.build_fusion(repo, out, chain)
    code = (out / "chain" / "s4f-pkg-x.html").read_text(encoding="utf-8")
    assert 'id="L1"' in code and 'id="L2"' in code, "chain 页缺行级锚点"
    assert ".row:target" in code
    D = _extract_D((out / "fusion.html").read_text(encoding="utf-8"))
    row = D["rows"][0]
    assert row["h"] == "chain/s4f-pkg-x.html#L2", "行级跳转必须落 #L{ln}"
    assert D["pages"]["pkg/x.py"] == "chain/s4f-pkg-x.html"

    # 同源锁：archify 令牌值出现在 chain 页（V-9.3 主题令牌同源验证）
    arch = render_theme.THEMES["archify"]
    assert arch["--accent"] in code


# ---------- render_report 集成 ----------

def test_render_report_fusion_integration():
    tmp, repo, con = _mk_repo()
    _seed(con)
    _seed_files(repo)
    result = render.render_report(repo)
    out = repo / ".vrp" / "report"
    assert result["counts"]["fusion"] in (0, 1)
    assert "report.html" in result["artifacts"]
    rep = (out / "report.html").read_text(encoding="utf-8")
    f = out / "fusion.html"
    if f.is_file():
        assert "fusion.html" in result["artifacts"]
        assert 'href="fusion.html"' in rep, "report.html 缺 fusion 入口链接"
        b1 = f.read_bytes()
        render.render_report(repo)
        assert f.read_bytes() == b1, "集成路径两跑字节不一致"
    else:
        assert result["counts"]["fusion"] == 0
        assert any("fusion" in d for d in result["degraded"]), "fusion 缺席必须留降级注记"


# ---------- 渲染审计回归（AU1-AU5） ----------

def test_fusion_escapes_db_derived_strings():
    """AU1：DB 派生串（文件名/符号/kind）进 innerHTML 前必须过 JS esc()。

    D JSON 保留原始值（过滤/搜索不受影响）；模板插入点静态锁 esc 覆盖。
    """
    tmp, repo, con = _mk_repo()
    _seed(con)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
                " start_line, end_line, content_hash)"
                " VALUES ('fid-img', 'p', 'pkg.<img>', '<img>', '<img>',"
                " 'pkg/<i>x</i>.py', 1, 1, 'h')")
    con.execute("INSERT INTO findings (pathology, kind, source, file_path, start_line,"
                " payload_json, anchor_fid)"
                " VALUES ('P1', '<b>kind</b>', 's2-astgrep', 'pkg/<i>x</i>.py', 1, '{}', 'fid-img')")
    con.commit()
    con.close()
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    render_fusion.build_fusion(repo, out, None)
    t = (out / "fusion.html").read_text(encoding="utf-8")
    # esc 助手 + 全部 DB 派生插入点静态锁
    assert "const esc = s =>" in t
    assert "esc(r.f)+':'+r.ln" in t
    assert "'+esc(r.k)+'</td>'" in t
    assert "<td>'+esc(r.sym)+" in t
    assert "<h3>'+esc(p.name)+'</h3>" in t
    assert "+esc(r.s)+'</b>" in t
    # D JSON 保留原始值（运行时 esc，数据不变形）
    D = _extract_D(t)
    row = next(r for r in D["rows"] if r["f"] == "pkg/<i>x</i>.py")
    assert row["k"] == "<b>kind</b>"
    assert row["sym"] == "<img>"


def test_fusion_conf_null_bucket():
    """AU4：CALLS 边 confidence NULL 时分布桶标签必须是 "null"（非 Python "None"）。"""
    tmp, repo, con = _mk_repo()
    _seed(con)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence)"
                " VALUES ('fid-gamma', 'fid-alpha', 'CALLS', 'structural')")
    con.commit()
    con.close()
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    render_fusion.build_fusion(repo, out, None)
    D = _extract_D((out / "fusion.html").read_text(encoding="utf-8"))
    assert ["null", 1] in D["conf"]
    assert not any(c[0] == "None" for c in D["conf"])


def test_fusion_placeholder_collision_safe():
    """AU5：narr 含字面 "__DATA__"（如业务线名）不得被占位替换二次注入。

    单趟 re.sub：替换产物不重扫——data_json 只落一次。
    """
    tmp, repo, con = _mk_repo()
    _seed(con)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.execute("INSERT INTO slices (slice_id, name, summary, payload_json, status)"
                " VALUES ('dunder', '__DATA__', 's', ?, 'active')",
                (json.dumps({"steps": [{"fid": "fid-alpha"}],
                             "acts": [{"name": "m", "from_step": 0, "to_step": 0}]}),))
    con.commit()
    con.close()
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    render_fusion.build_fusion(repo, out, None)
    t = (out / "fusion.html").read_text(encoding="utf-8")
    assert t.count("const D = {") == 1, "const D 赋值必须唯一"
    assert t.count('"rowsTruncated"') == 1, "data_json 不得被 narr 占位二次注入"
    D = _extract_D(t)  # JSON 仍可解析
    assert ["dunder", "__DATA__"] in D["slices"]


def test_clean_render_out_removes_stale_fusion():
    """AU2：fusion.html 属 render 自有产物——陈旧文件必须随 _clean_render_out 清除。"""
    tmp, repo, con = _mk_repo()
    con.close()
    out = repo / ".vrp" / "report"
    (out / "chain").mkdir(parents=True)
    (out / "chain" / "old.html").write_text("x", encoding="utf-8")
    (out / "fusion.html").write_text("stale", encoding="utf-8")
    render._clean_render_out(out)
    assert not (out / "fusion.html").exists(), "陈旧 fusion.html 未清除（R6' 孤儿产物）"
    assert not (out / "chain").exists()


def test_chain_file_caps_match_page_anchors():
    """AU3：file_caps 必须等于页内实际最大行锚点（尾部多空行文件防幻影区间）。"""
    if render.find_archify_mjs() is None or shutil.which("node") is None:
        pytest.skip("archify/node not available")
    tmp, repo, con = _mk_repo()
    _seed(con)
    # 尾部多空行（旧实现 caps 只减一个空行 → cap 12 > 实际锚点上限 10）
    (repo / "pkg").mkdir()
    (repo / "pkg" / "x.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n\n\n"
        "def gamma():\n    return 3\n\n\n", encoding="utf-8")
    con.close()
    out = repo / ".vrp" / "report"
    out.mkdir(parents=True)
    mjs = render.find_archify_mjs()
    chain = render_chain.build_chain(repo, out / "chain", mjs)
    assert chain["ok"]
    assert chain["file_caps"], "fixture 必须产出至少一个文件页（否则断言空转）"
    for fp, cap in chain["file_caps"].items():
        code = (out / "chain" / chain["file_pages"][fp]).read_text(encoding="utf-8")
        ids = [int(m) for m in re.findall(r'id="L(\d+)"', code)]
        assert ids, f"{fp} 页内无行锚点"
        assert max(ids) == cap, f"{fp}: cap={cap} != 页内实际锚点上限={max(ids)}"
