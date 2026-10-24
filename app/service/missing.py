"""Missing couples: guests to pair, placeholders to complete, reconciling the official list."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import datetime

from ..db import get_setting
from .core import TournamentError
from .teams import _require_setup, _same_phone, add_team, team, teams


def is_unpaired_guest(db: sqlite3.Connection, phone: str) -> bool:
    return any(_same_phone(g["phone"], phone) for g in unassigned_guests(db))


def link_guest(db: sqlite3.Connection, guest_id: int, team_id: int) -> None:
    """Attach a party-only guest to a couple: their name fills an empty player slot, their phone an empty phone."""
    _require_setup(db)
    g = db.execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone()
    t = team(db, team_id)
    if g is None or t is None:
        raise TournamentError("Iscritto o coppia non trovati.")
    if g["team_id"] is not None:
        raise TournamentError("Questo iscritto è già in una coppia.")
    p1, p2, phone1, phone2 = t["p1"], t["p2"], t["phone1"], t["phone2"]
    if g["name"] and g["name"].casefold() not in (p1.casefold(), p2.casefold()):
        if not p1:
            p1 = g["name"]
        elif not p2:
            p2 = g["name"]
    if not any(_same_phone(g["phone"], ph) for ph in (phone1, phone2) if ph):
        if not phone1:
            phone1 = g["phone"]
        elif not phone2:
            phone2 = g["phone"]
    with db:
        db.execute(
            "UPDATE teams SET p1 = ?, p2 = ?, phone1 = ?, phone2 = ? WHERE id = ?", (p1, p2, phone1, phone2, team_id)
        )
        db.execute("UPDATE guests SET team_id = ? WHERE id = ?", (team_id, guest_id))


def unassigned_guests(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM guests WHERE team_id IS NULL ORDER BY registered_at IS NULL, registered_at, id"
    ).fetchall()


@dataclass
class GuestView:
    guest: sqlite3.Row
    hints: list[str]


PAIRING_WINDOW_MIN = 15


def guest_views(db: sqlite3.Connection) -> list[GuestView]:
    """Unpaired party-only guests, with hints inferred from the form data."""
    guests = unassigned_guests(db)
    incomplete = [t for t in teams(db).values() if not (t["p1"] and t["p2"])]

    def label(g: sqlite3.Row) -> str:
        return g["name"] or g["phone"]

    views = []
    for g in guests:
        hints = []
        if g["registered_at"]:
            when = datetime.fromisoformat(g["registered_at"])
            for other in guests:
                if other["id"] == g["id"] or not other["registered_at"]:
                    continue
                gap = (datetime.fromisoformat(other["registered_at"]) - when).total_seconds() / 60
                if abs(gap) <= PAIRING_WINDOW_MIN:
                    hints.append(f"iscritto a {abs(round(gap))} min da {label(other)}: forse sono in coppia")
        if g["name"]:
            needle = _fold(g["name"])
            hints += [
                f"il nome compare in «{t['name']}», che è incompleta"
                for t in incomplete
                if needle and needle in _fold(f"{t['name']} {t['p1']} {t['p2']}")
            ]
        views.append(GuestView(g, hints))
    return views


def _fold(text: str) -> str:
    """Compare names ignoring case, accents, apostrophes and spacing ("L’orso" == "lorso")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if c.isalnum())


def same_team_name(a: str, b: str) -> bool:
    fa, fb = _fold(a), _fold(b)
    return fa == fb or SequenceMatcher(None, fa, fb).ratio() >= 0.85


@dataclass
class Reconciliation:
    matched: list[tuple[str, sqlite3.Row]]
    new: list[str]
    extra: list[sqlite3.Row]


def reconcile(db: sqlite3.Connection, official: str) -> Reconciliation:
    """Compare the official list of team names with the registered couples."""
    names: list[str] = []
    for raw in official.replace("\n", ",").split(","):
        name = " ".join(raw.split())
        if name and not any(_fold(name) == _fold(n) for n in names):
            names.append(name)
    remaining = list(teams(db).values())
    matched, new = [], []
    for name in names:
        hit = next((t for t in remaining if _fold(t["name"]) == _fold(name)), None) or next(
            (t for t in remaining if same_team_name(t["name"], name)), None
        )
        if hit is None:
            new.append(name)
        else:
            matched.append((name, hit))
            remaining.remove(hit)
    return Reconciliation(matched, new, remaining)


