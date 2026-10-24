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
    -- The short questionnaire after check-in: the couple's own guess, a prior for the live odds.
    q_level TEXT, q_aim1 INTEGER, q_aim2 INTEGER, q_goal TEXT, q_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    grp INTEGER,
    checked_in_at TEXT,  -- when the couple arrived at the venue: both its players, each with a tap
    arrived1_at TEXT, arrived2_at TEXT,
    beer1_at TEXT, beer2_at TEXT,  -- the free first beer each player collects on arrival
    -- How the couple presents itself, set from its own profile.
    emoji TEXT NOT NULL DEFAULT '', motto TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
    photo TEXT NOT NULL DEFAULT '', nick1 TEXT NOT NULL DEFAULT '', nick2 TEXT NOT NULL DEFAULT ''
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
-- The phones that entered a couple, each tied to the player using it when known (they can invite the partner).
CREATE TABLE IF NOT EXISTS devices (
    device TEXT PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player INTEGER CHECK (player IN (1, 2)),
    seen_at TEXT NOT NULL
);
-- Match notices already sent ("now" / "next"), so each goes out once.
CREATE TABLE IF NOT EXISTS push_sent (
    match_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (match_id, kind)
);
-- Live tracking: every tap from any phone; the consensus is computed when reading (see app/live.py).
-- Rule 5, the blow: a ball spinning in a cup blown out by the defending couple. The shot is a miss; the
-- row (tied to the referee's shot) counts as a save for the couple that blew.
CREATE TABLE IF NOT EXISTS blows (
    report_id INTEGER PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    side INTEGER NOT NULL CHECK (side IN (1, 2))
);
-- The crowd cheering a couple during a live match (one emoji per tap, capped per phone).
CREATE TABLE IF NOT EXISTS cheers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    side INTEGER NOT NULL CHECK (side IN (1, 2)),
    emoji TEXT NOT NULL,
    device TEXT NOT NULL,
    at REAL NOT NULL
);
-- The referee re-racks a couple's remaining cups into a compact formation when the shooters ask.
CREATE TABLE IF NOT EXISTS reracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    side INTEGER NOT NULL CHECK (side IN (1, 2)),
    at REAL NOT NULL
);
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
-- The couples enter their own result: which cups of the opponents' rack each couple sank.
CREATE TABLE IF NOT EXISTS cup_hits (
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    side INTEGER NOT NULL CHECK (side IN (1, 2)),
    cup INTEGER NOT NULL CHECK (cup BETWEEN 1 AND 10),
    player INTEGER CHECK (player IN (1, 2)),  -- which player of the couple sank it, as they claim it
    PRIMARY KEY (match_id, side, cup)
);
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
    first_side INTEGER CHECK (first_side IN (1, 2)),  -- the couple the referee picked to shoot first
    mode TEXT  -- how this match is scored, when not as the tournament (service.SCORING)
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
    "date": "",
    "scoring": "coppie",
    "referees": "volontari",
    "fee": "",
    "rules": "",
    "form_url": "",
    "pay_info": "",
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
    if "mode" not in match_columns:
        conn.execute("ALTER TABLE matches ADD COLUMN mode TEXT")
    if "first_side" not in match_columns:
        conn.execute("ALTER TABLE matches ADD COLUMN first_side INTEGER CHECK (first_side IN (1, 2))")
    if "organizer" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN organizer INTEGER NOT NULL DEFAULT 0")
    for column, kind in (("q_level", "TEXT"), ("q_aim1", "INTEGER"), ("q_aim2", "INTEGER"), ("q_goal", "TEXT"), ("q_at", "TEXT")):
        if column not in team_columns:
            conn.execute(f"ALTER TABLE teams ADD COLUMN {column} {kind}")
    hit_columns = {row[1] for row in conn.execute("PRAGMA table_info(cup_hits)")}
    if "player" not in hit_columns:
        conn.execute("ALTER TABLE cup_hits ADD COLUMN player INTEGER CHECK (player IN (1, 2))")
    if "checked_in_at" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN checked_in_at TEXT")
    for column in ("arrived1_at", "arrived2_at", "beer1_at", "beer2_at"):
        if column not in team_columns:
            conn.execute(f"ALTER TABLE teams ADD COLUMN {column} TEXT")
    for column in ("emoji", "motto", "color", "photo", "nick1", "nick2"):
        if column not in team_columns:
            conn.execute(f"ALTER TABLE teams ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    if "referee_player" not in team_columns:
        conn.execute("ALTER TABLE teams ADD COLUMN referee_player INTEGER CHECK (referee_player IN (1, 2))")
    # Table PINs for referees are gone: only the admin has a PIN. Map the old referee choices onto what is left.
    conn.execute("DELETE FROM settings WHERE key LIKE 'ref_pin_%'")
    conn.execute("UPDATE settings SET value = 'volontari' WHERE key = 'referees' AND value = 'entrambi'")
    conn.execute("UPDATE settings SET value = 'organizzatori' WHERE key = 'referees' AND value = 'pin'")


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
