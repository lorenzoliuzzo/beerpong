from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta

from app import db, service


# Invented couples: the public demo must never show the real participants.
DEMO_TEAMS = [
    "I Gufi", "Le Lontre", "I Castori", "Le Volpi", "I Ricci", "Le Marmotte", "I Fenicotteri", "Le Tartarughe",
    "I Pinguini", "Le Gazzelle", "I Bradipi", "Le Api", "I Koala", "Le Foche", "I Delfini", "Le Civette",
    "I Lama", "Le Zebre", "I Panda", "Le Rane", "I Tucani", "Le Orche", "I Canguri", "Le Lucciole",
    "I Camaleonti", "Le Meduse", "I Gabbiani", "Le Puzzole", "I Polpi", "Le Faine", "I Grilli", "Le Talpe",
]
DEMO_PLAYERS = [
    "Ada", "Bruno", "Carla", "Dario", "Elsa", "Furio", "Gina", "Ivo", "Lia", "Mauro", "Nora", "Otto",
    "Pia", "Rino", "Sara", "Tullio", "Ugo", "Vera", "Walter", "Zoe", "Aldo", "Bice", "Ciro", "Dora",
]


def add_demo_teams(db_conn: sqlite3.Connection) -> None:
    rng = random.Random(7)
    for i, name in enumerate(DEMO_TEAMS):
        p1, p2 = rng.sample(DEMO_PLAYERS, 2)
        service.save_team(db_conn, None, name, p1, p2, f"3330000{i:03d}", "", "", paid=True, present=True)


def setup_demo(db_conn: sqlite3.Connection) -> None:
    if not service.teams(db_conn):
        add_demo_teams(db_conn)
        db.set_setting(db_conn, "title", "DEMO · Torneo di Beer Pong")
        db.set_setting(db_conn, "demo", "1")
        db_conn.commit()
    # 1. Ensure all teams are checked in and paid
    with db_conn:
        db_conn.execute("UPDATE teams SET checked_in = 1, paid = 1, payment = 'contanti'")
        db_conn.execute("DELETE FROM reports")

    # 2. Reset and start tournament with 32 teams -> 8 groups of 4 across 4 tables
    cfg = service.load_config(db_conn)
    if cfg.phase != "setup":
        service.reset_tournament(db_conn)
    with db_conn:
        db.set_setting(db_conn, "fields", "4")
        db.set_setting(db_conn, "scoring", "tiri")  # the simulator plays every match shot by shot
        db.set_setting(db_conn, "start_time", "00:00")  # times count from half an hour ago, not from tonight
        db_conn.execute("UPDATE teams SET checked_in = 1, paid = 1, payment = 'contanti'")
        db_conn.execute("DELETE FROM reports")

    start_dt = datetime.now() - timedelta(minutes=30)
    service.start_tournament(db_conn, start_dt, seed=42)

    all_matches = [m for m in service.matches(db_conn) if m.stage == "group"]
    by_table: dict[int, list] = {}
    for m in all_matches:
        by_table.setdefault(m.field or 1, []).append(m)

    # Asynchronous progress per table:
    # Tavolo 1: 4 finished (playing #5)
    # Tavolo 2: 2 finished (playing #3)
    # Tavolo 3: 5 finished (playing #6)
    # Tavolo 4: 3 finished (playing #4)
    completed_per_table = {1: 4, 2: 2, 3: 5, 4: 3}

    rng = random.Random(12345)
    finished_count = 0

    for field_no, queue in sorted(by_table.items()):
        n_done = completed_per_table.get(field_no, 2)
        for idx, m in enumerate(queue[:n_done]):
            s1 = rng.choice([service.CUPS, rng.randint(2, service.CUPS - 1)])
            s2 = service.CUPS if s1 < service.CUPS else rng.randint(1, service.CUPS - 1)

            # Entered by the couples: the cups each side sank, then each player claims theirs.
            hits = {side: set(rng.sample(range(1, service.CUPS + 1), score)) for side, score in ((1, s1), (2, s2))}
            service.record_result(db_conn, m.id, s1, s2)
            service.save_cup_hits(db_conn, m.id, hits)
            for side in (1, 2):
                service.assign_cup_players(db_conn, m.id, side, {c: rng.choice([1, 2]) for c in hits[side]})
            finished_count += 1

    print(
        f"Demo created successfully! {finished_count} matches finished asynchronously across 4 tables, "
        "0 live now."
    )


if __name__ == "__main__":
    # It resets the tournament and marks every couple paid and present: never on the real database.
    if db.db_path().resolve() == db.DEFAULT_DB.resolve():
        raise SystemExit("Set BP_DB to a scratch database, e.g. BP_DB=data/demo.db python -m tools.populate_demo")
    db.init_db()
    conn = db.connect()
    try:
        setup_demo(conn)
    finally:
        conn.close()
