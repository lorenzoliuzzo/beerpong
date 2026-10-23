"""Web Push: phones subscribe from the site, and couples are told when their match is next or on now.

Messages are worked out in the request (cheap, deduplicated through push_sent) and delivered afterwards
in a background task, so a referee confirming a score never waits for the push services.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from . import service
from .db import connect, get_setting, set_setting

log = logging.getLogger("beerpong.push")


# Only the browsers' push services: a subscription can never make the Pi post to an arbitrary address.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", ".push.apple.com", ".notify.windows.com")
MAX_SUBSCRIPTIONS = 1000
SEND_TIMEOUT_S = 10


@dataclass
class Message:
    title: str
    body: str
    url: str = "/"


@dataclass
class Delivery:
    sub_id: int
    subscription: dict
    message: Message


def public_key(db: sqlite3.Connection) -> str:
    """The VAPID application server key, created on first use and kept in the database (never in the code)."""
    key = get_setting(db, "vapid_public")
    if key:
        return key
    vapid = Vapid()
    vapid.generate_keys()
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    with db:
        # OR IGNORE: if another request created the pair meanwhile, keep that one and return it below.
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('vapid_private', ?)", (vapid.private_pem().decode(),))
        if db.execute("SELECT changes()").fetchone()[0]:
            set_setting(db, "vapid_public", base64.urlsafe_b64encode(raw).rstrip(b"=").decode())
    return get_setting(db, "vapid_public")


def _key_length(b64: str) -> int:
    try:
        return len(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    except ValueError:
        return 0


def subscribe(db: sqlite3.Connection, endpoint: str, p256dh: str, auth: str, team_id: int | None) -> None:
    url = urlparse(endpoint)
    host = url.hostname or ""
    if url.scheme != "https" or not any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS):
        raise service.TournamentError("Iscrizione alle notifiche non valida.")
    # A browser's keys: an uncompressed P-256 point and a 16-byte secret.
    if _key_length(p256dh) != 65 or _key_length(auth) != 16:
        raise service.TournamentError("Iscrizione alle notifiche non valida.")
    known = db.execute("SELECT 1 FROM push_subs WHERE endpoint = ?", (endpoint,)).fetchone()
    if not known and db.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0] >= MAX_SUBSCRIPTIONS:
        raise service.TournamentError("Troppe iscrizioni alle notifiche.")
    with db:
        # The couple follows the session: a phone that left its couple stops getting that couple's notices.
        db.execute(
            "INSERT INTO push_subs(endpoint, p256dh, auth, team_id) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,"
            " team_id = excluded.team_id",
            (endpoint, p256dh, auth, team_id),
        )


def unsubscribe(db: sqlite3.Connection, endpoint: str) -> None:
    with db:
        db.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))


def subscriber_count(db: sqlite3.Connection) -> tuple[int, int]:
    row = db.execute("SELECT COUNT(*), COUNT(DISTINCT team_id) FROM push_subs").fetchone()
    return row[0], row[1]


def _subs(db: sqlite3.Connection, team_ids: set[int] | None = None) -> list[sqlite3.Row]:
    rows = db.execute("SELECT * FROM push_subs").fetchall()
    return [r for r in rows if team_ids is None or r["team_id"] in team_ids]


def _deliveries(subs: list[sqlite3.Row], message: Message) -> list[Delivery]:
    return [
        Delivery(s["id"], {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}, message)
        for s in subs
    ]


def broadcast(db: sqlite3.Connection, title: str, body: str) -> list[Delivery]:
    title, body = title.strip(), body.strip()
    if not body:
        raise service.TournamentError("Scrivi il testo della notifica.")
    return _deliveries(_subs(db), Message(title or service.load_config(db).title, body))


def _claim(db: sqlite3.Connection, match_id: int, kind: str) -> bool:
    """True the first time a (match, kind) notice is due: each one goes out once."""
    with db:
        cur = db.execute("INSERT OR IGNORE INTO push_sent(match_id, kind) VALUES (?, ?)", (match_id, kind))
    return cur.rowcount == 1


def due(db: sqlite3.Connection) -> list[Delivery]:
    """Notices for the couples whose match just went on a table or is up next there."""
    if get_setting(db, "phase") not in ("groups", "knockout") or not _subs(db):
        return []
    now, upcoming = service.live(service.matches(db))
    out: list[Delivery] = []
    for kind, ms in (("now", now), ("next", upcoming)):
        for m in ms:
            if m.t1 is None or m.t2 is None or not _claim(db, m.id, kind):
                continue
            for team, rival in ((m.t1, m.n2), (m.t2, m.n1)):
                if kind == "now":
                    message = Message(f"🔥 Tocca a voi! Tavolo {m.field}", f"{m.label} contro {rival}: andate al tavolo.")
                else:
                    message = Message(f"📣 Preparatevi: Tavolo {m.field}", f"Siete i prossimi contro {rival} (~{m.time}).")
                out += _deliveries(_subs(db, {team}), Message(message.title, message.body, f"/live/{m.id}"))
    return out


def first_matches(db: sqlite3.Connection) -> list[Delivery]:
    """At the start: every subscribed couple gets its first match. The matches already on or next are marked
    as notified, so nobody also gets "it's your turn" half an hour before the scheduled time."""
    views = service.matches(db)
    now, upcoming = service.live(views)
    for kind, ms in (("now", now), ("next", upcoming)):
        for m in ms:
            _claim(db, m.id, kind)
    out: list[Delivery] = []
    teams = {s["team_id"] for s in _subs(db) if s["team_id"] is not None}
    for team in sorted(teams):
        m = next((m for m in views if m.side(team) and not m.done and not m.bye), None)
        if m is None:
            continue
        rival = m.n2 if m.t1 == team else m.n1
        message = Message(f"🍺 Si parte! Prima partita alle {m.time}", f"{m.label}, Tavolo {m.field}, contro {rival}.", "/")
        out += _deliveries(_subs(db, {team}), message)
    return out


def _contact(public_url: str) -> str:
    """The VAPID "sub" claim must be a bare https origin (no port, path or trailing slash) or a mailto."""
    host = urlparse(public_url).hostname or ""
    return f"https://{host}" if "." in host and not host.replace(".", "").isdecimal() else "https://localhost"


def send(deliveries: list[Delivery]) -> None:
    """Runs after the response: its own connection, and subscriptions the push service dropped are removed."""
    if not deliveries:
        return
    db = connect()
    try:
        public_key(db)  # makes sure the pair exists before signing
        vapid = Vapid.from_pem(get_setting(db, "vapid_private").encode())
        claims = {"sub": _contact(get_setting(db, "public_url"))}
        gone = []
        for d in deliveries:
            payload = json.dumps({"title": d.message.title, "body": d.message.body, "url": d.message.url})
            try:
                webpush(
                    d.subscription, payload, vapid_private_key=vapid, vapid_claims=dict(claims), ttl=600,
                    timeout=SEND_TIMEOUT_S,
                )
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    gone.append(d.sub_id)
                else:
                    log.warning("push failed: %s", e)
            except requests.RequestException as e:  # one unreachable push service must not stop the others
                log.warning("push failed: %s", e)
            except ValueError as e:  # keys the encryption cannot use: this subscription can never work
                log.warning("push dropped, unusable keys: %s", e)
                gone.append(d.sub_id)
        if gone:
            with db:
                db.executemany("DELETE FROM push_subs WHERE id = ?", [(i,) for i in gone])
    finally:
        db.close()
