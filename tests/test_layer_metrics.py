"""C8 层度量与阈值测试面：fan_in/fan_out/I 公式/去重边对/多属/阈值/rc/CLI。"""

import json
import sqlite3
from pathlib import Path

from vrp import layer_metrics
from vrp.cli import main as cli_main

REPO_ROOT = Path(__file__).parent.parent
SCHEMA = REPO_ROOT / "vrp" / "schema" / "facts_schema.sql"

F1, F2, F3, F4 = "f1", "f2", "f3", "f4"


def _seed(con, thresholds=None):
    """两模块两层：core(f1,f2)→api(f3,f4) 三条逻辑边（两条多证据行去重）。"""
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for fid in (F1, F2, F3, F4):
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path)"
            " VALUES (?,'p','p.'||?,'Function',?,'a.py')",
            (fid, fid, fid),
        )
    # 三条逻辑边：f1→f3（两行证据——去重后 1 对）、f2→f3、f1→f4
    for src, dst in ((F1, F3), (F1, F3), (F2, F3), (F1, F4)):
        con.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,'CALLS','import')", (src, dst))
    payload = {
        "modules": [
            {"module_id": "core", "name": "core", "symbol_fids": [F1, F2], "layer": "bottom"},
            {"module_id": "api", "name": "api", "symbol_fids": [F3, F4], "layer": "top"},
        ],
        "layers": [
            {"name": "top", "may_depend_on": ["bottom"]},
            {"name": "bottom", "may_depend_on": []},
        ],
    }
    if thresholds is not None:
        payload["thresholds"] = thresholds
    con.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?,'active','t')",
        (json.dumps(payload, ensure_ascii=False),),
    )
    con.commit()


def test_fan_in_out_and_instability_formula(facts_db):
    _seed(facts_db)
    result = layer_metrics.run_layer_metrics(facts_db)
    assert result["structure"] is True
    mods = {m["name"]: m for m in result["modules"]}
    # core：出 3（f1→f3、f2→f3、f1→f4——多证据行去重）、入 0
    assert mods["core"]["fan_out"] == 3
    assert mods["core"]["fan_in"] == 0
    assert mods["core"]["instability"] == 1.0  # I=Ce/(Ca+Ce)=3/3
    assert mods["api"]["fan_in"] == 3
    assert mods["api"]["fan_out"] == 0
    assert mods["api"]["instability"] == 0.0


def test_layer_aggregation(facts_db):
    _seed(facts_db)
    result = layer_metrics.run_layer_metrics(facts_db)
    layers = {l["name"]: l for l in result["layers"]}
    assert layers["bottom"]["fan_out"] == 3
    assert layers["bottom"]["fan_in"] == 0
    assert layers["top"]["fan_in"] == 3
    assert layers["top"]["fan_out"] == 0


def test_isolated_scope_instability_none(facts_db):
    _seed(facts_db)
    facts_db.execute("INSERT INTO edges (src_fid, dst_fid, type, evidence) VALUES (?,?,'CALLS','import')", (F3, F4))
    # api 内部边不算跨模块——api 仍孤立面?不：api 已有 fan_in 3。改构造独立仓验证：
    facts_db.execute("DELETE FROM structure")
    payload = {
        "modules": [
            {"module_id": "solo", "name": "solo", "symbol_fids": [F1], "layer": "only"},
            {"module_id": "solo2", "name": "solo2", "symbol_fids": [F2], "layer": "only"},
        ],
        "layers": [{"name": "only", "may_depend_on": []}],
    }
    facts_db.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?,'active','t')",
        (json.dumps(payload, ensure_ascii=False),),
    )
    facts_db.commit()
    result = layer_metrics.run_layer_metrics(facts_db)
    mods = {m["name"]: m for m in result["modules"]}
    # F1↔F2 无边（已有边是 F1→F3 等，f3/f4 不在结构宇宙——排除噪声地板）
    assert mods["solo"]["fan_in"] == 0 and mods["solo"]["fan_out"] == 0
    assert mods["solo"]["instability"] is None  # 孤立——诚实未定义
    assert all(l["instability"] is None for l in result["layers"])


def test_multi_module_fid_membership_counted_per_pair(facts_db):
    _seed(facts_db)
    facts_db.execute("DELETE FROM structure")
    payload = {
        "modules": [
            {"module_id": "a", "name": "a", "symbol_fids": [F1], "layer": "l"},
            {"module_id": "b", "name": "b", "symbol_fids": [F1], "layer": "l"},  # F1 双属
            {"module_id": "c", "name": "c", "symbol_fids": [F3], "layer": "l"},
        ],
        "layers": [{"name": "l", "may_depend_on": []}],
    }
    facts_db.execute(
        "INSERT INTO structure (payload_json, status, created_at) VALUES (?,'active','t')",
        (json.dumps(payload, ensure_ascii=False),),
    )
    facts_db.commit()
    result = layer_metrics.run_layer_metrics(facts_db)
    mods = {m["name"]: m for m in result["modules"]}
    assert mods["a"]["fan_out"] == 1  # a→c 一对
    assert mods["b"]["fan_out"] == 1  # b→c 也计——多属模块对全组合（s7 先例）
    assert mods["c"]["fan_in"] == 2


