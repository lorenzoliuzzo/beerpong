"""The public board (poster / TV) and everything planned before it happens."""
from __future__ import annotations

import secrets
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import bracket, planning
from ..db import get_phase, get_setting, set_setting
from .core import GroupTable, MIN_TEAMS, MatchView, load_config
from .teams import _require_setup, team, teams
from .matches import group_tables
from .flow import _fmt, _group_schedule, _knockout_slots, _ko_timetable, _organizers, _referee_teams, draw_groups, planned_start
from .stats import knockout_rounds
from .progress import projected_first_round, standing_labels
from .setup import registration


def referee_table(db: sqlite3.Connection, team_id: int) -> int | None:
    """The table a referee couple plays at, and so referees: its group's table (the provisional one before the draw)."""
    t = team(db, team_id)
    if t is None or t["referee_player"] is None:
        return None
    if t["grp"] is not None:
        row = db.execute("SELECT field FROM matches WHERE stage = 'group' AND grp = ? AND field IS NOT NULL LIMIT 1",
                         (t["grp"],)).fetchone()
        if row:
            return row["field"]
    spot = provisional_spot(db, team_id, datetime.now())
    return spot.table if spot else None


@dataclass
class PlannedKo:
    label: str
    field: int
    time: str
    start_iso: str
    t1: int | None
    t2: int | None
    n1: str  # a team if the groups ended now, else where it comes from: "Vincente ottavo 3"
    n2: str


def planned_knockout(db: sqlite3.Connection) -> list[PlannedKo]:
    """During the groups: the knockout timetable, table by table, with the first round as if the groups ended now."""
    pairs = projected_first_round(db)
    # Forecast: right after the last group match still to play, as its table's queue now expects it.
    last = db.execute("SELECT MAX(start) AS start FROM matches WHERE stage = 'group' AND done = 0").fetchone()
    if not pairs or not last["start"]:
        return []
    cfg = load_config(db)
    names = {t["id"]: t["name"] for t in teams(db).values()}
    labels = standing_labels(db)
    total_rounds = (len(pairs) * 2).bit_length() - 1
    byes = {p: a or b for p, (a, b) in enumerate(pairs) if a is None or b is None}
    playable = {1: [p for p in range(len(pairs)) if p not in byes]}
    playable |= {r: list(range(len(pairs) >> (r - 1))) for r in range(2, total_rounds + 1)}
    start = datetime.fromisoformat(last["start"]) + timedelta(minutes=cfg.group_min + cfg.buffer_min)

    def side(r: int, p: int, s: int) -> tuple[int | None, str]:
        if r == 1:
            team = pairs[p][s - 1]
            return team, f"{names[team]} ({labels[team]})" if team else "Riposo"
        src = 2 * p + s - 1
        if r == 2 and src in byes:  # a bye goes straight through
            return byes[src], f"{names[byes[src]]} ({labels[byes[src]]})"
        return None, bracket.winner_of(r - 1, src, total_rounds)

    out = []
    for r, p, _, field_no, at in _ko_timetable(playable, cfg, start):
        (t1, n1), (t2, n2) = side(r, p, 1), side(r, p, 2)
        out.append(PlannedKo(bracket.round_name(r, total_rounds), field_no, at.strftime("%H:%M"), _fmt(at), t1, t2, n1, n2))
    return out


@dataclass
class BoardGroup:
    letter: str
    teams: list[sqlite3.Row]
    table: GroupTable | None  # None while the groups are only provisional


@dataclass
class BoardRound:
    name: str
    matches: list[MatchView | tuple[str, str]]  # real matches, or placeholder labels


def reshuffle_board(db: sqlite3.Connection) -> None:
    _require_setup(db)
    with db:
        set_setting(db, "board_seed", str(secrets.randbits(31)))


@dataclass
class PlannedMatch:
    field: int
    group: str
    team1: sqlite3.Row
    team2: sqlite3.Row


@dataclass
class PlannedSchedule:
    start: str
    slots: list[tuple[str, list[PlannedMatch], str]]  # (HH:MM, matches, ISO start for the countdown)
    groups_end: str
    final: str


