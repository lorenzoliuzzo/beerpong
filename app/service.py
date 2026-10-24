from __future__ import annotations

import hashlib
import random
import re
import secrets
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer

from . import bracket, planning
from .db import get_setting, random_key, random_pin, set_setting
from .standings import Result, Row, compute_table

MAX_TEAM_CUPS = 30
CUPS = 6  # per couple, as in the rules
FINAL_CUPS = 10  # the final is played with 10 cups and no time limit
FINAL_SLOT_MIN = 10
MIN_TEAMS = 4


class TournamentError(Exception):
    pass


# How a match is scored: chosen for the tournament, and changeable for a single match.
SCORING = {
    "coppie": "Le coppie inseriscono il risultato",
    "risultato": "L'arbitro inserisce il risultato finale",
    "tiri": "L'arbitro segna tiro per tiro",
}
# Who may referee when a match needs one.
REFEREES = {"pin": "Con il PIN del tavolo", "volontari": "Chi non sta giocando", "entrambi": "Tutti e due"}


@dataclass(frozen=True)
class Config:
    title: str
    fields: int
    group_min: int
    ko_min: int
    buffer_min: int
    public_url: str
    phase: str
    start_time: str
    scoring: str = "coppie"
    referees: str = "entrambi"

    @property
    def pin_referees(self) -> bool:
        return self.referees in ("pin", "entrambi")

    @property
    def volunteer_referees(self) -> bool:
        return self.referees in ("volontari", "entrambi")


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
    mode: str = "coppie"  # how it is scored (SCORING): its own choice or the tournament's
    playing: bool = False  # its turn on a table right now (see live()): the couples are called
    started_at: float | None = None  # the referee started it
    start_iso: str = ""  # scheduled start, local time, for the countdowns

    def side(self, team: int | None) -> int | None:
        return 1 if team is not None and team == self.t1 else 2 if team is not None and team == self.t2 else None

    def split_missing(self, side: int) -> bool:
        return self.done and not self.bye and (self.split1 if side == 1 else self.split2) is None


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
        phase=get_setting(db, "phase"),
        start_time=get_setting(db, "start_time"),
        scoring=get_setting(db, "scoring"),
        referees=get_setting(db, "referees"),
    )


def referee_pin(db: sqlite3.Connection, field_no: int) -> str:
    key = f"ref_pin_{field_no}"
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    taken = {r["value"] for r in db.execute("SELECT value FROM settings WHERE key LIKE 'ref_pin_%' OR key = 'admin_pin'")}
    pin = random_pin()
    while pin in taken:
        pin = random_pin()
    with db:
        set_setting(db, key, pin)
    return pin


def authenticate(db: sqlite3.Connection, pin: str) -> tuple[str, int | None] | None:
    if secrets.compare_digest(pin, get_setting(db, "admin_pin")):
        return "admin", None
    if get_setting(db, "referees") in ("pin", "entrambi"):
        for row in db.execute("SELECT key, value FROM settings WHERE key LIKE 'ref_pin_%'"):
            if secrets.compare_digest(pin, row["value"]):
                return "referee", int(row["key"].removeprefix("ref_pin_"))
    return None


def checkin_key_ok(db: sqlite3.Connection, key: str) -> bool:
    return secrets.compare_digest(key, get_setting(db, "reg_key"))


def rotate_checkin_key(db: sqlite3.Connection) -> None:
    with db:
        set_setting(db, "reg_key", random_key())


