"""Couples: registration, check-in, payments, questionnaire and how they present themselves."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..db import get_phase
from .core import TournamentError


PAYMENTS = {
    "contanti": "Pagato in contanti",
    "online": "Pagato online (Satispay/bonifico)",
    "cassa": "Paghiamo ora in cassa",
}


def teams(db: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["id"]: r for r in db.execute("SELECT * FROM teams ORDER BY name COLLATE NOCASE")}


def team(db: sqlite3.Connection, team_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()


def _require_setup(db: sqlite3.Connection) -> None:
    if get_phase(db).started:
        raise TournamentError("Il torneo è già iniziato.")


def normalize_phone(raw: str) -> str:
    raw = raw.strip()
    digits = "".join(c for c in raw if c.isdigit())
    return ("+" if raw.startswith("+") else "") + digits if digits else ""


def _clean_names(name: str, p1: str, p2: str, partial: bool = False) -> tuple[str, str, str]:
    """`partial` allows a registered couple whose players are not (all) known yet."""
    name, p1, p2 = name.strip(), p1.strip(), p2.strip()
    if partial and p2 and not p1:
        p1, p2 = p2, ""
    if partial and p1 and not name:
        name = f"Coppia di {p1}"
    if not name or (not partial and not (p1 and p2)):
        raise TournamentError(
            "Serve almeno il nome della coppia o di un giocatore." if partial
            else "Servono nome della coppia e due giocatori."
        )
    if p2 and p1.casefold() == p2.casefold():
        raise TournamentError("I due giocatori devono avere nomi diversi.")
    if max(len(name), len(p1), len(p2)) > 40:
        raise TournamentError("Nomi troppo lunghi (massimo 40 caratteri).")
    return name, p1, p2


def add_team(
    db: sqlite3.Connection, name: str, p1: str, p2: str, phone1: str = "", phone2: str = "", note: str = ""
) -> int:
    _require_setup(db)
    name, p1, p2 = _clean_names(name, p1, p2, partial=True)
    try:
        with db:
            cur = db.execute(
                "INSERT INTO teams(name, p1, p2, phone1, phone2, note) VALUES (?, ?, ?, ?, ?, ?)",
                (name, p1, p2, normalize_phone(phone1), normalize_phone(phone2), note.strip()),
            )
    except sqlite3.IntegrityError:
        raise TournamentError(f"Esiste già una coppia chiamata «{name}».") from None
    return cur.lastrowid


@dataclass
class ImportResult:
    teams: int = 0
    guests: int = 0
    skipped: int = 0


def import_teams(db: sqlite3.Connection, text: str) -> ImportResult:
    """Import couples and party-only guests. Re-importing the same file is safe: known names are skipped."""
    result = ImportResult()
    known = {t["name"].casefold() for t in teams(db).values()}
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.replace("\t", ";").split(";")]
        if n == 1 and parts[0].casefold() in ("coppia", "squadra"):  # a header row, old or new
            continue
        # Columns: squadra; giocatore1; giocatore2; [telefono1]; [telefono2]; [note, ignored]; [iscritto il]
        if not 3 <= len(parts) <= 7:
            raise TournamentError(f"Riga {n}: usa il formato «Coppia; Giocatore 1; Giocatore 2; Telefono».")
        if not parts[0] and not parts[2] and len(parts) >= 4 and parts[3]:
            # No team name: a party-only guest (name in the first player column).
            result.guests += add_guest(db, parts[3], parts[1], parts[6] if len(parts) > 6 else "")
            continue
        if parts[0].casefold() in known:
            result.skipped += 1
            continue
        add_team(db, *parts[:5], note=parts[5] if len(parts) > 5 else "")
        known.add(parts[0].casefold())
        result.teams += 1
    return result


def add_guest(db: sqlite3.Connection, phone: str, name: str = "", registered_at: str = "") -> bool:
    _require_setup(db)
    phone = normalize_phone(phone)
    if len(phone.lstrip("+")) < 8:
        raise TournamentError(f"Telefono non valido per «{name or phone}».")
    with db:
        cur = db.execute(
            "INSERT OR IGNORE INTO guests(phone, name, registered_at) VALUES (?, ?, ?)",
            (phone, name.strip(), registered_at or None),
        )
        if registered_at:
            db.execute(
                "UPDATE guests SET registered_at = ? WHERE phone = ? AND registered_at IS NULL", (registered_at, phone)
            )
    return cur.rowcount == 1


def _same_phone(a: str, b: str) -> bool:
    # Compare the last 8 digits so "+39 333..." and "333..." match.
    a, b = normalize_phone(a).lstrip("+")[-8:], normalize_phone(b).lstrip("+")[-8:]
    return len(a) == 8 and a == b


def register_guest_team(
    db: sqlite3.Connection, phone: str, name: str, p1: str, p2: str, phone2: str, payment: str
) -> int:
    """A party-only guest turns out to be a couple: create the team (its arrival is marked by the caller)."""
    _require_setup(db)
    guest = next(
        (g for g in db.execute("SELECT * FROM guests WHERE team_id IS NULL") if _same_phone(g["phone"], phone)), None
    )
    if guest is None:
        raise TournamentError("Questo numero non risulta tra gli iscritti al form (o ha già creato una coppia).")
    name, p1, p2 = _clean_names(name, p1, p2)
    phone2 = normalize_phone(phone2)
    if phone2 and len(phone2.lstrip("+")) < 8:
        raise TournamentError("Il secondo numero di telefono non è valido.")
    if payment not in PAYMENTS:
        raise TournamentError("Rispondi alla domanda sul ticket.")
    try:
        with db:
            cur = db.execute(
                "INSERT INTO teams(name, p1, p2, phone1, phone2, payment) VALUES (?, ?, ?, ?, ?, ?)",
                (name, p1, p2, guest["phone"], phone2, payment),
            )
            db.execute("UPDATE guests SET team_id = ? WHERE id = ?", (cur.lastrowid, guest["id"]))
    except sqlite3.IntegrityError:
        raise TournamentError(f"Esiste già una coppia chiamata «{name}».") from None
    return cur.lastrowid


def delete_team(db: sqlite3.Connection, team_id: int) -> None:
    _require_setup(db)
    with db:
        db.execute("DELETE FROM teams WHERE id = ?", (team_id,))


def update_couple(
    db: sqlite3.Connection, team_id: int, name: str, p1: str, p2: str, phone1: str, phone2: str
) -> None:
    """A player fixes the couple's details from its profile: names, and the numbers each of them enters with."""
    name, p1, p2 = _clean_names(name, p1, p2)
    phone1, phone2 = normalize_phone(phone1), normalize_phone(phone2)
    for player, phone in ((1, phone1), (2, phone2)):
        if phone and len(phone.lstrip("+")) < 8:
            raise TournamentError(f"Il numero di {p1 if player == 1 else p2} non è valido.")
    if not (phone1 or phone2):
        raise TournamentError("Serve almeno un numero di telefono.")
    try:
        with db:
            db.execute(
                "UPDATE teams SET name = ?, p1 = ?, p2 = ?, phone1 = ?, phone2 = ? WHERE id = ?",
                (name, p1, p2, phone1, phone2, team_id),
            )
    except sqlite3.IntegrityError:
        raise TournamentError(f"Esiste già una coppia chiamata «{name}».") from None


