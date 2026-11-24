"""#225 curate-refresh 确定性 fid 重映射测试。

两大根因面：content_hash 变（同项目 qn 正查）与项目名派生变更
（qn 剥旧前缀换当前前缀）；语义原样、死 fid 诚实剔除、空模块剔除、
全灭拒做、slice 全量重验、收据与 drift_history 落档、CLI rc 语义。
"""

import json
import sqlite3
from pathlib import Path

from vrp import cli
from vrp import curate as curate_mod

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


def _seed_symbols(
    repo: Path,
    project: str,
    names: list[str],
    fid_prefix: str = "fid-",
    file_of: dict[str, str] | None = None,
) -> list[str]:
    """符号 + fid_ledger 双写（ledger=重映射的桥，etl 同源行为）。"""
    con = _con(repo)
    fids = []
    for i, name in enumerate(names):
        fid = f"{fid_prefix}{name}"
        qn = f"{project}.pkg.{name}"
        fp = (file_of or {}).get(name, "pkg/x.py")
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
            " start_line, end_line, content_hash) VALUES (?, ?, ?, 'Function', ?, ?, ?, ?, ?)",
            (fid, project, qn, name, fp, i + 1, i + 3, f"h-{fid_prefix}"),
        )
        con.execute(
            "INSERT INTO fid_ledger (fid, project, qualified_name, file_path, first_seen_at)"
            " VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
            (fid, project, qn, fp),
        )
        fids.append(fid)
    con.commit()
    con.close()
    return fids


def _structure(mods: list[dict]) -> dict:
    return {
        "modules": mods,
        "layers": [{"name": "core", "may_depend_on": []}],
        "rules": [],
    }


def _mod(mid: str, fids: list[str]) -> dict:
    return {
        "module_id": mid,
        "name": f"模块{mid}",
        "responsibility": f"职责{mid}",
        "symbol_fids": fids,
        "layer": "core",
    }


def _ingest(repo: Path, structure: dict) -> None:
    con = _con(repo)
    try:
        receipt = curate_mod.curate_payload(con, "structure", json.dumps(structure, ensure_ascii=False))
        assert receipt["status"] == "accepted", receipt
    finally:
        con.close()


