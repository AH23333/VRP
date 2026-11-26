"""J9 S2 病理引擎并行测试（设计稿-J9 D1-D6）。

- run_thunks 契约：定序返回/全跑完不取消/首错按任务序/自动 workers=任务数/
  <=1 串行直跑；
- cmd_s2 引擎级四并发：串行 vs 并行等价对拍（D6：逐 source findings 行序
  +JSON 输出恒等——id/created_at 时点列不参与，J8 同裁定）、并发重叠实证、
  VRP_S2_WORKERS 逃生门（D3）、错误按引擎序抛首错（D4）、每引擎自建连接
  （D2 SQLite 并发纪律）。

时序控制纪律（R-W census Face D：tests/ 禁真实 time.sleep）：完成序倒挂/
串行化/首错序全部用 Event 链与事件序列钉死——确定性断言，零竞态窗口技巧。
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from vrp import cli
from vrp.procs import run_thunks

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"

WAIT_TIMEOUT = 15  # Event/Barrier 等待上限：死锁即红而非挂死（有界等待纪律）


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".vrp").mkdir()
    con = sqlite3.connect(tmp_path / ".vrp" / "facts.db")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()
    return tmp_path


def _reset_db(repo):
    db = repo / ".vrp" / "facts.db"
    db.unlink()
    con = sqlite3.connect(db)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    con.close()


def _findings_rows(repo):
    """逐 source 分组、组内按 id 升序（=单引擎单 executemany 插入序）。

    id/created_at 时点性元数据不参与对拍（J8 同裁定：全量自身也不自复现，
    红线绑下游产物）。
    """
    con = sqlite3.connect(repo / ".vrp" / "facts.db")
    try:
        rows = con.execute(
            "SELECT source, pathology, kind, anchor_fid, related_fid, file_path,"
            " start_line, end_line, payload_json FROM findings ORDER BY source, id"
        ).fetchall()
    finally:
        con.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r[1:])
    return grouped


def _run_cmd_s2(repo, capsys):
    rc = cli.cmd_s2(type("Args", (), {"repo": str(repo), "json": True})())
    out = capsys.readouterr().out
    return rc, out


# ---------- run_thunks 契约（D2/D4） ----------


def test_run_thunks_empty():
    assert run_thunks([]) == []


def test_run_thunks_single_direct():
    called = []
    result = run_thunks([lambda: called.append(1) or "x"])
    assert result == ["x"]
    assert called == [1]


def test_run_thunks_results_in_submission_order():
    # 完成序倒挂（后提交先完成）用 Event 链钉死：task0 等 task1 完成、
    # task1 等 task2、task2 等 task3——完成序 3→2→1→0，结果仍按提交序
    ev1, ev2, ev3 = (threading.Event() for _ in range(3))

    def mk(i, wait_ev, set_ev):
        def _run():
            if wait_ev is not None:
                assert wait_ev.wait(timeout=WAIT_TIMEOUT), f"task{i} predecessor never finished"
            if set_ev is not None:
                set_ev.set()
            return f"r{i}"

        return _run

    thunks = [mk(0, ev1, None), mk(1, ev2, ev1), mk(2, ev3, ev2), mk(3, None, ev3)]
    assert run_thunks(thunks) == ["r0", "r1", "r2", "r3"]


def test_run_thunks_auto_workers_allows_full_overlap():
    # 自动=任务数：4 任务齐齐到 Barrier 才放行——不足 4 并发即超时红
    barrier = threading.Barrier(4, timeout=WAIT_TIMEOUT)

    def wait_and_done():
        barrier.wait()
        return "ok"

    assert run_thunks([wait_and_done] * 4) == ["ok"] * 4


def test_run_thunks_max_workers_one_serializes():
    # 串行=事件序列严格 start/end 交替（两段独立加锁：并发下才会出现
    # start,start 相邻——确定性断言，不靠竞态窗口）
    events = []
    lock = threading.Lock()

    def task():
        with lock:
            events.append("start")
        with lock:
            events.append("end")
        return "ok"

    assert run_thunks([task] * 4, 1) == ["ok"] * 4
    assert events == ["start", "end"] * 4


def test_run_thunks_first_exception_by_task_order_and_no_cancel():
    """D4：全任务跑完不抢跑取消；异常按任务序抛首个（非完成序）。"""
    done = set()
    lock = threading.Lock()
    ev = threading.Event()

    def fail_wait(tag, wait_ev):
        def _run():
            if wait_ev is not None:
                assert wait_ev.wait(timeout=WAIT_TIMEOUT), f"{tag} predecessor never finished"
            with lock:
                done.add(tag)
            raise RuntimeError(tag)

        return _run

    def ok_set(tag):
        def _run():
            with lock:
                done.add(tag)
            ev.set()
            return tag

        return _run

    # 完成序：boom-third 立即失败 → second 完成并放行 → boom-first 最后失败；
    # 抛出的必须是任务序首错 boom-first
    thunks = [fail_wait("boom-first", ev), ok_set("second"), fail_wait("boom-third", None)]
    with pytest.raises(RuntimeError, match="boom-first"):
        run_thunks(thunks)
    # 后续任务未被取消，全部跑完
    assert done == {"boom-first", "second", "boom-third"}


def test_run_thunks_serial_path_raises_same_face():
    def boom():
        raise RuntimeError("serial-boom")

    ran = []

    def later():
        ran.append(1)
        return "x"

    with pytest.raises(RuntimeError, match="serial-boom"):
        run_thunks([boom, later], 1)
    # 串行路径：首错即停（与旧串行编排同面——workers=1 调试模式）
    assert ran == []


# ---------- _s2_workers 逃生门（D3） ----------


def test_s2_workers_env_parsing(monkeypatch):
    monkeypatch.delenv("VRP_S2_WORKERS", raising=False)
    assert cli._s2_workers() == 0
    monkeypatch.setenv("VRP_S2_WORKERS", "1")
    assert cli._s2_workers() == 1
    monkeypatch.setenv("VRP_S2_WORKERS", "4")
    assert cli._s2_workers() == 4
    monkeypatch.setenv("VRP_S2_WORKERS", "not-a-number")
    assert cli._s2_workers() == 0


# ---------- cmd_s2 引擎级四并发 ----------


def _install_fake_engines(monkeypatch, repo, probe=None):
    """四假引擎：各写 3 行自有 source findings（确定序），返回可判等 dict。

    probe 可选钩子（每引擎调用，参数=连接）——并发探针/连接探针复用。
    """
    import vrp.s2_astgrep as astgrep_mod
    import vrp.s2_deadcode as deadcode_mod
    import vrp.s2_jscpd as jscpd_mod
    import vrp.s2_p4 as p4_mod

    def fake(source, ret):
        def _scan(con, *_args, **_kwargs):
            if probe is not None:
                probe(con)
            con.execute(
                "DELETE FROM findings WHERE source = ?",
                (source,),
            )
            con.executemany(
                "INSERT INTO findings (pathology, kind, source, file_path, start_line, end_line, payload_json)"
                " VALUES ('P-test', 'k', ?, ?, ?, ?, '{}')",
                [(source, f"src/{source}/f{i}.py", i + 1, i + 2) for i in range(3)],
            )
            con.commit()
            return dict(ret)

        return _scan

    monkeypatch.setattr(astgrep_mod, "scan", fake("s2-astgrep", {"total": 3, "anchored": 0, "rejected": 0, "per_rule": {}}))
    monkeypatch.setattr(deadcode_mod, "scan", fake("s2-deadcode", {"total": 3, "by_confidence": {"high": 3, "medium": 0, "low": 0}}))
    monkeypatch.setattr(p4_mod, "scan", fake("s2-knip", {"knip": 3, "vulture": "skipped", "ledger_rows": 0}))
    monkeypatch.setattr(jscpd_mod, "scan", fake("s2-jscpd", {"jscpd": 3, "ledger_rows": 0}))


def test_cmd_s2_equivalent_serial_vs_parallel(repo, capsys, monkeypatch):
    """D6 等价判据：串行（VRP_S2_WORKERS=1）vs 并行（自动）逐 source
    findings 行序 + JSON 输出恒等。"""
    import json

    _install_fake_engines(monkeypatch, repo)

    monkeypatch.setenv("VRP_S2_WORKERS", "1")
    rc_s, out_s = _run_cmd_s2(repo, capsys)
    assert rc_s == 0
    rows_serial = _findings_rows(repo)

    _reset_db(repo)
    monkeypatch.delenv("VRP_S2_WORKERS", raising=False)
    rc_p, out_p = _run_cmd_s2(repo, capsys)
    assert rc_p == 0
    rows_parallel = _findings_rows(repo)

    assert rows_serial == rows_parallel
    assert sorted(rows_parallel) == ["s2-astgrep", "s2-deadcode", "s2-jscpd", "s2-knip"]
    for rows in rows_parallel.values():
        assert len(rows) == 3
    assert json.loads(out_s) == json.loads(out_p)
    payload = json.loads(out_p)
    assert payload["dead_code_candidates"] == 3
    assert payload["p4"]["knip"] == 3
    assert payload["p2"]["jscpd"] == 3


def test_cmd_s2_parallel_engines_overlap(repo, capsys, monkeypatch):
    """D1：引擎级四并发实证——四假引擎 Barrier(4) 齐放行，不足四并发即红。"""
    barrier = threading.Barrier(4, timeout=WAIT_TIMEOUT)

    def probe(_con):
        barrier.wait()

    _install_fake_engines(monkeypatch, repo, probe=probe)
    rc, _ = _run_cmd_s2(repo, capsys)
    assert rc == 0


def test_cmd_s2_workers_one_serializes(repo, capsys, monkeypatch):
    """D3：VRP_S2_WORKERS=1 串行回退——引擎 start/end 事件严格交替。"""
    events = []
    lock = threading.Lock()

    def probe(_con):
        with lock:
            events.append("start")
        with lock:
            events.append("end")

    _install_fake_engines(monkeypatch, repo, probe=probe)
    monkeypatch.setenv("VRP_S2_WORKERS", "1")
    rc, _ = _run_cmd_s2(repo, capsys)
    assert rc == 0
    assert events == ["start", "end"] * 4


def test_cmd_s2_each_engine_own_connection(repo, capsys, monkeypatch):
    """D2：每引擎线程自建连接——4 连接对象互异。

    强引用持有连接对象（非裸 id()）：顺序执行+及时关闭下 id 会被复用
    （同地址重分配，实测环境敏感 flake——干净 master 5/5 红）；强引用使
    4 对象必然共存，断言确定性（R-W 零竞态纪律；批④ BB5 审计收口）。
    """
    seen: list = []
    lock = threading.Lock()

    def probe(con):
        with lock:
            seen.append(con)

    _install_fake_engines(monkeypatch, repo, probe=probe)
    rc, _ = _run_cmd_s2(repo, capsys)
    assert rc == 0
    assert len(seen) == 4
    assert len({id(c) for c in seen}) == 4


def test_cmd_s2_error_first_by_engine_order(repo, capsys, monkeypatch):
    """D4：全引擎跑完后按引擎序抛首错——astgrep（引擎序 0）等 jscpd 完成
    后才失败也轮到它先抛；deadcode 立即失败（完成序在前）不抢首错；
    后续引擎照常完成写入（不取消）。"""
    import vrp.s2_astgrep as astgrep_mod
    import vrp.s2_deadcode as deadcode_mod
    import vrp.s2_p4 as p4_mod
    import vrp.s2_jscpd as jscpd_mod

    ev = threading.Event()
    ran = {}

    def astgrep_scan(con, *_a, **_k):
        assert ev.wait(timeout=WAIT_TIMEOUT), "jscpd engine never completed"
        raise RuntimeError("boom-astgrep")

    def deadcode_scan(con, *_a, **_k):
        raise RuntimeError("boom-deadcode")

    def ok(tag, set_ev=False):
        def _scan(con, *_a, **_k):
            con.execute(
                "INSERT INTO findings (pathology, kind, source, payload_json)"
                " VALUES ('P', 'k', ?, '{}')",
                (tag,),
            )
            con.commit()
            ran[tag] = True
            if set_ev:
                ev.set()
            return {"total": 1}

        return _scan

    monkeypatch.setattr(astgrep_mod, "scan", astgrep_scan)
    monkeypatch.setattr(deadcode_mod, "scan", deadcode_scan)
    monkeypatch.setattr(p4_mod, "scan", ok("s2-knip"))
    monkeypatch.setattr(jscpd_mod, "scan", ok("s2-jscpd", set_ev=True))

    with pytest.raises(RuntimeError, match="boom-astgrep"):
        cli.cmd_s2(type("Args", (), {"repo": str(repo)})())
    assert ran == {"s2-knip": True, "s2-jscpd": True}