# The questionnaire: how often they play, and the hit rate that habit usually comes with.
LEVELS = {"mai": ("Mai o quasi", 0.20), "qualche": ("Qualche volta", 0.28), "spesso": ("Spesso", 0.36), "pro": ("Siamo dei pro", 0.45)}
GOALS = {
    "gironi": "Ai gironi", "ottavi": "Agli ottavi", "quarti": "Ai quarti", "semi": "In semifinale",
    "finale": "In finale", "vittoria": "Vinciamo noi",
}


def save_questionnaire(db: sqlite3.Connection, team_id: int, level: str, aim1: int, aim2: int, goal: str) -> None:
    if level not in LEVELS or goal not in GOALS or not (0 <= aim1 <= 10 and 0 <= aim2 <= 10):
        raise TournamentError("Rispondi a tutte le domande.")
    with db:
        db.execute(
            "UPDATE teams SET q_level = ?, q_aim1 = ?, q_aim2 = ?, q_goal = ?, q_at = ? WHERE id = ?",
            (level, aim1, aim2, goal, datetime.now().isoformat(timespec="minutes"), team_id),
        )


def prior_rate(t: sqlite3.Row) -> float | None:
    """The couple's expected hit rate from its answers: its players' guesses, tempered by how often they play
    (people overrate themselves), kept within what beer pong players actually hit."""
    if t["q_level"] not in LEVELS or t["q_aim1"] is None or t["q_aim2"] is None:
        return None
    guess = (t["q_aim1"] + t["q_aim2"]) / 20
    return min(0.6, max(0.1, (guess + LEVELS[t["q_level"]][1]) / 2))


def phone_matches(t: sqlite3.Row, phone: str) -> bool:
    return any(p and _same_phone(p, phone) for p in (t["phone1"], t["phone2"]))


def teams_by_phone(db: sqlite3.Connection, phone: str) -> list[sqlite3.Row]:
    return [t for t in teams(db).values() if phone_matches(t, phone)]


