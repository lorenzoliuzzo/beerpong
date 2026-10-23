from __future__ import annotations

import io
import logging
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import planning, service, tracking
from .db import connect, get_setting, init_db, set_setting
from .service import TournamentError

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE / "templates")
router = APIRouter()
log = logging.getLogger("beerpong")

REPORT_MIN_GAP_S = 0.7
REPORT_MAX_PER_MINUTE = 40
_report_times: dict[str, deque[float]] = defaultdict(deque)
ATTEMPT_WINDOW_S = 600
ATTEMPT_MAX_FAILURES = 8
_failures: dict[str, deque[float]] = defaultdict(deque)


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


def _throttled(request: Request) -> bool:
    failures = _failures[request.client.host if request.client else "?"]
    while failures and failures[0] < time.monotonic() - ATTEMPT_WINDOW_S:
        failures.popleft()
    return len(failures) >= ATTEMPT_MAX_FAILURES


def _record_failure(request: Request) -> None:
    _failures[request.client.host if request.client else "?"].append(time.monotonic())


def is_admin(request: Request) -> bool:
    return request.session.get("role") == "admin"


def my_team_id(request: Request) -> int | None:
    return request.session.get("team")


def can_referee(request: Request, m: service.MatchView) -> bool:
    if m.bye or m.t1 is None or m.t2 is None:
        return False
    return is_admin(request) or (request.session.get("role") == "referee" and request.session.get("field") == m.field)


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
        "me": me,
        "flash": request.session.pop("flash", None),
        "path": request.url.path,
        "can_referee": lambda m: can_referee(request, m),
        "split_sides": lambda m: split_sides(request, m),
        "payments": service.PAYMENTS,
        "server_clock": datetime.now().strftime("%H:%M:%S"),
        "server_hm": datetime.now().strftime("%H:%M"),
        "server_ts": int(time.time() * 1000),
    }
    return templates.TemplateResponse(request, name, {**base, **ctx})


def public_site_url(request: Request, db: sqlite3.Connection) -> str:
    return (get_setting(db, "public_url") or str(request.base_url)).rstrip("/")


def checkin_url(request: Request, db: sqlite3.Connection) -> str:
    base = (get_setting(db, "public_url") or str(request.base_url)).rstrip("/")
    return f"{base}/checkin?{urlencode({'k': get_setting(db, 'reg_key')})}"


# --- Public tabs -----------------------------------------------------------


