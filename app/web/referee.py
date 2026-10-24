"""Staff: logging in, the referee screen and recording a match shot by shot."""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from .. import service, tracking
from ..service import TournamentError
from .common import can_referee, can_volunteer_referee, flash, get_db, int_or_none, page, record_failure, redirect, referee_back, table_page, team_referee_table, throttled
from .checkin import claim_demo_table
from .results import can_enter_result

router = APIRouter()


@router.get("/login")
def login_form(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, db, "login.html")


@router.post("/login")
def login(request: Request, pin: str = Form(...), db: sqlite3.Connection = Depends(get_db)) -> Response:
    if throttled(request, "admin"):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/login")
    if not service.is_admin_pin(db, pin.strip()):
        record_failure(request, "admin")
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
    field_no = request.session.get("field") if role == "referee" else own_table or int_or_none(campo)
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
    claim_demo_table(db, m.field)
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
    return redirect(referee_back(request, m))


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
    return redirect(referee_back(request, m))


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
    return redirect(referee_back(request, m))


@router.post("/partita/{match_id}/annulla-tiro")
def match_undo_shot(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = _refereed(request, db, match_id)
    if m is None:
        return redirect("/login")
    try:
        tracking.referee_undo(db, match_id)
    except TournamentError as e:
        flash(request, str(e), "err")
    return redirect(referee_back(request, m))


@router.post("/partita/{match_id}/lati")
def match_swap_sides(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Mirror left and right on this phone, to match where the couples stand at the table."""
    m = _refereed(request, db, match_id)
    if m is None:
        return redirect("/login")
    flipped = set(request.session.get("flip", []))
    flipped ^= {match_id}
    request.session["flip"] = sorted(flipped)[-20:]
    return redirect(referee_back(request, m))


@router.post("/partita/{match_id}/annulla-inizio")
def match_undo_start(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None:
        raise HTTPException(404)
    if not can_referee(request, m, db):
        return redirect("/login")
    service.undo_start(db, match_id)
    return redirect(table_page(request, m, db))


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
    claim_demo_table(db, m.field)
    return redirect(f"/segna/{match_id}")


@router.get("/segna/{match_id}")
def scoring(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The referee's scoring screen for one match, on its own: nothing but the table and the shots."""
    m = _refereed(request, db, match_id, shots=False)
    if m is None:
        return redirect("/login")
    if m.done or not m.mode.by_shots:
        return redirect(table_page(request, m, db) if m.done else f"/risultato/{match_id}")
    scoring = {m.id: tracking.referee_view(db, m.id)} if m.started_at is not None else {}
    return page(
        request, db, "segna.html", m=m, scoring=scoring, players=service.teams(db),
        flipped=set(request.session.get("flip", [])), now_ts=time.time(),
        official_counts={k: tracking.official_count(v) for k, v in scoring.items()},
        back_url=table_page(request, m, db),
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