def save_team(
    db: sqlite3.Connection,
    team_id: int | None,
    name: str,
    p1: str,
    p2: str,
    phone1: str,
    phone2: str,
    payment: str,
    paid: bool,
    present: bool,
    note: str = "",
    in_chat: bool = False,
    organizer: bool = False,
    referee_player: int | None = None,
) -> int:
    """Admin add (team_id None) or edit. After the draw only names, phones and payment can change."""
    setup = get_phase(db).is_setup
    current = team(db, team_id) if team_id is not None else None
    if team_id is not None and current is None:
        raise TournamentError("Coppia non trovata.")
    if current is None and not setup:
        raise TournamentError("Il torneo è già iniziato: non si possono aggiungere coppie.")
    name, p1, p2 = _clean_names(name, p1, p2, partial=True)
    phone1, phone2 = normalize_phone(phone1), normalize_phone(phone2)
    for phone in (phone1, phone2):
        if phone and len(phone.lstrip("+")) < 8:
            raise TournamentError(f"Numero di telefono non valido: {phone}")
    if payment and payment not in PAYMENTS:
        raise TournamentError("Pagamento non valido.")
    if not setup:
        present = bool(current["checked_in"])
        if not (p1 and p2):
            raise TournamentError("A torneo iniziato la coppia deve avere due giocatori.")
    if present and not (p1 and p2):
        raise TournamentError("Per segnarla presente servono entrambi i giocatori.")
    values = (name, p1, p2, phone1, phone2, payment or None, int(paid), int(present), note.strip(), int(in_chat), int(organizer),
              referee_player if referee_player in (1, 2) and organizer else None)
    try:
        with db:
            if current is None:
                cur = db.execute(
                    "INSERT INTO teams(name, p1, p2, phone1, phone2, payment, paid, checked_in, note, in_chat, organizer,"
                    " referee_player) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                return cur.lastrowid
            db.execute(
                "UPDATE teams SET name = ?, p1 = ?, p2 = ?, phone1 = ?, phone2 = ?, payment = ?, paid = ?,"
                " checked_in = ?, note = ?, in_chat = ?, organizer = ?, referee_player = ? WHERE id = ?",
                (*values, team_id),
            )
            return team_id
    except sqlite3.IntegrityError:
        raise TournamentError(f"Esiste già una coppia chiamata «{name}».") from None


def admin_check_in(db: sqlite3.Connection, team_id: int, present: bool) -> None:
    _require_setup(db)
    t = team(db, team_id)
    if present and t is not None and not (t["p1"] and t["p2"]):
        raise TournamentError(f"«{t['name']}» non ha ancora entrambi i giocatori: completa la coppia prima.")
    now = datetime.now().isoformat(timespec="minutes") if present else None
    with db:
        # The desk marks the whole couple; a player who already tapped keeps their own time.
        db.execute(
            "UPDATE teams SET checked_in = ?, checked_in_at = ?, arrived1_at = CASE WHEN ? THEN COALESCE(arrived1_at, ?)"
            " END, arrived2_at = CASE WHEN ? THEN COALESCE(arrived2_at, ?) END WHERE id = ?",
            (int(present), now, present, now, present, now, team_id),
        )


def players_arrive(db: sqlite3.Connection, team_id: int, players: set[int]) -> bool:
    """Players tapping «Sono arrivato/a» (or one confirming the partner): the couple is present, and goes into
    the draw, once both have arrived. True when it is."""
    _require_setup(db)
    t = team(db, team_id)
    if t is None or not players <= {1, 2}:
        raise TournamentError("Coppia non trovata.")
    if not (t["p1"] and t["p2"]):
        raise TournamentError(f"Manca il nome del compagno di «{t['name']}»: aggiungetelo in «Personalizza».")
    now = datetime.now().isoformat(timespec="minutes")
    with db:
        for player in players:
            db.execute(f"UPDATE teams SET arrived{player}_at = COALESCE(arrived{player}_at, ?) WHERE id = ?", (now, team_id))
        db.execute(
            "UPDATE teams SET checked_in = 1, checked_in_at = COALESCE(checked_in_at, ?)"
            " WHERE id = ? AND arrived1_at IS NOT NULL AND arrived2_at IS NOT NULL",
            (now, team_id),
        )
    return bool(team(db, team_id)["checked_in"])


def take_beer(db: sqlite3.Connection, team_id: int, player: int) -> None:
    """The bar marks a player's free first beer as collected (the voucher itself can be shown any number of times)."""
    t = team(db, team_id)
    if t is None or player not in (1, 2) or not t[f"arrived{player}_at"]:
        raise TournamentError("La prima birra si ritira all'arrivo alla serata.")
    if t[f"beer{player}_at"]:
        raise TournamentError(f"La prima birra di {t[f'p{player}']} è già stata ritirata.")
    with db:
        db.execute(f"UPDATE teams SET beer{player}_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), team_id))


def set_paid(db: sqlite3.Connection, team_id: int, paid: bool) -> None:
    with db:
        db.execute("UPDATE teams SET paid = ? WHERE id = ?", (int(paid), team_id))


def set_in_chat(db: sqlite3.Connection, team_id: int, in_chat: bool) -> None:
    with db:
        db.execute("UPDATE teams SET in_chat = ? WHERE id = ?", (int(in_chat), team_id))


# A fixed palette: the colour goes into the page's CSS, so it is never free text.
COLORS = {
    "arancio": "#d9480f", "rosso": "#e03131", "rosa": "#d6336c", "viola": "#7048e8",
    "blu": "#1c7ed6", "verde": "#2f9e44", "giallo": "#f08c00", "grigio": "#495057",
}


def display_name(t: sqlite3.Row) -> str:
    return f"{t['emoji']} {t['name']}" if t["emoji"] else t["name"]
