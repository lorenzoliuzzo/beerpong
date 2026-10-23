from __future__ import annotations

import os
import secrets
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "beerpong.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    p1 TEXT NOT NULL,
    p2 TEXT NOT NULL,
    phone1 TEXT NOT NULL DEFAULT '',
    phone2 TEXT NOT NULL DEFAULT '',
    checked_in INTEGER NOT NULL DEFAULT 0,
    payment TEXT CHECK (payment IN ('contanti', 'online', 'cassa')),
    paid INTEGER NOT NULL DEFAULT 0,
    in_chat INTEGER NOT NULL DEFAULT 0,
    organizer INTEGER NOT NULL DEFAULT 0,
    referee_player INTEGER CHECK (referee_player IN (1, 2)),  -- an organizer couple's player who referees its table
    note TEXT NOT NULL DEFAULT '',
    grp INTEGER
);
-- People who registered on the form for the party only: their phone lets them enter as a couple.
CREATE TABLE IF NOT EXISTS guests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    registered_at TEXT,
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL
);
-- Web Push subscriptions: one per browser, linked to the couple of that session when known.
CREATE TABLE IF NOT EXISTS push_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL
);
-- Match notices already sent ("now" / "next"), so each goes out once.
CREATE TABLE IF NOT EXISTS push_sent (
    match_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (match_id, kind)
);
-- Live tracking: every tap from any phone; the consensus is computed when reading (see app/live.py).
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    device TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start', 'shot')),
    side INTEGER CHECK (side IN (1, 2)),
    player INTEGER CHECK (player IN (1, 2)),
    cup INTEGER CHECK (cup BETWEEN 0 AND 10),
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_match ON reports(match_id, at);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL CHECK (stage IN ('group', 'ko')),
    grp INTEGER,
    round INTEGER NOT NULL DEFAULT 0,
    pos INTEGER NOT NULL DEFAULT 0,
    slot INTEGER,
    field INTEGER,
    start TEXT,
    duration INTEGER,
    team1 INTEGER REFERENCES teams(id),
    team2 INTEGER REFERENCES teams(id),
    c1a INTEGER, c1b INTEGER, c2a INTEGER, c2b INTEGER,
    score1 INTEGER,
    score2 INTEGER,
    winner INTEGER REFERENCES teams(id),
    done INTEGER NOT NULL DEFAULT 0,
    bye INTEGER NOT NULL DEFAULT 0,
    started_at REAL,  -- set when the referee starts the match: its clock and the table's times run from here
    first_side INTEGER CHECK (first_side IN (1, 2))  -- the couple the referee picked to shoot first
);
"""

DEFAULTS = {
    "fields": "2",
    "group_min": "5",
    "ko_min": "7",
    "buffer_min": "1",
    "public_url": "",
    "title": "Torneo di Beer Pong",
    "target_teams": "32",
    "phase": "setup",
    "start_time": "21:30",
}


def db_path() -> Path:
    return Path(os.environ.get("BP_DB", DEFAULT_DB))


def connect() -> sqlite3.Connection:
    # FastAPI opens and closes a sync dependency in different threadpool threads; each
    # connection still serves a single request at a time.
    conn = sqlite3.connect(db_path(), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def random_pin() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def random_key() -> str:
    return secrets.token_urlsafe(9)


def init_db() -> None:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        admin_pin = os.environ.get("BP_ADMIN_PIN") or random_pin()
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('admin_pin', ?)", (admin_pin,))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('secret', ?)", (secrets.token_hex(32),))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('reg_key', ?)", (random_key(),))
        # Seed of the provisional draw shown on the public board; starting the tournament reuses it.
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('board_seed', ?)", (str(secrets.randbits(31)),))
    conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive changes for databases created by an earlier version; never drops data."""
    guest_columns = {row[1] for row in conn.execute("PRAGMA table_info(guests)")}
    if "registered_at" not in guest_columns:
        conn.execute("ALTER TABLE guests ADD COLUMN registered_at TEXT")
    team_columns = {row[1] for row in conn.execute("PRAGMA table_info(teams)")}
    if "note" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "in_chat" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN in_chat INTEGER NOT NULL DEFAULT 0")
    match_columns = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
    if "started_at" not in match_columns:
        conn.execute("ALTER TABLE matches ADD COLUMN started_at REAL")
    if "first_side" not in match_columns:
        conn.execute("ALTER TABLE matches ADD COLUMN first_side INTEGER CHECK (first_side IN (1, 2))")
    if "organizer" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN organizer INTEGER NOT NULL DEFAULT 0")
    if "referee_player" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN referee_player INTEGER CHECK (referee_player IN (1, 2))")


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
