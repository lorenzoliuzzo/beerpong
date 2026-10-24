"""What every page shares: the database dependency, templates, sessions and roles, throttling."""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import service
from ..db import connect, get_setting
from ..states import LivePhase, Phase, Scoring


BASE = Path(__file__).resolve().parent.parent  # app/
templates = Jinja2Templates(directory=BASE / "templates")
# Changes with the stylesheet, so phones never keep an old copy after a deploy.
templates.env.globals["static_v"] = hashlib.sha256((BASE / "static" / "style.css").read_bytes()).hexdigest()[:10]
templates.env.globals["icons_v"] = hashlib.sha256((BASE / "static" / "icons.svg").read_bytes()).hexdigest()[:10]
templates.env.globals["team_colors"] = service.COLORS
templates.env.globals.update(Phase=Phase, Scoring=Scoring, LivePhase=LivePhase)
log = logging.getLogger("beerpong")
ATTEMPT_WINDOW_S = 600
# Phones at the venue often share one carrier address: a few typos must not lock everyone out of check-in.
ATTEMPT_MAX_FAILURES = 30
_failures: dict[str, deque[float]] = defaultdict(deque)


def get_db() -> Iterator[sqlite3.Connection]:
    db = connect()
    try:
        yield db
    finally:
        db.close()


def int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _attempts(request: Request, scope: str) -> deque[float]:
    # Separate counts for the admin PIN and for players' numbers/codes: typos in the crowd never lock the admin out.
    return _failures[f"{scope}:{request.client.host if request.client else '?'}"]


def throttled(request: Request, scope: str) -> bool:
    failures = _attempts(request, scope)
    while failures and failures[0] < time.monotonic() - ATTEMPT_WINDOW_S:
        failures.popleft()
    return len(failures) >= ATTEMPT_MAX_FAILURES


def record_failure(request: Request, scope: str) -> None:
    _attempts(request, scope).append(time.monotonic())


def is_admin(request: Request) -> bool:
    return request.session.get("role") == "admin"


def my_team_id(request: Request) -> int | None:
    # A phone is in a couple as one of its two players: every way in (number, personal link, the partner's
    # invite) says which. A session without one is from an older version and logs in again.
    return request.session.get("team") if request.session.get("player") in (1, 2) else None


def my_player(request: Request) -> int | None:
    """Which of the two players holds this phone, once known."""
    return request.session.get("player")


def device_id(request: Request) -> str:
    if "device" not in request.session:
        request.session["device"] = secrets.token_hex(8)
    return request.session["device"]


def enter_team(request: Request, db: sqlite3.Connection, team_id: int, player: int) -> None:
    """This phone now belongs to one player of the couple."""
    request.session.update({"team": team_id, "player": player})
    service.register_device(db, device_id(request), team_id, player)


def team_referee_table(request: Request, db: sqlite3.Connection) -> int | None:
    """The table this couple's referee player looks after, if the couple has one (cached for the request)."""
    if not hasattr(request.state, "ref_table"):
        team_id = my_team_id(request)
        request.state.ref_table = service.referee_table(db, team_id) if team_id is not None else None
    return request.state.ref_table


def can_volunteer_referee(request: Request, m: service.MatchView, db: sqlite3.Connection) -> bool:
    """When volunteers may referee: anyone not playing in this match (nor on another table right now)."""
    if m.mode.by_couples or not service.load_config(db).volunteer_referees:
        return False
    if m.done or m.bye or m.t1 is None or m.t2 is None:
        return False
    if not (m.playing or m.started_at is not None):
        return False
    if request.session.get("role") == "referee":
        return request.session.get("field") == m.field
    team_id = my_team_id(request)
    if team_id is not None and team_id in (m.t1, m.t2):
        return False
    if playing_now(request, db):
        return False
    return True


def can_referee(request: Request, m: service.MatchView, db: sqlite3.Connection) -> bool:
    """Matches the couples score themselves have no referee; the admin may always step in."""
    if m.bye or m.t1 is None or m.t2 is None:
        return False
    if is_admin(request):
        return True
    if m.mode.by_couples:
        return False
    if request.session.get("role") == "referee":  # only the demo hands out a table this way
        return request.session.get("field") == m.field
    if m.id in request.session.get("vol_matches", []) and can_volunteer_referee(request, m, db):
        return True
    return m.field is not None and team_referee_table(request, db) == m.field


def split_sides(request: Request, m: service.MatchView) -> list[int]:
    if not m.done or m.bye:
        return []
    if is_admin(request):
        return [1, 2]
    side = m.side(my_team_id(request))
    return [side] if side else []


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(303, headers={"Location": "/login"})


def page(request: Request, db: sqlite3.Connection, name: str, **ctx: object) -> Response:
    me = None
    if my_team_id(request) is not None:
        me = service.team(db, my_team_id(request))
        if me is None:
            request.session.pop("team", None)
    base = {
        "cfg": service.load_config(db),
        "role": request.session.get("role"),
        "my_field": request.session.get("field"),
        "can_referee": lambda m: can_referee(request, m, db),
        "can_volunteer": lambda m: can_volunteer_referee(request, m, db),
        "ref_table": team_referee_table(request, db),
        "scoring_modes": service.SCORING,
        "me": me,
        "flash": request.session.pop("flash", None),
        "path": request.url.path,
        "split_sides": lambda m: split_sides(request, m),
        "payments": service.PAYMENTS,
        "server_clock": datetime.now().strftime("%H:%M:%S"),
        "server_hm": datetime.now().strftime("%H:%M"),
        "server_ts": int(time.time() * 1000),
        "demo": is_demo(db),
    }
    return templates.TemplateResponse(request, name, {**base, **ctx})


def is_demo(db: sqlite3.Connection) -> bool:
    """Set only in the demo's own database (tools/populate_demo.py): never true on the real site."""
    return get_setting(db, "demo") == "1"


def public_site_url(request: Request, db: sqlite3.Connection) -> str:
    return (get_setting(db, "public_url") or str(request.base_url)).rstrip("/")


def checkin_url(request: Request, db: sqlite3.Connection) -> str:
    base = (get_setting(db, "public_url") or str(request.base_url)).rstrip("/")
    return f"{base}/checkin?{urlencode({'k': get_setting(db, 'reg_key')})}"


def playing_now(request: Request, db: sqlite3.Connection) -> bool:
    """A couple at the table, its match started: it is playing, not following matches on the phone."""
    team_id = my_team_id(request)
    if team_id is None:
        return False
    return db.execute(
        "SELECT 1 FROM matches WHERE done = 0 AND started_at IS NOT NULL AND ? IN (team1, team2)", (team_id,)
    ).fetchone() is not None


def invite_url(request: Request, db: sqlite3.Connection) -> str | None:
    """The link this player hands the partner, so the partner enters as themselves."""
    team_id, player = my_team_id(request), my_player(request)
    if team_id is None or player is None:
        return None
    return f"{public_site_url(request, db)}/entra/{service.invite_token(db, team_id, 3 - player)}"


def referee_back(request: Request, m: service.MatchView) -> str:
    """Scoring actions land back on the scoring screen of the match; once it is over, on the table."""
    return f"/segna/{m.id}"


def table_page(request: Request, m: service.MatchView, db: sqlite3.Connection | None = None) -> str:
    if is_admin(request):
        return f"/arbitro?campo={m.field}"
    if request.session.get("role") == "referee" or (db is not None and team_referee_table(request, db) == m.field):
        return "/arbitro"
    return f"/gironi?tavolo={m.field}" if m.field else "/gironi"
