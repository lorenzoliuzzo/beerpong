"""Standings, scorers, rankings, head-to-heads and the profiles of couples and players."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .. import planning
from ..db import get_phase
from .core import FINAL_CUPS, GroupTable, MatchView, group_name, load_config
from .teams import team, teams
from .matches import _group_rows, group_tables, live, matches


@dataclass
class OverallStandingsRow:
    rank: int
    team_id: int
    team_name: str
    grp: int
    grp_name: str
    played: int
    won: int
    drawn: int
    lost: int
    diff: int
    points: int


def overall_standings(db: sqlite3.Connection) -> list[OverallStandingsRow]:
    g_rows = _group_rows(db)
    all_rows = []
    for g, rows in g_rows.items():
        for r in rows:
            all_rows.append((g, r))
    all_rows.sort(key=lambda item: (-item[1].points, -item[1].diff, -item[1].won))
    t_names = teams(db)
    return [
        OverallStandingsRow(
            rank=i + 1,
            team_id=r.team,
            team_name=t_names[r.team]["name"],
            grp=g,
            grp_name=group_name(g),
            played=r.played,
            won=r.won,
            drawn=r.drawn,
            lost=r.lost,
            diff=r.diff,
            points=r.points,
        )
        for i, (g, r) in enumerate(all_rows)
    ]


def thirds_needed(db: sqlite3.Connection) -> int:
    g = db.execute("SELECT COUNT(DISTINCT grp) FROM teams WHERE grp IS NOT NULL").fetchone()[0]
    if not g:
        return 0
    return planning.bracket_plan(g)[1] - 2 * g


def knockout_rounds(db: sqlite3.Connection) -> list[tuple[str, list[MatchView]]]:
    ko = [m for m in matches(db) if m.stage == "ko"]
    rounds: dict[int, list[MatchView]] = defaultdict(list)
    for m in ko:
        rounds[m.round].append(m)
    return [(ms[0].label, sorted(ms, key=lambda m: m.pos)) for _, ms in sorted(rounds.items())]


@dataclass
class Scorer:
    player: str
    team: str
    team_id: int
    grp: int | None = None
    grp_name: str = ""
    cups: int = 0
    played: int = 0

    @property
    def average(self) -> float:
        return self.cups / self.played if self.played else 0.0


def scorers(db: sqlite3.Connection) -> list[Scorer]:
    """Player ranking: only cups the team has split between its two players count."""
    team_rows = teams(db)
    stats = {
        (t["id"], p): Scorer(
            t[p], t["name"], t["id"], grp=t["grp"], grp_name=group_name(t["grp"]) if t["grp"] is not None else ""
        )
        for t in team_rows.values()
        if t["checked_in"]
        for p in ("p1", "p2")
    }
    for m in matches(db):
        for team, split in ((m.t1, m.split1), (m.t2, m.split2)):
            if split is None:
                continue
            for player, c in zip(("p1", "p2"), split):
                s = stats[(team, player)]
                s.cups += c
                s.played += 1
    return sorted(stats.values(), key=lambda s: (-s.cups, -s.average, s.player.lower()))


@dataclass
class TeamStats:
    team: int
    name: str
    grp: int | None = None
    grp_name: str = ""
    played: int = 0
    won: int = 0
    cups_for: int = 0
    cups_against: int = 0

    @property
    def average(self) -> float:
        return self.cups_for / self.played if self.played else 0.0

    @property
    def diff(self) -> int:
        return self.cups_for - self.cups_against


def team_ranking(db: sqlite3.Connection) -> list[TeamStats]:
    stats = {
        t["id"]: TeamStats(
            t["id"], t["name"], grp=t["grp"], grp_name=group_name(t["grp"]) if t["grp"] is not None else ""
        )
        for t in teams(db).values()
        if t["checked_in"]
    }
    for m in matches(db):
        if not m.done or m.bye:
            continue
        for team, cf, ca in ((m.t1, m.s1, m.s2), (m.t2, m.s2, m.s1)):
            s = stats[team]
            s.played += 1
            s.cups_for += cf
            s.cups_against += ca
            s.won += m.winner == team
    return sorted(stats.values(), key=lambda s: (-s.cups_for, -s.average, s.name.lower()))


@dataclass
class HeadToHead:
    matches: list[MatchView]
    wins_a: int
    wins_b: int
    draws: int
    cups_a: int
    cups_b: int


def head_to_head(db: sqlite3.Connection, a: int, b: int) -> HeadToHead:
    ms = [m for m in matches(db) if {m.t1, m.t2} == {a, b}]
    h = HeadToHead(ms, 0, 0, 0, 0, 0)
    for m in ms:
        if not m.done:
            continue
        sa, sb = (m.s1, m.s2) if m.t1 == a else (m.s2, m.s1)
        h.cups_a += sa
        h.cups_b += sb
        if m.winner == a:
            h.wins_a += 1
        elif m.winner == b:
            h.wins_b += 1
        else:
            h.draws += 1
    return h


@dataclass
class TeamDashboard:
    team: sqlite3.Row
    status: str | None
    next_match: MatchView | None
    matches: list[MatchView]
    table: GroupTable | None
    position: int | None
    eliminated: bool
    champion: bool


def team_dashboard(db: sqlite3.Connection, team_id: int) -> TeamDashboard | None:
    row = team(db, team_id)
    if row is None:
        return None
    views = matches(db)
    mine = [m for m in views if m.side(team_id) and not m.bye]
    now, upcoming = live(views)
    status = (
        "now" if any(m.side(team_id) for m in now) else "next" if any(m.side(team_id) for m in upcoming) else None
    )
    table = next((g for g in group_tables(db) if g.grp == row["grp"]), None) if row["grp"] is not None else None
    position = next((i for i, r in enumerate(table.rows, 1) if r.team == team_id), None) if table else None
    ko = [m for m in views if m.stage == "ko"]
    phase = get_phase(db)
    in_bracket = any(m.side(team_id) for m in ko)
    eliminated = (phase.knockout_reached and not in_bracket) or any(
        m.done and m.side(team_id) and m.winner != team_id for m in ko
    )
    final = max(ko, key=lambda m: m.round, default=None)
    return TeamDashboard(
        team=row,
        status=status,
        next_match=next((m for m in mine if not m.done), None),
        matches=mine,
        table=table,
        position=position,
        eliminated=eliminated,
        champion=bool(final and final.done and final.winner == team_id),
    )


@dataclass
class PlayerProfile:
    index: int
    name: str
    scorer_rank: int | None
    total_scorers: int
    official_cups: int
    official_played: int
    official_avg: float
    share_pct: int
    fav_cup: int | None
    fav_cup_hits: int
    heatmap: dict[int, int] = field(default_factory=dict)  # cup -> hits by this player
    # From the referee's shots, in matches scored shot by shot (all zero otherwise):
    live_shots: int = 0
    live_hits: int = 0
    accuracy_rank: int | None = None
    finishers: int = 0

    @property
    def live_accuracy(self) -> float:
        return 100.0 * self.live_hits / self.live_shots if self.live_shots else 0.0


LITERS_PER_TEAM_MATCH = 1.0  # 1 L of beer in each couple's rack per match


def beer_stats(
    db: sqlite3.Connection, all_matches: list[MatchView] | None = None
) -> tuple[float, int, dict[int, tuple[float, int]]]:
    """(tournament_liters_live, total_cups_live, {team_id: (liters_drunk, cups_drunk)}) across done + live matches."""
    if all_matches is None:
        all_matches = matches(db)
    total_rounds = max((m.round for m in all_matches if m.stage == "ko" and m.round), default=0)
    by_team: dict[int, list[float | int]] = {}
    total_liters = 0.0
    total_cups = 0
    for m in all_matches:
        if m.bye or m.t1 is None or m.t2 is None:
            continue
        rack_cups = FINAL_CUPS if (m.stage == "ko" and m.round == total_rounds) else 6
        if m.done:
            s1, s2 = m.s1 or 0, m.s2 or 0
        elif m.live_score:
            s1, s2 = m.live_score
        else:
            continue
        # Team 1 drinks the cups hit by Team 2 (s2), Team 2 drinks the cups hit by Team 1 (s1)
        for tid, drunk_cups in ((m.t1, s2), (m.t2, s1)):
            liters = drunk_cups * (LITERS_PER_TEAM_MATCH / rack_cups)
            entry = by_team.setdefault(tid, [0.0, 0])
            entry[0] += liters
            entry[1] += drunk_cups
            total_liters += liters
            total_cups += drunk_cups
    return round(total_liters, 2), total_cups, {tid: (round(float(v[0]), 2), int(v[1])) for tid, v in by_team.items()}


@dataclass
class TeamProfile:
    field: int | None
    grp_name: str
    grp_rank: int | None
    grp_total: int
    grp_points: int
    grp_diff: int
    overall_group_rank: int | None
    team_cups_rank: int | None
    total_teams: int
    played: int
    won: int
    drawn: int
    lost: int
    win_rate: int
    cups_for: int
    cups_against: int
    cups_diff: int
    cups_avg: float
    team_heatmap: dict[int, int]
    players: list[PlayerProfile]
    team_shots: int = 0  # shot by shot only, like the fields below
    team_hits: int = 0
    team_accuracy_rank: int | None = None
    blows: int = 0  # balls the couple blew out of its cups (rule 5)
    liters_drunk: float = 0.0
    cups_drunk_live: int = 0
    tournament_liters: float = 0.0
    total_cups_live: int = 0

    @property
    def team_accuracy(self) -> float:
        return 100.0 * self.team_hits / self.team_shots if self.team_shots else 0.0


def team_profile(db: sqlite3.Connection, team_id: int) -> TeamProfile | None:
    from .. import tracking
    row = team(db, team_id)
    if row is None:
        return None
    cfg = load_config(db)
    g_tables = group_tables(db)
    g_table = next((g for g in g_tables if g.grp == row["grp"]), None) if row["grp"] is not None else None
    grp_row = next((r for r in g_table.rows if r.team == team_id), None) if g_table else None
    grp_rank = next((i for i, r in enumerate(g_table.rows, 1) if r.team == team_id), None) if g_table else None

    ov = overall_standings(db)
    overall_group_rank = next((r.rank for r in ov if r.team_id == team_id), None)

    t_rank_list = team_ranking(db)
    team_cups_rank = next((i for i, s in enumerate(t_rank_list, 1) if s.team == team_id), None)
    t_stat = next((s for s in t_rank_list if s.team == team_id), None)

    all_views = matches(db)
    t_liters, t_cups_live, by_team_beer = beer_stats(db, all_views)
    team_liters, team_cups_drunk = by_team_beer.get(team_id, (0.0, 0))
    all_ms = [m for m in all_views if m.side(team_id) and not m.bye]
    done_ms = [m for m in all_ms if m.done]
    played = len(done_ms)
    won = sum(1 for m in done_ms if m.winner == team_id)
    drawn = sum(1 for m in done_ms if m.winner is None)
    lost = played - won - drawn
    win_rate = round(100 * won / played) if played else 0
    cups_for = t_stat.cups_for if t_stat else sum((m.s1 if m.t1 == team_id else m.s2) or 0 for m in done_ms)
    cups_against = t_stat.cups_against if t_stat else sum((m.s2 if m.t1 == team_id else m.s1) or 0 for m in done_ms)
    cups_diff = cups_for - cups_against
    cups_avg = cups_for / played if played else 0.0

    # Determine assigned table (field)
    group_ms = [m for m in all_ms if m.stage == "group" and m.field is not None]
    if group_ms:
        assigned_field = group_ms[0].field
    elif row["grp"] is not None and g_tables:
        assigned_field = planning.table_for_group(row["grp"], len(g_tables), cfg.fields)
    else:
        assigned_field = None

    # Scorer rankings
    sc_list = scorers(db)
    sc_by_key = {(s.team_id, s.player): (idx, s) for idx, s in enumerate(sc_list, 1)}

    # Where the couple hits: the cups each player marked as theirs after the match (cup_hits).
    team_heat: dict[int, int] = {c: 0 for c in range(1, 11)}
    player_heat: dict[int, dict[int, int]] = {1: {c: 0 for c in range(1, 11)}, 2: {c: 0 for c in range(1, 11)}}
    for h in db.execute(
        "SELECT h.cup, h.player FROM cup_hits h JOIN matches m ON m.id = h.match_id"
        " WHERE (h.side = 1 AND m.team1 = ?) OR (h.side = 2 AND m.team2 = ?)",
        (team_id, team_id),
    ):
        team_heat[h["cup"]] += 1
        if h["player"] in (1, 2):
            player_heat[h["player"]][h["cup"]] += 1

    # Shots, where a referee scored them one by one.
    p_shots, p_hits, p_finishers = {1: 0, 2: 0}, {1: 0, 2: 0}, {1: 0, 2: 0}
    for m in all_ms:
        side = m.side(team_id)
        st = tracking.state(db, m.id)
        last_hit = next((s for s in reversed(st.shots) if s.removed is not None), None) if st.winner_side else None
        for s in st.shots:
            if s.side != side or s.player not in (1, 2):
                continue
            p_shots[s.player] += 1
            if s.removed is not None:
                p_hits[s.player] += 1
                p_finishers[s.player] += s is last_hit
    shot_st = tracking.shot_stats(db) if p_shots[1] + p_shots[2] else None
    acc_rank = {}
    team_acc_rank = None
    if shot_st:
        ranked = sorted((p for p in shot_st.players if p.shots), key=lambda p: (-p.accuracy, -p.hits, p.player.lower()))
        acc_rank = {(p.team_id, p.player): i for i, p in enumerate(ranked, 1)}
        totals = tracking._team_side_stats(db)
        by_acc = sorted(((tid, h / sh, h) for tid, (h, sh) in totals.items() if sh), key=lambda x: (-x[1], -x[2]))
        team_acc_rank = next((i for i, x in enumerate(by_acc, 1) if x[0] == team_id), None)

    p1_sc = sc_by_key.get((team_id, row["p1"]))
    p2_sc = sc_by_key.get((team_id, row["p2"]))
    c1 = p1_sc[1].cups if p1_sc else 0
    c2 = p2_sc[1].cups if p2_sc else 0
    split_total = c1 + c2

    players_out: list[PlayerProfile] = []
    for idx, sc_entry in ((1, p1_sc), (2, p2_sc)):
        pname = row[f"p{idx}"] or f"Giocatore {idx}"
        ocups = sc_entry[1].cups if sc_entry else 0
        oplayed = sc_entry[1].played if sc_entry else 0
        oavg = sc_entry[1].average if sc_entry else 0.0
        srank = sc_entry[0] if sc_entry else None
        share = round(100 * ocups / split_total) if split_total else 50
        p_heat = player_heat[idx]
        best_cup = max(p_heat.items(), key=lambda kv: (kv[1], -kv[0])) if any(p_heat.values()) else (None, 0)
        players_out.append(
            PlayerProfile(
                index=idx,
                name=pname,
                scorer_rank=srank,
                total_scorers=len(sc_list),
                official_cups=ocups,
                official_played=oplayed,
                official_avg=oavg,
                share_pct=share,
                fav_cup=best_cup[0],
                fav_cup_hits=best_cup[1],
                heatmap=p_heat,
                live_shots=p_shots[idx],
                live_hits=p_hits[idx],
                accuracy_rank=acc_rank.get((team_id, pname)),
                finishers=p_finishers[idx],
            )
        )

    return TeamProfile(
        field=assigned_field,
        grp_name=g_table.name if g_table else "",
        grp_rank=grp_rank,
        grp_total=len(g_table.rows) if g_table else 0,
        grp_points=grp_row.points if grp_row else 0,
        grp_diff=grp_row.diff if grp_row else 0,
        overall_group_rank=overall_group_rank,
        team_cups_rank=team_cups_rank,
        total_teams=len(t_rank_list),
        played=played,
        won=won,
        drawn=drawn,
        lost=lost,
        win_rate=win_rate,
        cups_for=cups_for,
        cups_against=cups_against,
        cups_diff=cups_diff,
        cups_avg=cups_avg,
        team_heatmap=team_heat,
        players=players_out,
        team_shots=p_shots[1] + p_shots[2],
        team_hits=p_hits[1] + p_hits[2],
        team_accuracy_rank=team_acc_rank,
        blows=db.execute(
            "SELECT COUNT(*) FROM blows b JOIN matches m ON m.id = b.match_id"
            " WHERE (b.side = 1 AND m.team1 = ?) OR (b.side = 2 AND m.team2 = ?)",
            (team_id, team_id),
        ).fetchone()[0],
        liters_drunk=team_liters,
        cups_drunk_live=team_cups_drunk,
        tournament_liters=t_liters,
        total_cups_live=t_cups_live,
    )


@dataclass
class CheckinOverview:
    present: list[sqlite3.Row]
    missing: list[sqlite3.Row]
    to_verify: list[sqlite3.Row]
    partial: list[sqlite3.Row]
    guests: int
    guest_teams: set[int]


def checkin_overview(db: sqlite3.Connection) -> CheckinOverview:
    rows = list(teams(db).values())
    guest_rows = db.execute("SELECT team_id FROM guests").fetchall()
    return CheckinOverview(
        present=[t for t in rows if t["checked_in"]],
        missing=[t for t in rows if not t["checked_in"]],
        to_verify=[t for t in rows if not t["paid"]],  # tickets are paid by bank transfer before the night
        partial=[t for t in rows if not (t["p1"] and t["p2"])],
        guests=len(guest_rows),
        guest_teams={g["team_id"] for g in guest_rows if g["team_id"] is not None},
    )
