"""R2' curate 测试：验证器全拒绝路径参数化 + E2E + 幂等/stale/留痕。"""

import json
import sqlite3
from pathlib import Path

import pytest

from vrp import curate as curate_mod
from vrp import nav

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
    """a -> b -> c 符号链 + 真实边（slices 验证素材）。"""
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


def _slice(fids: list[str], **overrides) -> dict:
    payload = {
        "slice_id": "test-flow",
        "name": "测试线",
        "summary": "a 到 c 的链路",
        "steps": [{"fid": f, "role": "process"} for f in fids],
    }
    payload.update(overrides)
    return payload


def _structure(fids: list[str], **overrides) -> dict:
    payload = {
        "modules": [
            {"module_id": "m1", "name": "模块一", "responsibility": "负责一",
             "symbol_fids": fids, "layer": "domain"},
        ],
        "layers": [{"name": "domain", "may_depend_on": []}],
        "rules": [],
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------- slices 验证器拒绝路径


@pytest.mark.parametrize(
    "payload, code",
    [
        ("not-a-dict", "SLICE_SCHEMA"),
        ({"steps": []}, "SLICE_SCHEMA"),
        ({"slice_id": "x", "steps": []}, "SLICE_SCHEMA"),
        (_slice(["fid-a"], name=""), "SLICE_EMPTY_NAME"),
        (_slice(["fid-a"], summary=" "), "SLICE_EMPTY_SUMMARY"),
        (_slice(["fid-a", "fid-ghost"]), "SLICE_UNKNOWN_FID"),
        (_slice(["fid-a", "fid-c"]), "SLICE_MISSING_EDGE"),
        (_slice(["fid-c", "fid-a"]), "SLICE_MISSING_EDGE"),
        (_slice(["fid-a", "fid-b", "fid-c"], acts=[{"name": "幕", "from_step": 0, "to_step": 0}]), "SLICE_ACT_BOUNDS"),
        (_slice(["fid-a", "fid-b", "fid-c"], acts=[{"name": "幕", "from_step": 5, "to_step": 9}]), "SLICE_ACT_BOUNDS"),
        (_slice(["fid-a", "fid-b", "fid-c"], acts=[{"name": "x", "from_step": 0, "to_step": 0},
                                                   {"name": "y", "from_step": 2, "to_step": 2}]), "SLICE_ACT_BOUNDS"),
    ],
)
def test_slice_rejection_paths(tmp_path, payload, code):
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        errors = curate_mod._validate_one_slice(con, payload)
        assert errors, f"must reject: {payload!r}"
        assert any(e["code"] == code for e in errors), f"expected {code}, got {[e['code'] for e in errors]}"
    finally:
        con.close()


def test_slice_role_enum_rejected(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        bad = _slice(["fid-a"])
        bad["steps"] = [{"fid": "fid-a", "role": "wat"}]
        errors = curate_mod._validate_one_slice(con, bad)
        assert any(e["code"] == "SLICE_SCHEMA" and "role" in e["detail"] for e in errors)
    finally:
        con.close()


def test_slice_valid_with_acts(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        good = _slice(
            ["fid-a", "fid-b", "fid-c"],
            acts=[{"name": "上半", "from_step": 0, "to_step": 1}, {"name": "下半", "from_step": 2, "to_step": 2}],
        )
        good["steps"][0]["role"] = "entry"
        assert curate_mod._validate_one_slice(con, good) == []
        # 反向边也算相邻（任一方向）
        rev = _slice(["fid-c", "fid-b", "fid-a"])
        assert curate_mod._validate_one_slice(con, rev) == []
    finally:
        con.close()


# ---------------------------------------------- structure 验证器拒绝路径


@pytest.mark.parametrize(
    "payload, code",
    [
        ("not-a-dict", "STRUCT_SCHEMA"),
        ({"modules": []}, "STRUCT_SCHEMA"),
        ({"modules": [{"module_id": "m", "name": "n", "responsibility": "r", "symbol_fids": ["fid-a"], "layer": "domain"}],
          "layers": []}, "STRUCT_SCHEMA"),
        (_structure(["fid-a"], modules=[{"module_id": "m", "name": "", "responsibility": "r",
                                         "symbol_fids": ["fid-a"], "layer": "domain"}]), "STRUCT_EMPTY_NAME"),
        (_structure(["fid-a"], modules=[{"module_id": "m", "name": "n", "responsibility": "",
                                         "symbol_fids": ["fid-a"], "layer": "domain"}]), "STRUCT_EMPTY_RESPONSIBILITY"),
        (_structure(["fid-ghost"]), "STRUCT_UNKNOWN_FID"),
        (_structure(["fid-a"], modules=[
            {"module_id": "m1", "name": "n", "responsibility": "r", "symbol_fids": ["fid-a", "fid-b"], "layer": "domain"},
            {"module_id": "m2", "name": "n", "responsibility": "r", "symbol_fids": ["fid-b"], "layer": "domain"},
        ]), "STRUCT_FID_OVERLAP"),
        (_structure(["fid-a"], layers=[{"name": "domain", "may_depend_on": ["service"]},
                                       {"name": "service", "may_depend_on": ["domain"]}]), "STRUCT_LAYER_CYCLE"),
        (_structure(["fid-a"], layers=[{"name": "service", "may_depend_on": []}]), "STRUCT_UNKNOWN_LAYER"),
        (_structure(["fid-a"], layers=[{"name": "domain", "may_depend_on": ["ghost"]}]), "STRUCT_UNKNOWN_LAYER"),
        (_structure(["fid-a"], rules=[{"rule": "x", "from_layer": "ghost"}]), "STRUCT_RULE_LAYER"),
    ],
)
def test_structure_rejection_paths(tmp_path, payload, code):
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        errors = curate_mod._validate_structure(con, payload)
        assert errors, f"must reject: {payload!r}"
        assert any(e["code"] == code for e in errors), f"expected {code}, got {[e['code'] for e in errors]}"
    finally:
        con.close()


def test_structure_valid_dag(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        payload = _structure(["fid-a", "fid-b"])
        payload["layers"] = [{"name": "domain", "may_depend_on": []},
                             {"name": "service", "may_depend_on": ["domain"]}]
        payload["rules"] = [{"rule": "service 只依赖 domain", "enforcement_hint": "import-linter",
                             "from_layer": "service", "to_layer": "domain"}]
        payload["modules"].append(
            {"module_id": "m2", "name": "模块二", "responsibility": "负责二", "symbol_fids": ["fid-c"], "layer": "service"}
        )
        assert curate_mod._validate_structure(con, payload) == []
    finally:
        con.close()


# ----------------------------------------------------------------- E2E


def test_curate_e2e_files_to_ingest(tmp_path):
    """手工 AI 工件 -> .vrp/ai/ -> curate 入库 + 收据落盘。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "slices.json").write_text(json.dumps([_slice(fids)], ensure_ascii=False), encoding="utf-8")
    (ai / "structure.json").write_text(json.dumps(_structure(fids), ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        receipt = curate_mod.curate(repo, con)
    finally:
        con.close()
    assert [f["status"] for f in receipt["files"]] == ["accepted", "accepted"]
    assert receipt["stale"] == {"slices": [], "structure": False}
    assert (repo / ".vrp" / "curate-receipt.json").exists()
    con = _con(repo)
    try:
        rows = con.execute("SELECT slice_id, status FROM slices").fetchall()
        assert rows == [("test-flow", "active")]
        assert con.execute("SELECT status FROM structure").fetchone()[0] == "active"
    finally:
        con.close()
    # nav 联动：L0 走 structure，L2 带 slices，nav_slice 可下钻
    arch = nav.nav_architecture(repo)
    assert arch["source"] == "structure"
    assert arch["modules"][0]["module_id"] == "m1"
    assert arch["unassigned_symbols"] == 0
    sym = nav.nav_symbol(repo, "fid-a")
    assert sym["slices"] == [{"slice_id": "test-flow", "name": "测试线", "step": 0}]
    sl = nav.nav_slice(repo, "test-flow")
    assert sl["status"] == "active"
    assert [s["fid"] for s in sl["steps"]] == fids
    assert sl["steps"][0]["qualified_name"] == "pkg.a"


def test_curate_atomic_reject_no_partial_ingest(tmp_path):
    """整文件原子拒：一条坏 slice 不入库，好条目也不入。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    good = _slice(fids)
    bad = _slice(["fid-a", "fid-ghost"])
    (ai / "slices.json").write_text(json.dumps([good, bad], ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        receipt = curate_mod.curate(repo, con)
    finally:
        con.close()
    entry = receipt["files"][0]
    assert entry["status"] == "rejected"
    assert any(e["code"] == "SLICE_UNKNOWN_FID" for e in entry["errors"])
    con = _con(repo)
    try:
        assert con.execute("SELECT COUNT(*) FROM slices").fetchone()[0] == 0
    finally:
        con.close()


def test_curate_idempotent_resubmit(tmp_path):
    """同 slice_id 重提交 = 幂等覆盖（DELETE 后 INSERT，行数不变）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "slices.json").write_text(json.dumps([_slice(fids, name="第一版")], ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        curate_mod.curate(repo, con)
        (ai / "slices.json").write_text(json.dumps([_slice(fids, name="第二版")], ensure_ascii=False), encoding="utf-8")
        curate_mod.curate(repo, con)
        rows = con.execute("SELECT slice_id, name, COUNT(*) FROM slices GROUP BY slice_id, name").fetchall()
        assert rows == [("test-flow", "第二版", 1)]
        assert con.execute("SELECT COUNT(*) FROM structure").fetchone()[0] == 0
    finally:
        con.close()


def test_curate_stale_revalidation(tmp_path):
    """入库后边漂移 -> 复验标 stale（不删除）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "slices.json").write_text(json.dumps([_slice(fids)], ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        curate_mod.curate(repo, con)
        # 模拟漂移：删一条相邻边
        con.execute("DELETE FROM edges WHERE src_fid = 'fid-b'")
        con.commit()
        receipt = curate_mod.curate(repo, con)
        assert receipt["stale"]["slices"] == ["test-flow"]
        status = con.execute("SELECT status FROM slices WHERE slice_id = 'test-flow'").fetchone()[0]
        assert status == "stale"
    finally:
        con.close()
    # nav_slice 仍可读（stale 可见，不删除）
    sl = nav.nav_slice(repo, "test-flow")
    assert sl["status"] == "stale"


def test_curate_structure_stale_on_fid_drift(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "structure.json").write_text(json.dumps(_structure(fids), ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        curate_mod.curate(repo, con)
        con.execute("DELETE FROM symbols WHERE fid = 'fid-a'")
        con.commit()
        receipt = curate_mod.curate(repo, con)
        assert receipt["stale"]["structure"] is True
    finally:
        con.close()
    # structure stale 后 nav_architecture 回落目录聚合兜底
    arch = nav.nav_architecture(repo)
    assert arch["fallback"] == "dir-aggregation"


def test_reject_leaves_decision_record(tmp_path):
    """--reject 留痕：status→rejected + decisions 行。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "slices.json").write_text(json.dumps([_slice(fids)], ensure_ascii=False), encoding="utf-8")
    (ai / "structure.json").write_text(json.dumps(_structure(fids), ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        curate_mod.curate(repo, con)
        out = curate_mod.reject(con, "slice", "test-flow", "不准")
        assert out["status"] == "rejected"
        out2 = curate_mod.reject(con, "structure", "structure", "面也不准")
        assert out2["status"] == "rejected"
        assert con.execute("SELECT status FROM slices WHERE slice_id = 'test-flow'").fetchone()[0] == "rejected"
        assert con.execute("SELECT status FROM structure").fetchone()[0] == "rejected"
        decisions = con.execute("SELECT kind, target_id, decision, note FROM decisions ORDER BY id").fetchall()
        assert decisions == [
            ("slice", "test-flow", "reject", "不准"),
            ("structure", "structure", "reject", "面也不准"),
        ]
        try:
            curate_mod.reject(con, "slice", "ghost", "")
            raise AssertionError("unknown slice must raise")
        except SystemExit:
            pass
    finally:
        con.close()
    # 拒收后 nav 取材面自动失效
    sym = nav.nav_symbol(repo, "fid-a")
    assert sym["slices"] == []  # rejected 不再出现在符号所属业务线
    arch = nav.nav_architecture(repo)
    assert arch["fallback"] == "dir-aggregation"  # rejected structure 不算 active


def test_curate_payload_mcp_path(tmp_path):
    """curate_payload（MCP 路径）与文件路径同一验证器。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    con = _con(repo)
    try:
        ok = curate_mod.curate_payload(con, "slices", json.dumps([_slice(fids)], ensure_ascii=False))
        assert ok["status"] == "accepted"
        bad = curate_mod.curate_payload(con, "slices", json.dumps([_slice(["fid-ghost"])]))
        assert bad["status"] == "rejected"
        assert any(e["code"] == "SLICE_UNKNOWN_FID" for e in bad["errors"])
        not_json = curate_mod.curate_payload(con, "slices", "{oops")
        assert not_json["errors"][0]["code"] == "FILE_NOT_JSON"
        wrong_shape = curate_mod.curate_payload(con, "slices", json.dumps({"not": "list"}))
        assert wrong_shape["errors"][0]["code"] == "FILE_WRONG_SHAPE"
        st = curate_mod.curate_payload(con, "structure", json.dumps(_structure(fids), ensure_ascii=False))
        assert st["status"] == "accepted"
        assert curate_mod.get_structure(con)["status"] == "active"
        slices = curate_mod.list_slices(con)
        assert slices[0]["slice_id"] == "test-flow"
        assert slices[0]["steps"] == 3
        try:
            curate_mod.curate_payload(con, "bogus", "[]")
            raise AssertionError("bogus kind must raise")
        except SystemExit:
            pass
    finally:
        con.close()


def test_list_slices_nondict_payload_steps_zero_not_crash(tmp_path):
    """R-X：合法 JSON 非 dict 的 slice payload（手改库/legacy）列表页 steps 记 0，非裸
    AttributeError——curate.py:399 旧链式 json.loads(...).get 为 census 漏网、
    守卫 test_no_chained_json_loads_get_in_vrp 抓到的第 7 站。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO slices (slice_id, name, summary, status, payload_json)"
            " VALUES ('legacy', 'n', 's', 'active', '[1, 2]')"
        )
        con.commit()
        listed = curate_mod.list_slices(con)
    finally:
        con.close()
    assert listed[0]["slice_id"] == "legacy"
    assert listed[0]["steps"] == 0


def test_curate_receipt_read_back(tmp_path):
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "slices.json").write_text(json.dumps([_slice(fids)], ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        assert curate_mod.read_receipt(repo) is None
        curate_mod.curate(repo, con)
    finally:
        con.close()
    receipt = curate_mod.read_receipt(repo)
    assert receipt["files"][0]["status"] == "accepted"


def test_curate_no_ai_dir_still_revalidates(tmp_path):
    """无 .vrp/ai/ 时 curate 仍做 stale 复验（files 空不报错）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    con = _con(repo)
    try:
        receipt = curate_mod.curate(repo, con)
        assert receipt["files"] == []
        assert receipt["stale"] == {"slices": [], "structure": False}
    finally:
        con.close()


# --------------------------------------- R-B 批：重复 id 拒绝 + 精确否决


def test_curate_payload_duplicate_slice_id_rejected(tmp_path):
    """同批 slices 数组重复 slice_id：整批拒绝（原先后条静默覆盖、收据谎报条数）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    con = _con(repo)
    try:
        rows = [_slice(fids), _slice(fids)]  # 两条同 slice_id="test-flow"
        receipt = curate_mod.curate_payload(con, "slices", json.dumps(rows, ensure_ascii=False))
        assert receipt["status"] == "rejected"
        assert receipt["rejected"] == 2
        assert any(e["code"] == "SLICE_SCHEMA" and "duplicate" in e["detail"] for e in receipt["errors"])
        assert con.execute("SELECT COUNT(*) FROM slices").fetchone()[0] == 0
    finally:
        con.close()


def test_curate_file_duplicate_slice_id_rejected(tmp_path):
    """CLI 文件路径同洞：重复 slice_id 整文件拒绝，不静默覆盖。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    (ai / "slices.json").write_text(json.dumps([_slice(fids), _slice(fids)], ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        receipt = curate_mod.curate(repo, con)
        entry = receipt["files"][0]
        assert entry["status"] == "rejected"
        assert any(e["code"] == "SLICE_SCHEMA" and "duplicate" in e["detail"] for e in entry["errors"])
        assert con.execute("SELECT COUNT(*) FROM slices").fetchone()[0] == 0
    finally:
        con.close()


def test_structure_duplicate_module_id_rejected(tmp_path):
    """structure modules 重复 module_id：拒绝（载荷内模块身份必须唯一）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    con = _con(repo)
    try:
        payload = _structure(fids)
        payload["modules"].append(
            {"module_id": "m1", "name": "重复", "responsibility": "r", "symbol_fids": ["fid-b"], "layer": "domain"}
        )
        receipt = curate_mod.curate_payload(con, "structure", json.dumps(payload, ensure_ascii=False))
        assert receipt["status"] == "rejected"
        assert any(e["code"] == "STRUCT_SCHEMA" and "duplicate" in e["detail"] for e in receipt["errors"])
        assert con.execute("SELECT COUNT(*) FROM structure").fetchone()[0] == 0
    finally:
        con.close()


def test_reject_structure_numeric_target_id(tmp_path):
    """reject structure：数字 target_id 精确否决该行；未知 id 诚实报错；字面量仍否决最新。"""
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        for _ in range(2):
            con.execute(
                "INSERT INTO structure (payload_json, status, created_at) VALUES ('{\"modules\":[]}', 'active', 't')"
            )
        con.commit()
        out = curate_mod.reject(con, "structure", "1", "精确否决第一版")
        assert out["target_id"] == "1"
        assert con.execute("SELECT status FROM structure WHERE id = 1").fetchone()[0] == "rejected"
        assert con.execute("SELECT status FROM structure WHERE id = 2").fetchone()[0] == "active"
        with pytest.raises(SystemExit):
            curate_mod.reject(con, "structure", "999", "不存在的行")
        curate_mod.reject(con, "structure", "structure", "CLI 惯例字面量")
        assert con.execute("SELECT status FROM structure WHERE id = 2").fetchone()[0] == "rejected"
        decisions = con.execute("SELECT target_id FROM decisions ORDER BY id").fetchall()
        assert [d[0] for d in decisions] == ["1", "structure"]  # 999 失败不留痕
    finally:
        con.close()
# --------------------------------------- R-AD：非 dict payload 复验 + 收据计数


def test_curate_stale_revalidation_nondict_payload(tmp_path):
    """R-AD：合法 JSON 非 dict 的 active 载荷复验标 stale（坏载荷=不可复验，诚实）。"""
    repo = _mk_repo(tmp_path)
    _seed_chain(repo)
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO slices (slice_id, name, summary, payload_json, status)"
            " VALUES ('bad-nondict', 'n', 's', '[1, 2]', 'active')"
        )
        con.execute("INSERT INTO structure (payload_json, status) VALUES ('\"scalar\"', 'active')")
        con.commit()
        receipt = curate_mod.curate(repo, con)
        assert receipt["stale"]["slices"] == ["bad-nondict"]
        assert receipt["stale"]["structure"] is True
        # slice 不可复验=报告-only（与 JSONDecodeError 同语义，不改库）；structure 复验失败落库 stale
        status = con.execute("SELECT status FROM slices WHERE slice_id = 'bad-nondict'").fetchone()[0]
        assert status == "active"
        assert con.execute("SELECT status FROM structure").fetchone()[0] == "stale"
    finally:
        con.close()


def test_curate_structure_rejection_receipt_counts_one(tmp_path):
    """R-AD：CLI structure 拒收收据 rejected=1（与 MCP curate_payload 同形，收据计数红线）。"""
    repo = _mk_repo(tmp_path)
    fids = _seed_chain(repo)
    ai = repo / ".vrp" / "ai"
    ai.mkdir(parents=True)
    bad = _structure(fids, layers=[{"name": "other", "may_depend_on": []}])
    (ai / "structure.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    con = _con(repo)
    try:
        receipt = curate_mod.curate(repo, con)
        entry = receipt["files"][0]
        assert entry["status"] == "rejected"
        assert entry["accepted"] == 0
        assert entry["rejected"] == 1
        assert any(e["code"] == "STRUCT_UNKNOWN_LAYER" for e in entry["errors"])
    finally:
        con.close()
