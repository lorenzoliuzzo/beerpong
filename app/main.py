from __future__ import annotations

import hashlib
import io
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import qrcode
import qrcode.image.svg
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import planning, push, service, tracking
from .db import connect, db_path, get_phase, get_setting, init_db, set_setting
from .states import LivePhase, Phase, Scoring
from .service import TournamentError

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE / "templates")
# Changes with the stylesheet, so phones never keep an old copy after a deploy.
templates.env.globals["static_v"] = hashlib.sha256((BASE / "static" / "style.css").read_bytes()).hexdigest()[:10]
templates.env.globals["team_colors"] = service.COLORS
templates.env.globals.update(Phase=Phase, Scoring=Scoring, LivePhase=LivePhase)
router = APIRouter()
log = logging.getLogger("beerpong")

REPORT_MIN_GAP_S = 0.7
REPORT_MAX_PER_MINUTE = 40
_report_times: dict[str, deque[float]] = defaultdict(deque)
ATTEMPT_WINDOW_S = 600
# Phones at the venue often share one carrier address: a few typos must not lock everyone out of check-in.
ATTEMPT_MAX_FAILURES = 30
_failures: dict[str, deque[float]] = defaultdict(deque)
_results_lock = threading.Lock()


def get_db() -> Iterator[sqlite3.Connection]:
    db = connect()
    try:
        yield db
    finally:
        db.close()


def _int(value: str | None) -> int | None:
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


def _throttled(request: Request, scope: str) -> bool:
    failures = _attempts(request, scope)
    while failures and failures[0] < time.monotonic() - ATTEMPT_WINDOW_S:
        failures.popleft()
    return len(failures) >= ATTEMPT_MAX_FAILURES


def _record_failure(request: Request, scope: str) -> None:
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


# --- Public tabs -----------------------------------------------------------


