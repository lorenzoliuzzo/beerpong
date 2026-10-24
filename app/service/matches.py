"""Reading matches: the views every page builds on, what is live, the table schedules, group tables."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .. import bracket, planning
from ..db import get_setting
from ..states import Scoring
from ..standings import Result, Row, compute_table
from .core import GroupTable, MatchView, group_name, load_config
from .teams import display_name, teams


def _group_rows(db: sqlite3.Connection) -> dict[int, list[Row]]:
    members: dict[int, list[int]] = defaultdict(list)
    for r in db.execute("SELECT id, grp FROM teams WHERE grp IS NOT NULL"):
        members[r["grp"]].append(r["id"])
    results = [
        Result(r["team1"], r["team2"], r["score1"], r["score2"])
        for r in db.execute("SELECT * FROM matches WHERE stage = 'group' AND done = 1")
    ]
    return {g: compute_table(ids, results) for g, ids in sorted(members.items())}


def matches(db: sqlite3.Connection) -> list[MatchView]:
    team_rows = teams(db)
    rows = db.execute("SELECT * FROM matches ORDER BY slot IS NULL, slot, field, id").fetchall()
    total_rounds = max((r["round"] for r in rows if r["stage"] == "ko"), default=0)

    def name(r: sqlite3.Row, side: int) -> str:
        team = r[f"team{side}"]
        if team is not None:
            return display_name(team_rows[team])
        if r["bye"]:
            return "Riposo"
        if r["stage"] == "ko" and r["round"] > 1:
            return bracket.winner_of(r["round"] - 1, 2 * r["pos"] + side - 1, total_rounds)
        return "Da definire"

    def split(a: int | None, b: int | None) -> tuple[int, int] | None:
        return (a, b) if a is not None and b is not None else None

    from .. import tracking  # tracking imports this module

    default_mode = Scoring.of(get_setting(db, "scoring"))
    has_reports = {r[0] for r in db.execute("SELECT DISTINCT match_id FROM reports").fetchall()}

    views = []
    for r in rows:
        label = group_name(r["grp"]) if r["stage"] == "group" else bracket.round_name(r["round"], total_rounds)
        views.append(
            MatchView(
                id=r["id"],
                stage=r["stage"],
                label=label,
                slot=r["slot"],
                field=r["field"],
                time=r["start"][11:16] if r["start"] else "",
                duration=r["duration"],
                t1=r["team1"],
                t2=r["team2"],
                n1=name(r, 1),
                n2=name(r, 2),
                s1=r["score1"],
                s2=r["score2"],
                split1=split(r["c1a"], r["c1b"]),
                split2=split(r["c2a"], r["c2b"]),
                winner=r["winner"],
                done=bool(r["done"]),
                bye=bool(r["bye"]),
                grp=r["grp"],
                round=r["round"],
                pos=r["pos"],
                started_at=r["started_at"],
                start_iso=r["start"] or "",
                live_score=tracking.live_score(db, r["id"]) if r["id"] in has_reports and not r["done"] else None,
                mode=Scoring.of(r["mode"] or default_mode),
            )
        )
    for m in live(views)[0]:
        m.playing = True
    return views


def match(db: sqlite3.Connection, match_id: int) -> MatchView | None:
    return next((m for m in matches(db) if m.id == match_id), None)


def by_slot(views: list[MatchView]) -> list[tuple[int, str, list[MatchView]]]:
    slots: dict[int, list[MatchView]] = defaultdict(list)
    for m in views:
        if m.slot is not None:
            slots[m.slot].append(m)
    return [(s, ms[0].time, ms) for s, ms in sorted(slots.items())]


def tables_independent(pending: list[tuple[int | None, int | None, int | None]]) -> bool:
    """(field, team1, team2) of unplayed matches: True when no team still has to play on two tables,
    so each table can move through its own queue without waiting for the others."""
    fields_of: dict[int, set[int]] = defaultdict(set)
    for f, *pair in pending:
        if None in pair:
            return False  # a knockout match still waiting for winners of matches on other tables
        for t in pair:
            if f is not None:
                fields_of[t].add(f)
    return all(len(fs) == 1 for fs in fields_of.values())


def live(views: list[MatchView]) -> tuple[list[MatchView], list[MatchView]]:
    """Matches on the tables now and next up: per table when tables run their own queues,
    otherwise the earliest unfinished slot (the groups share the tables) and the slot after it."""
    by_field: dict[int, list[MatchView]] = defaultdict(list)
    unassigned: list[MatchView] = []
    for m in views:
        if m.done or m.bye:
            continue
        if m.field is not None:
            by_field[m.field].append(m)
        if m.slot is not None:
            unassigned.append(m)
    if by_field and tables_independent([(m.field, m.t1, m.t2) for ms in by_field.values() for m in ms]):
        now: list[MatchView] = []
        upcoming: list[MatchView] = []
        for f in sorted(by_field):
            q = [m for m in by_field[f] if m.t1 is not None and m.t2 is not None] or by_field[f]
            if q:
                now.append(q[0])
            if len(q) > 1:
                upcoming.append(q[1])
        return now, upcoming
    pending = by_slot(unassigned)
    now = pending[0][2] if pending else []
    upcoming = pending[1][2] if len(pending) > 1 else []
    return now, upcoming


@dataclass
class TableScheduleView:
    field: int
    name: str
    groups: list[GroupTable]
    group_letters: str
    current: MatchView | None
    next_match: MatchView | None
    upcoming: list[MatchView]
    played: list[MatchView]
    done_count: int
    total_count: int
    kind: str = "Principale"
    is_main: bool = True
    phase_label: str = "Fase a Gironi"
    phase_matches: list[MatchView] = field(default_factory=list)


def table_views(
    db: sqlite3.Connection,
    g_tables: list[GroupTable] | None = None,
    all_matches: list[MatchView] | None = None,
) -> list[TableScheduleView]:
    cfg = load_config(db)
    if g_tables is None:
        g_tables = group_tables(db)
    if all_matches is None:
        all_matches = matches(db)
    ko_ms = [m for m in all_matches if m.stage == "ko" and not m.bye]
    if cfg.phase.knockout_reached and ko_ms:
        active_round = min((m.round for m in ko_ms if not m.done), default=max(m.round for m in ko_ms))
        phase_ms = [m for m in ko_ms if m.round == active_round]
        phase_label = phase_ms[0].label if phase_ms else "Eliminazione diretta"
        fields = sorted({m.field for m in phase_ms if m.field}) or list(range(1, cfg.fields + 1))
    else:
        phase_ms = [m for m in all_matches if m.stage == "group" and not m.bye]
        phase_label = "Fase a Gironi"
        fields = sorted({m.field for m in phase_ms if m.field}) or list(range(1, cfg.fields + 1))

    all_group_ms = [m for m in all_matches if m.stage == "group" and not m.bye]
    total_fields = len(sorted({m.field for m in all_group_ms if m.field}) or fields)
    pending_all = [m for m in phase_ms if not m.done and not m.bye]
    independent = tables_independent([(m.field, m.t1, m.t2) for m in pending_all])
    now, upcoming = live(all_matches)
    out: list[TableScheduleView] = []
    for f in fields:
        f_matches = [m for m in phase_ms if m.field == f and not m.bye]
        f_grps_from_matches = {m.grp for m in all_group_ms if m.field == f and m.grp is not None}
        if f_grps_from_matches:
            t_groups = [g for g in g_tables if g.grp in f_grps_from_matches]
        else:
            t_groups = [
                g for g in g_tables if planning.table_for_group(g.grp, len(g_tables), total_fields) == f
            ]
        letters = " · ".join(g.name.replace("Girone ", "") for g in t_groups)
        pending = [m for m in f_matches if not m.done]
        if independent:
            current = pending[0] if pending else None
            next_match = pending[1] if len(pending) > 1 else None
        else:
            # Groups share the tables: a table only moves on with the slot, as live() does, so a couple is
            # never shown on two tables at once.
            current = next((m for m in now if m.field == f), None)
            next_match = next((m for m in upcoming if m.field == f), None)
        later = [m for m in pending if m is not current and m is not next_match]
        played = [m for m in f_matches if m.done]
        is_main = f <= 2
        out.append(
            TableScheduleView(
                field=f,
                name=f"Tavolo {f}",
                groups=t_groups,
                group_letters=letters,
                current=current,
                next_match=next_match,
                upcoming=later,
                played=played,
                done_count=len(played),
                total_count=len(f_matches),
                kind="Principale" if is_main else "Secondario",
                is_main=is_main,
                phase_label=phase_label,
                phase_matches=f_matches,
            )
        )
    return out


def group_tables(db: sqlite3.Connection) -> list[GroupTable]:
    views = [m for m in matches(db) if m.stage == "group"]
    out = []
    for g, rows in _group_rows(db).items():
        ms = [m for m in views if m.grp == g]
        order = [r.team for r in rows]
        index = {t: i for i, t in enumerate(order)}
        matrix: list[list[str | None]] = [[None] * len(order) for _ in order]
        for m in ms:
            if m.done:
                i, j = index[m.t1], index[m.t2]
                matrix[i][j] = f"{m.s1}-{m.s2}"
                matrix[j][i] = f"{m.s2}-{m.s1}"
        out.append(GroupTable(g, group_name(g), rows, ms, matrix))
    return out
