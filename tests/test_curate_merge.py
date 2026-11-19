"""M16 curate 三方合并测试：六分支/字段级/base 生命周期/原子性/幂等/CLI。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import cli
from vrp import curate as curate_mod
from vrp import curate_merge
from vrp import etl

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


def _seed_chain(repo: Path) -> list[str]:
    """a -> b -> c 符号链 + 真实边（slices/structure 验证素材）。"""
    con = _con(repo)
    fids = []
    for i, name in enumerate(("a", "b", "c")):
        fid = f"fid-{name}"
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path, start_line, end_line, content_hash)"
            " VALUES (?, 'p', ?, 'Function', ?, ?, ?, ?, 'h')",
            (fid, f"pkg.{name}", name, "pkg/x.py", i + 1, i + 3),
        )
        fids.append(fid)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-a', 'fid-b', 'CALLS', 'structural')")
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES ('fid-b', 'fid-c', 'CALL_REFERENCE', 'structural')")
    con.commit()
    con.close()
    return fids


def _slice(fids: list[str], sid: str = "s1", name: str = "测试线", summary: str = "链路") -> dict:
    return {
        "slice_id": sid,
        "name": name,
        "summary": summary,
        "steps": [{"fid": f, "role": "process"} for f in fids],
    }


def _structure(mods: list[dict], layers: list[dict] | None = None) -> dict:
    return {"modules": mods, "layers": layers or [{"name": "domain", "may_depend_on": []}], "rules": []}


def _mod(mid: str, fids: list[str], layer: str = "domain") -> dict:
    return {"module_id": mid, "name": f"模块{mid}", "responsibility": f"职责{mid}", "symbol_fids": fids, "layer": layer}


def _write_theirs(tmp_path: Path, slices: list | None, structure: dict | None) -> Path:
    d = tmp_path / "theirs"
    d.mkdir(exist_ok=True)
    if slices is not None:
        (d / "slices.json").write_text(json.dumps(slices, ensure_ascii=False), encoding="utf-8")
    if structure is not None:
        (d / "structure.json").write_text(json.dumps(structure, ensure_ascii=False), encoding="utf-8")
    return d


def _ingest(repo: Path, slices: list | None = None, structure: dict | None = None) -> None:
    con = _con(repo)
    try:
        if slices is not None:
            curate_mod._ingest_slices(con, slices)
        if structure is not None:
            curate_mod._ingest_structure(con, structure)
    finally:
        con.close()


def _merge(repo: Path, theirs: Path) -> dict:
    con = _con(repo)
    try:
        return curate_merge.run_curate_merge(repo, con, theirs)
    finally:
        con.close()


def _slice_rows(repo: Path) -> dict[str, tuple[str, str]]:
    """slice_id -> (status, payload_json)。"""
    con = _con(repo)
    try:
        return dict(
            con.execute("SELECT slice_id, status || char(31) || payload_json FROM slices").fetchall()
        ) | {}
    finally:
        con.close()


def _decisions(repo: Path) -> list[tuple[str, str, str]]:
    con = _con(repo)
    try:
        return con.execute(
            "SELECT kind, target_id, decision FROM decisions WHERE kind = 'curate_merge' ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _active_structure(repo: Path) -> str | None:
    con = _con(repo)
    try:
        row = con.execute(
            "SELECT payload_json FROM structure WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _set_base(repo: Path, key: str, payload: dict) -> None:
    con = _con(repo)
    try:
        con.execute(
            "INSERT OR REPLACE INTO curate_base (key, payload_json) VALUES (?, ?)",
            (key, json.dumps(payload, ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------- slices 六分支


def test_theirs_only_adopted(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    theirs = _write_theirs(tmp_path, [_slice(fids)], None)
    result = _merge(repo, theirs)
    assert result["slices"]["adopted"] == ["s1"]
    rows = _slice_rows(repo)
    assert rows["s1"].split(chr(31))[0] == "active"


def test_ours_only_listed_kept(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, sid="s1")])
    theirs = _write_theirs(tmp_path, [_slice(fids, sid="s2")], None)
    result = _merge(repo, theirs)
    assert result["slices"]["adopted"] == ["s2"]
    assert result["slices"]["kept"] == ["s1"]  # ours-only：theirs 未触碰
    assert len(_slice_rows(repo)) == 2


def test_same_value_unchanged(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids)])
    theirs = _write_theirs(tmp_path, [_slice(fids)], None)
    result = _merge(repo, theirs)
    assert result["slices"]["unchanged"] == ["s1"]
    assert result["slices"]["adopted"] == []


def test_ours_unmoved_adopts_theirs(tmp_path):
    """ours==base → 采 theirs（ours 未动，theirs 是新梳理）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="v1")])
    _ingest(repo, slices=[_slice(fids, summary="v2")])
    # 公共路径后 base['slice:s1'] 已由第二次 ingest 刷新为 v1
    theirs = _write_theirs(tmp_path, [_slice(fids, summary="v3")], None)
    _set_base(repo, "slice:s1", _slice(fids, summary="v2"))  # ours==base 钉死
    result = _merge(repo, theirs)
    assert result["slices"]["adopted"] == ["s1"]
    assert "v3" in _slice_rows(repo)["s1"]


