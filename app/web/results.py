"""Results entered by the couples themselves, and who hit which cups."""
from __future__ import annotations

import sqlite3
import threading

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from .. import push, service, tracking
from ..db import get_phase
from ..states import Phase, Scoring
from ..service import TournamentError
from .common import can_referee, flash, get_db, int_or_none, is_admin, my_team_id, page, redirect, split_sides, table_page

router = APIRouter()


_results_lock = threading.Lock()


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
    side = int_or_none(str(form.get("side") or ""))
    if side not in split_sides(request, m):
        return redirect("/")
    players = {
        int(k.removeprefix("cup")): int_or_none(str(v)) or 0
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
            service.record_result(db, match_id, len(hits[1]), len(hits[2]), int_or_none(str(form.get("vincitore") or "")))
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
    return redirect(table_page(request, m, db))


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
            service.record_result(db, match_id, s1, s2, int_or_none(vincitore))
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
    return redirect(table_page(request, m, db))
