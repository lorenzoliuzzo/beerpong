"""Constants, errors and the shapes every other part of the service shares."""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field

from ..db import get_phase, get_setting, random_key, set_setting
from ..states import Phase, Scoring
from ..standings import Row


MAX_TEAM_CUPS = 30
CUPS = 6  # per couple, as in the rules
FINAL_CUPS = 10  # the final is played with 10 cups and no time limit
FINAL_SLOT_MIN = 10
MIN_TEAMS = 4


class TournamentError(Exception):
    pass


# How a match is scored: chosen for the tournament, and changeable for a single match.
SCORING = {s: s.label for s in Scoring}
# Who may referee when a match needs one.
REFEREES = {"volontari": "Anche chi non sta giocando", "organizzatori": "Solo gli organizzatori"}


@dataclass(frozen=True)
class Config:
    title: str
    fields: int
    group_min: int
    ko_min: int
    buffer_min: int
    public_url: str
    phase: Phase
    start_time: str
    scoring: Scoring = Scoring.COPPIE
    referees: str = "volontari"

    @property
    def volunteer_referees(self) -> bool:
        return self.referees == "volontari"


@dataclass
class MatchView:
    id: int
    stage: str
    label: str
    slot: int | None
    field: int | None
    time: str
    duration: int | None
    t1: int | None
    t2: int | None
    n1: str
    n2: str
    s1: int | None
    s2: int | None
    split1: tuple[int, int] | None
    split2: tuple[int, int] | None
    winner: int | None
    done: bool
    bye: bool
    grp: int | None
    round: int
    pos: int
    live_score: tuple[int, int] | None = None  # from the referee's shots, while it is played shot by shot
    mode: Scoring = Scoring.COPPIE  # how it is scored (SCORING): its own choice or the tournament's
    playing: bool = False  # its turn on a table right now (see live()): the couples are called
    started_at: float | None = None  # the referee started it
    start_iso: str = ""  # scheduled start, local time, for the countdowns

    def side(self, team: int | None) -> int | None:
        return 1 if team is not None and team == self.t1 else 2 if team is not None and team == self.t2 else None


@dataclass
class GroupTable:
    grp: int
    name: str
    rows: list[Row]
    matches: list[MatchView]
    matrix: list[list[str | None]] = field(default_factory=list)


def group_name(grp: int) -> str:
    return f"Girone {chr(65 + grp)}"


def load_config(db: sqlite3.Connection) -> Config:
    return Config(
        title=get_setting(db, "title"),
        fields=int(get_setting(db, "fields")),
        group_min=int(get_setting(db, "group_min")),
        ko_min=int(get_setting(db, "ko_min")),
        buffer_min=int(get_setting(db, "buffer_min")),
        public_url=get_setting(db, "public_url"),
        phase=get_phase(db),
        start_time=get_setting(db, "start_time"),
        scoring=Scoring.of(get_setting(db, "scoring")),
        referees=get_setting(db, "referees"),
    )


def is_admin_pin(db: sqlite3.Connection, pin: str) -> bool:
    return secrets.compare_digest(pin, get_setting(db, "admin_pin"))


def checkin_key_ok(db: sqlite3.Connection, key: str) -> bool:
    return secrets.compare_digest(key, get_setting(db, "reg_key"))


def rotate_checkin_key(db: sqlite3.Connection) -> None:
    with db:
        set_setting(db, "reg_key", random_key())