def test_theirs_unmoved_keeps_ours(tmp_path):
    """theirs==base → 保 ours（theirs 是旧梳理，ours 才是最新）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="v1")])
    _ingest(repo, slices=[_slice(fids, summary="v2")])
    theirs = _write_theirs(tmp_path, [_slice(fids, summary="v1")], None)
    result = _merge(repo, theirs)
    assert result["slices"]["kept"] == ["s1"]
    assert "v2" in _slice_rows(repo)["s1"]


def test_both_moved_conflict_keeps_ours(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="v1")])
    _ingest(repo, slices=[_slice(fids, summary="v2")])
    theirs = _write_theirs(tmp_path, [_slice(fids, summary="v3")], None)
    result = _merge(repo, theirs)
    assert result["slices"]["conflict"] == ["s1"]
    assert "v2" in _slice_rows(repo)["s1"]  # 冲突保 ours
    assert _decisions(repo) == [("curate_merge", "s1", "conflict")]


def test_no_base_both_new_conflict(tmp_path):
    """base=∅ 双新增不同内容 → 冲突（两代理独立梳理同一 slice_id）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="甲")])
    theirs = _write_theirs(tmp_path, [_slice(fids, summary="乙")], None)
    result = _merge(repo, theirs)
    assert result["slices"]["conflict"] == ["s1"]


def test_normalized_equality_key_order_not_conflict(tmp_path):
    """D5：键序不同不算冲突（parsed 对象比较，非字符串精确）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids)])
    payload = _slice(fids)
    reordered = {"steps": payload["steps"], "summary": payload["summary"],
                 "name": payload["name"], "slice_id": payload["slice_id"]}
    theirs = _write_theirs(tmp_path, [reordered], None)
    result = _merge(repo, theirs)
    assert result["slices"]["unchanged"] == ["s1"]


def test_conflict_rerun_idempotent_no_duplicate_ledger(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="v1")])
    _ingest(repo, slices=[_slice(fids, summary="v2")])
    theirs = _write_theirs(tmp_path, [_slice(fids, summary="v3")], None)
    _merge(repo, theirs)
    _merge(repo, theirs)
    assert len(_decisions(repo)) == 1  # 幂等：同冲突重跑不重复留痕
    assert "v2" in _slice_rows(repo)["s1"]


def test_ours_bad_payload_adopts_theirs(tmp_path):
    """ours 载荷损坏 → 视同无 ours 诚实采纳（不炸不吞对侧）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    con = _con(repo)
    con.execute(
        "INSERT INTO slices (slice_id, name, summary, payload_json, status)"
        " VALUES ('s1', 'n', 's', 'not-json', 'active')"
    )
    con.commit()
    con.close()
    theirs = _write_theirs(tmp_path, [_slice(fids)], None)
    result = _merge(repo, theirs)
    assert result["slices"]["adopted"] == ["s1"]


# ------------------------------------------------------------- structure 三方