def test_no_structure_honest_rc_zero(facts_db):
    facts_db.executescript(SCHEMA.read_text(encoding="utf-8"))
    facts_db.commit()
    result = layer_metrics.run_layer_metrics(facts_db)
    assert result["structure"] is False
    assert result["violations"] == []
    assert "未 curate" in result["notes"][0]


def test_stale_structure_distinguished_from_absent(facts_db):
    _seed(facts_db)
    facts_db.execute("UPDATE structure SET status='stale'")
    facts_db.commit()
    result = layer_metrics.run_layer_metrics(facts_db)
    assert result["structure"] is False  # s7 同语义只消费 active
    assert "已陈旧（status=stale" in result["notes"][0]
    assert "curate-auto" in result["notes"][0]


def test_thresholds_violations_and_sorted(facts_db):
    _seed(
        facts_db,
        thresholds={
            "modules": {
                "core": {"max_fan_out": 2},  # 实际 3 → 违例
                "api": {"max_fan_in": 10, "max_fan_out": 5},  # 全过
            },
            "layers": {"bottom": {"max_fan_out": 1}},  # 实际 3 → 违例
        },
    )
    result = layer_metrics.run_layer_metrics(facts_db)
    assert result["thresholds_declared"] is True
    v = result["violations"]
    assert v == [
        {"scope": "layer", "name": "bottom", "metric": "fan_out", "value": 3, "limit": 1},
        {"scope": "module", "name": "core", "metric": "fan_out", "value": 3, "limit": 2},
    ]


def test_threshold_instability_and_tolerant_shapes(facts_db):
    _seed(
        facts_db,
        thresholds={
            "modules": {
                "core": {"max_instability": 0.5},  # I=1.0 → 违例
                "api": {"max_instability": 1.0},  # I=0.0 过
            },
            "bogus_key": {"whatever": 1},  # 未知面宽容跳过
        },
    )
    result = layer_metrics.run_layer_metrics(facts_db)
    assert result["violations"] == [
        {"scope": "module", "name": "core", "metric": "instability", "value": 1.0, "limit": 0.5}
    ]


def test_thresholds_non_dict_and_bool_tolerated(facts_db):
    _seed(facts_db, thresholds={"modules": {"core": {"max_fan_out": True}}})  # bool 非法
    facts_db.execute(
        "UPDATE structure SET payload_json = ?",
        (json.dumps({"modules": [{"module_id": "core", "symbol_fids": [F1], "layer": "l"}],
                     "layers": [{"name": "l", "may_depend_on": []}],
                     "thresholds": "not-a-dict"}),),
    )
    facts_db.commit()
    result = layer_metrics.run_layer_metrics(facts_db)
    assert result["thresholds_declared"] is False  # 非 dict=未声明（不猜不造）
    assert result["violations"] == []


def test_render_text_faces(facts_db):
    _seed(facts_db, thresholds={"modules": {"core": {"max_fan_out": 2}}})
    result = layer_metrics.run_layer_metrics(facts_db)
    text = layer_metrics.render_text(result)
    assert "core: fan_in=0 fan_out=3 I=1.0000" in text
    assert "api: fan_in=3 fan_out=0 I=0.0000" in text
    assert "[FAIL] module core: fan_out=3 exceeds limit 2" in text
    result_ok = layer_metrics.run_layer_metrics(facts_db)
    facts_db.execute("UPDATE structure SET payload_json = replace(payload_json, '2}', '99}')")
    facts_db.commit()
    text_ok = layer_metrics.render_text(layer_metrics.run_layer_metrics(facts_db))
    assert "thresholds declared: no violations" in text_ok


def test_cli_layermetrics_rc_semantics(tmp_path, facts_db, capsys):
    _seed(facts_db)
    db_path = tmp_path / ".vrp" / "facts.db"
    db_path.parent.mkdir()
    facts_db.backup(sqlite3.connect(db_path))
    rc = cli_main(["layermetrics", str(tmp_path), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0  # 无 thresholds——纯度量报告
    assert data["thresholds_declared"] is False
    disk = sqlite3.connect(db_path)
    disk.execute(
        "UPDATE structure SET payload_json = replace(payload_json, '\"layers\":', "
        "'\"thresholds\": {\"modules\": {\"core\": {\"max_fan_out\": 2}}}, \"layers\":')"
    )
    disk.commit()
    disk.close()
    rc = cli_main(["layermetrics", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1  # 阈值违例——deptrac 语义 CI 可用
    assert "[FAIL] module core: fan_out=3 exceeds limit 2" in out
