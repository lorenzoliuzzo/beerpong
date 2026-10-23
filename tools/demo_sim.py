"""Keep the demo alive: the "referee" starts each called match, every few seconds it gets a shot, and
finished matches get their result.

It goes through the real paths (live reports, record_result, notices), so the demo shows racks emptying,
tables moving on, the bracket filling and "tocca a voi" notifications. When the final is over it waits a
few minutes and starts a new tournament. Only for the demo database: it refuses any other.

Usage: BP_DB=data/demo.db python -m tools.demo_sim [--tick 2.5] [--restart-after 300]
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import time
from datetime import datetime

from app import db, push, service, tracking
from tools.populate_demo import setup_demo

DEVICE = "demo-sim"
HIT_RATE = 0.38

log = logging.getLogger("beerpong.demo")


def _finish(conn: sqlite3.Connection, m: service.MatchView, st: object, rng: random.Random) -> None:
    s1, s2 = st.side_hits(1), st.side_hits(2)
    winner = rng.choice([1, 2]) if m.stage == "ko" and s1 == s2 else None  # overtime, decided by a coin
    service.record_result(conn, m.id, s1, s2, winner)
    tracking.apply_live_splits(conn, m.id)


def tick(conn: sqlite3.Connection, rng: random.Random, now: float) -> int:
    """One step for every match on a table. Returns how many results were recorded."""
    finished = 0
    now_ms, _ = service.live(service.matches(conn))
    for m in now_ms:
        if m.done or m.t1 is None or m.t2 is None:
            continue
        if m.started_at is None:  # the couples were called: the referee starts the match
            service.start_match(conn, m.id, now)
            continue
        st = tracking.state(conn, m.id)
        out_of_time = m.duration is not None and now - st.started_at >= m.duration * 60
        if st.winner_side or out_of_time:
            _finish(conn, m, st, rng)
            finished += 1
            continue
        # Two-shot turns, the two players of a side taking one shot each.
        side = 1 if len(st.shots) % 4 < 2 else 2
        player = 1 + len(st.shots) % 2
        standing = sorted(st.racks[3 - side].cups)
        cup = rng.choice(standing) if standing and rng.random() < HIT_RATE else 0
        tracking.add_report(conn, m.id, DEVICE, "shot", side, player, cup, now)
    if finished:
        push.send(push.due(conn))
    return finished


def run(tick_s: float, restart_after_s: float) -> None:
    conn = db.connect()
    rng = random.Random()
    setup_demo(conn)  # a fresh tournament, halfway through the groups, timed from now
    finished_at: float | None = None
    while True:
        now = time.time()
        if db.get_setting(conn, "phase") == "finished":
            finished_at = finished_at or now
            if now - finished_at >= restart_after_s:
                log.warning("demo: new tournament")
                setup_demo(conn)
                finished_at = None
        else:
            tick(conn, rng, now)
        time.sleep(tick_s)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tick", type=float, default=2.5, help="seconds between shots on each table")
    parser.add_argument("--restart-after", type=float, default=300, help="seconds the final stays on screen")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    db.init_db()
    conn = db.connect()
    if db.db_path().resolve() == db.DEFAULT_DB.resolve() or db.get_setting(conn, "demo") != "1":
        raise SystemExit("Only for the demo database: BP_DB=data/demo.db, filled by tools.populate_demo")
    conn.close()
    log.warning("demo simulator running at %s, started %s", db.db_path(), datetime.now().strftime("%H:%M"))
    run(args.tick, args.restart_after)


if __name__ == "__main__":
    main()