def test_structure_adopted_when_no_ours(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    theirs = _write_theirs(tmp_path, None, _structure([_mod("m1", fids)]))
    result = _merge(repo, theirs)
    assert result["structure"]["action"] == "adopted"
    assert _active_structure(repo) is not None


def test_structure_unchanged(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, structure=_structure([_mod("m1", fids)]))
    theirs = _write_theirs(tmp_path, None, _structure([_mod("m1", fids)]))
    result = _merge(repo, theirs)
    assert result["structure"]["action"] == "unchanged"


def test_structure_field_merge_union(tmp_path):
    """D4 字段级：双方各动一处（不同 module）→ 并集自动合。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, structure=_structure([_mod("m1", fids[:1])]))
    _set_base(repo, "structure", _structure([_mod("m0", fids)]))
    theirs = _write_theirs(tmp_path, None, _structure([_mod("m2", fids[1:])]))
    result = _merge(repo, theirs)
    assert result["structure"]["action"] == "merged"
    raw = json.loads(_active_structure(repo))
    assert [m["module_id"] for m in raw["modules"]] == ["m1", "m2"]


def test_structure_field_conflict(tmp_path):
    """同 module_id 双动且异 → 冲突保 ours + decisions 留痕。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, structure=_structure([_mod("m1", fids[:1], layer="domain")]))
    _set_base(repo, "structure", _structure([_mod("m1", fids)]))
    theirs = _write_theirs(tmp_path, None, _structure([_mod("m1", fids[1:])]))
    result = _merge(repo, theirs)
    assert result["structure"]["action"] == "conflict"
    assert result["structure"]["conflict_keys"]
    raw = json.loads(_active_structure(repo))
    assert [m["module_id"] for m in raw["modules"]] == ["m1"]  # 保 ours
    assert ("curate_merge", "structure", "conflict") in _decisions(repo)


def test_structure_merged_validation_fallback(tmp_path):
    """校验兜底：字段级拼合违语义（fid 跨模块重叠）→ 整体降级冲突。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, structure=_structure([_mod("m1", fids[:2])]))
    _set_base(repo, "structure", _structure([_mod("m0", fids)]))
    theirs = _write_theirs(tmp_path, None, _structure([_mod("m2", fids[1:])]))
    result = _merge(repo, theirs)
    # m1 持 fid-b、m2 也持 fid-b → 合并后 STRUCT_FID_OVERLAP → 冲突
    assert result["structure"]["action"] == "conflict"
    raw = json.loads(_active_structure(repo))
    assert [m["module_id"] for m in raw["modules"]] == ["m1"]  # 不落半成品


# ------------------------------------------------------------- 校验/原子/输入面


def test_bad_theirs_rejected_zero_writes(tmp_path):
    """坏载荷先全量校验零写入即拒（R-B 整批语义）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    theirs = _write_theirs(tmp_path, [_slice(fids), _slice(["fid-ghost"], sid="s2")], None)
    with pytest.raises(SystemExit, match="zero writes"):
        _merge(repo, theirs)
    assert _slice_rows(repo) == {}
    assert _decisions(repo) == []


def test_bad_theirs_json_rejected(tmp_path):
    repo = _mk_repo(tmp_path)
    f = tmp_path / "bad.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit, match="cannot read/parse"):
        _merge(repo, f)


def test_theirs_path_missing(tmp_path):
    repo = _mk_repo(tmp_path)
    with pytest.raises(SystemExit, match="not found"):
        _merge(repo, tmp_path / "nope")


def test_empty_dir_rejected(tmp_path):
    repo = _mk_repo(tmp_path)
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(SystemExit, match="no slices.json/structure.json"):
        _merge(repo, d)


