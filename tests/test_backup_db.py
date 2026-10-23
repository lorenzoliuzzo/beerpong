import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from tools.backup_db import backup


def test_backup_copies_data_and_keeps_only_the_latest(tmp_path: Path) -> None:
    src = tmp_path / "live.db"
    with sqlite3.connect(src) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE teams (name TEXT)")
        conn.execute("INSERT INTO teams VALUES ('Le Lontre')")
    start = datetime(2026, 10, 23, 20, 0)
    made = [backup(src, tmp_path / "backups", 3, start + timedelta(minutes=15 * i)) for i in range(5)]

    assert sorted((tmp_path / "backups").iterdir()) == made[-3:]
    with sqlite3.connect(made[-1]) as copy:
        assert copy.execute("SELECT name FROM teams").fetchall() == [("Le Lontre",)]


def test_backup_refuses_a_missing_database(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "nope.db", tmp_path / "backups", 3, datetime(2026, 10, 23, 20, 0))
    assert not (tmp_path / "nope.db").exists()
