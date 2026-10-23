"""Snapshot the live database while the app keeps running, keeping only the most recent copies.

Run by a systemd timer on the Pi, so whatever the admins change online can always be recovered.

Usage: python -m tools.backup_db [--keep 96]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from app import db

PREFIX = "beerpong-"


def backup(src: Path, dest_dir: Path, keep: int, now: datetime) -> Path:
    if not src.is_file():
        raise FileNotFoundError(f"No database at {src}: is BP_DB set like the server's?")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{PREFIX}{now:%Y%m%d-%H%M%S}.db"
    # Read-only: a wrong path fails here instead of creating an empty database and rotating the real copies away.
    # The backup API copies a consistent snapshot even with the WAL open by the server.
    with sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    for old in sorted(dest_dir.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink()
    return dest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=96)
    args = parser.parse_args()
    src = db.db_path()
    print(backup(src, src.parent / "backups", args.keep, datetime.now()))


if __name__ == "__main__":
    main()
