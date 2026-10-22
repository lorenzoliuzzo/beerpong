from __future__ import annotations

import random
import re
import secrets
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import datetime, timedelta

from . import bracket, planning
from .db import get_setting, random_key, random_pin, set_setting
from .standings import Result, Row, compute_table

MAX_TEAM_CUPS = 30
FINAL_SLOT_MIN = 10
MIN_TEAMS = 4


class TournamentError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    title: str
    fields: int
    group_min: int
    ko_min: int
    buffer_min: int
    public_url: str
    phase: str


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
    """A party-only guest turns out to be a couple: create the team, already checked in."""
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
                "INSERT INTO teams(name, p1, p2, phone1, phone2, payment, checked_in) VALUES (?, ?, ?, ?, ?, ?, 1)",
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


def check_in(
    db: sqlite3.Connection, team_id: int, name: str, p1: str, p2: str, phone1: str, phone2: str, payment: str
) -> None:
    _require_setup(db)
    name, p1, p2 = _clean_names(name, p1, p2)
    phone1, phone2 = normalize_phone(phone1), normalize_phone(phone2)
    if len(phone1.lstrip("+")) < 8:
        raise TournamentError("Serve almeno un numero di telefono valido.")
    if phone2 and len(phone2.lstrip("+")) < 8:
        raise TournamentError("Il secondo numero di telefono non è valido.")
    if payment not in PAYMENTS:
        raise TournamentError("Rispondi alla domanda sul ticket.")
    try:
        with db:
            db.execute(
                "UPDATE teams SET name = ?, p1 = ?, p2 = ?, phone1 = ?, phone2 = ?, payment = ?, checked_in = 1"
                " WHERE id = ?",
                (name, p1, p2, phone1, phone2, payment, team_id),
            )
    except sqlite3.IntegrityError:
        raise TournamentError(f"Esiste già una squadra chiamata «{name}».") from None