@router.get("/")
def home(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    views = service.matches(db)
    now, upcoming = service.live(views)
    ko = service.knockout_rounds(db)
    team_id = my_team_id(request)
    scorers = service.scorers(db)
    shots = tracking.shot_stats(db)
    done_matches = [m for m in views if m.done and not m.bye]
    total_cups = sum((m.s1 or 0) + (m.s2 or 0) for m in done_matches)
    total_valid_matches = len([m for m in views if not m.bye])
    recent_matches = done_matches[-4:][::-1]
    return page(
        request, db, "home.html", now=now, upcoming=upcoming, overview=service.checkin_overview(db),
        dash=service.team_dashboard(db, team_id) if team_id else None,
        final=ko[-1][1][0] if ko else None, progress=service.tournament_progress(db),
        total_cups=total_cups, played_count=len(done_matches), total_matches=total_valid_matches,
        top_scorer=scorers[0] if scorers else None, shots=shots, recent_matches=recent_matches,
        live=True,
    )


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
    tables = service.group_tables(db)
    cfg = service.load_config(db)
    if cfg.phase in ("knockout", "finished") and not archivio and not g and not tavolo and not sez:
        return redirect("/tabellone")
    t_views = service.table_views(db, g_tables=tables)
    team_id = my_team_id(request)
    me_team = service.team(db, team_id) if team_id else None
    my_table = None
    if me_team and me_team["grp"] is not None and tables:
        my_table = planning.table_for_group(me_team["grp"], len(tables), cfg.fields)

    if tavolo.isdigit() and 1 <= int(tavolo) <= cfg.fields:
        active_tavolo = int(tavolo)
    elif my_table:
        active_tavolo = my_table
    else:
        active_tavolo = 1

    if g == "all":
        active_grp = "all"
    elif g.isdigit() and any(t.grp == int(g) for t in tables):
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

    return page(
        request, db, "gironi.html", tables=tables, table_views=t_views, T=service.teams(db),
        active_grp=active_grp, active_tavolo=active_tavolo, active_sez=active_sez, my_table=my_table,
        thirds=service.thirds_needed(db), progress=service.tournament_progress(db),
        projection=service.projected_first_round(db), labels=service.standing_labels(db),
        live=True,
    )


@router.get("/programma")
def schedule(request: Request, team: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    team_id = _int(team)
    views = service.matches(db)
    if team_id:
        views = [m for m in views if team_id in (m.t1, m.t2)]
    return page(request, db, "programma.html", slots=service.by_slot(views), team_id=team_id, live=True)


@router.get("/classifiche")
def rankings(request: Request, vista: str = "", db: sqlite3.Connection = Depends(get_db)) -> Response:
    tables = service.group_tables(db)
    view = vista if vista in ("coppie", "giocatori", "tiri", "gironi") else "coppie"
    return page(
        request, db, "classifiche.html", view=view, tables=tables, T=service.teams(db),
        thirds=service.thirds_needed(db), scorers=service.scorers(db), team_stats=service.team_ranking(db),
        progress=service.tournament_progress(db), shots=tracking.shot_stats(db) if view == "tiri" else None,
        live=True,
    )


@router.get("/tabellone")
def knockout(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(
        request, db, "tabellone.html", rounds=service.knockout_rounds(db),
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
    fields = service.load_config(db).fields
    # Two or more groups per table: show the board as one column per table, with what is playing there.
    tables = None
    if groups and fields > 1 and len(groups) % fields == 0:
        live_tables = {} if provisional else {tv.field: tv for tv in service.table_views(db)}
        tables = [
            (
                f,
                [g for g in groups if planning.table_for_group(ord(g.letter) - 65, len(groups), fields) == f],
                live_tables.get(f),
            )
            for f in range(1, fields + 1)
        ]
    return page(
        request, db, "cartellone.html", groups=groups, provisional=provisional, now=now, upcoming=upcoming,
        tables=tables,
        rounds=service.board_bracket(db, len(groups)), progress=service.tournament_progress(db),
        thirds=planning.bracket_plan(len(groups))[1] - 2 * len(groups) if groups else 0,
        final=ko[-1][1][0] if ko else None, site=public_site_url(request, db), live=True,
    )


@router.get("/qr-sito.svg")
def site_qr(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    img = qrcode.make(public_site_url(request, db), image_factory=qrcode.image.svg.SvgPathImage, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@router.get("/squadra/{team_id}")
def team_page(request: Request, team_id: int, vs: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    dash = service.team_dashboard(db, team_id)
    if dash is None:
        raise HTTPException(404)
    profile = service.team_profile(db, team_id)
    other = _int(vs)
    h2h = service.head_to_head(db, team_id, other) if other and other != team_id else None
    rivals = [t for t in service.teams(db).values() if t["checked_in"] and t["id"] != team_id]
    return page(
        request, db, "squadra.html", dash=dash, profile=profile, T=service.teams(db),
        thirds=service.thirds_needed(db), h2h=h2h, other=other, rivals=rivals, live=True,
    )


# --- Live tracking ---------------------------------------------------------


class ReportIn(BaseModel):
    kind: str
    side: int | None = None
    player: int | None = None
    cup: int | None = None


class UndoIn(BaseModel):
    id: int


def server_now() -> float:
    """The one clock every phone follows (wall time on the Pi)."""
    return time.time()


def device_id(request: Request) -> str:
    if "device" not in request.session:
        request.session["device"] = secrets.token_hex(8)
    return request.session["device"]


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


@router.get("/api/live/{match_id}")
def live_state(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    try:
        return JSONResponse(tracking.live_view(db, match_id, server_now(), request.session.get("device")))
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@router.post("/api/live/{match_id}/segnala")
def live_report(
    request: Request, match_id: int, body: ReportIn, db: sqlite3.Connection = Depends(get_db)
) -> JSONResponse:
    device = device_id(request)
    if not _report_allowed(request, device):
        return JSONResponse({"error": "Piano! Troppe segnalazioni ravvicinate."}, status_code=429)
    try:
        tracking.add_report(db, match_id, device, body.kind, body.side, body.player, body.cup, server_now())
        return JSONResponse(tracking.live_view(db, match_id, server_now(), device))
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/live/{match_id}/annulla")
def live_undo(request: Request, match_id: int, body: UndoIn, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    device = device_id(request)
    try:
        tracking.undo_report(db, match_id, device, body.id, server_now())
        return JSONResponse(tracking.live_view(db, match_id, server_now(), device))
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# --- Check-in --------------------------------------------------------------


@router.get("/checkin")
def checkin_list(request: Request, k: str = "", db: sqlite3.Connection = Depends(get_db)) -> Response:
    if k:
        if service.checkin_key_ok(db, k):
            request.session["checkin_ok"] = True
        return redirect("/checkin")
    return page(
        request, db, "checkin.html", teams=service.teams(db), allowed=request.session.get("checkin_ok", False)
    )


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
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    if not request.session.get("checkin_ok"):
        flash(request, "Per fare il check-in scansiona il QR code sul tavolo.", "err")
        return redirect("/checkin/nuova")
    if _throttled(request):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/checkin/nuova")
    try:
        team_id = service.register_guest_team(db, phone1, name, p1, p2, phone2, payment)
    except TournamentError as e:
        _record_failure(request)
        flash(request, str(e), "err")
        return redirect("/checkin/nuova")
    request.session["team"] = team_id
    flash(request, "Check-in fatto! Siete in gara.")
    return redirect("/")


@router.get("/checkin/{team_id}")
def checkin_form(request: Request, team_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    t = service.team(db, team_id)
    if t is None:
        raise HTTPException(404)
    return page(request, db, "checkin_team.html", t=t, allowed=request.session.get("checkin_ok", False))


@router.post("/checkin/{team_id}")
def checkin_save(
    request: Request,
    team_id: int,
    name: str = Form(""),
    p1: str = Form(""),
    p2: str = Form(""),
    phone1: str = Form(""),
    phone2: str = Form(""),
    payment: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    t = service.team(db, team_id)
    if t is None:
        raise HTTPException(404)
    # Re-editing your own check-in is fine; checking in someone else needs the QR on the table.
    if not request.session.get("checkin_ok") and my_team_id(request) != team_id:
        flash(request, "Per fare il check-in scansiona il QR code sul tavolo.", "err")
        return redirect(f"/checkin/{team_id}")
    if t["checked_in"] and my_team_id(request) != team_id:
        flash(request, "Questa coppia ha già fatto il check-in: entra con il vostro numero di telefono.", "err")
        return redirect(f"/checkin/{team_id}")
    try:
        service.check_in(db, team_id, name, p1, p2, phone1, phone2, payment)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/checkin/{team_id}")
    request.session["team"] = team_id
    flash(request, "Check-in fatto! Ci vediamo al tavolo.")
    return redirect("/")


@router.post("/checkin/{team_id}/entra")
def checkin_join(
    request: Request, team_id: int, phone: str = Form(...), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    t = service.team(db, team_id)
    if t is None:
        raise HTTPException(404)
    if _throttled(request):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect(f"/checkin/{team_id}")
    if not service.phone_matches(t, phone):
        _record_failure(request)
        flash(request, "Il numero non corrisponde a quelli lasciati al check-in.", "err")
        return redirect(f"/checkin/{team_id}")
    request.session["team"] = team_id
    flash(request, f"Benvenuto in «{t['name']}»!")
    return redirect("/")


@router.post("/esci-squadra")
def leave_team(request: Request) -> Response:
    request.session.pop("team", None)
    return redirect("/")


# --- Player cup split ------------------------------------------------------


@router.get("/canestri/{match_id}")
def split_form(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    sides = split_sides(request, m)
    if not sides:
        flash(request, "Puoi dividere i canestri solo per le partite della tua squadra, dopo il risultato.", "err")
        return redirect("/")
    return page(request, db, "canestri.html", m=m, sides=sides, T=service.teams(db))


@router.post("/canestri/{match_id}")
def split_save(
    request: Request, match_id: int, side: int = Form(...), primo: int = Form(...),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if side not in split_sides(request, m):
        return redirect("/")
    try:
        service.assign_cups(db, match_id, side, primo)
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/canestri/{match_id}")
    flash(request, "Canestri assegnati.")
    return redirect("/" if my_team_id(request) else "/arbitro")


# --- Staff -----------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "login.html")


@router.post("/login")
def login(request: Request, pin: str = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    if _throttled(request):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/login")
    who = service.authenticate(db, pin.strip())
    if who is None:
        _record_failure(request)
        flash(request, "PIN non valido.", "err")
        return redirect("/login")
    role, field_no = who
    request.session.update({"role": role, "field": field_no})
    return redirect("/admin" if role == "admin" else "/arbitro")


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.pop("role", None)
    request.session.pop("field", None)
    return redirect("/")


@router.get("/arbitro")
def referee(request: Request, campo: str | None = None, db: sqlite3.Connection = Depends(get_db)) -> Response:
    role = request.session.get("role")
    if role not in ("admin", "referee"):
        return redirect("/login")
    field_no = request.session.get("field") if role == "referee" else _int(campo)
    views = [m for m in service.matches(db) if m.slot is not None and (field_no is None or m.field == field_no)]
    if field_no is None:
        now_ms, _ = service.live(views)
        now_ids = {m.id for m in now_ms}
        current = now_ms
        pending = [m for m in views if not m.done and m.id not in now_ids]
    else:
        all_pending = [m for m in views if not m.done]
        current = all_pending[:1]
        pending = all_pending[1:]
    return page(
        request, db, "arbitro.html", current=current, pending=pending,
        played=[m for m in views if m.done][::-1], field_no=field_no, live=True,
    )


@router.get("/partita/{match_id}")
def match_form(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m):
        return redirect("/login")
    return page(request, db, "partita.html", m=m, live_score=None if m.done else tracking.live_score(db, match_id))


@router.post("/partita/{match_id}")
def match_save(
    request: Request,
    match_id: int,
    s1: int = Form(...),
    s2: int = Form(...),
    vincitore: str | None = Form(None),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m):
        return redirect("/login")
    try:
        service.record_result(db, match_id, s1, s2, _int(vincitore))
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect(f"/partita/{match_id}")
    tracking.apply_live_splits(db, match_id)
    msg = f"Confermato: {m.n1} {s1}–{s2} {m.n2}"
    if m.stage == "group" and get_setting(db, "phase") == "knockout":
        msg += ". Gironi conclusi: tabellone generato!"
    flash(request, msg)
    return redirect("/arbitro")


# --- Admin -----------------------------------------------------------------


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@admin.get("")
def admin_home(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    cfg = service.load_config(db)
    pins = [(f, service.referee_pin(db, f)) for f in range(1, cfg.fields + 1)]
    pending_groups = db.execute("SELECT COUNT(*) FROM matches WHERE stage = 'group' AND done = 0").fetchone()[0]
    missing_splits = sum(1 for m in service.matches(db) for s in (1, 2) if m.split_missing(s))
    return page(
        request, db, "admin.html", overview=service.checkin_overview(db), chk=service.checklist(db), checks=service.CHECKS,
        solo=service.unassigned_guests(db), pins=pins,
        admin_pin=get_setting(db, "admin_pin"), pending_groups=pending_groups, missing_splits=missing_splits,
        checkin_url=checkin_url(request, db),
    )


@admin.get("/qr")
def admin_qr(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "qr.html", url=checkin_url(request, db))


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
) -> dict:
    return {
        "name": name, "p1": p1, "p2": p2, "phone1": phone1, "phone2": phone2, "payment": payment,
        "paid": pagato == "1", "present": presente == "1", "note": "" if verificata else note, "in_chat": gruppo == "1",
        "organizer": organizzatori == "1",
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
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        service.save_team(
            db, None, **_team_form_values(
                name, p1, p2, phone1, phone2, payment, pagato, presente, note,
                gruppo=gruppo, organizzatori=organizzatori,
            ),
        )
    except TournamentError as e:
        flash(request, str(e), "err")
        return redirect("/admin/squadre/nuova")
    flash(request, f"Coppia «{name.strip() or 'Squadra di ' + p1.strip()}» aggiunta.")
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
    organizzatori: str | None = Form(None),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        service.save_team(
            db, team_id,
            **_team_form_values(
                name, p1, p2, phone1, phone2, payment, pagato, presente, note, verificata, gruppo, organizzatori
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
        if k.startswith("rinomina_") and k.removeprefix("rinomina_").isdigit() and v
    }
    flag = {
        int(team_id): "Non presente nell'elenco ufficiale: da verificare"
        for team_id in form.getlist("fuori_elenco")
        if str(team_id).isdigit() and int(team_id) not in renames
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
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    if not 1 <= fields <= 20 or min(group_min, ko_min) < 1 or buffer_min < 0:
        flash(request, "Valori non validi: almeno 1 campo e durate positive.", "err")
        return redirect("/admin#impostazioni")
    values = {
        "title": title.strip() or "Torneo di Beer Pong", "fields": str(fields), "group_min": str(group_min),
        "ko_min": str(ko_min), "buffer_min": str(buffer_min), "public_url": public_url.strip(),
    }
    with db:
        for k, v in values.items():
            set_setting(db, k, v)
    flash(request, "Impostazioni salvate.")
    return redirect("/admin#impostazioni")


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
def admin_start(request: Request, seed: str | None = Form(None), db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _admin_action(
        request, lambda: service.start_tournament(db, datetime.now(), _int(seed)),
        "Si parte! Gironi sorteggiati e programma generato.",
    )


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
def admin_reset(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
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
    if len(pin) < 6 or not pin.isdigit():
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
    log.warning("PIN admin: %s", get_setting(db, "admin_pin"))
    db.close()

    app = FastAPI(title="Beer Pong", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", max_age=60 * 60 * 24 * 2)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(router)
    app.include_router(admin)
    return app