def _latest_structure(repo: Path) -> tuple[int, str, str]:
    con = _con(repo)
    try:
        row = con.execute(
            "SELECT id, payload_json, status FROM structure ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return row[0], row[1], row[2]


def _rename_project(repo: Path, old: str, new: str) -> None:
    """项目名派生变更模拟：symbols 旧删新插（qn 前缀同换），ledger 累积双史。"""
    con = _con(repo)
    con.execute("DELETE FROM symbols WHERE project = ?", (old,))
    con.commit()
    con.close()


def test_refresh_remaps_after_project_rename(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "old-p", ["a", "b", "c"], fid_prefix="old-")
    _ingest(repo, _structure([_mod("m1", old_fids[:2]), _mod("m2", old_fids[2:])]))
    _rename_project(repo, "old-p", "new-p")
    new_fids = _seed_symbols(repo, "new-p", ["a", "b", "c"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["structure"]["remapped"] == 3
    assert receipt["structure"]["dropped"] == 0
    _, payload_json, status = _latest_structure(repo)
    assert status == "active"
    payload = json.loads(payload_json)
    assert [m["module_id"] for m in payload["modules"]] == ["m1", "m2"]
    assert payload["modules"][0]["symbol_fids"] == new_fids[:2]
    assert payload["modules"][1]["symbol_fids"] == [new_fids[2]]
    # 语义逐字保留
    assert payload["modules"][0]["name"] == "模块m1"
    assert payload["modules"][0]["responsibility"] == "职责m1"
    assert payload["modules"][0]["layer"] == "core"
    # 收据落盘
    receipt_disk = json.loads(
        (repo / ".vrp" / "curate-refresh-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt_disk["structure"]["action"] == "refreshed"
    # drift_history 事件
    con = _con(repo)
    events = [r[0] for r in con.execute(
        "SELECT event FROM drift_history ORDER BY id DESC LIMIT 1").fetchall()]
    con.close()
    assert events == ["curate-refresh"]


def test_refresh_content_churn_same_project(tmp_path: Path) -> None:
    """content_hash 轮换根因面：同项目同 qn、fid 新旧更替（qn 正查候选）。"""
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="v1-")
    _ingest(repo, _structure([_mod("m", old_fids)]))
    con = _con(repo)
    con.execute("DELETE FROM symbols WHERE project = 'p'")
    con.commit()
    con.close()
    new_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="v2-")
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["structure"]["remapped"] == 2
    _, payload_json, _ = _latest_structure(repo)
    assert json.loads(payload_json)["modules"][0]["symbol_fids"] == new_fids


def test_refresh_drops_dead_fid_and_empty_module(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a", "b", "c"])
    _ingest(repo, _structure([_mod("m1", old_fids[:2]), _mod("m2", [old_fids[2]])]))
    con = _con(repo)
    # b、c 消亡（qn 无现行者）：m1 掉一 fid，m2 整模块空
    con.execute("DELETE FROM symbols WHERE qualified_name IN (?, ?)",
                ("p.pkg.b", "p.pkg.c"))
    con.commit()
    con.close()
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["structure"]["dropped"] == 2
    assert receipt["structure"]["modules_dropped"] == 1
    _, payload_json, _ = _latest_structure(repo)
    payload = json.loads(payload_json)
    assert [m["module_id"] for m in payload["modules"]] == ["m1"]
    assert payload["modules"][0]["symbol_fids"] == [old_fids[0]]


def test_refresh_failed_when_all_dead(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a"])
    _ingest(repo, _structure([_mod("m", old_fids)]))
    con = _con(repo)
    con.execute("DELETE FROM symbols")
    con.execute("UPDATE structure SET status = 'stale'")
    # cbm_project 兜底：symbols 空域取不到 → 预置 meta
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'p')")
    con.commit()
    before = _latest_structure(repo)
    con.close()
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "failed"
    assert "all modules unremappable" in receipt["structure"]["errors"][0]
    # 原 structure 原样保留（拒做不落库）
    assert _latest_structure(repo) == before


def test_refresh_fresh_noop(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo, "p", ["a", "b"])
    _ingest(repo, _structure([_mod("m", fids)]))
    before_id = _latest_structure(repo)[0]
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "fresh"
    assert receipt["structure"]["kept_live"] == 2
    assert _latest_structure(repo)[0] == before_id  # 无重入库


def test_refresh_missing_structure(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo, "p", ["a"])
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "missing"
    assert (repo / ".vrp" / "curate-refresh-receipt.json").is_file()


def test_refresh_dedupes_same_target_fid(tmp_path: Path) -> None:
    """跨时代双 fid 同指：旧时代 fid 重映射与新时代 fid 撞键 → 去重计数。

    symbols 层 UNIQUE(project, qualified_name) 使同 qn 双文件不可能，
    故真实歧义面是同符号跨时代双 fid 并存于一个模块。
    """
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo, "old-p", ["a"], fid_prefix="old-")
    _seed_symbols(repo, "new-p", ["a"], fid_prefix="new-")
    _ingest(repo, _structure([_mod("m", ["old-a", "new-a"])]))
    con = _con(repo)
    # 旧时代符号退场（项目名派生变更后旧域清除）
    con.execute("DELETE FROM symbols WHERE project = 'old-p'")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["structure"]["remapped"] == 1
    assert receipt["structure"]["kept_live"] == 1
    assert receipt["structure"]["deduped"] == 1
    _, payload_json, _ = _latest_structure(repo)
    assert json.loads(payload_json)["modules"][0]["symbol_fids"] == ["new-a"]


def test_refresh_slice_refreshed(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="old-")
    con = _con(repo)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                " ('old-a', 'old-b', 'CALLS', 'structural')")
    con.commit()
    con.close()
    slice_payload = [{
        "slice_id": "s1", "name": "线", "summary": "链路",
        "steps": [{"fid": f, "role": "process"} for f in old_fids],
    }]
    con = _con(repo)
    try:
        receipt = curate_mod.curate_payload(con, "slices", json.dumps(slice_payload, ensure_ascii=False))
        assert receipt["status"] == "accepted", receipt
    finally:
        con.close()
    con = _con(repo)
    con.execute("DELETE FROM symbols WHERE project = 'p'")
    con.commit()
    con.close()
    new_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                " ('new-a', 'new-b', 'CALLS', 'structural')")
    con.commit()
    con.close()
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    # slices-only 仓：structure missing 不拦 slice 刷新（独立处理）
    assert receipt["structure"]["action"] == "missing"
    assert receipt["slices"][0]["action"] == "refreshed"
    con = _con(repo)
    row = con.execute("SELECT payload_json, status FROM slices WHERE slice_id = 's1'").fetchone()
    con.close()
    assert row[1] == "active"
    assert [s["fid"] for s in json.loads(row[0])["steps"]] == new_fids
    # 非语义字段原样
    assert json.loads(row[0])["steps"][0]["role"] == "process"


def test_refresh_slice_skipped_when_step_gone(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="old-")
    con = _con(repo)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                " ('old-a', 'old-b', 'CALLS', 'structural')")
    con.commit()
    con.close()
    slice_payload = [{
        "slice_id": "s1", "name": "线", "summary": "链路",
        "steps": [{"fid": f, "role": "process"} for f in old_fids],
    }]
    con = _con(repo)
    try:
        curate_mod.curate_payload(con, "slices", json.dumps(slice_payload, ensure_ascii=False))
    finally:
        con.close()
    con = _con(repo)
    # b 永久消亡：slice 不可整线重映射 → 原样保留
    con.execute("DELETE FROM symbols WHERE project = 'p'")
    con.commit()
    con.close()
    _seed_symbols(repo, "p", ["a"], fid_prefix="new-")
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["slices"][0]["action"] == "skipped"
    assert receipt["slices"][0]["reason"] == "unremappable step fid"
    con = _con(repo)
    row = con.execute("SELECT payload_json FROM slices WHERE slice_id = 's1'").fetchone()
    con.close()
    assert [s["fid"] for s in json.loads(row[0])["steps"]] == old_fids


