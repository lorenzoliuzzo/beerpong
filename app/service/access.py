"""The phones of a couple, entering before the night, checking in at the venue, arrivals."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer

from ..db import get_setting
from .core import SCORING, TournamentError
from .teams import COLORS, _same_phone, team
from .setup import TournamentInfo, registration


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


def entered_elsewhere(db: sqlite3.Connection, team_id: int, device: str) -> bool:
    """Whether another phone is already in this couple: from then on the way in is the partner's invite."""
    return db.execute("SELECT 1 FROM devices WHERE team_id = ? AND device != ?", (team_id, device)).fetchone() is not None


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


PHOTO_MAX_BYTES = 300_000
PHOTO_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}


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