# --- Teams -----------------------------------------------------------------

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
    if get_setting(db, "phase") != "setup":
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
        name = f"Squadra di {p1}"
    if not name or (not partial and not (p1 and p2)):
        raise TournamentError(
            "Serve almeno il nome della squadra o di un giocatore." if partial
            else "Servono nome della squadra e due giocatori."
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
        raise TournamentError(f"Esiste già una squadra chiamata «{name}».") from None
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
        if n == 1 and parts[0].casefold() == "squadra":
            continue
        # Columns: squadra; giocatore1; giocatore2; [telefono1]; [telefono2]; [note, ignored]; [iscritto il]
        if not 3 <= len(parts) <= 7:
            raise TournamentError(f"Riga {n}: usa il formato «Squadra; Giocatore 1; Giocatore 2; Telefono».")
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
        raise TournamentError("Questo numero non risulta tra gli iscritti al form (o ha già creato una squadra).")
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
        raise TournamentError(f"Esiste già una squadra chiamata «{name}».") from None
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
        raise TournamentError(f"Esiste già una squadra chiamata «{name}».") from None


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


def is_unpaired_guest(db: sqlite3.Connection, phone: str) -> bool:
    return any(_same_phone(g["phone"], phone) for g in unassigned_guests(db))


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
    setup = get_setting(db, "phase") == "setup"
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
        raise TournamentError(f"Esiste già una squadra chiamata «{name}».") from None


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


BEER_SHOWN_S = 60


def take_beer(db: sqlite3.Connection, team_id: int, player: int) -> None:
    """The first beer: the voucher is used the moment it is shown, and shows for a minute only."""
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


# --- Tournament flow -------------------------------------------------------


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
    if cfg.phase != "setup":
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
        set_setting(db, "phase", "groups")


def reset_tournament(db: sqlite3.Connection) -> None:
    with db:
        db.execute("DELETE FROM matches")
        db.execute("DELETE FROM push_sent")
        db.execute("UPDATE teams SET grp = NULL")
        set_setting(db, "phase", "setup")


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
        set_setting(db, "phase", "finished")
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
        raise TournamentError("Le squadre di questa partita non sono ancora definite.")
    if not (0 <= s1 <= MAX_TEAM_CUPS and 0 <= s2 <= MAX_TEAM_CUPS):
        raise TournamentError(f"Il punteggio deve essere tra 0 e {MAX_TEAM_CUPS}.")

    was_done = bool(m["done"])

    if m["stage"] == "group":
        if get_setting(db, "phase") != "groups":
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
        raise TournamentError("Puoi dividere i canestri solo dopo che è stato inserito il risultato.")
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
    if cfg.phase != "groups":
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
        set_setting(db, "phase", "knockout")


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


def reopen_groups(db: sqlite3.Connection) -> None:
    if get_setting(db, "phase") != "knockout":
        raise TournamentError("I gironi sono già aperti.")
    played = db.execute(
        "SELECT COUNT(*) FROM matches WHERE stage = 'ko' AND (done = 1 OR started_at IS NOT NULL) AND bye = 0"
    ).fetchone()[0]
    if played:
        raise TournamentError("Si è già giocata una partita a eliminazione: non si possono riaprire i gironi.")
    with db:
        db.execute("DELETE FROM matches WHERE stage = 'ko'")
        set_setting(db, "phase", "groups")


def _group_rows(db: sqlite3.Connection) -> dict[int, list[Row]]:
    members: dict[int, list[int]] = defaultdict(list)
    for r in db.execute("SELECT id, grp FROM teams WHERE grp IS NOT NULL"):
        members[r["grp"]].append(r["id"])
    results = [
        Result(r["team1"], r["team2"], r["score1"], r["score2"])
        for r in db.execute("SELECT * FROM matches WHERE stage = 'group' AND done = 1")
    ]
    return {g: compute_table(ids, results) for g, ids in sorted(members.items())}


# --- Read models -----------------------------------------------------------


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

    from . import tracking  # tracking imports this module

    default_mode = get_setting(db, "scoring")
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
                mode=r["mode"] or default_mode,
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
    if cfg.phase in ("knockout", "finished") and ko_ms:
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
    to_split: list[MatchView]
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
    phase = get_setting(db, "phase")
    in_bracket = any(m.side(team_id) for m in ko)
    eliminated = (phase in ("knockout", "finished") and not in_bracket) or any(
        m.done and m.side(team_id) and m.winner != team_id for m in ko
    )
    final = max(ko, key=lambda m: m.round, default=None)
    return TeamDashboard(
        team=row,
        status=status,
        next_match=next((m for m in mine if not m.done), None),
        matches=mine,
        to_split=[m for m in mine if m.split_missing(m.side(team_id))],
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
    from . import tracking
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


# --- Missing couples: guests to pair, placeholders to complete, official list ---------------


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
            raise TournamentError(f"Esiste già una squadra chiamata «{new_name}».") from None
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


# --- How the tournament is unfolding ----------------------------------------


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
    if get_setting(db, "phase") != "groups":
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


# --- Public board (poster / TV) --------------------------------------------------------------


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
    if get_setting(db, "phase") == "setup":
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


# --- The tournament as the organizers set it up -----------------------------


@dataclass
class TournamentInfo:
    title: str
    date: str  # ISO day, "" until set
    start_time: str
    target_teams: int
    fields: int
    fee: str  # per couple, as typed: "10" or "12,50"
    rules: str
    form_url: str
    pay_info: str  # how to pay: Satispay number, IBAN, ...
    scoring: str = "coppie"
    referees: str = "entrambi"

    @property
    def created(self) -> bool:
        return bool(self.date)


TOURNAMENT_KEYS = (
    "title", "date", "start_time", "target_teams", "fields", "fee", "rules", "form_url", "pay_info", "scoring", "referees",
)


def load_tournament(db: sqlite3.Connection) -> TournamentInfo:
    values = {k: get_setting(db, k) for k in TOURNAMENT_KEYS}
    return TournamentInfo(**{**values, "target_teams": int(values["target_teams"]), "fields": int(values["fields"])})


def save_tournament(db: sqlite3.Connection, info: TournamentInfo) -> None:
    if not info.title.strip():
        raise TournamentError("Dai un nome al torneo.")
    try:
        datetime.strptime(info.date, "%Y-%m-%d")
    except ValueError:
        raise TournamentError("Scegli la data del torneo.") from None
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", info.start_time):
        raise TournamentError("Orario di inizio non valido: usa il formato 21:30.")
    if not MIN_TEAMS <= info.target_teams <= 64:
        raise TournamentError(f"Le coppie devono essere tra {MIN_TEAMS} e 64.")
    if not 1 <= info.fields <= 20:
        raise TournamentError("Serve almeno un tavolo (massimo 20).")
    fee = info.fee.strip().replace(".", ",")
    if fee and not re.fullmatch(r"\d{1,4}(,\d{1,2})?", fee):
        raise TournamentError("Quota non valida: scrivi solo l'importo, es. 10 o 12,50.")
    # Shown as a link on the public page: only web addresses, never javascript: or the like.
    if info.form_url and not re.fullmatch(r"https?://\S+", info.form_url.strip()):
        raise TournamentError("Il link di iscrizione deve iniziare con https://")
    if info.scoring not in SCORING or info.referees not in REFEREES:
        raise TournamentError("Scegli come si segnano le partite e chi arbitra.")
    values = {
        "title": info.title.strip(), "date": info.date, "start_time": info.start_time,
        "target_teams": str(info.target_teams), "fields": str(info.fields), "fee": fee,
        "rules": info.rules.strip()[:4000], "form_url": info.form_url.strip(), "pay_info": info.pay_info.strip()[:1000],
        "scoring": info.scoring, "referees": info.referees,
    }
    with db:
        for k, v in values.items():
            set_setting(db, k, v)


def new_tournament(db: sqlite3.Connection, archive_dir: Path) -> Path:
    """Start over for the next tournament: the current database is archived first, then couples, guests and
    matches go. PINs, keys, phones subscribed to notices and the tournament settings stay."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"torneo-{datetime.now():%Y%m%d-%H%M%S}.db"
    dest = sqlite3.connect(target)
    with dest:
        db.backup(dest)
    dest.close()
    with db:
        db.execute("DELETE FROM matches")  # cup hits, reports and cheers go with them
        db.execute("DELETE FROM push_sent")
        db.execute("DELETE FROM guests")
        db.execute("DELETE FROM teams")
        set_setting(db, "phase", "setup")
        set_setting(db, "date", "")
        set_setting(db, "reg_key", random_key())
        set_setting(db, "board_seed", str(secrets.randbits(31)))
    return target


def archives(archive_dir: Path) -> list[Path]:
    return sorted(archive_dir.glob("torneo-*.db"), reverse=True) if archive_dir.is_dir() else []


def pay_code(team_id: int) -> str:
    """The reference a couple writes in the payment: it is how the statement finds them."""
    return f"BP{team_id:03d}"


PAY_CODE_IN_TEXT = re.compile(r"\bBP[\s\-]?0*(\d{1,5})\b", re.IGNORECASE)


@dataclass
class Registration:
    """The places: the first couples registered (form order) fill them, the others wait."""

    target: int
    placed: list[sqlite3.Row]
    waiting: list[sqlite3.Row]

    @property
    def confirmed(self) -> list[sqlite3.Row]:
        # The organizers' own couples have a place without paying.
        return [t for t in self.placed if t["paid"] or t["organizer"]]

    @property
    def unpaid(self) -> list[sqlite3.Row]:
        return [t for t in self.placed if not (t["paid"] or t["organizer"])]


def registration(db: sqlite3.Connection) -> Registration:
    rows = db.execute("SELECT * FROM teams ORDER BY id").fetchall()
    target = int(get_setting(db, "target_teams"))
    return Registration(target, rows[:target], rows[target:])


@dataclass
class StatementResult:
    marked: list[str]
    already: list[str]
    unmatched: list[str]


def mark_paid_from_statement(db: sqlite3.Connection, text: str) -> StatementResult:
    """A pasted Satispay or bank statement: a line with a couple's code (BP007), or failing that one of its
    phone numbers, confirms that couple's payment. Lines with figures that match nobody are reported."""
    result = StatementResult([], [], [])
    by_id = teams(db)
    for line in text.splitlines():
        if not line.strip():
            continue
        hits = [by_id[int(n)] for n in PAY_CODE_IN_TEXT.findall(line) if int(n) in by_id]
        if not hits:
            hits = [t for raw in PHONE_IN_TEXT.findall(line) for t in teams_by_phone(db, raw)]
        if not hits:
            if any(c.isdigit() for c in line):
                result.unmatched.append(line.strip()[:120])
            continue
        for t in {t["id"]: t for t in hits}.values():
            if t["paid"] or t["name"] in result.marked:
                result.already.append(t["name"])
                continue
            with db:
                db.execute("UPDATE teams SET paid = 1 WHERE id = ?", (t["id"],))
            result.marked.append(t["name"])
    return result


def whatsapp_number(phone: str) -> str:
    """wa.me wants the international number without "+": Italian mobiles are often typed without +39."""
    digits = normalize_phone(phone)
    if digits.startswith("+"):
        return digits[1:]
    if digits.startswith("00"):
        return digits[2:]
    return "39" + digits if len(digits) == 10 and digits.startswith("3") else digits


def payment_message(info: TournamentInfo, t: sqlite3.Row) -> str:
    fee = f" di {info.fee} €" if info.fee else ""
    lines = [
        f"Ciao! Per confermare il posto di «{t['name']}» al {info.title} manca il pagamento{fee}.",
        f"Nella causale scrivete: {pay_code(t['id'])}",
    ]
    if info.pay_info:
        lines.append(info.pay_info)
    return "\n".join(lines)


# --- The phones of a couple ---------------------------------------------------

INVITE_MAX_AGE_S = 12 * 3600


def player_for_phone(t: sqlite3.Row, phone: str) -> int | None:
    """Which player of the couple a phone number belongs to: phone1 is player 1's, phone2 player 2's."""
    for player in (1, 2):
        if t[f"phone{player}"] and _same_phone(t[f"phone{player}"], phone):
            return player
    return None


def register_device(db: sqlite3.Connection, device: str, team_id: int, player: int | None) -> None:
    with db:
        db.execute(
            "INSERT INTO devices(device, team_id, player, seen_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(device) DO UPDATE SET team_id = excluded.team_id, player = excluded.player,"
            " seen_at = excluded.seen_at",
            (device, team_id, player, datetime.now().isoformat(timespec="minutes")),
        )


def forget_device(db: sqlite3.Connection, device: str) -> None:
    with db:
        db.execute("DELETE FROM devices WHERE device = ?", (device,))


def team_devices(db: sqlite3.Connection, team_id: int) -> dict[int, int]:
    """{player: phones} for the players of a couple who entered on their own phone."""
    counts = {1: 0, 2: 0}
    for r in db.execute("SELECT player, COUNT(*) FROM devices WHERE team_id = ? AND player IS NOT NULL GROUP BY player", (team_id,)):
        counts[r[0]] = r[1]
    return counts


def _invites(db: sqlite3.Connection) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_setting(db, "secret"), salt="invito-compagno")


def invite_token(db: sqlite3.Connection, team_id: int, player: int) -> str:
    """A link for one player's partner: it enters the couple as that player, no phone number needed."""
    return _invites(db).dumps({"t": team_id, "p": player})


def read_invite(db: sqlite3.Connection, token: str) -> tuple[int, int]:
    try:
        data = _invites(db).loads(token, max_age=INVITE_MAX_AGE_S)
    except SignatureExpired:
        raise TournamentError("L'invito è scaduto: chiedete al compagno di mostrarvi di nuovo il QR.") from None
    except BadSignature:
        raise TournamentError("Invito non valido.") from None
    if team(db, data["t"]) is None or data["p"] not in (1, 2):
        raise TournamentError("Questa coppia non esiste più.")
    return data["t"], data["p"]


# --- Entering before the night, checking in at the venue ------------------------


def may_enter_early(t: sqlite3.Row) -> bool:
    """A couple with a confirmed place may open its profile from home; being present is a separate step."""
    return bool(t["paid"] or t["organizer"] or t["checked_in"])


def _access(db: sqlite3.Connection) -> URLSafeSerializer:
    return URLSafeSerializer(get_setting(db, "secret"), salt="accesso-coppia")


def access_token(db: sqlite3.Connection, team_id: int, player: int) -> str:
    """A player's personal link, sent to their own number once the payment is in: valid for this tournament."""
    return _access(db).dumps([team_id, player])


def read_access(db: sqlite3.Connection, token: str) -> tuple[sqlite3.Row, int]:
    try:
        team_id, player = _access(db).loads(token)
    except (BadSignature, TypeError, ValueError):
        raise TournamentError("Link di accesso non valido: entra con il tuo numero.") from None
    t = team(db, team_id)
    if t is None:
        raise TournamentError("Questa coppia non è iscritta al torneo in corso.")
    if not may_enter_early(t):
        raise TournamentError("Il vostro posto si conferma con il pagamento: appena arriva vi scriviamo.")
    if player not in (1, 2):
        raise TournamentError("Link di accesso non valido: entra con il tuo numero.")
    return t, player


def venue_code(db: sqlite3.Connection) -> str:
    """Four digits printed under the check-in QR and shown on the TV: typing them proves you are at the venue.
    They follow the check-in key, so a new key (Admin → Accessi) changes them too."""
    digest = hashlib.sha256(get_setting(db, "reg_key").encode()).hexdigest()
    return f"{int(digest, 16) % 10000:04d}"


def venue_code_ok(db: sqlite3.Connection, code: str) -> bool:
    return secrets.compare_digest(code.strip(), venue_code(db))


def access_message(info: TournamentInfo, t: sqlite3.Row, player: int, url: str) -> str:
    return "\n".join([
        f"Ciao {t[f'p{player}']}! Pagamento ricevuto ✓ «{t['name']}» è iscritta al {info.title}.",
        f"Questo è il tuo accesso personale: {url}",
        "Da lì attivi le notifiche e personalizzate il profilo della coppia. "
        "Alla serata inquadra il QR all'ingresso per segnarti arrivato/a.",
    ])


# --- How a couple presents itself ------------------------------------------------

# A fixed palette: the colour goes into the page's CSS, so it is never free text.
COLORS = {
    "arancio": "#d9480f", "rosso": "#e03131", "rosa": "#d6336c", "viola": "#7048e8",
    "blu": "#1c7ed6", "verde": "#2f9e44", "giallo": "#f08c00", "grigio": "#495057",
}
PHOTO_MAX_BYTES = 300_000
PHOTO_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}


