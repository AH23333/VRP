"""#144 测试：CBM 运行时双面——A 件升级就绪基建（cbm_version 溯源+status 软提示）
+ B 件运行时调用证据（vrp traces，学 CBM ingest_traces）。"""

import json
import sqlite3
from pathlib import Path

from vrp import cli, etl, s1_cbm, traces

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', '')")  # M4: 非 git 仓 = ''
    con.commit()
    con.close()
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _add_symbol(repo: Path, fid: str, qn: str) -> None:
    con = _con(repo)
    con.execute(
        "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
        " VALUES (?, 'p', ?, 'Function', ?, 'src/x.py', 1, 3, 'h')",
        (fid, qn, qn),
    )
    con.commit()
    con.close()


def _add_edge(repo: Path, src: str, dst: str, etype: str = "CALLS") -> None:
    con = _con(repo)
    con.execute(
        "INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?, ?, ?, 'structural')",
        (src, dst, etype),
    )
    con.commit()
    con.close()


def _write_traces(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _rows(repo: Path) -> list[tuple]:
    con = _con(repo)
    try:
        return con.execute(
            "SELECT label, caller_qn, callee_qn, count FROM runtime_calls ORDER BY caller_qn, callee_qn"
        ).fetchall()
    finally:
        con.close()


# ---------- B 件：ingest 语义 ----------


def test_ingest_expands_path_adjacent_pairs(tmp_path):
    """调用路径展开相邻对（a→b→c = (a,b)+(b,c)）；label 取文件字段。"""
    repo = _mk_repo(tmp_path)
    for fid, qn in (("f1", "pkg.a"), ("f2", "pkg.b"), ("f3", "pkg.c")):
        _add_symbol(repo, fid, qn)
    f = _write_traces(tmp_path, "t.json", {
        "traces": [{"path": ["pkg.a", "pkg.b", "pkg.c"]}],
        "label": "pytest run",
    })
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    assert out["ok"] and out["label"] == "pytest run"
    assert out["stored_pairs"] == 2 and out["skipped_unresolved"] == 0
    assert _rows(repo) == [
        ("pytest run", "pkg.a", "pkg.b", 1),
        ("pytest run", "pkg.b", "pkg.c", 1),
    ]


def test_ingest_bare_pairs_accumulate_count(tmp_path):
    """裸 caller/callee 对 + count 累计（同对多 trace 求和）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    _add_symbol(repo, "f2", "pkg.b")
    f = _write_traces(tmp_path, "t.json", {
        "traces": [
            {"caller": "pkg.a", "callee": "pkg.b", "count": 2},
            {"caller": "pkg.a", "callee": "pkg.b", "count": 3},
        ],
    })
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    assert out["label"] == "unlabeled"  # CBM 同默认
    assert _rows(repo) == [("unlabeled", "pkg.a", "pkg.b", 5)]


def test_ingest_label_whole_run_replace_idempotent(tmp_path):
    """per label 整批替换：同 label 重导入旧行清空，他 label 不受累。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    _add_symbol(repo, "f2", "pkg.b")
    f1 = _write_traces(tmp_path, "t1.json", {"traces": [{"caller": "pkg.a", "callee": "pkg.b"}], "label": "run-1"})
    f2 = _write_traces(tmp_path, "t2.json", {"traces": [], "label": "run-1"})
    f3 = _write_traces(tmp_path, "t3.json", {"traces": [{"caller": "pkg.b", "callee": "pkg.a"}], "label": "run-2"})
    con = _con(repo)
    try:
        traces.ingest_traces(con, f1)
        traces.ingest_traces(con, f3)
        assert _rows(repo) == [
            ("run-1", "pkg.a", "pkg.b", 1),
            ("run-2", "pkg.b", "pkg.a", 1),
        ]
        out = traces.ingest_traces(con, f2)  # run-1 重导入（空）——整批替换
    finally:
        con.close()
    assert out["stored_pairs"] == 0
    assert _rows(repo) == [("run-2", "pkg.b", "pkg.a", 1)]


def test_ingest_unresolved_skipped_and_sampled(tmp_path):
    """未解析 qn 诚实跳过+样本上报（cap 10），不静默丢。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    _add_symbol(repo, "f2", "pkg.b")
    f = _write_traces(tmp_path, "t.json", {
        "traces": [
            {"caller": "pkg.a", "callee": "pkg.b"},
            {"caller": "pkg.a", "callee": "pkg.gone"},
        ],
    })
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    assert out["stored_pairs"] == 1 and out["skipped_unresolved"] == 1
    assert out["unresolved_sample"] == ["pkg.gone"]
    assert _rows(repo) == [("unlabeled", "pkg.a", "pkg.b", 1)]


def test_ingest_all_unresolved_still_ok(tmp_path):
    """全未解析仍 ok（空导入合法），label 清空诚实。"""
    repo = _mk_repo(tmp_path)
    f = _write_traces(tmp_path, "t.json", {"traces": [{"caller": "x.y", "callee": "z.w"}]})
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    assert out["ok"] and out["stored_pairs"] == 0 and out["skipped_unresolved"] == 1
    assert _rows(repo) == []


def test_ingest_empty_traces_array_ok(tmp_path):
    repo = _mk_repo(tmp_path)
    f = _write_traces(tmp_path, "t.json", {"traces": []})
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    assert out["ok"] and out["trace_items"] == 0 and out["distinct_pairs"] == 0


def test_agreement_three_way_classification(tmp_path):
    """对账三分类：agreed / observed_only / static_only（qn→fid 对现行符号）。"""
    repo = _mk_repo(tmp_path)
    for fid, qn in (("f1", "pkg.a"), ("f2", "pkg.b"), ("f3", "pkg.c")):
        _add_symbol(repo, fid, qn)
    _add_edge(repo, "f1", "f2")           # a→b 静态有边 + 观测到 → agreed
    _add_edge(repo, "f2", "f3", "CALL_REFERENCE")  # b→c 静态有边未观测 → static_only
    f = _write_traces(tmp_path, "t.json", {
        "traces": [
            {"caller": "pkg.a", "callee": "pkg.b"},  # agreed
            {"caller": "pkg.a", "callee": "pkg.c"},  # observed_only（静态无边）
        ],
    })
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    ag = out["agreement"]
    assert ag["agreed"] == 1 and ag["observed_only"] == 1
    assert ag["static_only"] == 1 and ag["static_pairs_total"] == 2
    assert ag["observed_only_sample"] == ["pkg.a -> pkg.c"]


def test_ingest_two_runs_deterministic_business_columns(tmp_path):
    """两跑业务列（label/qn 对/count）确定——created_at 时间戳除外。"""
    runs = []
    for i in (1, 2):
        repo = _mk_repo(tmp_path / f"r{i}")
        _add_symbol(repo, "f1", "pkg.a")
        _add_symbol(repo, "f2", "pkg.b")
        f = _write_traces(tmp_path / f"r{i}", "t.json", {
            "traces": [{"path": ["pkg.a", "pkg.b"]}, {"caller": "pkg.b", "callee": "pkg.a", "count": 4}],
            "label": "run",
        })
        con = _con(repo)
        try:
            traces.ingest_traces(con, f)
        finally:
            con.close()
        runs.append(_rows(repo))
    assert runs[0] == runs[1]


def test_ingest_self_loop_skipped(tmp_path):
    """自环跳过（静态边零自环口径一致）。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    f = _write_traces(tmp_path, "t.json", {"traces": [{"caller": "pkg.a", "callee": "pkg.a"}]})
    con = _con(repo)
    try:
        out = traces.ingest_traces(con, f)
    finally:
        con.close()
    assert out["distinct_pairs"] == 0 and out["stored_pairs"] == 0


def test_ingest_invalid_shapes_rejected(tmp_path):
    """形状违例整文件拒绝（诚实失败优于部分采信）。"""
    import pytest

    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    con = _con(repo)
    try:
        with pytest.raises(SystemExit, match="not valid JSON"):
            traces.ingest_traces(con, bad)
        with pytest.raises(SystemExit, match="must be a JSON object"):
            traces.ingest_traces(con, _write_traces(tmp_path, "arr.json", [1, 2]))
        with pytest.raises(SystemExit, match="'traces' must be an array"):
            traces.ingest_traces(con, _write_traces(tmp_path, "nol.json", {"label": "x"}))
        with pytest.raises(SystemExit, match="must be an object"):
            traces.ingest_traces(con, _write_traces(tmp_path, "item.json", {"traces": ["nope"]}))
        with pytest.raises(SystemExit, match="2..256"):
            traces.ingest_traces(con, _write_traces(tmp_path, "short.json", {"traces": [{"path": ["only.one"]}]}))
        with pytest.raises(SystemExit, match="positive integer"):
            traces.ingest_traces(con, _write_traces(tmp_path, "cnt.json", {"traces": [{"caller": "pkg.a", "callee": "pkg.a", "count": 0}]}))
        with pytest.raises(SystemExit, match="file not found"):
            traces.ingest_traces(con, tmp_path / "missing.json")
    finally:
        con.close()
    assert _rows(repo) == []  # 拒绝路径零写入


def test_cli_traces_wiring(tmp_path, capsys):
    """CLI vrp traces 接线：--file/--label 经 _open_facts_rw（M4 闸）落库。"""
    repo = _mk_repo(tmp_path)
    _add_symbol(repo, "f1", "pkg.a")
    _add_symbol(repo, "f2", "pkg.b")
    f = _write_traces(tmp_path, "t.json", {"traces": [{"caller": "pkg.a", "callee": "pkg.b"}]})
    parser = cli.build_parser()
    args = parser.parse_args(["traces", str(repo), "--file", str(f), "--label", "cli-run", "--json"])
    rc = args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["label"] == "cli-run"
    assert _rows(repo) == [("cli-run", "pkg.a", "pkg.b", 1)]


def test_cli_traces_missing_facts_rejected(tmp_path, capsys):
    repo = tmp_path / "empty"
    (repo / ".vrp").mkdir(parents=True)
    f = _write_traces(tmp_path, "t.json", {"traces": []})
    parser = cli.build_parser()
    args = parser.parse_args(["traces", str(repo), "--file", str(f)])
    import pytest

    with pytest.raises(SystemExit, match="facts.db missing"):
        args.func(args)


# ---------- A 件：cbm_version 溯源 + status 软提示 ----------


def test_cbm_version_parses_tail(tmp_path, monkeypatch):
    """`--version` 输出取尾段；不做进程内缓存（模块状态纪律，test_module_scope）。"""
    fake = tmp_path / "fake-cbm.cmd"
    fake.write_text("@echo codebase-memory-mcp 9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(s1_cbm, "find_cbm", lambda: fake)
    assert s1_cbm.cbm_version() == "9.9.9"
    fake.write_text("@echo codebase-memory-mcp 1.0.0\n", encoding="utf-8")
    assert s1_cbm.cbm_version() == "1.0.0"  # 无缓存恒直查（纪律：零模块级可变状态）


def test_cbm_version_missing_exe_returns_none(tmp_path, monkeypatch):
    def _raise():
        raise FileNotFoundError("no cbm")

    monkeypatch.setattr(s1_cbm, "find_cbm", _raise)
    assert s1_cbm.cbm_version() is None


def test_write_meta_records_cbm_version(tmp_path, monkeypatch):
    """etl _write_meta 记 meta.cbm_version；查询失败→空串诚实。"""
    repo = _mk_repo(tmp_path)
    dst = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        monkeypatch.setattr(s1_cbm, "cbm_version", lambda timeout=30: "0.12.1")
        etl._write_meta(dst, "proj", {"db_uid": "u", "mutation_gen": "1"}, repo, {"symbols": 3})
        assert dst.execute("SELECT value FROM meta WHERE key = 'cbm_version'").fetchone()[0] == "0.12.1"
        monkeypatch.setattr(s1_cbm, "cbm_version", lambda timeout=30: None)
        etl._write_meta(dst, "proj", {}, repo, {})
        assert dst.execute("SELECT value FROM meta WHERE key = 'cbm_version'").fetchone()[0] == ""
    finally:
        dst.close()


def test_cmd_status_cbm_version_stale_faces(tmp_path, monkeypatch, capsys):
    """status cbm_version 节：stale true/false/None（unknown）三面。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_version', '0.12.1')")
    con.commit()
    con.close()
    parser = cli.build_parser()
    args = parser.parse_args(["status", str(repo), "--json"])
    for current, stale in (("0.13.0", True), ("0.12.1", False), (None, None)):
        monkeypatch.setattr(s1_cbm, "cbm_version", lambda timeout=30, c=current: c)
        args.func(args)
        out = json.loads(capsys.readouterr().out)
        node = out["cbm_version"]
        assert node["recorded"] == "0.12.1"
        assert node["current"] == current
        assert node["stale"] is stale


def test_cmd_status_cbm_version_no_record_unknown(tmp_path, monkeypatch, capsys):
    """pre-#144 库（meta 无 cbm_version）→ recorded None、stale None 诚实。"""
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(s1_cbm, "cbm_version", lambda timeout=30: "0.12.1")
    parser = cli.build_parser()
    args = parser.parse_args(["status", str(repo), "--json"])
    args.func(args)
    out = json.loads(capsys.readouterr().out)
    node = out["cbm_version"]
    assert node["recorded"] is None and node["stale"] is None
