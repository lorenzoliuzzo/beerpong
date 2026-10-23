from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    team1: int
    team2: int
    score1: int
    score2: int


@dataclass
class Row:
    team: int
    played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    cups_for: int = 0
    cups_against: int = 0

    @property
    def points(self) -> int:
        return 3 * self.won + self.drawn

    @property
    def diff(self) -> int:
        return self.cups_for - self.cups_against


def _tally(team_ids: list[int], results: list[Result]) -> dict[int, Row]:
    rows = {t: Row(t) for t in team_ids}
    for r in results:
        if r.team1 not in rows or r.team2 not in rows:
            continue
        a, b = rows[r.team1], rows[r.team2]
        a.played += 1
        b.played += 1
        a.cups_for += r.score1
        a.cups_against += r.score2
        b.cups_for += r.score2
        b.cups_against += r.score1
        if r.score1 > r.score2:
            a.won += 1
            b.lost += 1
        elif r.score2 > r.score1:
            b.won += 1
            a.lost += 1
        else:
            a.drawn += 1
            b.drawn += 1
    return rows


def compute_table(team_ids: list[int], results: list[Result]) -> list[Row]:
    """Order, as in the rules: points, then head-to-head among tied teams, then cups scored, then cups conceded."""
    rows = _tally(team_ids, results)
    by_points: dict[int, list[int]] = defaultdict(list)
    for t, row in rows.items():
        by_points[row.points].append(t)
    ordered: list[int] = []
    for pts in sorted(by_points, reverse=True):
        tied = by_points[pts]
        mini = _tally(tied, results)
        tied.sort(key=lambda t: (-mini[t].points, -mini[t].diff, -rows[t].cups_for, rows[t].cups_against, t))
        ordered += tied
    return [rows[t] for t in ordered]