def test_single_file_slices_shape(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    f = tmp_path / "solo.json"
    f.write_text(json.dumps([_slice(fids)], ensure_ascii=False), encoding="utf-8")
    result = _merge(repo, f)
    assert result["slices"]["adopted"] == ["s1"]


def test_single_file_structure_shape(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    f = tmp_path / "solo.json"
    f.write_text(json.dumps(_structure([_mod("m1", fids)]), ensure_ascii=False), encoding="utf-8")
    result = _merge(repo, f)
    assert result["structure"]["action"] == "adopted"


# ------------------------------------------------------------- base 生命周期


def test_base_snapshot_on_ingest_slice(tmp_path):
    """D2：ingest 覆盖前快照——第二次 ingest 后 base=第一次的载荷。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="v1")])
    assert _base_keys(repo) == set()  # 首次 ingest 无覆盖 → 无 base
    _ingest(repo, slices=[_slice(fids, summary="v2")])
    base = _base_payload(repo, "slice:s1")
    assert base is not None and base["summary"] == "v1"


def test_base_snapshot_on_ingest_structure(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, structure=_structure([_mod("m1", fids)]))
    _ingest(repo, structure=_structure([_mod("m2", fids)]))
    base = _base_payload(repo, "structure")
    assert base is not None and base["modules"][0]["module_id"] == "m1"


def test_base_owned_survives_etl_snapshot_restore(tmp_path):
    """OWNED 存续：curate_base 在 OWNED_TABLES 内，snapshot/restore 往返不丢。"""
    repo = _mk_repo(tmp_path)
    _set_base(repo, "slice:s1", {"slice_id": "s1"})
    snap = etl.snapshot_owned(repo / ".vrp" / "facts.db")
    assert "curate_base" in snap  # OWNED_TABLES 成员锁（漏登即红）
    assert snap["curate_base"][1] == [("slice:s1", json.dumps({"slice_id": "s1"}, ensure_ascii=False))]


def _base_keys(repo: Path) -> set[str]:
    con = _con(repo)
    try:
        return {k for (k,) in con.execute("SELECT key FROM curate_base").fetchall()}
    finally:
        con.close()


def _base_payload(repo: Path, key: str) -> dict | None:
    con = _con(repo)
    try:
        row = con.execute("SELECT payload_json FROM curate_base WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    finally:
        con.close()


# ------------------------------------------------------------- 确定性/呈现/CLI


def test_merge_deterministic_two_repos_equal(tmp_path):
    """同输入双仓合并结果相等（列表序=字典序钉死；merged_at/stale 时间面除外）。"""
    results = []
    for i in range(2):
        repo = _mk_repo(tmp_path / f"case{i}")
        fids = _seed_chain(repo)
        _ingest(repo, slices=[_slice(fids, sid="s1")])
        theirs = _write_theirs(tmp_path / f"case{i}", [_slice(fids, sid="s2"), _slice(fids, sid="s3")], None)
        r = _merge(repo, theirs)
        r.pop("merged_at")
        r.pop("stale")
        r["theirs"].pop("source")  # 路径含 case 目录名，非合并语义面
        results.append(r)
    assert results[0] == results[1]


def test_render_text_shape(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    theirs = _write_theirs(tmp_path, [_slice(fids)], None)
    result = _merge(repo, theirs)
    text = curate_merge.render_text(result)
    assert "curate-merge - " in text
    assert "adopted 1" in text
    assert "structure: absent" in text


def test_cli_curate_merge_end_to_end(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    theirs = _write_theirs(tmp_path, [_slice(fids)], _structure([_mod("m1", fids)]))
    rc = cli.main(["curate-merge", str(repo), "--theirs", str(theirs), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["slices"]["adopted"] == ["s1"]
    assert out["structure"]["action"] == "adopted"


def test_cli_curate_merge_conflict_rc(tmp_path, capsys):
    """rc=1 = 有冲突待人工裁决（零静默丢信号）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    _ingest(repo, slices=[_slice(fids, summary="甲")])
    theirs = _write_theirs(tmp_path, [_slice(fids, summary="乙")], None)
    rc = cli.main(["curate-merge", str(repo), "--theirs", str(theirs), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["slices"]["conflict"] == ["s1"]


def test_cli_curate_merge_freshness_rejected(tmp_path, capsys):
    """M4 freshness：stale facts.db 硬拒（curate 写路径同权；cli.main 转 rc=1）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    con = _con(repo)
    con.execute("UPDATE meta SET value = 'stale-head' WHERE key = 'git_head'")
    con.commit()
    con.close()
    theirs = _write_theirs(tmp_path, [_slice(fids)], None)
    rc = cli.main(["curate-merge", str(repo), "--theirs", str(theirs), "--json"])
    assert rc == 1
    assert "stale" in capsys.readouterr().err


def test_cli_parser_accepts_curate_merge():
    parser = cli.build_parser()
    args = parser.parse_args(["curate-merge", "some/repo", "--theirs", "dir"])
    assert args.repo == "some/repo" and args.theirs == "dir" and args.json is False