def planned_schedule(db: sqlite3.Connection, now: datetime) -> PlannedSchedule | None:
    """Before the draw: the timetable the board's provisional groups would get if the tournament started on time."""
    groups, provisional = board_groups(db)
    if not provisional or not groups:
        return None
    cfg = load_config(db)
    base = planned_start(cfg, now)
    step = timedelta(minutes=cfg.group_min + cfg.buffer_min)
    by_slot: dict[int, list[PlannedMatch]] = defaultdict(list)
    rows = {t["id"]: t for g in groups for t in g.teams}
    for si, field_no, f in _group_schedule([[t["id"] for t in g.teams] for g in groups], cfg.fields, _referee_teams(db)):
        by_slot[si].append(PlannedMatch(field_no, groups[f.grp].letter, rows[f.team1], rows[f.team2]))
    groups_end = base + len(by_slot) * step
    ko_end = groups_end + _knockout_slots(planning.bracket_plan(len(groups))[0], cfg.fields) * timedelta(
        minutes=cfg.ko_min + cfg.buffer_min
    )
    return PlannedSchedule(
        start=base.strftime("%H:%M"),
        slots=[
            ((base + si * step).strftime("%H:%M"), sorted(ms, key=lambda m: m.field), _fmt(base + si * step))
            for si, ms in sorted(by_slot.items())
        ],
        groups_end=groups_end.strftime("%H:%M"),
        final=ko_end.strftime("%H:%M"),
    )


def board_tables(db: sqlite3.Connection, groups: list[BoardGroup]) -> list[tuple[int, list[BoardGroup]]] | None:
    """Groups by table when every table hosts its own groups (see _group_schedule); None when they share tables.

    After the draw the tables are the ones the group's matches are on, whatever "Campi" says now."""
    if groups and all(g.table for g in groups):
        by_field: dict[int, list[BoardGroup]] = defaultdict(list)
        for g in groups:
            fields_of_group = {m.field for m in g.table.matches if m.field}
            if len(fields_of_group) != 1:
                return None
            by_field[fields_of_group.pop()].append(g)
        return sorted(by_field.items()) if len(by_field) > 1 else None
    fields = load_config(db).fields
    if not groups or fields < 2 or len(groups) % fields:
        return None
    return [
        (f, [g for g in groups if planning.table_for_group(ord(g.letter) - 65, len(groups), fields) == f])
        for f in range(1, fields + 1)
    ]


@dataclass
class ProvisionalSpot:
    group: BoardGroup
    table: int | None
    first_time: str | None
    first_rival: sqlite3.Row | None


def provisional_spot(db: sqlite3.Connection, team_id: int, now: datetime) -> ProvisionalSpot | None:
    """Before the draw: the couple's group, table and first match on the board's provisional draw."""
    groups, provisional = board_groups(db)
    group = next((g for g in groups if any(t["id"] == team_id for t in g.teams)), None) if provisional else None
    if group is None:
        return None
    table = next((f for f, gs in board_tables(db, groups) or [] if group in gs), None)
    plan = planned_schedule(db, now)
    first = next(
        ((time, m) for time, ms, _ in (plan.slots if plan else []) for m in ms if team_id in (m.team1["id"], m.team2["id"])),
        None,
    )
    rival = (first[1].team2 if first[1].team1["id"] == team_id else first[1].team1) if first else None
    return ProvisionalSpot(group, table, first[0] if first else None, rival)


def board_groups(db: sqlite3.Connection) -> tuple[list[BoardGroup], bool]:
    """Groups for the board and whether they are provisional (every couple with a place, as if confirmed)."""
    rows = teams(db)
    if get_phase(db).is_setup:
        placed = [t["id"] for t in registration(db).placed]  # the waiting list has no group yet
        if len(placed) < MIN_TEAMS:
            return [], True
        groups = draw_groups(
            placed, int(get_setting(db, "board_seed")), int(get_setting(db, "fields")), _organizers(db)
        )
        return [BoardGroup(chr(65 + i), [rows[t] for t in g], None) for i, g in enumerate(groups)], True
    tables = group_tables(db)
    return [BoardGroup(chr(65 + t.grp), [rows[r.team] for r in t.rows], t) for t in tables], False


def board_bracket(db: sqlite3.Connection, group_count: int) -> list[BoardRound]:
    """The knockout tree: real matches once generated, otherwise its empty shape (with a live preview of round 1)."""
    ko = knockout_rounds(db)
    if ko:
        return [BoardRound(name, [m for m in ms]) for name, ms in ko]
    if not group_count:
        return []
    size, _ = planning.bracket_plan(group_count)
    total = size.bit_length() - 1
    rows = teams(db)
    labels = standing_labels(db)
    projection = projected_first_round(db)
    rounds = []
    for r in range(1, total + 1):
        if r == 1 and projection:
            pairs = [
                tuple(f"{rows[t]['name']} ({labels[t]})" if t else "Passa il turno" for t in pair) for pair in projection
            ]
        else:
            pairs = [("", "")] * (size >> r)
        rounds.append(BoardRound(bracket.round_name(r, total), pairs))
    return rounds