@router.get("/")
def home(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return groups_page(request, db=db)


@router.get("/live")
def live_tab_redirect() -> Response:
    return redirect("/")


@router.get("/gironi")
def groups_page(
    request: Request,
    g: str = "",
    tavolo: str = "",
    sez: str = "",
    archivio: str = "",
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    all_views = service.matches(db)
    tables = service.group_tables(db)
    cfg = service.load_config(db)
    t_views = service.table_views(db, g_tables=tables, all_matches=all_views)
    team_id = my_team_id(request)
    me_team = service.team(db, team_id) if team_id else None
    is_ko_phase = cfg.phase.knockout_reached and not archivio
    my_table = None
    if team_id and is_ko_phase:
        my_table = next(
            (tv.field for tv in t_views if any(team_id in (m.t1, m.t2) for m in tv.phase_matches)), None
        )
    if my_table is None and me_team and me_team["grp"] is not None and tables:
        my_table = next((tv.field for tv in t_views if any(gt.grp == me_team["grp"] for gt in tv.groups)), None)

    if tavolo.isdecimal() and any(tv.field == int(tavolo) for tv in t_views):
        active_tavolo = int(tavolo)
    elif my_table and any(tv.field == my_table for tv in t_views):
        active_tavolo = my_table
    elif t_views:
        active_tavolo = t_views[0].field
    else:
        active_tavolo = 1

    if g == "all":
        active_grp = "all"
    elif g.isdecimal() and any(t.grp == int(g) for t in tables):
        active_grp = f"g{int(g)}"
    elif me_team and me_team["grp"] is not None:
        active_grp = f"g{me_team['grp']}"
    elif tables:
        active_grp = f"g{tables[0].grp}"
    else:
        active_grp = "all"

    if sez in ("tavoli", "classifiche"):
        active_sez = sez
    elif g:
        active_sez = "classifiche"
    else:
        active_sez = "tavoli"

    setup_tables = None
    if cfg.phase.is_setup:
        board, _ = service.board_groups(db)
        setup_tables = service.board_tables(db, board) or ([(0, board)] if board else None)
        spot = service.provisional_spot(db, team_id, datetime.now()) if team_id else None
        if not tavolo.isdecimal() and spot and spot.table:
            active_tavolo = spot.table

    ko = service.knockout_rounds(db)
    dash = service.team_dashboard(db, team_id) if team_id else None
    t_liters, t_cups_live, _ = service.beer_stats(db, all_views)
    done_matches = [m for m in all_views if m.done and not m.bye]
    total_valid_matches = len([m for m in all_views if not m.bye])
    all_group_fields = sorted({m.field for m in all_views if m.stage == "group" and m.field}) or list(range(1, cfg.fields + 1))
    active_fields = {tv.field for tv in t_views}
    removed_tables = [f for f in all_group_fields if f not in active_fields] if is_ko_phase else []
    current_phase_label = t_views[0].phase_label if t_views else ("Fase a Gironi" if cfg.phase is Phase.GROUPS else "Eliminazione diretta")

    return page(
        request, db, "gironi.html", tables=tables, table_views=t_views, T=service.teams(db), setup_tables=setup_tables,
        active_grp=active_grp, active_tavolo=active_tavolo, active_sez=active_sez, my_table=my_table,
        thirds=service.thirds_needed(db), progress=service.tournament_progress(db),
        projection=service.projected_first_round(db), labels=service.standing_labels(db),
        ko_plan=service.planned_knockout(db) if cfg.phase is Phase.GROUPS else [],
        dash=dash, overview=service.checkin_overview(db), final=ko[-1][1][0] if ko else None,
        tournament_liters=t_liters, total_cups=t_cups_live,
        played_count=len(done_matches), total_matches=total_valid_matches,
        is_ko_phase=is_ko_phase, current_phase_label=current_phase_label, removed_tables=removed_tables,
        live=True,
    )


def photo_dir() -> Path:
    return db_path().parent / "foto"


@router.get("/arrivi")
def arrivals_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "arrivi.html", arr=service.arrivals(db), live=True)


@router.get("/foto/{name}")
def team_photo(name: str) -> Response:
    if not re.fullmatch(r"\d+-[0-9a-f]{12}\.(jpg|png|webp)", name) or not (photo_dir() / name).is_file():
        raise HTTPException(404)
    return FileResponse(photo_dir() / name, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/io/profilo")
def look_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    t = service.team(db, my_team_id(request)) if my_team_id(request) is not None else None
    if t is None:
        return redirect("/checkin")
    return page(request, db, "profilo.html", t=t, colors=service.COLORS, me_player=my_player(request))


@router.post("/io/profilo")
def look_save(
    request: Request, emoji: str = Form(""), motto: str = Form(""), colore: str = Form(""),
    nick1: str = Form(""), nick2: str = Form(""), db: sqlite3.Connection = Depends(get_db),
) -> Response:
    team_id = my_team_id(request)
    if team_id is None:
        return redirect("/checkin")
    try:
        service.save_look(db, team_id, emoji, motto, colore, nick1, nick2)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/io/profilo")
    flash(request, "Profilo aggiornato ✓")
    return redirect("/io")


@router.post("/io/coppia")
def couple_save(
    request: Request, name: str = Form(""), p1: str = Form(""), p2: str = Form(""),
    phone1: str = Form(""), phone2: str = Form(""), db: sqlite3.Connection = Depends(get_db),
) -> Response:
    team_id = my_team_id(request)
    if team_id is None:
        return redirect("/checkin")
    try:
        service.update_couple(db, team_id, name, p1, p2, phone1, phone2)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/io/profilo#coppia")
    flash(request, "Dati della coppia aggiornati ✓")
    return redirect("/io")


@router.post("/io/foto")
async def photo_upload(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    team_id = my_team_id(request)
    if team_id is None:
        return JSONResponse({"error": "Entrate prima nel profilo della coppia."}, status_code=403)
    # Refuse big bodies before reading them: the phone sends a shrunk JPEG of a few hundred KB at most.
    if int(request.headers.get("content-length") or 0) > 2 * service.PHOTO_MAX_BYTES:
        return JSONResponse({"error": "Foto troppo grande."}, status_code=413)
    form = await request.form()
    upload = form.get("foto")
    data = await upload.read() if upload is not None and hasattr(upload, "read") else b""
    try:
        name = service.save_photo(db, team_id, data, photo_dir())
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "url": f"/foto/{name}"})


@router.post("/io/foto/togli")
def photo_remove(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    if my_team_id(request) is not None:
        service.remove_photo(db, my_team_id(request), photo_dir())
    return redirect("/io/profilo")


@router.get("/regole")
def rules_page(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "regole.html", info=service.load_tournament(db), places=service.registration(db))


@router.get("/programma")
def schedule(request: Request, team: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    team_id = _int(team)
    views = service.matches(db)
    if team_id:
        views = [m for m in views if team_id in (m.t1, m.t2)]
    plan = service.planned_schedule(db, datetime.now()) if not views else None
    return page(request, db, "programma.html", slots=service.by_slot(views), plan=plan, team_id=team_id, live=True)


@router.get("/classifiche")
def rankings(request: Request, vista: str = "", db: sqlite3.Connection = Depends(get_db)) -> Response:
    tables = service.group_tables(db)
    rounds = service.knockout_rounds(db)
    view = vista if vista in ("gironi", "eliminazione", "coppie", "giocatori", "tiri") else "gironi"
    team_stats, scorers = service.team_ranking(db), service.scorers(db)
    t_liters, t_cups_live, _ = service.beer_stats(db)
    team_id = my_team_id(request)
    mine = None
    if team_id is not None:
        mine = {
            "cups_pos": next((i for i, s in enumerate(team_stats, 1) if s.team == team_id and s.played), None),
            "players": [(i, s) for i, s in enumerate(scorers, 1) if s.team_id == team_id and s.cups],
        }
    return page(
        request, db, "classifiche.html", view=view, tables=tables, rounds=rounds,
        thirds=service.thirds_needed(db), T=service.teams(db),
        labels=service.standing_labels(db), projection=service.projected_first_round(db),
        mine=mine, scorers=scorers, team_stats=team_stats,
        shots=tracking.shot_stats(db) if view == "tiri" else None,
        has_shots=db.execute("SELECT 1 FROM reports LIMIT 1").fetchone() is not None,
        tournament_liters=t_liters, total_cups_live=t_cups_live,
        live=True,
    )


@router.get("/tabellone")
def knockout(request: Request, vista: str = "", db: sqlite3.Connection = Depends(get_db)) -> Response:
    rounds = service.knockout_rounds(db)
    ko = [m for _, ms in rounds for m in ms if not m.bye]
    plan = [] if ko else service.planned_knockout(db)
    by_table: dict[int, list] = defaultdict(list)
    for m in sorted(ko, key=lambda m: m.slot or 0) or plan:
        if m.field:
            by_table[m.field].append(m)
    return page(
        request, db, "tabellone.html", rounds=rounds, view="tavoli" if vista == "tavoli" else "turni",
        by_table=sorted(by_table.items()), plan=plan,
        table_views=service.table_views(db), tables=service.group_tables(db),
        thirds=service.thirds_needed(db), progress=service.tournament_progress(db),
        labels=service.standing_labels(db), projection=service.projected_first_round(db), T=service.teams(db),
        live=True,
    )


@router.get("/cartellone")
def board(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    groups, provisional = service.board_groups(db)
    views = service.matches(db)
    now, upcoming = service.live(views)
    ko = service.knockout_rounds(db)
    # Two or more groups per table: show the board as one column per table, with what is playing there.
    live_tables = {} if provisional else {tv.field: tv for tv in service.table_views(db)}
    tables = [(f, gs, live_tables.get(f)) for f, gs in service.board_tables(db, groups) or []] or None
    return page(
        request, db, "cartellone.html", groups=groups, provisional=provisional, now=now, upcoming=upcoming,
        tables=tables,
        rounds=service.board_bracket(db, len(groups)), progress=service.tournament_progress(db),
        thirds=planning.bracket_plan(len(groups))[1] - 2 * len(groups) if groups else 0,
        final=ko[-1][1][0] if ko else None, site=public_site_url(request, db), live=True,
        # The TV the organizers open shows the check-in QR while couples arrive: one QR at the venue.
        checkin_qr=is_admin(request) and get_phase(db).is_setup,
        venue_code=service.venue_code(db) if is_admin(request) else None,
    )


SERVICE_WORKER = """
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("push", (e) => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || "Beer Pong", {
    body: d.body || "", icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    data: { url: d.url || "/" }, tag: d.url || "bp", renotify: true, vibrate: [200, 100, 200],
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = e.notification.data.url;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) if ("focus" in w) { w.navigate(url); return w.focus(); }
    return self.clients.openWindow(url);
  }));
});
"""


@router.get("/sw.js")
def service_worker() -> Response:
    # Served from the root so it controls the whole site; no-cache so a new version is picked up at once.
    return Response(SERVICE_WORKER, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@router.get("/manifest.webmanifest")
def manifest(db: sqlite3.Connection = Depends(get_db)) -> Response:
    return JSONResponse({
        "name": get_setting(db, "title"), "short_name": "Beer Pong", "start_url": "/", "display": "standalone",
        "background_color": "#12100d", "theme_color": "#b93d0b",
        "icons": [{"src": f"/static/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png"} for s in (192, 512)],
    }, media_type="application/manifest+json")


@router.get("/push/chiave")
def push_key(db: sqlite3.Connection = Depends(get_db)) -> Response:
    return JSONResponse({"key": push.public_key(db)})


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=16, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class PushSubscription(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: PushKeys


@router.post("/push/iscrivi")
def push_subscribe(request: Request, sub: PushSubscription, db: sqlite3.Connection = Depends(get_db)) -> Response:
    try:
        push.subscribe(db, sub.endpoint, sub.keys.p256dh, sub.keys.auth, my_team_id(request))
    except TournamentError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/push/disiscrivi")
def push_unsubscribe(endpoint: str = Body(..., embed=True), db: sqlite3.Connection = Depends(get_db)) -> Response:
    push.unsubscribe(db, endpoint)
    return JSONResponse({"ok": True})


@router.get("/qr-sito.svg")
def site_qr(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    img = qrcode.make(public_site_url(request, db), image_factory=qrcode.image.svg.SvgPathImage, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


def _team_page(request: Request, db: sqlite3.Connection, team_id: int, vs: str | None, own: bool) -> Response:
    dash = service.team_dashboard(db, team_id)
    if dash is None:
        raise HTTPException(404)
    profile = service.team_profile(db, team_id)
    other = _int(vs)
    h2h = service.head_to_head(db, team_id, other) if other and other != team_id else None
    rivals = [t for t in service.teams(db).values() if t["checked_in"] and t["id"] != team_id]
    spot = None
    if own and get_phase(db).is_setup:
        spot = service.provisional_spot(db, team_id, datetime.now())
    return page(
        request, db, "squadra.html", dash=dash, profile=profile, T=service.teams(db), own=own, spot=spot,
        thirds=service.thirds_needed(db), h2h=h2h, other=other, rivals=rivals, live=True,
        me_player=my_player(request) if own else None, checkin_ok=request.session.get("checkin_ok", False),
        devices=service.team_devices(db, team_id) if own else None,
        invite=invite_url(request, db) if own else None,
    )


@router.get("/io")
def my_profile(request: Request, vs: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    team_id = my_team_id(request)
    if team_id is None or service.team(db, team_id) is None:
        t_liters, t_cups_live, _ = service.beer_stats(db)
        return page(
            request, db, "io.html", teams=service.teams(db),
            tournament_liters=t_liters, total_cups_live=t_cups_live, live=True,
        )
    return _team_page(request, db, team_id, vs, own=True)


@router.get("/squadra/{team_id}")
def team_page(request: Request, team_id: int, vs: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    if team_id == my_team_id(request):
        return redirect("/io" + (f"?vs={vs}" if vs else ""))
    return _team_page(request, db, team_id, vs, own=False)


@router.get("/squadra/{team_id}/statistiche")
def team_stats(request: Request, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    dash = service.team_dashboard(db, team_id)
    if dash is None:
        raise HTTPException(404)
    own = team_id == my_team_id(request)
    return page(
        request, db, "statistiche.html", dash=dash, profile=service.team_profile(db, team_id), own=own,
        me_player=my_player(request) if own else None,
    )


# --- Live tracking ---------------------------------------------------------


def server_now() -> float:
    """The one clock every phone follows (wall time on the Pi)."""
    return time.time()


def _report_allowed(request: Request, device: str) -> bool:
    """Any phone may report, so cap the rate per device and per address."""
    now = time.monotonic()
    for key in (f"d:{device}", f"ip:{request.client.host if request.client else '?'}"):
        times = _report_times[key]
        while times and times[0] < now - 60:
            times.popleft()
        limit = REPORT_MAX_PER_MINUTE * (1 if key.startswith("d:") else 3)
        if len(times) >= limit or (key.startswith("d:") and times and now - times[-1] < REPORT_MIN_GAP_S):
            return False
    for key in (f"d:{device}", f"ip:{request.client.host if request.client else '?'}"):
        _report_times[key].append(now)
    return True


@router.get("/live/{match_id}")
def live_page(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None or m.bye or m.t1 is None or m.t2 is None:
        raise HTTPException(404)
    device_id(request)
    return page(request, db, "live.html", m=m)


def cheer_account(request: Request) -> str:
    """Who is cheering: the couple when logged in (all its phones count once), else this browser."""
    team_id = my_team_id(request)
    return f"team:{team_id}" if team_id is not None else f"device:{device_id(request)}"


def playing_now(request: Request, db: sqlite3.Connection) -> bool:
    """A couple at the table, its match started: it is playing, not following matches on the phone."""
    team_id = my_team_id(request)
    if team_id is None:
        return False
    return db.execute(
        "SELECT 1 FROM matches WHERE done = 0 AND started_at IS NOT NULL AND ? IN (team1, team2)", (team_id,)
    ).fetchone() is not None


PLAYING_MSG = "State giocando: occhi sul tavolo! 🏓 Il live lo seguite dopo."


def _live_json(request: Request, db: sqlite3.Connection, match_id: int, device: str | None) -> JSONResponse:
    data = tracking.live_view(db, match_id, server_now())
    data["my_cheers"] = tracking.my_cheers(db, match_id, cheer_account(request) if device else None)
    data["you_play"] = playing_now(request, db)
    return JSONResponse(data)


@router.get("/api/live/{match_id}")
def live_state(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    try:
        return _live_json(request, db, match_id, request.session.get("device"))
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


class CheerIn(BaseModel):
    side: int
    emoji: str = Field(max_length=8)


@router.post("/api/live/{match_id}/tifo")
def live_cheer(request: Request, match_id: int, body: CheerIn, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    device = device_id(request)
    if playing_now(request, db):
        return JSONResponse({"error": PLAYING_MSG}, status_code=403)
    if not _report_allowed(request, device):
        return JSONResponse({"error": "Piano! 🍻"}, status_code=429)
    try:
        tracking.cheer(db, match_id, body.side, body.emoji, cheer_account(request), server_now())
        return _live_json(request, db, match_id, device)
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# --- Check-in --------------------------------------------------------------


@router.get("/checkin")
def checkin_list(request: Request, k: str = "", db: sqlite3.Connection = Depends(get_db)) -> Response:
    if k:
        if not service.checkin_key_ok(db, k):
            return redirect("/checkin")
        request.session["checkin_ok"] = True
        # Already in the profile (link after the payment): the QR at the venue is the arrival itself.
        t = service.team(db, my_team_id(request)) if my_team_id(request) is not None else None
        if t is not None:
            if my_player(request) is not None:
                arrive(request, db, t, {my_player(request)})
            return redirect("/io")
        return redirect("/checkin")
    return page(
        request, db, "checkin.html", teams=service.teams(db), allowed=request.session.get("checkin_ok", False)
    )


@router.post("/checkin/accedi")
def checkin_phone(
    request: Request, phone: str = Form(""), team_id: str | None = Form(None), db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Each player types their own number: they are in the couple's profile as themselves and, with the QR of
    the venue scanned, arrived."""
    if _throttled(request, "players"):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/checkin")
    found = service.teams_by_phone(db, phone)
    if _int(team_id) is not None:
        found = [t for t in found if t["id"] == _int(team_id)]
    if not found:
        if service.is_unpaired_guest(db, phone):
            flash(request, "Sei iscritto «solo serata»: se giocate in coppia, completate qui.")
            return redirect("/checkin/nuova")
        _record_failure(request, "players")
        flash(request, "Numero non trovato: se il tuo non è tra quelli dell'iscrizione, fatti invitare dal compagno "
                       "(ha il QR nel suo profilo) o cerca la coppia nell'elenco.", "err")
        return redirect("/checkin")
    if len(found) > 1:
        return page(request, db, "checkin_scegli.html", found=found, phone=phone)
    t = found[0]
    player = service.player_for_phone(t, phone)
    if service.entered_elsewhere(db, t["id"], device_id(request)):
        # A number is easy to know: it opens a couple only for its first phone, then the partner invites.
        flash(request, f"«{t['name']}» è già aperta su un altro telefono: chiedi al compagno il QR «Invita» "
                       "dal suo profilo, oppure passa dallo staff.", "err")
        return redirect("/checkin")
    if not t["checked_in"]:
        if get_phase(db).started:
            flash(request, f"Il torneo è già iniziato e «{t['name']}» non ha fatto il check-in: parlate con l'organizzazione.", "err")
            return redirect("/checkin")
        if request.session.get("checkin_ok"):
            arrive(request, db, t, {player})  # in the profile even if not yet: there they fix what is missing
        elif service.may_enter_early(t):
            flash(request, f"Ciao {t[f'p{player}']}! {AT_VENUE}")
        else:
            flash(request, "Il vostro posto si conferma con il pagamento: appena arriva vi mandiamo il link per entrare.", "err")
            return redirect("/checkin")
    else:
        flash(request, f"Bentornato/a, {t[f'p{player}']}!")
    enter_team(request, db, t["id"], player)
    return redirect("/io")  # the questionnaire is an invitation on the profile, never a gate before it


AT_VENUE = "Alla serata inquadra il QR all'ingresso, o scrivi nel profilo il codice della serata: ti segna arrivato/a."


def arrive(request: Request, db: sqlite3.Connection, t: sqlite3.Row, players: set[int]) -> bool:
    """These players are at the venue; the couple is present for the draw once both are.
    False (with the reason flashed) when it cannot be."""
    if t["checked_in"]:
        flash(request, f"Bentornati, {t['name']}!")
        return True
    if get_phase(db).started:
        flash(request, f"Il torneo è già iniziato e «{t['name']}» non ha fatto il check-in: parlate con l'organizzazione.", "err")
        return False
    try:
        both = service.players_arrive(db, t["id"], players)
    except TournamentError as e:
        flash(request, str(e), "err")
        return False
    if both:
        flash(request, f"Check-in fatto ✓ «{t['name']}» è al completo: ritirate la prima birra 🍺")
    else:
        missing = t["p2"] if players == {1} else t["p1"]
        flash(request, f"Ci sei ✓ Manca {missing}: quando arriva tocca «Sono arrivato/a» dal suo telefono, "
                       "o confermalo tu dal profilo.")
    return True


@router.post("/io/arrivo")
def arrival(
    request: Request, codice: str = Form(""), compagno: str = Form(""), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    """A player in the profile says they are here: the QR of the venue scanned on this phone, or its 4-digit
    code, proves it. A player already here may confirm the partner who has no phone."""
    t = service.team(db, my_team_id(request)) if my_team_id(request) is not None else None
    player = my_player(request)
    if t is None or player is None:
        return redirect("/checkin")
    if not request.session.get("checkin_ok"):
        if _throttled(request, "players"):
            flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
            return redirect("/io")
        if not service.venue_code_ok(db, codice):
            _record_failure(request, "players")
            flash(request, "Codice sbagliato: è il numero di 4 cifre sotto il QR all'ingresso e sulla TV.", "err")
            return redirect("/io")
        request.session["checkin_ok"] = True
    arrive(request, db, t, {player, 3 - player} if compagno else {player})
    return redirect("/io")


@router.post("/io/birra")
def first_beer(request: Request, giocatore: int = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    team_id = my_team_id(request)
    if team_id is None:
        return redirect("/checkin")
    try:
        service.take_beer(db, team_id, giocatore)
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect("/io")


@router.get("/accesso/{token}")
def personal_access(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The link sent with the payment confirmation: it opens the couple's profile as its first player."""
    try:
        t, player = service.read_access(db, token)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/checkin")
    enter_team(request, db, t["id"], player)
    flash(request, f"Ciao {t[f'p{player}']}! Questo telefono ora è il tuo in «{t['name']}». {AT_VENUE}")
    return redirect("/io")


@router.get("/questionario")
def questionnaire_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    t = service.team(db, my_team_id(request)) if my_team_id(request) is not None else None
    if t is None:
        return redirect("/checkin")
    return page(request, db, "questionario.html", t=t, levels=service.LEVELS, goals=service.GOALS)


@router.post("/questionario")
def questionnaire_save(
    request: Request, livello: str = Form(""), mira1: int = Form(-1), mira2: int = Form(-1), obiettivo: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    team_id = my_team_id(request)
    if team_id is None:
        return redirect("/checkin")
    try:
        service.save_questionnaire(db, team_id, livello, mira1, mira2, obiettivo)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/questionario")
    flash(request, "Grazie! Adesso vediamo se avete ragione 😉")
    return redirect("/io")


@router.post("/demo/coppia")
def demo_as_couple(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    if not is_demo(db):
        raise HTTPException(404)
    _, upcoming = service.live(service.matches(db))
    # A couple that plays next, so its notices arrive within a couple of minutes.
    soon = [t for m in upcoming for t in (m.t1, m.t2) if t is not None]
    present = [t["id"] for t in service.teams(db).values() if t["checked_in"]]
    pool = soon or present
    if not pool:
        raise HTTPException(404)
    team_id = secrets.choice(pool)
    request.session.pop("role", None)
    request.session.pop("field", None)
    enter_team(request, db, team_id, 1)
    flash(request, f"Siete «{service.team(db, team_id)['name']}»: attivate le notifiche, tra poco tocca a voi!")
    return redirect("/io")


@router.post("/demo/arbitro")
def demo_as_referee(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The table nobody has refereed for the longest: the simulator leaves it to this visitor."""
    if not is_demo(db):
        raise HTTPException(404)
    fields = range(1, service.load_config(db).fields + 1)
    field_no = min(fields, key=lambda f: float(get_setting(db, f"demo_ref_{f}") or 0))
    _claim_demo_table(db, field_no)
    request.session.pop("team", None)
    request.session.update({"role": "referee", "field": field_no})
    flash(request, f"Sei l'arbitro del Tavolo {field_no}: qui segni tu, il simulatore si ferma su questo tavolo.")
    return redirect("/arbitro")


@router.post("/demo/admin")
def demo_as_admin(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    if not is_demo(db):
        raise HTTPException(404)
    request.session.pop("team", None)
    request.session.update({"role": "admin", "field": None})
    return redirect("/admin")


def _claim_demo_table(db: sqlite3.Connection, field_no: int | None) -> None:
    """In the demo a person refereeing a table keeps the simulator off it (tools/demo_sim.py)."""
    if field_no is not None and is_demo(db):
        with db:
            set_setting(db, f"demo_ref_{field_no}", str(time.time()))


@router.get("/checkin/nuova")
def guest_team_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "checkin_nuova.html", allowed=request.session.get("checkin_ok", False))


@router.post("/checkin/nuova")
def guest_team_save(
    request: Request,
    phone1: str = Form(""),
    name: str = Form(""),
    p1: str = Form(""),
    p2: str = Form(""),
    phone2: str = Form(""),
    payment: str = Form(""),
    insieme: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    if not request.session.get("checkin_ok"):
        flash(request, "Si fa alla serata: inquadrate il QR all'ingresso.", "err")
        return redirect("/checkin/nuova")
    if _throttled(request, "players"):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/checkin/nuova")
    try:
        team_id = service.register_guest_team(db, phone1, name, p1, p2, phone2, payment)
    except TournamentError as e:
        _record_failure(request, "players")
        flash(request, str(e), "err")
        return redirect("/checkin/nuova")
    enter_team(request, db, team_id, 1)  # the guest who fills the form is the first player
    arrive(request, db, service.team(db, team_id), {1, 2} if insieme else {1})
    return redirect("/io")


@router.get("/checkin/{team_id}")
def checkin_team(request: Request, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """A couple picked from the list: its player enters with their own number, which says who they are."""
    t = service.team(db, team_id)
    if t is None:
        raise HTTPException(404)
    return page(request, db, "checkin_team.html", t=t, allowed=request.session.get("checkin_ok", False))


@router.post("/esci-squadra")
def leave_team(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    request.session.pop("team", None)
    request.session.pop("player", None)
    service.forget_device(db, device_id(request))
    return redirect("/")


def invite_url(request: Request, db: sqlite3.Connection) -> str | None:
    """The link this player hands the partner, so the partner enters as themselves."""
    team_id, player = my_team_id(request), my_player(request)
    if team_id is None or player is None:
        return None
    return f"{public_site_url(request, db)}/entra/{service.invite_token(db, team_id, 3 - player)}"


@router.get("/io/invito.svg")
def invite_qr(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    url = invite_url(request, db)
    if url is None:
        raise HTTPException(404)
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/entra/{token}")
def accept_invite(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    try:
        team_id, player = service.read_invite(db, token)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/checkin")
    enter_team(request, db, team_id, player)
    t = service.team(db, team_id)
    flash(request, f"Ciao {t[f'p{player}']}! Siete in «{t['name']}»: questo telefono ora è il tuo.")
    return redirect("/io")


# --- Player cup split ------------------------------------------------------


@router.get("/canestri/{match_id}")
def split_form(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    sides = split_sides(request, m)
    if not sides:
        flash(request, "Puoi dividere i bicchieri solo per le partite della tua coppia, dopo il risultato.", "err")
        return redirect("/")
    return page(request, db, "canestri.html", m=m, sides=sides, T=service.teams(db), hits=service.cup_hits(db, match_id))


@router.post("/canestri/{match_id}")
async def split_save(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    form = await request.form()
    side = _int(str(form.get("side") or ""))
    if side not in split_sides(request, m):
        return redirect("/")
    players = {
        int(k.removeprefix("cup")): _int(str(v)) or 0
        for k, v in form.items()
        if k.startswith("cup") and k.removeprefix("cup").isdecimal()
    }
    try:
        service.assign_cup_players(db, match_id, side, players)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/canestri/{match_id}")
    flash(request, "Bicchieri assegnati.")
    return redirect("/io" if my_team_id(request) else "/admin")


# --- Staff -----------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "login.html")


@router.post("/login")
def login(request: Request, pin: str = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    if _throttled(request, "admin"):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/login")
    if not service.is_admin_pin(db, pin.strip()):
        _record_failure(request, "admin")
        flash(request, "PIN non valido.", "err")
        return redirect("/login")
    request.session.update({"role": "admin", "field": None})
    return redirect("/admin")


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.pop("role", None)
    request.session.pop("field", None)
    return redirect("/")


@router.get("/arbitro")
def referee(request: Request, campo: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    role = request.session.get("role")
    own_table = team_referee_table(request, db) if role is None else None
    if role not in ("admin", "referee") and own_table is None:
        return redirect("/login")
    field_no = request.session.get("field") if role == "referee" else own_table or _int(campo)
    all_views = service.matches(db)
    views = [m for m in all_views if m.slot is not None and (field_no is None or m.field == field_no)]
    # The same "on the table now" as everywhere else, so a referee never starts a couple still playing elsewhere.
    now_ids = {m.id for m in all_views if m.playing}
    current = [m for m in views if m.id in now_ids]
    pending = [m for m in views if not m.done and m.id not in now_ids]
    pending = sorted(pending, key=lambda m: (m.time or "", m.field or 0))  # each table runs its own queue
    return page(
        request, db, "arbitro.html", current=current, pending=pending,
        played=[m for m in views if m.done][::-1], field_no=field_no, live=True,
    )


def _refereed(
    request: Request, db: sqlite3.Connection, match_id: int, shots: bool = True
) -> service.MatchView | None:
    """The match, when this phone referees it; the shot by shot actions only for matches scored that way."""
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m, db) or (shots and not m.mode.by_shots):
        return None
    _claim_demo_table(db, m.field)
    return m


@router.post("/partita/{match_id}/inizia")
def match_start(
    request: Request, match_id: int, primo: int = Form(1), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    m = _refereed(request, db, match_id, shots=False)
    if m is None:
        return redirect("/login")
    try:
        service.start_match(db, match_id, time.time(), primo)
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect(_referee_back(request, m))


@router.post("/partita/{match_id}/tiro")
def match_shot(
    request: Request, match_id: int, giocatore: int = Form(...), bicchiere: str = Form(...),
    n: int | None = Form(None), db: sqlite3.Connection = Depends(get_db),
) -> Response:
    m = _refereed(request, db, match_id)
    if m is None:
        return redirect("/login")
    blown = bicchiere == "soffio"
    try:
        cup = 0 if blown else int(bicchiere)
        tracking.referee_shot(db, match_id, giocatore, cup, time.time(), n, blown)
    except ValueError:
        flash(request, "Tiro non valido.", "err")
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect(_referee_back(request, m))


@router.post("/partita/{match_id}/riordina")
def match_rerack(
    request: Request, match_id: int, lato: int = Form(...), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    m = _refereed(request, db, match_id)
    if m is None:
        return redirect("/login")
    try:
        tracking.referee_rerack(db, match_id, lato, time.time())
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect(_referee_back(request, m))


@router.post("/partita/{match_id}/annulla-tiro")
def match_undo_shot(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = _refereed(request, db, match_id)
    if m is None:
        return redirect("/login")
    try:
        tracking.referee_undo(db, match_id)
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect(_referee_back(request, m))


@router.post("/partita/{match_id}/lati")
def match_swap_sides(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Mirror left and right on this phone, to match where the couples stand at the table."""
    m = _refereed(request, db, match_id)
    if m is None:
        return redirect("/login")
    flipped = set(request.session.get("flip", []))
    flipped ^= {match_id}
    request.session["flip"] = sorted(flipped)[-20:]
    return redirect(_referee_back(request, m))


@router.post("/partita/{match_id}/annulla-inizio")
def match_undo_start(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m, db):
        return redirect("/login")
    service.undo_start(db, match_id)
    return redirect(_table_page(request, m, db))


def _referee_back(request: Request, m: service.MatchView) -> str:
    """Scoring actions land back on the scoring screen of the match; once it is over, on the table."""
    return f"/segna/{m.id}"


def _table_page(request: Request, m: service.MatchView, db: sqlite3.Connection | None = None) -> str:
    if is_admin(request):
        return f"/arbitro?campo={m.field}"
    if request.session.get("role") == "referee" or (db is not None and team_referee_table(request, db) == m.field):
        return "/arbitro"
    return f"/gironi?tavolo={m.field}" if m.field else "/gironi"


@router.post("/partita/{match_id}/arbitra")
def match_volunteer_referee(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Any non-playing person offers to referee this match and record all shots."""
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m, db) and not can_volunteer_referee(request, m, db):
        flash(request, "Può arbitrare chi non è in campo in questo momento.", "err")
        return redirect(f"/gironi?tavolo={m.field}" if m.field else "/gironi")
    vols = set(request.session.get("vol_matches", []))
    vols.add(match_id)
    request.session["vol_matches"] = sorted(vols)[-20:]
    _claim_demo_table(db, m.field)
    return redirect(f"/segna/{match_id}")


@router.get("/segna/{match_id}")
def scoring(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The referee's scoring screen for one match, on its own: nothing but the table and the shots."""
    m = _refereed(request, db, match_id, shots=False)
    if m is None:
        return redirect("/login")
    if m.done or not m.mode.by_shots:
        return redirect(_table_page(request, m, db) if m.done else f"/risultato/{match_id}")
    scoring = {m.id: tracking.referee_view(db, m.id)} if m.started_at is not None else {}
    return page(
        request, db, "segna.html", m=m, scoring=scoring, players=service.teams(db),
        flipped=set(request.session.get("flip", [])), now_ts=time.time(),
        official_counts={k: tracking.official_count(v) for k, v in scoring.items()},
        back_url=_table_page(request, m, db),
    )


@router.get("/partita/{match_id}")
def match_form(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not m.mode.by_shots and can_enter_result(request, m, db):
        return redirect(f"/risultato/{match_id}")
    if not can_referee(request, m, db):
        return redirect("/login")
    return page(request, db, "partita.html", m=m, live_score=None if m.done else tracking.live_score(db, match_id))


# --- Result entered by the couples themselves ------------------------------


def can_enter_result(request: Request, m: service.MatchView, db: sqlite3.Connection) -> bool:
    """Who fills in the result form (cups sunk and where): the two couples when they score themselves, the
    referee when it enters the final result. Shot by shot, the referee confirms from its own screen."""
    if m.bye or m.t1 is None or m.t2 is None:
        return False
    if is_admin(request):
        return True
    if m.mode.by_couples:
        return m.side(my_team_id(request)) is not None
    return m.mode is Scoring.RISULTATO and can_referee(request, m, db)


@router.get("/risultato/{match_id}")
def result_form(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_enter_result(request, m, db):
        flash(request, "Il risultato lo inseriscono le due coppie della partita." if m.mode.by_couples
              else "Il risultato di questa partita lo inserisce l'arbitro del tavolo.", "err")
        return redirect(f"/gironi?tavolo={m.field}" if m.field else "/gironi")
    hits = {side: set(h) for side, h in service.cup_hits(db, match_id).items()}
    cups = service.cups_in_play(db, match_id)
    rows = [[7, 8, 9, 10], [4, 5, 6], [2, 3], [1]] if cups == 10 else [[4, 5, 6], [2, 3], [1]]
    return page(
        request, db, "risultato.html", m=m, hits=hits, rows=rows, cups=cups, T=service.teams(db),
        my_side=m.side(my_team_id(request)),
    )


@router.post("/risultato/{match_id}")
async def result_save(request: Request, match_id: int, background: BackgroundTasks, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_enter_result(request, m, db):
        return redirect("/")
    form = await request.form()
    cups = service.cups_in_play(db, match_id)
    hits: dict[int, set[int]] = {}
    for side in (1, 2):
        hits[side] = {int(c) for c in form.getlist(f"c{side}") if str(c).isdecimal() and 1 <= int(c) <= cups}
    try:
        # Both couples may confirm the same match at once: the last group result must build the bracket only once.
        with _results_lock:
            service.record_result(db, match_id, len(hits[1]), len(hits[2]), _int(str(form.get("vincitore") or "")))
            service.save_cup_hits(db, match_id, hits)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/risultato/{match_id}")
    background.add_task(push.send, push.due(db))
    msg = f"Risultato salvato: {m.n1} {len(hits[1])}–{len(hits[2])} {m.n2}"
    if m.stage == "group" and get_phase(db) is Phase.KNOCKOUT:
        msg += ". Gironi conclusi: tabellone generato!"
    flash(request, msg)
    if m.side(my_team_id(request)):
        return redirect(f"/canestri/{match_id}")
    return redirect(_table_page(request, m, db))


@router.post("/partita/{match_id}")
def match_save(
    request: Request,
    match_id: int,
    background: BackgroundTasks,
    s1: int = Form(...),
    s2: int = Form(...),
    vincitore: str | None = Form(None),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m, db):
        return redirect("/login")
    try:
        with _results_lock:
            service.record_result(db, match_id, s1, s2, _int(vincitore))
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/partita/{match_id}")
    tracking.apply_live_splits(db, match_id)
    tracking.save_shot_hits(db, match_id)
    background.add_task(push.send, push.due(db))
    msg = f"Confermato: {m.n1} {s1}–{s2} {m.n2}"
    if m.stage == "group" and get_phase(db) is Phase.KNOCKOUT:
        msg += ". Gironi conclusi: tabellone generato!"
    flash(request, msg)
    return redirect(_table_page(request, m, db))


# --- Admin -----------------------------------------------------------------


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@admin.get("")
def admin_home(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    cfg = service.load_config(db)
    pending_groups = db.execute("SELECT COUNT(*) FROM matches WHERE stage = 'group' AND done = 0").fetchone()[0]
    views = service.matches(db)
    missing_splits = sum(1 for m in views for s in (1, 2) if m.split_missing(s))
    playable = [m for m in views if not m.bye and m.t1 is not None and m.t2 is not None]
    tables = service.table_views(db, all_matches=views) if cfg.phase.in_play else []
    return page(
        request, db, "admin.html", overview=service.checkin_overview(db), chk=service.checklist(db), checks=service.CHECKS,
        push_subs=push.subscriber_count(db),
        answered=sum(1 for t in service.teams(db).values() if t["q_at"]),
        solo=service.unassigned_guests(db),
        admin_pin=get_setting(db, "admin_pin"), pending_groups=pending_groups, missing_splits=missing_splits,
        checkin_url=checkin_url(request, db), tables=tables,
        played=sum(m.done for m in playable), total=sum(1 for m in views if not m.bye),
        info=service.load_tournament(db), reg=service.registration(db),
        pay_code=service.pay_code, wa_number=service.whatsapp_number, pay_message=service.payment_message,
        access_message=lambda info, t, player: service.access_message(
            info, t, player, f"{public_site_url(request, db)}/accesso/{service.access_token(db, t['id'], player)}"
        ),
    )


@admin.get("/qr")
def admin_qr(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "qr.html", url=checkin_url(request, db), code=service.venue_code(db))


@admin.get("/qr.svg")
def admin_qr_svg(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    img = qrcode.make(checkin_url(request, db), image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def _admin_action(request: Request, action: Callable[[], None], success: str, back: str = "/admin") -> Response:
    try:
        action()
    except TournamentError as e:
        flash(request, str(e), "err")
    else:
        flash(request, success)
    return redirect(back)


@admin.post("/squadre/importa")
def admin_import(request: Request, testo: str = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    try:
        result = service.import_teams(db, testo)
    except TournamentError as e:
        flash(request, f"{e} (le righe precedenti sono state importate)", "err")
    else:
        skipped = f" {result.skipped} già presenti, saltate." if result.skipped else ""
        flash(request, f"{result.teams} coppie e {result.guests} iscritti solo serata importati.{skipped}")
    return redirect("/admin#coppie")


def _team_form_values(
    name: str, p1: str, p2: str, phone1: str, phone2: str, payment: str, pagato: str | None, presente: str | None,
    note: str = "", verificata: str | None = None, gruppo: str | None = None, organizzatori: str | None = None,
    arbitro: str = "",
) -> dict:
    return {
        "name": name, "p1": p1, "p2": p2, "phone1": phone1, "phone2": phone2, "payment": payment,
        "paid": pagato == "1", "present": presente == "1", "note": "" if verificata else note, "in_chat": gruppo == "1",
        "organizer": organizzatori == "1",
        "referee_player": _int(arbitro),
    }


@admin.get("/squadre/nuova")
def admin_new_team_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "admin_team.html", t=None, guest_team=False)


@admin.post("/squadre/nuova")
def admin_new_team(
    request: Request,
    name: str = Form(""), p1: str = Form(""), p2: str = Form(""), phone1: str = Form(""), phone2: str = Form(""),
    payment: str = Form(""), pagato: str | None = Form(None), presente: str | None = Form(None),
    note: str = Form(""), gruppo: str | None = Form(None), organizzatori: str | None = Form(None),
    arbitro: str = Form(""), db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        service.save_team(
            db, None, **_team_form_values(
                name, p1, p2, phone1, phone2, payment, pagato, presente, note,
                gruppo=gruppo, organizzatori=organizzatori, arbitro=arbitro,
            ),
        )
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/admin/squadre/nuova")
    flash(request, f"Coppia «{name.strip() or 'Coppia di ' + p1.strip()}» aggiunta.")
    return redirect("/admin#coppie")


@admin.get("/squadre/{team_id}")
def admin_team_form(request: Request, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    t = service.team(db, team_id)
    if t is None:
        raise HTTPException(404)
    return page(
        request, db, "admin_team.html", t=t, guest_team=team_id in service.checkin_overview(db).guest_teams,
        guests=service.unassigned_guests(db),
    )


@admin.post("/squadre/{team_id}")
def admin_team_save(
    request: Request,
    team_id: int,
    name: str = Form(""), p1: str = Form(""), p2: str = Form(""), phone1: str = Form(""), phone2: str = Form(""),
    payment: str = Form(""), pagato: str | None = Form(None), presente: str | None = Form(None),
    note: str = Form(""), verificata: str | None = Form(None), gruppo: str | None = Form(None),
    organizzatori: str | None = Form(None), arbitro: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        service.save_team(
            db, team_id,
            **_team_form_values(
                name, p1, p2, phone1, phone2, payment, pagato, presente, note, verificata, gruppo, organizzatori,
                arbitro,
            ),
        )
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/admin/squadre/{team_id}")
    flash(request, "Coppia aggiornata.")
    return redirect("/admin#coppie")


@admin.get("/mancanti")
def admin_missing(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "admin_mancanti.html", ov=service.missing_overview(db), rec=None, elenco="")


@admin.post("/mancanti/confronta")
def admin_missing_compare(request: Request, elenco: str = Form(""), db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(
        request, db, "admin_mancanti.html", ov=service.missing_overview(db), rec=service.reconcile(db, elenco),
        elenco=elenco,
    )


@admin.post("/mancanti/applica")
async def admin_missing_apply(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    form = await request.form()
    add = [str(v) for v in form.getlist("aggiungi")]
    renames = {
        int(k.removeprefix("rinomina_")): str(v)
        for k, v in form.items()
        if k.startswith("rinomina_") and k.removeprefix("rinomina_").isdecimal() and v
    }
    flag = {
        int(team_id): "Non presente nell'elenco ufficiale: da verificare"
        for team_id in form.getlist("fuori_elenco")
        if str(team_id).isdecimal() and int(team_id) not in renames
    }
    try:
        added, renamed = service.apply_reconciliation(db, add, renames, flag)
    except TournamentError as e:
        flash(request, str(e), "err")
    else:
        flash(request, f"{added} coppie aggiunte da completare, {renamed} rinominate.")
    return redirect("/admin/mancanti")


@admin.post("/mancanti/obiettivo")
def admin_missing_target(request: Request, obiettivo: int = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    if not service.MIN_TEAMS <= obiettivo <= 64:
        flash(request, "Obiettivo non valido.", "err")
    else:
        with db:
            set_setting(db, "target_teams", str(obiettivo))
    return redirect("/admin/mancanti")


@admin.post("/iscritti/{guest_id}/collega")
def admin_link_guest(
    request: Request, guest_id: int, team_id: int = Form(...), back: str = Form("/admin/mancanti"),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    target = back if back.startswith("/admin") else "/admin/mancanti"
    return _admin_action(request, lambda: service.link_guest(db, guest_id, team_id), "Iscritto collegato alla coppia.", target)


@admin.post("/iscritti/{guest_id}/nuova-coppia")
def admin_guest_new_team(
    request: Request, guest_id: int, name: str = Form(""), compagno: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    g = next((g for g in service.unassigned_guests(db) if g["id"] == guest_id), None)
    if g is None:
        flash(request, "Iscritto non trovato o già in coppia.", "err")
        return redirect("/admin/mancanti")

    def create() -> None:
        team_id = service.add_team(db, name, g["name"], compagno, g["phone"])
        service.link_guest(db, guest_id, team_id)

    return _admin_action(request, create, "Coppia creata.", "/admin/mancanti")


@admin.post("/squadre/{team_id}/togli-foto")
def admin_remove_photo(request: Request, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(
        request, lambda: service.remove_photo(db, team_id, photo_dir()), "Foto tolta.", f"/admin/squadre/{team_id}"
    )


@admin.post("/squadre/{team_id}/elimina")
def admin_delete_team(request: Request, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(request, lambda: service.delete_team(db, team_id), "Coppia eliminata.", "/admin#coppie")


@admin.post("/squadre/{team_id}/presente")
def admin_presence(
    request: Request, team_id: int, presente: int = Form(...), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    return _admin_action(
        request, lambda: service.admin_check_in(db, team_id, bool(presente)),
        "Presenza aggiornata.", "/admin#checkin",
    )


@admin.post("/squadre/{team_id}/pagato")
def admin_paid(
    request: Request, team_id: int, pagato: int = Form(...), back: str = Form("/admin#coppie"),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    target = back if back.startswith("/admin") else "/admin#coppie"
    return _admin_action(request, lambda: service.set_paid(db, team_id, bool(pagato)), "Pagamento aggiornato.", target)


@admin.post("/squadre/{team_id}/gruppo")
def admin_in_chat(
    request: Request, team_id: int, gruppo: int = Form(...), back: str = Form("/admin/mancanti#fuori-gruppo"),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    target = back if back.startswith("/admin") else "/admin/mancanti#fuori-gruppo"
    return _admin_action(request, lambda: service.set_in_chat(db, team_id, bool(gruppo)), "Gruppo aggiornato.", target)


@admin.post("/gruppo/elenco")
def admin_chat_list(request: Request, elenco: str = Form(""), db: sqlite3.Connection = Depends(get_db)) -> Response:
    r = service.mark_chat_from_list(db, elenco)
    msg = f"{len(r.marked)} coppie segnate nel gruppo"
    if r.already:
        msg += f", {len(r.already)} lo erano già"
    if r.unmatched:
        msg += f". Numeri senza coppia: {', '.join(r.unmatched)}"
    flash(request, msg + ".")
    return redirect("/admin#coppie")


@admin.post("/pagamenti/elenco")
def admin_paid_list(request: Request, elenco: str = Form(""), db: sqlite3.Connection = Depends(get_db)) -> Response:
    r = service.mark_paid_from_list(db, elenco)
    msg = f"{len(r.marked)} pagamenti segnati come verificati"
    if r.already:
        msg += f", {len(r.already)} lo erano già"
    if r.unmatched:
        msg += f". Non trovate: {', '.join(r.unmatched)}"
    flash(request, msg + ".", "err" if r.unmatched else "ok")
    return redirect("/admin#coppie")


@admin.post("/impostazioni")
def admin_settings(
    request: Request,
    title: str = Form(...),
    fields: int = Form(...),
    group_min: int = Form(...),
    ko_min: int = Form(...),
    buffer_min: int = Form(...),
    public_url: str = Form(""),
    start_time: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    if not 1 <= fields <= 20 or min(group_min, ko_min) < 1 or buffer_min < 0:
        flash(request, "Valori non validi: almeno 1 tavolo e durate positive.", "err")
        return redirect("/admin#impostazioni")
    if start_time and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", start_time):
        flash(request, "Orario di inizio non valido: usa il formato 21:30.", "err")
        return redirect("/admin#impostazioni")
    values = {
        "title": title.strip() or "Torneo di Beer Pong", "fields": str(fields), "group_min": str(group_min),
        "ko_min": str(ko_min), "buffer_min": str(buffer_min), "public_url": public_url.strip(),
        "start_time": start_time or get_setting(db, "start_time"),
    }
    with db:
        for k, v in values.items():
            set_setting(db, k, v)
    flash(request, "Impostazioni salvate.")
    return redirect("/admin#impostazioni")


def archive_dir() -> Path:
    return db_path().parent / "archivio"


@admin.get("/torneo")
def admin_tournament_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(
        request, db, "admin_torneo.html", info=service.load_tournament(db), archives=service.archives(archive_dir()),
        scoring=service.SCORING, referees=service.REFEREES,
        teams_count=len(service.teams(db)),
    )


@admin.post("/torneo")
def admin_tournament_save(
    request: Request,
    title: str = Form(""), date: str = Form(""), start_time: str = Form(""),
    target_teams: int = Form(...), fields: int = Form(...), fee: str = Form(""),
    rules: str = Form(""), form_url: str = Form(""), pay_info: str = Form(""),
    scoring: str = Form(Scoring.COPPIE), referees: str = Form("volontari"),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    info = service.TournamentInfo(
        title, date, start_time, target_teams, fields, fee, rules, form_url, pay_info, scoring, referees
    )
    return _admin_action(request, lambda: service.save_tournament(db, info), "Torneo salvato.", "/admin/torneo")


@admin.post("/torneo/nuovo")
def admin_new_tournament(request: Request, conferma: str = Form(""), db: sqlite3.Connection = Depends(get_db)) -> Response:
    if conferma.strip().upper() != "NUOVO":
        flash(request, "Per ripartire da zero scrivi NUOVO nella casella.", "err")
        return redirect("/admin/torneo")
    saved = service.new_tournament(db, archive_dir())
    flash(request, f"Torneo precedente archiviato in {saved.name}: compila i dati del nuovo torneo.")
    return redirect("/admin/torneo")


@admin.post("/pagamenti/estratto")
def admin_paid_statement(request: Request, estratto: str = Form(""), db: sqlite3.Connection = Depends(get_db)) -> Response:
    r = service.mark_paid_from_statement(db, estratto)
    msg = f"{len(r.marked)} pagamenti confermati"
    if r.marked:
        msg += f" ({', '.join(r.marked)})"
    if r.already:
        msg += f", {len(r.already)} già confermati"
    if r.unmatched:
        msg += f". Righe senza codice né telefono riconosciuto: {' · '.join(r.unmatched[:10])}"
    flash(request, msg + ".", "err" if r.unmatched else "ok")
    return redirect("/admin#pagamenti")


@admin.post("/partita/{match_id}/modalita")
def admin_match_mode(
    request: Request, match_id: int, modalita: str = Form(""), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    back = f"/risultato/{match_id}"
    return _admin_action(request, lambda: service.set_match_mode(db, match_id, modalita), "Modalità della partita cambiata.", back)


@admin.get("/gironi")
def admin_draw_preview(
    request: Request, base: str = "presenti", seed: str | None = None, db: sqlite3.Connection = Depends(get_db)
) -> Response:
    present_only = base != "iscritte"
    seed_value = _int(seed)
    if seed_value is None:
        return redirect(f"/admin/gironi?base={base}&seed={secrets.randbits(31)}")
    try:
        preview = service.preview_draw(db, present_only, seed_value)
    except TournamentError as e:
        preview, error = None, str(e)
    else:
        error = None
    return page(request, db, "admin_gironi.html", preview=preview, error=error, present_only=present_only, base=base)


@admin.post("/cartellone/rimescola")
def admin_board_reshuffle(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(
        request, lambda: service.reshuffle_board(db), "Nuovo sorteggio provvisorio pubblicato sul cartellone.", "/admin"
    )


@admin.post("/avvia")
def admin_start(
    request: Request, background: BackgroundTasks, seed: str | None = Form(None),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    def start() -> None:
        service.start_tournament(db, datetime.now(), _int(seed))
        background.add_task(push.send, push.first_matches(db))

    return _admin_action(request, start, "Si parte! Gironi sorteggiati e programma generato.")


@admin.post("/notifica")
def admin_notify(
    request: Request, background: BackgroundTasks, titolo: str = Form(""), testo: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    def notify() -> None:
        deliveries = push.broadcast(db, titolo, testo)
        background.add_task(push.send, deliveries)
        flash(request, f"Notifica inviata a {len(deliveries)} telefoni.")

    try:
        notify()
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect("/admin#notifiche")


@admin.post("/riapri")
def admin_reopen(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(
        request, lambda: service.reopen_groups(db),
        "Gironi riaperti: correggi il risultato, il tabellone si rigenera all'ultima conferma.",
    )


@admin.post("/sposta")
def admin_shift(request: Request, minuti: int = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(
        request, lambda: service.shift_schedule(db, minuti), f"Orari delle partite da giocare spostati di {minuti} min."
    )


@admin.post("/annulla")
def admin_reset(request: Request, conferma: str = Form(""), db: sqlite3.Connection = Depends(get_db)) -> Response:
    if conferma.strip() != "ANNULLA":
        flash(request, "Sorteggio non annullato: per confermare scrivi ANNULLA.", "err")
        return redirect("/admin#pericolo")
    return _admin_action(request, lambda: service.reset_tournament(db), "Sorteggio annullato: si torna al check-in.")


@admin.post("/chiave")
def admin_rotate_key(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(
        request, lambda: service.rotate_checkin_key(db), "Nuova chiave di check-in: ristampa il QR code.",
        "/admin#qr",
    )


@admin.post("/pin")
def admin_pin(request: Request, pin: str = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    pin = pin.strip()
    if len(pin) < 6 or not pin.isdecimal():
        flash(request, "Il PIN admin deve avere almeno 6 cifre.", "err")
        return redirect("/admin#pin")
    with db:
        set_setting(db, "admin_pin", pin)
    flash(request, "PIN admin aggiornato.")
    return redirect("/admin#pin")


def create_app() -> FastAPI:
    init_db()
    db = connect()
    secret = get_setting(db, "secret")
    db.close()

    app = FastAPI(title="Beer Pong", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", max_age=60 * 60 * 24 * 2)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(router)
    app.include_router(admin)
    return app
