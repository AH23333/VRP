import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\Visual Studio Code\Something\vibe-restruct-pipe")
SCRATCH = ROOT / "work" / "tx-e2e"
CACHE_DB = Path(r"D:\cbm-cache") / "tx-e2e.db"
ENV = {**os.environ, "CBM_CACHE_DIR": r"D:\cbm-cache"}

CALC_BOTH = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_PLUS_SUB = "def plus(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_PLUS = "def plus(a, b):\n    return a + b\n"
CALC_PLUS_EXTRA = "def plus(a, b):\n    return a + b\n\n\ndef extra():\n    return 9\n"
CALC_PLUS_ZERO = "def plus(a, b):\n    return a + b + 0\n"
CALC_TOTAL = "def total(a, b):\n    return a + b\n"
CALC_GRAND = "def grand(a, b):\n    return a + b\n"
TEST_ADD = "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_PLUS = "from pkg.calc import plus\n\n\ndef test_add():\n    assert plus(1, 2) == 3\n"
TEST_TOTAL = "from pkg.calc import total\n\n\ndef test_add():\n    assert total(1, 2) == 3\n"
TEST_GRAND = "from pkg.calc import grand\n\n\ndef test_add():\n    assert grand(1, 2) == 3\n"
API_PLUS = "from pkg.calc import plus\n\n\ndef compute():\n    return plus(1, 2)\n"
API_TOTAL = "from pkg.calc import total\n\n\ndef compute():\n    return total(1, 2)\n"


def run(args, expect_rc=0):
    proc = subprocess.run(
        [sys.executable, "-m", "vrp.cli", *args],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != expect_rc:
        print(f"FAIL rc={proc.returncode} for: {' '.join(args)}")
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        sys.exit(1)
    return proc.stdout


def git(*args, expect_rc=0):
    proc = subprocess.run(["git", *args], cwd=SCRATCH, capture_output=True, text=True)
    if proc.returncode != expect_rc:
        print(f"FAIL git rc={proc.returncode}: git {' '.join(args)}")
        print(proc.stdout, proc.stderr)
        sys.exit(1)
    return proc.stdout


def write_tx(name, tx):
    path = SCRATCH / ".vrp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tx, ensure_ascii=False), encoding="utf-8")
    return str(path)


def transact(tx_file, expect_status, expect_rc=0, confirm=False, expect_failed_gate=None, expect_code=None):
    args = ["transact", str(SCRATCH), str(tx_file)]
    if confirm:
        args.append("--confirm")
    out = run(args, expect_rc=expect_rc)
    verdict = json.loads(
        (SCRATCH / ".vrp" / "verdicts" / Path(tx_file).name).read_text(encoding="utf-8")
    )
    if verdict["status"] != expect_status:
        print(f"FAIL verdict {verdict['status']} != {expect_status}: {json.dumps(verdict, indent=2)[:2000]}")
        sys.exit(1)
    gates = {g["gate"]: g["status"] for g in verdict["gates"]}
    if expect_failed_gate is not None and verdict["failed_gate"] != expect_failed_gate:
        print(f"FAIL failed_gate {verdict['failed_gate']} != {expect_failed_gate}: {json.dumps(verdict, indent=2)[:2000]}")
        sys.exit(1)
    if expect_code is not None:
        failed = next(g for g in verdict["gates"] if g["status"] == "fail")
        if failed["detail"].get("code") != expect_code:
            print(f"FAIL rule code {failed['detail'].get('code')} != {expect_code}: {json.dumps(verdict, indent=2)[:2000]}")
            sys.exit(1)
    print(f"OK {verdict['tid']} -> {verdict['status']} gates={gates} confidence={verdict.get('confidence')}")
    return verdict


shutil.rmtree(SCRATCH, ignore_errors=True)
if CACHE_DB.exists():
    CACHE_DB.unlink()
(SCRATCH / "pkg").mkdir(parents=True)
(SCRATCH / "pkg" / "__init__.py").write_text("", encoding="utf-8")
(SCRATCH / "pkg" / "calc.py").write_text(CALC_BOTH, encoding="utf-8")
(SCRATCH / "test_calc.py").write_text(TEST_ADD, encoding="utf-8")
(SCRATCH / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
(SCRATCH / ".gitignore").write_text(".vrp/\n__pycache__/\n", encoding="utf-8")
git("init", "-q")
git("add", "-A")
git("-c", "user.email=e@e", "-c", "user.name=e", "commit", "-q", "-m", "init")
run(["index", str(SCRATCH), "--reindex"])
run(["etl", str(SCRATCH)])
print("OK index+etl")

transact(
    write_tx(
        "tx-001.json",
        {
            "id": "tx-001",
            "type": "T1",
            "summary": "rename add to plus",
            "declared_moves": [{"from": "tx-e2e.pkg.calc.add", "to": "tx-e2e.pkg.calc.plus"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS_SUB},
                {"file": "test_calc.py", "op": "modify", "content": TEST_PLUS},
            ],
        },
    ),
    "committed",
)
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_SUB
assert "vrp tx-001" in git("log", "-1", "--format=%s")

transact(
    write_tx(
        "tx-002.json",
        {
            "id": "tx-002",
            "type": "T2",
            "summary": "remove dead sub",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS}],
        },
    ),
    "committed",
)
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS

transact(
    write_tx(
        "tx-003.json",
        {
            "id": "tx-003",
            "type": "T1",
            "summary": "undeclared extra function",
            "declared_moves": [{"from": "tx-e2e.pkg.calc.plus", "to": "tx-e2e.pkg.calc.plus"}],
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS_EXTRA}],
        },
    ),
    "rolled_back",
    expect_rc=1,
    expect_failed_gate="G4",
    expect_code="gate/G4/undeclared-change",
)
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS
status = git("status", "--porcelain")
assert not status.strip(), f"tree dirty after rollback: {status}"

transact(
    write_tx(
        "tx-004.json",
        {
            "id": "tx-004",
            "type": "T4",
            "summary": "touch plus behavior",
            "edits": [{"file": "pkg/calc.py", "op": "modify", "content": CALC_PLUS_ZERO}],
        },
    ),
    "needs_confirmation",
    expect_rc=1,
)
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS

transact(
    SCRATCH / ".vrp" / "tx-004.json",
    "committed",
    confirm=True,
)
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_ZERO

transact(
    write_tx(
        "tx-005.json",
        {
            "id": "tx-005",
            "type": "T3",
            "summary": "add api module using plus",
            "edits": [{"file": "pkg/api.py", "op": "create", "content": API_PLUS}],
        },
    ),
    "committed",
)
assert (SCRATCH / "pkg" / "api.py").read_text(encoding="utf-8") == API_PLUS

transact(
    write_tx(
        "tx-006.json",
        {
            "id": "tx-006",
            "type": "T1",
            "summary": "rename plus to total, forgot api caller",
            "declared_moves": [{"from": "tx-e2e.pkg.calc.plus", "to": "tx-e2e.pkg.calc.total"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_TOTAL},
                {"file": "test_calc.py", "op": "modify", "content": TEST_TOTAL},
            ],
        },
    ),
    "rolled_back",
    expect_rc=1,
    expect_failed_gate="G4",
    expect_code="gate/G4/edge-mismatch",
)
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_PLUS_ZERO
status = git("status", "--porcelain")
assert not status.strip(), f"tree dirty after rollback: {status}"

transact(
    write_tx(
        "tx-007.json",
        {
            "id": "tx-007",
            "type": "T1",
            "summary": "rename plus to total everywhere",
            "declared_moves": [{"from": "tx-e2e.pkg.calc.plus", "to": "tx-e2e.pkg.calc.total"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_TOTAL},
                {"file": "test_calc.py", "op": "modify", "content": TEST_TOTAL},
                {"file": "pkg/api.py", "op": "modify", "content": API_TOTAL},
            ],
        },
    ),
    "committed",
)
assert (SCRATCH / "pkg" / "api.py").read_text(encoding="utf-8") == API_TOTAL

txs_dir = SCRATCH / ".vrp" / "txs"
txs_dir.mkdir(parents=True, exist_ok=True)
(txs_dir / "01-remove-api.json").write_text(
    json.dumps(
        {
            "id": "tx-b01",
            "type": "T2",
            "summary": "remove dead api module",
            "edits": [{"file": "pkg/api.py", "op": "delete"}],
        }
    ),
    encoding="utf-8",
)
(txs_dir / "02-total-to-grand.json").write_text(
    json.dumps(
        {
            "id": "tx-b02",
            "type": "T1",
            "summary": "rename total to grand",
            "declared_moves": [{"from": "tx-e2e.pkg.calc.total", "to": "tx-e2e.pkg.calc.grand"}],
            "edits": [
                {"file": "pkg/calc.py", "op": "modify", "content": CALC_GRAND},
                {"file": "test_calc.py", "op": "modify", "content": TEST_GRAND},
            ],
        }
    ),
    encoding="utf-8",
)
out = run(["transact-batch", str(SCRATCH), str(txs_dir)])
lines = out.splitlines()
start = next(i for i, line in enumerate(lines) if line.startswith("== "))
batch_summary = json.loads("\n".join(lines[start + 1:]))
print("batch:", batch_summary)
assert batch_summary["statuses"] == ["committed", "committed"], batch_summary
assert batch_summary["stopped_at"] is None
assert not (SCRATCH / "pkg" / "api.py").exists()
assert (SCRATCH / "pkg" / "calc.py").read_text(encoding="utf-8") == CALC_GRAND
assert "vrp tx-b02" in git("log", "-1", "--format=%s")

sys.path.insert(0, str(ROOT))
import sqlite3

con = sqlite3.connect(SCRATCH / ".vrp" / "facts.db")
rows = con.execute("SELECT tid, kind, status FROM transactions ORDER BY tid").fetchall()
print("transactions:", rows)
assert [r[2] for r in rows] == [
    "committed",
    "committed",
    "rolled_back",
    "needs_confirmation",
    "committed",
    "committed",
    "rolled_back",
    "committed",
    "committed",
    "committed",
]
print("ALL E2E PASS")