def test_cli_curate_refresh_end_to_end(tmp_path: Path, capsys) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "old-p", ["a"], fid_prefix="old-")
    _ingest(repo, _structure([_mod("m", old_fids)]))
    con = _con(repo)
    con.execute("DELETE FROM symbols WHERE project = 'old-p'")
    con.commit()
    con.close()
    _seed_symbols(repo, "new-p", ["a"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.commit()
    con.close()
    rc = cli.main(["curate-refresh", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["structure"]["action"] == "refreshed"
    assert out["structure"]["remapped"] == 1
    _, payload_json, status = _latest_structure(repo)
    assert status == "active"
    assert json.loads(payload_json)["modules"][0]["symbol_fids"] == ["new-a"]


def test_cli_curate_refresh_missing_rc(tmp_path: Path, capsys) -> None:
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo, "p", ["a"])
    rc = cli.main(["curate-refresh", str(repo), "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["structure"]["action"] == "missing"


# --------------------------------------------------- #229 治愈写回磁盘


def _write_ai(repo: Path, name: str, payload: object) -> Path:
    ai_dir = repo / ".vrp" / "ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    path = ai_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path


def _ingest_slices_payload(repo: Path, slices: list[dict]) -> None:
    con = _con(repo)
    try:
        receipt = curate_mod.curate_payload(con, "slices", json.dumps(slices, ensure_ascii=False))
        assert receipt["status"] == "accepted", receipt
    finally:
        con.close()


def test_refresh_writes_back_structure_to_disk(tmp_path: Path) -> None:
    """#229 主路径：治愈 remap 顺手写回磁盘声明——fid 换新、语义面原样。"""
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "old-p", ["a", "b", "c"], fid_prefix="old-")
    disk_payload = _structure([_mod("m1", old_fids[:2]), _mod("m2", old_fids[2:])])
    _write_ai(repo, "structure.json", disk_payload)
    _ingest(repo, disk_payload)
    _rename_project(repo, "old-p", "new-p")
    new_fids = _seed_symbols(repo, "new-p", ["a", "b", "c"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["disk_sync"]["structure"] == {
        "file": ".vrp/ai/structure.json", "present": True, "remapped": 3, "dropped": 0,
    }
    disk = json.loads((repo / ".vrp" / "ai" / "structure.json").read_text(encoding="utf-8"))
    assert [m["module_id"] for m in disk["modules"]] == ["m1", "m2"]
    assert disk["modules"][0]["symbol_fids"] == new_fids[:2]
    assert disk["modules"][1]["symbol_fids"] == [new_fids[2]]
    # 语义面原样（最小侵入）
    assert disk["modules"][0]["name"] == "模块m1"
    assert disk["modules"][0]["responsibility"] == "职责m1"
    assert disk["modules"][0]["layer"] == "core"
    assert disk["layers"] == [{"name": "core", "may_depend_on": []}]


def test_refresh_writes_back_slices_to_disk(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="old-")
    con = _con(repo)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                " ('old-a', 'old-b', 'CALLS', 'structural')")
    con.commit()
    con.close()
    slices = [{
        "slice_id": "s1", "name": "线", "summary": "链路",
        "steps": [{"fid": f, "role": "process"} for f in old_fids],
    }]
    _write_ai(repo, "slices.json", slices)
    _ingest_slices_payload(repo, slices)
    con = _con(repo)
    con.execute("DELETE FROM symbols WHERE project = 'p'")
    con.commit()
    con.close()
    new_fids = _seed_symbols(repo, "p", ["a", "b"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                " ('new-a', 'new-b', 'CALLS', 'structural')")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    assert receipt["slices"][0]["action"] == "refreshed"
    assert receipt["disk_sync"]["slices"] == {
        "file": ".vrp/ai/slices.json", "present": True, "remapped": 2,
    }
    disk = json.loads((repo / ".vrp" / "ai" / "slices.json").read_text(encoding="utf-8"))
    assert [s["fid"] for s in disk[0]["steps"]] == new_fids
    assert disk[0]["steps"][0]["role"] == "process"


def test_refresh_writes_back_dropped_fid_and_empty_module(tmp_path: Path) -> None:
    """#229 剔除面同构写回：死 fid 剔除 + 空模块剔除（镜像库内治愈）。"""
    repo = _mk_repo(tmp_path)
    _seed_symbols(repo, "old-p", ["a", "b", "c"], fid_prefix="old-")
    old_fids = ["old-a", "old-b", "old-c"]
    disk_payload = _structure([_mod("m1", old_fids[:2]), _mod("m2", [old_fids[2]])])
    _write_ai(repo, "structure.json", disk_payload)
    _ingest(repo, disk_payload)
    _rename_project(repo, "old-p", "new-p")
    _seed_symbols(repo, "new-p", ["a"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["structure"]["dropped"] == 2
    assert receipt["structure"]["modules_dropped"] == 1
    sync = receipt["disk_sync"]["structure"]
    assert sync["remapped"] == 1
    assert sync["dropped"] == 2
    disk = json.loads((repo / ".vrp" / "ai" / "structure.json").read_text(encoding="utf-8"))
    # m1：old-a→new-a，old-b 剔除；m2：old-c 剔除后空模块同构剔除
    assert [m["module_id"] for m in disk["modules"]] == ["m1"]
    assert disk["modules"][0]["symbol_fids"] == ["new-a"]


def test_refresh_slices_disk_sync_shared_fid_no_cross_slice_harm(tmp_path: Path) -> None:
    """#229 核心边界：跨 slice 共享 fid——治愈条目换新，skipped 条目原样
    （全文件替换会误伤 skipped 条目制造新脱节，按 slice_id 精确匹配防）。"""
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "p", ["a", "b", "c"], fid_prefix="old-")
    con = _con(repo)
    for src, dst in (("old-a", "old-b"), ("old-a", "old-c")):
        con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                    f" ('{src}', '{dst}', 'CALLS', 'structural')")
    con.commit()
    con.close()
    slices = [
        {"slice_id": "s1", "name": "好线", "summary": "a 到 b",
         "steps": [{"fid": "old-a", "role": "process"}, {"fid": "old-b", "role": "process"}]},
        {"slice_id": "s2", "name": "坏线", "summary": "a 到 c",
         "steps": [{"fid": "old-a", "role": "process"}, {"fid": "old-c", "role": "process"}]},
    ]
    _write_ai(repo, "slices.json", slices)
    _ingest_slices_payload(repo, slices)
    con = _con(repo)
    # 全符号退场（c 永久消亡；a/b 轮换回归）——s1 可整线重映射，s2 不可
    con.execute("DELETE FROM symbols WHERE project = 'p'")
    con.commit()
    con.close()
    _seed_symbols(repo, "p", ["a", "b"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES"
                " ('new-a', 'new-b', 'CALLS', 'structural')")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    by_id = {s["slice_id"]: s for s in receipt["slices"]}
    assert by_id["s1"]["action"] == "refreshed"
    assert by_id["s2"]["action"] == "skipped"
    assert receipt["disk_sync"]["slices"]["remapped"] == 2
    disk = {s["slice_id"]: s for s in json.loads(
        (repo / ".vrp" / "ai" / "slices.json").read_text(encoding="utf-8"))}
    assert [st["fid"] for st in disk["s1"]["steps"]] == ["new-a", "new-b"]
    # skipped 条目原样：共享的 old-a 不被误换
    assert [st["fid"] for st in disk["s2"]["steps"]] == ["old-a", "old-c"]


def test_refresh_disk_sync_missing_file_honest(tmp_path: Path) -> None:
    """curate-auto 场景（MCP 提交，无磁盘声明）：诚实记 present=False，
    不替 LLM 造声明（不创建文件/目录）。"""
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "old-p", ["a"], fid_prefix="old-")
    _ingest(repo, _structure([_mod("m", old_fids)]))
    _rename_project(repo, "old-p", "new-p")
    _seed_symbols(repo, "new-p", ["a"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    assert receipt["structure"]["action"] == "refreshed"
    assert receipt["disk_sync"]["structure"] == {
        "file": ".vrp/ai/structure.json", "present": False, "remapped": 0, "dropped": 0,
    }
    assert not (repo / ".vrp" / "ai").exists()


def test_refresh_disk_sync_unparseable_file_untouched(tmp_path: Path) -> None:
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "old-p", ["a"], fid_prefix="old-")
    _ingest(repo, _structure([_mod("m", old_fids)]))
    (repo / ".vrp" / "ai").mkdir(parents=True)
    bad = repo / ".vrp" / "ai" / "structure.json"
    bad.write_text("not-json{", encoding="utf-8")
    _rename_project(repo, "old-p", "new-p")
    _seed_symbols(repo, "new-p", ["a"], fid_prefix="new-")
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    sync = receipt["disk_sync"]["structure"]
    assert sync["present"] is True
    assert sync["unparseable"] is True
    assert sync["remapped"] == 0
    assert bad.read_text(encoding="utf-8") == "not-json{"


def test_refresh_disk_sync_absent_when_fresh(tmp_path: Path) -> None:
    """fresh（无治愈发生）不写不记：收据无 disk_sync 节。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_symbols(repo, "p", ["a", "b"])
    _write_ai(repo, "structure.json", _structure([_mod("m", fids)]))
    _ingest(repo, _structure([_mod("m", fids)]))
    before = (repo / ".vrp" / "ai" / "structure.json").read_text(encoding="utf-8")
    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()
    assert receipt["structure"]["action"] == "fresh"
    assert "disk_sync" not in receipt
    assert (repo / ".vrp" / "ai" / "structure.json").read_text(encoding="utf-8") == before


def test_refresh_disk_sync_no_rewrite_when_already_synced(tmp_path: Path) -> None:
    """#228 手工同步场景：磁盘已先行换新 fid，写回幂等（不命中不重写）。"""
    repo = _mk_repo(tmp_path)
    old_fids = _seed_symbols(repo, "old-p", ["a", "b"], fid_prefix="old-")
    _ingest(repo, _structure([_mod("m", old_fids)]))
    _rename_project(repo, "old-p", "new-p")
    new_fids = _seed_symbols(repo, "new-p", ["a", "b"], fid_prefix="new-")
    # 磁盘先行人工同步（#228 手工实录场景）
    synced = _structure([_mod("m", new_fids)])
    _write_ai(repo, "structure.json", synced)
    con = _con(repo)
    con.execute("INSERT INTO meta (key, value) VALUES ('cbm_project', 'new-p')")
    con.execute("UPDATE structure SET status = 'stale'")
    con.commit()
    con.close()

    con = _con(repo)
    try:
        receipt = curate_mod.curate_refresh(repo, con)
    finally:
        con.close()

    assert receipt["structure"]["action"] == "refreshed"
    sync = receipt["disk_sync"]["structure"]
    assert sync["remapped"] == 0  # 磁盘已同步：remapping 键不命中
    disk = json.loads((repo / ".vrp" / "ai" / "structure.json").read_text(encoding="utf-8"))
    assert disk["modules"][0]["symbol_fids"] == new_fids
