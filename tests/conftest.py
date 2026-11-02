import sqlite3
from pathlib import Path

import pytest

SCHEMA = Path(__file__).parent.parent / "vrp" / "schema" / "facts_schema.sql"


@pytest.fixture
def facts_db():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    yield con
    con.close()