def display_name(t: sqlite3.Row) -> str:
    return f"{t['emoji']} {t['name']}" if t["emoji"] else t["name"]


def save_look(
    db: sqlite3.Connection, team_id: int, emoji: str, motto: str, color: str, nick1: str, nick2: str
) -> None:
    emoji, motto, nick1, nick2 = emoji.strip(), " ".join(motto.split()), nick1.strip(), nick2.strip()
    if len(emoji) > 8 or any(c.isascii() and c.isalnum() for c in emoji):
        raise TournamentError("Scegliete una sola emoji.")
    if len(motto) > 60:
        raise TournamentError("Il motto è troppo lungo: massimo 60 caratteri.")
    if color and color not in COLORS:
        raise TournamentError("Colore non valido.")
    if len(nick1) > 20 or len(nick2) > 20:
        raise TournamentError("I soprannomi possono avere al massimo 20 caratteri.")
    with db:
        db.execute(
            "UPDATE teams SET emoji = ?, motto = ?, color = ?, nick1 = ?, nick2 = ? WHERE id = ?",
            (emoji, motto, color, nick1, nick2, team_id),
        )


def save_photo(db: sqlite3.Connection, team_id: int, data: bytes, photo_dir: Path) -> str:
    """The couple's photo, already shrunk on the phone: only small real images are kept."""
    if not data or len(data) > PHOTO_MAX_BYTES:
        raise TournamentError("Foto troppo grande: riprovate, il telefono la rimpicciolisce da solo.")
    ext = next((e for magic, e in PHOTO_TYPES.items() if data.startswith(magic)), None)
    if ext is None or (ext == "webp" and data[8:12] != b"WEBP"):
        raise TournamentError("Il file non è una foto.")
    photo_dir.mkdir(parents=True, exist_ok=True)
    name = f"{team_id}-{secrets.token_hex(6)}.{ext}"  # a new name: phones never show a cached old photo
    (photo_dir / name).write_bytes(data)
    remove_photo(db, team_id, photo_dir)
    with db:
        db.execute("UPDATE teams SET photo = ? WHERE id = ?", (name, team_id))
    return name