def phone_matches(t: sqlite3.Row, phone: str) -> bool:
    return any(p and _same_phone(p, phone) for p in (t["phone1"], t["phone2"]))


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
    values = (name, p1, p2, phone1, phone2, payment or None, int(paid), int(present), note.strip())
    try:
        with db:
            if current is None:
                cur = db.execute(
                    "INSERT INTO teams(name, p1, p2, phone1, phone2, payment, paid, checked_in, note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                return cur.lastrowid
            db.execute(
                "UPDATE teams SET name = ?, p1 = ?, p2 = ?, phone1 = ?, phone2 = ?, payment = ?, paid = ?,"
                " checked_in = ?, note = ? WHERE id = ?",
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
    with db:
        db.execute("UPDATE teams SET checked_in = ? WHERE id = ?", (int(present), team_id))


def set_paid(db: sqlite3.Connection, team_id: int, paid: bool) -> None:
    with db:
        db.execute("UPDATE teams SET paid = ? WHERE id = ?", (int(paid), team_id))


# --- Tournament flow -------------------------------------------------------


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _next_minute(dt: datetime) -> datetime:
    floor = dt.replace(second=0, microsecond=0)
    return floor if floor == dt else floor + timedelta(minutes=1)


def draw_groups(team_ids: list[int], seed: int) -> list[list[int]]:
    """Same teams + same seed = same groups, so a previewed draw can be started as-is."""
    ids = sorted(team_ids)
    random.Random(seed).shuffle(ids)
    groups, pos = [], 0
    for size in planning.group_sizes(len(ids)):
        groups.append(ids[pos : pos + size])
        pos += size
    return groups


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


def preview_draw(db: sqlite3.Connection, present_only: bool, seed: int) -> DrawPreview:
    cfg = load_config(db)
    rows = teams(db)
    ids = [t["id"] for t in rows.values() if t["checked_in"] or (not present_only and t["p1"] and t["p2"])]
    if len(ids) < MIN_TEAMS:
        raise TournamentError(f"Servono almeno {MIN_TEAMS} coppie {'presenti' if present_only else 'complete'}.")
    groups = draw_groups(ids, seed)
    fixtures = planning.build_fixtures(groups)
    slots = planning.pack_slots(fixtures, cfg.fields)
    size, qualifiers = planning.bracket_plan(len(groups))
    ko_slots, remaining = 0, size
    while remaining > 2:  # every round before the final
        ko_slots += -(-(remaining // 2) // cfg.fields)
        remaining //= 2
    return DrawPreview(
        seed=seed,
        groups=[[rows[t] for t in g] for g in groups],
        bracket_size=size,
        thirds=qualifiers - 2 * len(groups),
        byes=size - qualifiers,
        matches=len(fixtures),
        group_minutes=len(slots) * (cfg.group_min + cfg.buffer_min),
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
    groups = draw_groups(ids, int(get_setting(db, "board_seed")) if seed is None else seed)
    slots = planning.pack_slots(planning.build_fixtures(groups), cfg.fields)
    base = _next_minute(now)
    step = timedelta(minutes=cfg.group_min + cfg.buffer_min)

    with db:
        for gi, members in enumerate(groups):
            db.executemany("UPDATE teams SET grp = ? WHERE id = ?", [(gi, t) for t in members])
        for si, slot in enumerate(slots):
            start = _fmt(base + si * step)
            for fi, f in enumerate(slot):
                db.execute(
                    "INSERT INTO matches(stage, grp, round, slot, field, start, duration, team1, team2)"
                    " VALUES ('group', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f.grp, f.round, si, fi + 1, start, cfg.group_min, f.team1, f.team2),
                )
        set_setting(db, "phase", "groups")


def reset_tournament(db: sqlite3.Connection) -> None:
    with db:
        db.execute("DELETE FROM matches")
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


def record_result(db: sqlite3.Connection, match_id: int, s1: int, s2: int, winner_side: int | None = None) -> None:
    m = db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None or m["bye"]:
        raise TournamentError("Partita non trovata.")
    if m["team1"] is None or m["team2"] is None:
        raise TournamentError("Le squadre di questa partita non sono ancora definite.")
    if not (0 <= s1 <= MAX_TEAM_CUPS and 0 <= s2 <= MAX_TEAM_CUPS):
        raise TournamentError(f"Il punteggio deve essere tra 0 e {MAX_TEAM_CUPS}.")

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
        if nxt is not None and nxt["done"]:
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

    if m["stage"] == "group":
        pending = db.execute("SELECT COUNT(*) FROM matches WHERE stage = 'group' AND done = 0").fetchone()[0]
        if pending == 0:
            generate_bracket(db)


def assign_cups(db: sqlite3.Connection, match_id: int, side: int, first_player: int) -> None:
    m = db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if m is None or not m["done"] or m["bye"]:
        raise TournamentError("Puoi dividere i canestri solo dopo che l'arbitro ha registrato il risultato.")
    score = m[f"score{side}"]
    if not 0 <= first_player <= score:
        raise TournamentError(f"I canestri di un giocatore devono essere tra 0 e {score}.")
    a, b = ("c1a", "c1b") if side == 1 else ("c2a", "c2b")
    with db:
        db.execute(f"UPDATE matches SET {a} = ?, {b} = ? WHERE id = ?", (first_player, score - first_player, match_id))


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

    last = db.execute("SELECT MAX(slot) AS slot, MAX(start) AS start FROM matches WHERE stage = 'group'").fetchone()
    slot = last["slot"] + 1
    # Start right after the last group slot as actually scheduled (it may have been shifted).
    t = datetime.fromisoformat(last["start"]) + timedelta(minutes=cfg.group_min + cfg.buffer_min)

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

        for r in range(1, total_rounds + 1):
            is_final = r == total_rounds
            playable = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM matches WHERE stage = 'ko' AND round = ? AND bye = 0 ORDER BY pos", (r,)
                )
            ]
            for i in range(0, len(playable), cfg.fields):
                for fi, mid in enumerate(playable[i : i + cfg.fields]):
                    db.execute(
                        "UPDATE matches SET slot = ?, field = ?, start = ?, duration = ? WHERE id = ?",
                        (slot, fi + 1, _fmt(t), None if is_final else cfg.ko_min, mid),
                    )
                slot += 1
                t += timedelta(minutes=(FINAL_SLOT_MIN if is_final else cfg.ko_min) + cfg.buffer_min)
        set_setting(db, "phase", "knockout")


def reopen_groups(db: sqlite3.Connection) -> None:
    if get_setting(db, "phase") != "knockout":
        raise TournamentError("I gironi sono già aperti.")
    played = db.execute("SELECT COUNT(*) FROM matches WHERE stage = 'ko' AND done = 1 AND bye = 0").fetchone()[0]
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

    def name(team: int | None, bye: bool) -> str:
        if team is not None:
            return team_rows[team]["name"]
        return "Riposo" if bye else "Da definire"

    def split(a: int | None, b: int | None) -> tuple[int, int] | None:
        return (a, b) if a is not None and b is not None else None

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
                n1=name(r["team1"], bool(r["bye"])),
                n2=name(r["team2"], bool(r["bye"])),
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
            )
        )
    return views


def match(db: sqlite3.Connection, match_id: int) -> MatchView | None:
    return next((m for m in matches(db) if m.id == match_id), None)


def by_slot(views: list[MatchView]) -> list[tuple[int, str, list[MatchView]]]:
    slots: dict[int, list[MatchView]] = defaultdict(list)
    for m in views:
        if m.slot is not None:
            slots[m.slot].append(m)
    return [(s, ms[0].time, ms) for s, ms in sorted(slots.items())]


def live(views: list[MatchView]) -> tuple[list[MatchView], list[MatchView]]:
    """Matches on the tables now (earliest unfinished slot) and the slot after it."""
    pending = by_slot([m for m in views if not m.done])
    now = pending[0][2] if pending else []
    upcoming = pending[1][2] if len(pending) > 1 else []
    return now, upcoming


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
    cups: int = 0
    played: int = 0

    @property
    def average(self) -> float:
        return self.cups / self.played if self.played else 0.0


def scorers(db: sqlite3.Connection) -> list[Scorer]:
    """Player ranking: only cups the team has split between its two players count."""
    team_rows = teams(db)
    stats = {
        (t["id"], p): Scorer(t[p], t["name"], t["id"])
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
    played: int = 0
    won: int = 0
    cups_for: int = 0
    cups_against: int = 0

    @property
    def average(self) -> float:
        return self.cups_for / self.played if self.played else 0.0


def team_ranking(db: sqlite3.Connection) -> list[TeamStats]:
    stats = {t["id"]: TeamStats(t["id"], t["name"]) for t in teams(db).values() if t["checked_in"]}
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


@dataclass
class MissingOverview:
    target: int
    complete: int
    incomplete: list[sqlite3.Row]
    guests: list[GuestView]
    unpaid: list[sqlite3.Row]


def missing_overview(db: sqlite3.Connection) -> MissingOverview:
    rows = list(teams(db).values())
    return MissingOverview(
        target=int(get_setting(db, "target_teams")),
        complete=sum(1 for t in rows if t["p1"] and t["p2"]),
        incomplete=[t for t in rows if not (t["p1"] and t["p2"]) or not t["phone1"] or t["note"]],
        guests=guest_views(db),
        unpaid=[t for t in rows if not t["paid"]],
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


def board_groups(db: sqlite3.Connection) -> tuple[list[BoardGroup], bool]:
    """Groups for the board and whether they are provisional (every registered couple, as if confirmed)."""
    rows = teams(db)
    if get_setting(db, "phase") == "setup":
        if len(rows) < MIN_TEAMS:
            return [], True
        groups = draw_groups(list(rows), int(get_setting(db, "board_seed")))
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