PLAYERS_IN_NAME = re.compile(r"^\s*(\S+)\s+(?:e|&|and|\+)\s+(\S+)\s*$", re.IGNORECASE)


def players_from_team_name(name: str) -> tuple[str, str]:
    """"ada e bice" -> ("Ada", "Bice"): team names that are just the two players' names."""
    match = PLAYERS_IN_NAME.match(name)
    if not match:
        return "", ""
    return tuple(w[:1].upper() + w[1:] for w in match.groups())  # type: ignore[return-value]


def apply_reconciliation(
    db: sqlite3.Connection, add: list[str], renames: dict[int, str], flag_extra: dict[int, str] | None = None
) -> tuple[int, int]:
    """Rename couples that appear under another name, then add the remaining names as couples to verify.

    `flag_extra` attaches a note to registered couples missing from the list (team id -> note).
    """
    _require_setup(db)
    renamed = 0
    for team_id, new_name in renames.items():
        new_name = new_name.strip()
        if not new_name:
            continue
        try:
            with db:
                db.execute("UPDATE teams SET name = ? WHERE id = ?", (new_name, team_id))
        except sqlite3.IntegrityError:
            raise TournamentError(f"Esiste già una coppia chiamata «{new_name}».") from None
        renamed += 1
    with db:
        for team_id, note in (flag_extra or {}).items():
            db.execute("UPDATE teams SET note = ? WHERE id = ?", (note, team_id))
    taken = {n.casefold() for n in renames.values()}
    added = 0
    for name in add:
        if name.casefold() in taken:
            continue
        p1, p2 = players_from_team_name(name)
        note = (
            "Dall'elenco ufficiale: giocatori dedotti dal nome, da verificare" if p1
            else "Dall'elenco ufficiale: giocatori e telefono da completare"
        )
        add_team(db, name, p1, p2, note=note)
        added += 1
    return added, renamed


# Filter keys of the admin couples list, with the label shown on its buttons.
CHECKS = {
    "nonpagate": "Non pagate",
    "fuorigruppo": "Fuori dal gruppo",
    "senzatelefono": "Senza telefono",
    "dacompletare": "Da completare",
}


def team_checks(t: sqlite3.Row) -> list[str]:
    """What is still to verify for a couple; organizers' couples have nothing to verify."""
    if t["organizer"]:
        return []
    failing = {
        "nonpagate": not t["paid"],
        "fuorigruppo": not t["in_chat"],
        "senzatelefono": not t["phone1"],
        "dacompletare": not (t["p1"] and t["p2"]) or bool(t["note"]),
    }
    return [key for key, bad in failing.items() if bad]


@dataclass
class Checklist:
    rows: list[sqlite3.Row]
    checks: dict[int, list[str]]

    @property
    def players(self) -> list[sqlite3.Row]:
        return [t for t in self.rows if not t["organizer"]]

    @property
    def organizers(self) -> list[sqlite3.Row]:
        return [t for t in self.rows if t["organizer"]]

    def failing(self, key: str) -> list[sqlite3.Row]:
        return [t for t in self.rows if key in self.checks[t["id"]]]

    @property
    def done(self) -> list[sqlite3.Row]:
        return [t for t in self.players if not self.checks[t["id"]]]


def checklist(db: sqlite3.Connection) -> Checklist:
    rows = sorted(teams(db).values(), key=lambda t: t["name"].casefold())
    return Checklist(rows, {t["id"]: team_checks(t) for t in rows})


@dataclass
class MissingOverview:
    target: int
    complete: int
    incomplete: list[sqlite3.Row]
    guests: list[GuestView]
    unpaid: list[sqlite3.Row]
    not_in_chat: list[sqlite3.Row]


def missing_overview(db: sqlite3.Connection) -> MissingOverview:
    rows = list(teams(db).values())
    return MissingOverview(
        target=int(get_setting(db, "target_teams")),
        complete=sum(1 for t in rows if t["p1"] and t["p2"]),
        incomplete=[t for t in rows if {"senzatelefono", "dacompletare"} & set(team_checks(t))],
        guests=guest_views(db),
        unpaid=[t for t in rows if "nonpagate" in team_checks(t)],
        not_in_chat=[t for t in rows if "fuorigruppo" in team_checks(t)],
    )