def remove_photo(db: sqlite3.Connection, team_id: int, photo_dir: Path) -> None:
    t = team(db, team_id)
    if t is not None and t["photo"]:
        (photo_dir / t["photo"]).unlink(missing_ok=True)
        with db:
            db.execute("UPDATE teams SET photo = '' WHERE id = ?", (team_id,))


@dataclass
class Arrivals:
    arrived: list[sqlite3.Row]  # latest first
    expected: list[sqlite3.Row]


def arrivals(db: sqlite3.Connection) -> Arrivals:
    """Who is already at the venue, among the couples with a place."""
    placed = registration(db).placed
    arrived = sorted((t for t in placed if t["checked_in"]), key=lambda t: t["checked_in_at"] or "", reverse=True)
    return Arrivals(arrived, sorted((t for t in placed if not t["checked_in"]), key=lambda t: t["name"].casefold()))



def set_match_mode(db: sqlite3.Connection, match_id: int, mode: str) -> None:
    """One match scored differently from the tournament (say, the final shot by shot); "" goes back to it."""
    if mode and mode not in SCORING:
        raise TournamentError("Modalità non valida.")
    m = db.execute("SELECT done, started_at FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None:
        raise TournamentError("Partita non trovata.")
    if m["done"] or m["started_at"] is not None:
        raise TournamentError("La partita è già iniziata: la modalità non si cambia più.")
    with db:
        db.execute("UPDATE matches SET mode = ? WHERE id = ?", (mode or None, match_id))
