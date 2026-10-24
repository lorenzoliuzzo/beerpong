"""Running the tournament: the draw, starting and recording matches, the knockout bracket."""
from __future__ import annotations

import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import bracket, planning
from ..db import get_phase, get_setting, set_phase
from ..states import Phase
from .core import CUPS, Config, FINAL_CUPS, FINAL_SLOT_MIN, MAX_TEAM_CUPS, MIN_TEAMS, TournamentError, load_config
from .teams import teams
from .matches import _group_rows, tables_independent


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _next_minute(dt: datetime) -> datetime:
    floor = dt.replace(second=0, microsecond=0)
    return floor if floor == dt else floor + timedelta(minutes=1)


def planned_start(cfg: Config, now: datetime) -> datetime:
    """The scheduled start time today, or the next minute if the tournament starts late."""
    hour, minute = (int(x) for x in cfg.start_time.split(":"))
    planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if planned - now > timedelta(hours=18):  # e.g. 23:30 seen at 00:10: that was last night's start
        planned -= timedelta(days=1)
    return max(_next_minute(now), planned)


def _knockout_slots(bracket_size: int, fields: int) -> int:
    slots, remaining = 0, bracket_size
    while remaining > 2:  # every round before the final
        slots += -(-(remaining // 2) // fields)
        remaining //= 2
    return slots


def draw_groups(
    team_ids: list[int], seed: int, fields: int = 1, organizers: frozenset[int] = frozenset()
) -> list[list[int]]:
    """Same teams + same seed = same groups, so a previewed draw can be started as-is.

    Organizers' couples go to groups spread evenly over the list, so no two share a group and, as long as
    there are enough tables, they sit at different tables (groups are assigned to tables in contiguous blocks).
    """
    rng = random.Random(seed)
    ids = sorted(team_ids)
    rng.shuffle(ids)
    orgs = [t for t in ids if t in organizers]
    rest = [t for t in ids if t not in organizers]
    sizes = planning.group_sizes(len(ids), fields)
    groups: list[list[int]] = [[] for _ in sizes]
    if orgs:
        offset = rng.randrange(len(sizes))
        for i, t in enumerate(orgs):
            target = (offset + i * len(sizes) // len(orgs)) % len(sizes)
            # More organizers than groups can hold at their spread positions: use the next group with room.
            while len(groups[target]) >= sizes[target]:
                target = (target + 1) % len(sizes)
            groups[target].append(t)
    for g, size in zip(groups, sizes):
        while len(g) < size:
            g.append(rest.pop(0))
    return groups


def _referee_teams(db: sqlite3.Connection) -> frozenset[int]:
    return frozenset(r["id"] for r in db.execute("SELECT id FROM teams WHERE referee_player IS NOT NULL"))


def _organizers(db: sqlite3.Connection) -> frozenset[int]:
    return frozenset(r["id"] for r in db.execute("SELECT id FROM teams WHERE organizer = 1"))


@dataclass
class DrawPreview:
    seed: int
    groups: list[list[sqlite3.Row]]
    bracket_size: int
    thirds: int
    byes: int
    matches: int
    group_minutes: int
    knockout_minutes: int


def _group_schedule(
    groups: list[list[int]], fields: int, referees: frozenset[int] = frozenset()
) -> list[tuple[int, int, planning.Fixture]]:
    """(slot, field, fixture). With the same number of groups on every table each table runs its own queue;
    otherwise the groups share all tables slot by slot, so no table sits idle while another has a long queue."""
    if len(groups) % fields == 0:
        queues = planning.build_table_queues(groups, fields, referees)
        return [(si, f, fx) for f, queue in sorted(queues.items()) for si, fx in enumerate(queue)]
    slots = planning.pack_slots(planning.build_fixtures(groups), fields)
    return [(si, fi + 1, fx) for si, slot in enumerate(slots) for fi, fx in enumerate(slot)]


def preview_draw(db: sqlite3.Connection, present_only: bool, seed: int) -> DrawPreview:
    cfg = load_config(db)
    rows = teams(db)
    ids = [t["id"] for t in rows.values() if t["checked_in"] or (not present_only and t["p1"] and t["p2"])]
    if len(ids) < MIN_TEAMS:
        raise TournamentError(f"Servono almeno {MIN_TEAMS} coppie {'presenti' if present_only else 'complete'}.")
    groups = draw_groups(ids, seed, cfg.fields, _organizers(db))
    schedule = _group_schedule(groups, cfg.fields, _referee_teams(db))
    size, qualifiers = planning.bracket_plan(len(groups))
    ko_slots = _knockout_slots(size, cfg.fields)
    return DrawPreview(
        seed=seed,
        groups=[[rows[t] for t in g] for g in groups],
        bracket_size=size,
        thirds=qualifiers - 2 * len(groups),
        byes=size - qualifiers,
        matches=len(schedule),
        group_minutes=(max(si for si, _, _ in schedule) + 1) * (cfg.group_min + cfg.buffer_min),
        knockout_minutes=ko_slots * (cfg.ko_min + cfg.buffer_min),
    )


def start_tournament(db: sqlite3.Connection, now: datetime, seed: int | None = None) -> None:
    cfg = load_config(db)
    if cfg.phase.started:
        raise TournamentError("Il torneo è già iniziato.")
    ids = [r["id"] for r in db.execute("SELECT id FROM teams WHERE checked_in = 1")]
    if len(ids) < MIN_TEAMS:
        raise TournamentError(f"Servono almeno {MIN_TEAMS} coppie presenti.")
    # Default to the board's draw, so the groups people saw on the poster are the ones played.
    groups = draw_groups(
        ids, int(get_setting(db, "board_seed")) if seed is None else seed, cfg.fields, _organizers(db)
    )
    base = planned_start(cfg, now)
    step = timedelta(minutes=cfg.group_min + cfg.buffer_min)

    with db:
        for gi, members in enumerate(groups):
            db.executemany("UPDATE teams SET grp = ? WHERE id = ?", [(gi, t) for t in members])
        for si, field_no, f in _group_schedule(groups, cfg.fields, _referee_teams(db)):
            db.execute(
                "INSERT INTO matches(stage, grp, round, slot, field, start, duration, team1, team2)"
                " VALUES ('group', ?, ?, ?, ?, ?, ?, ?, ?)",
                (f.grp, f.round, si, field_no, _fmt(base + si * step), cfg.group_min, f.team1, f.team2),
            )
        set_phase(db, Phase.GROUPS)


def reset_tournament(db: sqlite3.Connection) -> None:
    with db:
        db.execute("DELETE FROM matches")
        db.execute("DELETE FROM push_sent")
        db.execute("UPDATE teams SET grp = NULL")
        set_phase(db, Phase.SETUP)


def shift_schedule(db: sqlite3.Connection, minutes: int) -> None:
    """Postpone (or bring forward) every match not yet played."""
    rows = db.execute("SELECT id, start FROM matches WHERE done = 0 AND start IS NOT NULL").fetchall()
    with db:
        for r in rows:
            new = datetime.fromisoformat(r["start"]) + timedelta(minutes=minutes)
            db.execute("UPDATE matches SET start = ? WHERE id = ?", (_fmt(new), r["id"]))


def _next_match(db: sqlite3.Connection, m: sqlite3.Row) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM matches WHERE stage = 'ko' AND round = ? AND pos = ?", (m["round"] + 1, m["pos"] // 2)
    ).fetchone()


def _advance(db: sqlite3.Connection, m: sqlite3.Row, winner: int) -> None:
    nxt = _next_match(db, m)
    if nxt is None:
        set_phase(db, Phase.FINISHED)
        return
    column = "team1" if m["pos"] % 2 == 0 else "team2"
    db.execute(f"UPDATE matches SET {column} = ? WHERE id = ?", (winner, nxt["id"]))


def recalc_table_schedule(
    db: sqlite3.Connection, field_no: int, stage: str = "group", now: datetime | None = None
) -> None:
    """Dynamically recalculate start times for remaining matches on a single table using the single server clock."""
    cfg = load_config(db)
    base = _next_minute(now or datetime.now())
    dur = cfg.group_min if stage == "group" else cfg.ko_min
    step = timedelta(minutes=dur + cfg.buffer_min)
    pending = db.execute(
        "SELECT id FROM matches WHERE field = ? AND stage = ? AND done = 0 AND bye = 0"
        " ORDER BY slot IS NULL, slot, id",
        (field_no, stage),
    ).fetchall()
    with db:
        for idx, r in enumerate(pending):
            db.execute("UPDATE matches SET start = ? WHERE id = ?", (_fmt(base + idx * step), r["id"]))


def cups_in_play(db: sqlite3.Connection, match_id: int) -> int:
    m = db.execute("SELECT stage, round FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None or m["stage"] != "ko":
        return CUPS
    last = db.execute("SELECT MAX(round) FROM matches WHERE stage = 'ko'").fetchone()[0]
    return FINAL_CUPS if m["round"] == last else CUPS


def start_match(db: sqlite3.Connection, match_id: int, now: float, first_side: int = 1) -> None:
    """The referee starts the match on the table: its clock runs from now, and so do the table's next times."""
    if first_side not in (1, 2):
        raise TournamentError("Scegli chi tira per primo.")
    m = db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None or m["bye"] or m["team1"] is None or m["team2"] is None:
        raise TournamentError("Partita non disponibile.")
    if m["done"]:
        raise TournamentError("La partita è già conclusa.")
    if m["started_at"] is not None:
        raise TournamentError("La partita è già iniziata.")
    with db:
        db.execute("UPDATE matches SET started_at = ?, first_side = ? WHERE id = ?", (now, first_side, match_id))
    pending = db.execute(
        "SELECT field, team1, team2 FROM matches WHERE stage = ? AND done = 0 AND bye = 0", (m["stage"],)
    ).fetchall()
    if m["field"] is not None and tables_independent([tuple(r) for r in pending]):
        recalc_table_schedule(db, m["field"], m["stage"], datetime.fromtimestamp(now).replace(second=0, microsecond=0))


def undo_start(db: sqlite3.Connection, match_id: int) -> None:
    with db:
        cur = db.execute("UPDATE matches SET started_at = NULL, first_side = NULL WHERE id = ? AND done = 0", (match_id,))
        if cur.rowcount:
            db.execute("DELETE FROM reports WHERE match_id = ? AND device = ?", (match_id, "arbitro"))
            db.execute("DELETE FROM reracks WHERE match_id = ?", (match_id,))


def record_result(db: sqlite3.Connection, match_id: int, s1: int, s2: int, winner_side: int | None = None) -> None:
    m = db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None or m["bye"]:
        raise TournamentError("Partita non trovata.")
    if m["team1"] is None or m["team2"] is None:
        raise TournamentError("Le coppie di questa partita non sono ancora definite.")
    if not (0 <= s1 <= MAX_TEAM_CUPS and 0 <= s2 <= MAX_TEAM_CUPS):
        raise TournamentError(f"Il punteggio deve essere tra 0 e {MAX_TEAM_CUPS}.")

    was_done = bool(m["done"])

    if m["stage"] == "group":
        if get_phase(db) is not Phase.GROUPS:
            raise TournamentError("Gironi chiusi: per correggere, l'admin deve prima riaprire i gironi.")
        winner = m["team1"] if s1 > s2 else m["team2"] if s2 > s1 else None
    else:
        if s1 == s2:
            if winner_side not in (1, 2):
                raise TournamentError("Punteggio in parità: indica chi ha vinto all'overtime.")
            winner = m[f"team{winner_side}"]
        else:
            winner = m["team1"] if s1 > s2 else m["team2"]
        nxt = _next_match(db, m)
        if nxt is not None and (nxt["done"] or nxt["started_at"] is not None):
            raise TournamentError("Il turno successivo è già stato giocato: risultato non più modificabile.")

    with db:
        # A corrected score invalidates the players' split for that side.
        if m["score1"] != s1:
            db.execute("UPDATE matches SET c1a = NULL, c1b = NULL WHERE id = ?", (match_id,))
        if m["score2"] != s2:
            db.execute("UPDATE matches SET c2a = NULL, c2b = NULL WHERE id = ?", (match_id,))
        db.execute(
            "UPDATE matches SET score1 = ?, score2 = ?, winner = ?, done = 1 WHERE id = ?",
            (s1, s2, winner, match_id),
        )
        if m["stage"] == "ko":
            _advance(db, m, winner)

    if not was_done and m["field"] is not None:
        pending = db.execute(
            "SELECT field, team1, team2 FROM matches WHERE stage = ? AND done = 0 AND bye = 0", (m["stage"],)
        ).fetchall()
        # A table may only run ahead when none of its teams is still due on another table.
        if tables_independent([tuple(r) for r in pending]):
            recalc_table_schedule(db, m["field"], m["stage"])

    if m["stage"] == "group":
        pending = db.execute("SELECT COUNT(*) FROM matches WHERE stage = 'group' AND done = 0").fetchone()[0]
        if pending == 0:
            generate_bracket(db)


def save_cup_hits(db: sqlite3.Connection, match_id: int, hits: dict[int, set[int]]) -> None:
    """The cups each couple sank (from the result form): a cup kept keeps the player who claimed it."""
    with db:
        for side in (1, 2):
            cups = sorted(hits[side])
            stale = [r["cup"] for r in db.execute("SELECT cup FROM cup_hits WHERE match_id = ? AND side = ?", (match_id, side))
                     if r["cup"] not in hits[side]]
            db.executemany("DELETE FROM cup_hits WHERE match_id = ? AND side = ? AND cup = ?", [(match_id, side, c) for c in stale])
            db.executemany(
                "INSERT OR IGNORE INTO cup_hits(match_id, side, cup) VALUES (?, ?, ?)", [(match_id, side, c) for c in cups]
            )
            _sync_split(db, match_id, side)


def cup_hits(db: sqlite3.Connection, match_id: int) -> dict[int, dict[int, int | None]]:
    """{side: {cup: player or None}} for one match."""
    hits: dict[int, dict[int, int | None]] = {1: {}, 2: {}}
    for r in db.execute("SELECT side, cup, player FROM cup_hits WHERE match_id = ? ORDER BY cup", (match_id,)):
        hits[r["side"]][r["cup"]] = r["player"]
    return hits


def assign_cup_players(db: sqlite3.Connection, match_id: int, side: int, players: dict[int, int]) -> None:
    """Each player of the couple claims the cups they sank: the split per player follows from it."""
    m = db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None or not m["done"] or m["bye"] or side not in (1, 2):
        raise TournamentError("Puoi dividere i bicchieri solo dopo che è stato inserito il risultato.")
    hit = cup_hits(db, match_id)[side]
    if set(players) != set(hit) or any(p not in (1, 2) for p in players.values()):
        raise TournamentError("Indica chi ha centrato ogni bicchiere.")
    with db:
        db.executemany(
            "UPDATE cup_hits SET player = ? WHERE match_id = ? AND side = ? AND cup = ?",
            [(p, match_id, side, c) for c, p in players.items()],
        )
        _sync_split(db, match_id, side)


def _sync_split(db: sqlite3.Connection, match_id: int, side: int) -> None:
    """The per-player cup count of a side, once every cup it sank has its player (none sunk: 0 + 0)."""
    rows = db.execute("SELECT player FROM cup_hits WHERE match_id = ? AND side = ?", (match_id, side)).fetchall()
    a, b = ("c1a", "c1b") if side == 1 else ("c2a", "c2b")
    if any(r["player"] is None for r in rows):
        split = (None, None)
    else:
        split = (sum(r["player"] == 1 for r in rows), sum(r["player"] == 2 for r in rows))
    db.execute(f"UPDATE matches SET {a} = ?, {b} = ? WHERE id = ?", (*split, match_id))


def generate_bracket(db: sqlite3.Connection) -> None:
    cfg = load_config(db)
    if cfg.phase is not Phase.GROUPS:
        raise TournamentError("Il tabellone si genera alla fine dei gironi.")
    pending = db.execute("SELECT COUNT(*) FROM matches WHERE stage = 'group' AND done = 0").fetchone()[0]
    if pending:
        raise TournamentError(f"Mancano ancora {pending} partite dei gironi.")

    tables = _group_rows(db)
    size, qualifiers = planning.bracket_plan(len(tables))
    first = bracket.first_round(bracket.qualifier_seeds(tables, qualifiers), size)
    total_rounds = size.bit_length() - 1

    last = db.execute("SELECT MAX(slot) AS slot FROM matches WHERE stage = 'group'").fetchone()
    slot = last["slot"] + 1
    # The groups are over now, whatever the timetable said: the knockout starts from the next minute,
    # as a table's queue does when its referee starts or closes a match (recalc_table_schedule).
    t = _next_minute(datetime.now())

    with db:
        for r in range(1, total_rounds + 1):
            for p in range(size >> r):
                t1 = t2 = None
                if r == 1:
                    a, b = first[p]
                    t1, t2 = (a.team if a else None), (b.team if b else None)
                db.execute(
                    "INSERT INTO matches(stage, round, pos, team1, team2) VALUES ('ko', ?, ?, ?, ?)", (r, p, t1, t2)
                )
        for p, (a, b) in enumerate(first):
            if a is None or b is None:
                m = db.execute("SELECT * FROM matches WHERE stage = 'ko' AND round = 1 AND pos = ?", (p,)).fetchone()
                winner = (a or b).team
                db.execute("UPDATE matches SET bye = 1, done = 1, winner = ? WHERE id = ?", (winner, m["id"]))
                _advance(db, m, winner)

        playable: dict[int, list[int]] = defaultdict(list)
        for row in db.execute("SELECT round, pos FROM matches WHERE stage = 'ko' AND bye = 0 ORDER BY round, pos"):
            playable[row["round"]].append(row["pos"])
        for r, p, offset, field_no, at in _ko_timetable(playable, cfg, t):
            db.execute(
                "UPDATE matches SET slot = ?, field = ?, start = ?, duration = ? WHERE stage = 'ko' AND round = ? AND pos = ?",
                (slot + offset, field_no, _fmt(at), None if r == total_rounds else cfg.ko_min, r, p),
            )
        set_phase(db, Phase.KNOCKOUT)


def _ko_timetable(
    playable: dict[int, list[int]], cfg: Config, start: datetime
) -> list[tuple[int, int, int, int, datetime]]:
    """(round, pos, slot offset, table, start) of every knockout match played, round after round on all tables.

    The same for the real bracket and for the plan shown during the groups, so the times announced hold."""
    out, slot, t = [], 0, start
    final = max(playable, default=0)
    for r in sorted(playable):
        positions = playable[r]
        for i in range(0, len(positions), cfg.fields):
            for fi, p in enumerate(positions[i : i + cfg.fields]):
                out.append((r, p, slot, fi + 1, t))
            slot += 1
            t += timedelta(minutes=(FINAL_SLOT_MIN if r == final else cfg.ko_min) + cfg.buffer_min)
    return out


def reopen_groups(db: sqlite3.Connection) -> None:
    if get_phase(db) is not Phase.KNOCKOUT:
        raise TournamentError("I gironi sono già aperti.")
    played = db.execute(
        "SELECT COUNT(*) FROM matches WHERE stage = 'ko' AND (done = 1 OR started_at IS NOT NULL) AND bye = 0"
    ).fetchone()[0]
    if played:
        raise TournamentError("Si è già giocata una partita a eliminazione: non si possono riaprire i gironi.")
    with db:
        db.execute("DELETE FROM matches WHERE stage = 'ko'")
        set_phase(db, Phase.GROUPS)
