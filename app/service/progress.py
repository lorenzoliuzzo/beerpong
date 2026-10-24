"""How the tournament is unfolding, and marking payments and chat membership from pasted lists."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .. import bracket, planning
from ..db import get_phase
from ..states import Phase
from .teams import normalize_phone, phone_matches, teams
from .matches import _group_rows, matches
from .stats import knockout_rounds
from .missing import same_team_name


@dataclass
class Step:
    label: str
    done: int
    total: int

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.done == self.total


def tournament_progress(db: sqlite3.Connection) -> list[Step]:
    """Group stage plus every knockout round, including the ones not generated yet."""
    views = matches(db)
    group = [m for m in views if m.stage == "group"]
    if not group:
        return []
    steps = [Step("Gironi", sum(m.done for m in group), len(group))]
    ko = knockout_rounds(db)
    if ko:
        for name, ms in ko:
            playable = [m for m in ms if not m.bye]
            steps.append(Step(name, sum(m.done for m in playable), len(playable)))
        return steps
    size, _ = planning.bracket_plan(len({m.grp for m in group}))
    total_rounds = size.bit_length() - 1
    steps += [Step(bracket.round_name(r, total_rounds), 0, size >> r) for r in range(1, total_rounds + 1)]
    return steps


def standing_labels(db: sqlite3.Connection) -> dict[int, str]:
    """team id → "1° A": where each team finished (or currently sits) in its group."""
    return {
        row.team: f"{place}° {chr(65 + g)}"
        for g, rows in _group_rows(db).items()
        for place, row in enumerate(rows, start=1)
    }


def projected_first_round(db: sqlite3.Connection) -> list[tuple[int | None, int | None]]:
    """While groups are being played: the first knockout round if the groups ended now."""
    if get_phase(db) is not Phase.GROUPS:
        return []
    tables = _group_rows(db)
    size, qualifiers = planning.bracket_plan(len(tables))
    pairs = bracket.first_round(bracket.qualifier_seeds(tables, qualifiers), size)
    return [(a.team if a else None, b.team if b else None) for a, b in pairs]


PAID_MARKS = ("✅", "✔", "☑", "✓")


@dataclass
class PaidListResult:
    marked: list[str]
    already: list[str]
    unmatched: list[str]


def mark_paid_from_list(db: sqlite3.Connection, text: str) -> PaidListResult:
    """Lines like "12. le lontre ✅": checked names become verified payments; unchecked lines are left alone.

    "I Gufi (ada e bice)" also matches a couple registered under the name in brackets.
    """
    result = PaidListResult([], [], [])
    rows = list(teams(db).values())
    for line in text.splitlines():
        if not any(mark in line for mark in PAID_MARKS):
            continue
        name = line
        for mark in PAID_MARKS:
            name = name.replace(mark, "")
        name = re.sub(r"^\s*\d+[.)]?\s*", "", name).strip()
        inner = re.search(r"\(([^)]*)\)", name)
        candidates = [re.sub(r"\([^)]*\)", "", name).strip()] + ([inner.group(1).strip()] if inner else [])
        hit = next((t for c in candidates for t in rows if c and same_team_name(t["name"], c)), None)
        if hit is None:
            result.unmatched.append(name)
        elif hit["paid"]:
            result.already.append(hit["name"])
        else:
            with db:
                db.execute("UPDATE teams SET paid = 1 WHERE id = ?", (hit["id"],))
            result.marked.append(hit["name"])
    return result


@dataclass
class ChatListResult:
    marked: list[str]
    already: list[str]
    unmatched: list[str]


# A run of digits that may be broken by spaces, dots or dashes, as WhatsApp shows "+39 333 123 4567".
PHONE_IN_TEXT = re.compile(r"\+?\d[\d \t.\-/()]{6,}\d")


def mark_chat_from_list(db: sqlite3.Connection, text: str) -> ChatListResult:
    """Phone numbers pasted from the group chat's member list: couples with a matching phone are in the chat."""
    result = ChatListResult([], [], [])
    rows = list(teams(db).values())
    for raw in PHONE_IN_TEXT.findall(text):
        phone = normalize_phone(raw)
        if len(phone.lstrip("+")) < 8:
            continue
        hit = next((t for t in rows if phone_matches(t, phone)), None)
        if hit is None:
            result.unmatched.append(phone)
        elif hit["in_chat"] or hit["name"] in result.marked:
            if hit["name"] not in result.already + result.marked:
                result.already.append(hit["name"])
        else:
            with db:
                db.execute("UPDATE teams SET in_chat = 1 WHERE id = ?", (hit["id"],))
            result.marked.append(hit["name"])
    return result
