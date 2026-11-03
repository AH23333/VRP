"""M5: on POSIX, _kill_tree must SIGKILL the whole process group.

The child is spawned with start_new_session=True, so it is its own group
leader; proc.kill() only reaps the leader and leaves grandchildren behind.

NOTE: os.killpg / signal.SIGKILL do not exist on Windows, so the test stubs
them (raising=False) and exercises the branch logic only. Real syscall
behavior on POSIX is static-verified, not executed here.
"""

from __future__ import annotations

import pytest

from vrp import s4_freeze

FAKE_SIGKILL = 9  # stand-in for signal.SIGKILL on platforms lacking it


class _FakeProc:
    pid = 4242

    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True


@pytest.fixture()
def posix(monkeypatch):
    monkeypatch.setattr(s4_freeze.sys, "platform", "linux")
    monkeypatch.setattr(s4_freeze.signal, "SIGKILL", FAKE_SIGKILL, raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(s4_freeze.os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)), raising=False)
    return calls


def test_kill_tree_posix_signals_process_group(posix, monkeypatch):
    monkeypatch.setattr(s4_freeze.os, "getpgid", lambda pid: (posix.append(("getpgid", pid)), 9999)[1], raising=False)
    proc = _FakeProc()
    s4_freeze._kill_tree(proc)
    assert ("getpgid", 4242) in posix
    assert ("killpg", 9999, FAKE_SIGKILL) in posix
    assert not proc.killed


def test_kill_tree_posix_tolerates_dead_process(posix, monkeypatch):
    def _raise(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(s4_freeze.os, "getpgid", _raise, raising=False)
    s4_freeze._kill_tree(_FakeProc())  # must not raise
    assert not any(c[0] == "killpg" for c in posix)
