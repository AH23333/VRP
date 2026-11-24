"""批④ A3 前置：drift 复验历史事件流测试。

drift_history 表（facts_schema.sql + OWNED_TABLES 注册跨 etl 存续）+
_revalidate_ingested 五类时刻落库（etl-full/etl-incremental/curate/
curate-merge/mcp）+ drift_history 读出面 + CLI --history 面。
A3 外推本体留触发池（#227 到点看盘实录细化门槛：非零漂移观测 ≥5 后再看
——事件计数是无信号假阳性触发器，12 事件全零实录），本面守数据积累+信号计数。
"""

import json
import sqlite3
from pathlib import Path

from vrp import curate as curate_mod
from vrp import etl
from vrp.cli import cmd_drift

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".vrp").mkdir(parents=True)
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("INSERT INTO meta (key, value) VALUES ('git_head', 'head-1')")
    con.commit()
    con.close()
    return repo


def _con(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(repo / ".vrp" / "facts.db")


def _seed_symbol(repo: Path, fid: str) -> None:
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO symbols (fid, project, qualified_name, label, name, file_path,"
            " start_line, end_line, content_hash) VALUES (?, 'p', ?, 'Function', ?, 'pkg/x.py', 1, 3, 'h')",
            (fid, f"pkg.{fid}", fid),
        )
        con.commit()
    finally:
        con.close()


def _insert_active_slice(repo: Path, fids: list[str], slice_id: str = "s1") -> None:
    payload = {
        "slice_id": slice_id,
        "name": "n",
        "summary": "s",
        "steps": [{"fid": f, "role": "process"} for f in fids],
    }
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO slices (slice_id, name, summary, payload_json, status)"
            " VALUES (?, 'n', 's', ?, 'active')",
            (slice_id, json.dumps(payload, ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()


def _rows(repo: Path) -> list[tuple]:
    con = _con(repo)
    try:
        return con.execute(
            "SELECT git_head, event, payload_json FROM drift_history ORDER BY id"
        ).fetchall()
    finally:
        con.close()


# ------------------------------------------------------------- 表与注册面


def test_drift_history_in_schema_and_owned():
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS drift_history" in sql
    assert "drift_history" in etl.OWNED_TABLES  # 跨 etl 存续（漏登即红）


def test_owned_snapshot_restore_roundtrip(tmp_path):
    """OWNED 存续：drift_history 行经 snapshot/restore 往返不丢（跨 etl 历史不丢）。"""
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        curate_mod._revalidate_ingested(con, git_head="head-1", event="etl-full")
    finally:
        con.close()
    snap = etl.snapshot_owned(repo / ".vrp" / "facts.db")
    assert "drift_history" in snap  # OWNED_TABLES 成员锁（漏登即红）
    dst = sqlite3.connect(tmp_path / "new.db")
    try:
        dst.executescript(SCHEMA.read_text(encoding="utf-8"))
        warnings = etl.restore_owned(dst, snap)
        dst.commit()
        assert warnings == []
        assert dst.execute("SELECT COUNT(*) FROM drift_history").fetchone()[0] == 1
    finally:
        dst.close()


# ------------------------------------------------------------- 落库面


def test_revalidate_records_history_fresh(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        out = curate_mod._revalidate_ingested(con, git_head="head-1", event="etl-full")
    finally:
        con.close()
    assert out == {"slices": [], "structure": False}
    rows = _rows(repo)
    assert len(rows) == 1
    head, event, payload_json = rows[0]
    assert head == "head-1" and event == "etl-full"
    payload = json.loads(payload_json)
    assert payload["slices_checked"] == 1
    assert payload["slices_new_stale"] == 0
    assert payload["new_stale_slice_ids"] == []
    assert payload["structure_new_stale"] is False
    assert payload["slices_active_after"] == 1
    assert payload["slices_stale_after"] == 0
    assert payload["structure_active_after"] == 0


def test_revalidate_records_history_stale(tmp_path):
    repo = _mk_repo(tmp_path)
    _insert_active_slice(repo, ["fid-gone"])  # fid 不在 symbols → 复验标 stale
    con = _con(repo)
    try:
        out = curate_mod._revalidate_ingested(con, git_head="", event="curate")
    finally:
        con.close()
    assert out["slices"] == ["s1"]
    _, event, payload_json = _rows(repo)[0]
    assert event == "curate"
    payload = json.loads(payload_json)
    assert payload["slices_checked"] == 1
    assert payload["slices_new_stale"] == 1
    assert payload["new_stale_slice_ids"] == ["s1"]
    assert payload["slices_active_after"] == 0
    assert payload["slices_stale_after"] == 1


def test_revalidate_payload_bytes_deterministic(tmp_path):
    """同状态两次复验 payload_json 字节恒等（sort_keys 序列化；created_at 在列不在 payload）。"""
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        curate_mod._revalidate_ingested(con)
        curate_mod._revalidate_ingested(con)
        rows = con.execute("SELECT payload_json FROM drift_history ORDER BY id").fetchall()
    finally:
        con.close()
    assert rows[0][0] == rows[1][0]


def test_curate_records_history(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    (repo / ".vrp" / "ai").mkdir()
    (repo / ".vrp" / "ai" / "slices.json").write_text(
        json.dumps(
            [
                {
                    "slice_id": "s1",
                    "name": "n",
                    "summary": "s",
                    "steps": [{"fid": "fid-a", "role": "process"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    con = _con(repo)
    try:
        receipt = curate_mod.curate(repo, con)
    finally:
        con.close()
    assert receipt["files"][0]["status"] == "accepted"
    rows = _rows(repo)
    assert len(rows) == 1
    head, event, _payload = rows[0]
    assert event == "curate"
    assert head == "head-1"  # git_head 批次锚取 meta（_meta_git_head）


def test_curate_payload_records_history_mcp(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    payload_json = json.dumps(
        [{"slice_id": "s1", "name": "n", "summary": "s", "steps": [{"fid": "fid-a", "role": "process"}]}]
    )
    con = _con(repo)
    try:
        receipt = curate_mod.curate_payload(con, "slices", payload_json)
    finally:
        con.close()
    assert receipt["status"] == "accepted"
    _, event, _p = _rows(repo)[0]
    assert event == "mcp"


# ------------------------------------------------------------- 读出面


def test_drift_history_reader(tmp_path):
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        curate_mod._revalidate_ingested(con, git_head="h1", event="etl-full")
        curate_mod._revalidate_ingested(con, git_head="h2", event="etl-incremental")
        out = curate_mod.drift_history(con, limit=1)
    finally:
        con.close()
    assert out["total"] == 2
    assert len(out["events"]) == 1
    assert out["nonzero_drift_events"] == 0  # 两次复验均零漂移（fresh）
    first = out["events"][0]
    assert first["git_head"] == "h2"  # 倒序最近在前
    assert first["event"] == "etl-incremental"
    assert first["payload"]["slices_checked"] == 1


def test_drift_history_nonzero_count(tmp_path):
    """#227 A3 看盘计数：非零漂移观测全表计数（limit 窗口外也计——看盘问积累
    总量）；slices_new_stale>0 与 structure_new_stale 双键各计一次；curate-refresh
    形态（remap 动作收据，无此二键）与损坏 payload 天然不计数。"""
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        curate_mod._revalidate_ingested(con, git_head="h0", event="etl-full")  # 零漂移
        for head, payload in [
            ("h1", {"slices_checked": 1, "slices_new_stale": 1,
                    "new_stale_slice_ids": ["s9"], "structure_new_stale": False}),
            ("h2", {"slices_checked": 1, "slices_new_stale": 0,
                    "new_stale_slice_ids": [], "structure_new_stale": True}),
            ("h3", {"structure_action": "fresh", "slices_refreshed": 1}),  # curate-refresh 形态
        ]:
            con.execute(
                "INSERT INTO drift_history (created_at, git_head, event, payload_json)"
                " VALUES ('t', ?, ?, ?)",
                (head, "etl-full", json.dumps(payload, ensure_ascii=False)),
            )
        con.execute(
            "INSERT INTO drift_history (created_at, git_head, event, payload_json)"
            " VALUES ('t', 'h4', 'etl-full', 'not-json')"
        )
        con.commit()
        out = curate_mod.drift_history(con, limit=2)  # 窗口截断仍全表计数
    finally:
        con.close()
    assert out["total"] == 5
    assert len(out["events"]) == 2
    assert out["nonzero_drift_events"] == 2  # h1 (slices_new_stale=1) + h2 (structure_new_stale)


def test_drift_history_reader_corrupt_payload_tolerant(tmp_path):
    """payload_json 损坏（用户可写 DB，R-X 族）→ payload_raw 原样给出不崩。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        con.execute(
            "INSERT INTO drift_history (created_at, git_head, event, payload_json)"
            " VALUES ('t', 'h', 'etl-full', 'not-json')"
        )
        con.commit()
        out = curate_mod.drift_history(con)
    finally:
        con.close()
    assert out["total"] == 1
    assert out["nonzero_drift_events"] == 0  # 损坏 payload 诚实跳过不计数
    assert out["events"][0]["payload"] == {"payload_raw": "not-json"}


def test_drift_history_reader_old_db_tolerant(tmp_path):
    """批④前旧库（ro 读未经 ensure_schema 自愈）→ 空结果+note 诚实标注。"""
    repo = _mk_repo(tmp_path)
    con = _con(repo)
    try:
        con.execute("DROP TABLE drift_history")
        con.commit()
        out = curate_mod.drift_history(con)
    finally:
        con.close()
    assert out["events"] == []
    assert out["nonzero_drift_events"] == 0
    assert "note" in out


# ------------------------------------------------------------- CLI 面


def test_cmd_drift_history_flag(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        curate_mod._revalidate_ingested(con, git_head="h1", event="etl-full")
    finally:
        con.close()
    args = type("Args", (), {"repo": str(repo), "history": 10})()
    rc = cmd_drift(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "S2.5 drift history" in out
    assert "nonzero_drift_events" in out  # A3 看盘信号面（#227）
    assert "etl-full" in out


def test_cmd_drift_history_off_by_default(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    _seed_symbol(repo, "fid-a")
    _insert_active_slice(repo, ["fid-a"])
    con = _con(repo)
    try:
        curate_mod._revalidate_ingested(con)
    finally:
        con.close()
    args = type("Args", (), {"repo": str(repo), "history": 0})()
    rc = cmd_drift(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "drift history" not in out
