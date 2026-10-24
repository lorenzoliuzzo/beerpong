"""The tournament as the organizers set it up: details, archives, registration and payments."""
from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..db import get_setting, random_key, set_phase, set_setting
from ..states import Phase, Scoring
from .core import MIN_TEAMS, REFEREES, SCORING, TournamentError
from .teams import normalize_phone, teams, teams_by_phone
from .progress import PHONE_IN_TEXT


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
    scoring: str = Scoring.COPPIE
    referees: str = "volontari"

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
        set_phase(db, Phase.SETUP)
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
