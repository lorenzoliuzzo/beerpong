"""The public tabs: groups, schedule, rankings, bracket, board, the couple's profile, push and PWA."""
from __future__ import annotations

import io
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlencode
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .. import planning, push, service, tracking
from ..db import db_path, get_phase, get_setting
from ..states import Phase
from ..service import TournamentError
from .common import flash, get_db, int_or_none, invite_url, is_admin, my_player, my_team_id, page, public_site_url, redirect

router = APIRouter()


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
    if (sez == "classifiche" or g or archivio) and not tavolo:  # groups moved to Classifica: old links land there
        return redirect("/classifiche?" + urlencode({"vista": "gironi", **({"g": g} if g else {})}))
    all_views = service.matches(db)
    tables = service.group_tables(db)
    cfg = service.load_config(db)
    t_views = service.table_views(db, g_tables=tables, all_matches=all_views)
    team_id = my_team_id(request)
    me_team = service.team(db, team_id) if team_id else None
    is_ko_phase = cfg.phase.knockout_reached
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
        active_tavolo=active_tavolo, my_table=my_table,
        thirds=service.thirds_needed(db), progress=service.tournament_progress(db),
        labels=service.standing_labels(db),
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
    team_id = int_or_none(team)
    views = service.matches(db)
    if team_id:
        views = [m for m in views if team_id in (m.t1, m.t2)]
    plan = service.planned_schedule(db, datetime.now()) if not views else None
    return page(request, db, "programma.html", slots=service.by_slot(views), plan=plan, team_id=team_id, live=True)


VIEW_ALIASES = {"eliminazione": "tabellone", "tiri": "giocatori"}  # the names before the merge


def active_group(g: str, tables: list[service.GroupTable], me_team: dict | None) -> str:
    if g == "all":
        return "all"
    if g.isdecimal() and any(t.grp == int(g) for t in tables):
        return f"g{int(g)}"
    if me_team and me_team["grp"] is not None:
        return f"g{me_team['grp']}"
    return f"g{tables[0].grp}" if tables else "all"


@router.get("/classifiche")
def rankings(request: Request, vista: str = "", g: str = "", per: str = "", db: sqlite3.Connection = Depends(get_db)) -> Response:
    tables = service.group_tables(db)
    rounds = service.knockout_rounds(db)
    vista = VIEW_ALIASES.get(vista, vista)
    view = vista if vista in ("gironi", "tabellone", "giocatori", "coppie") else "gironi"
    team_stats, scorers = service.team_ranking(db), service.scorers(db)
    t_liters, t_cups_live, _ = service.beer_stats(db)
    has_shots = db.execute("SELECT 1 FROM reports LIMIT 1").fetchone() is not None
    team_id = my_team_id(request)
    by_table: dict[int, list] = defaultdict(list)
    plan = []
    if view == "tabellone":
        ko = [m for _, ms in rounds for m in ms if not m.bye]
        plan = [] if ko else service.planned_knockout(db)
        for m in sorted(ko, key=lambda m: m.slot or 0) or plan:
            if m.field:
                by_table[m.field].append(m)
    mine = None
    if team_id is not None:
        mine = {
            "cups_pos": next((i for i, s in enumerate(team_stats, 1) if s.team == team_id and s.played), None),
            "players": [(i, s) for i, s in enumerate(scorers, 1) if s.team_id == team_id and s.cups],
        }
    return page(
        request, db, "classifiche.html", view=view, tables=tables, rounds=rounds,
        thirds=service.thirds_needed(db), T=service.teams(db),
        labels=service.standing_labels(db) if view == "tabellone" else {},
        projection=service.projected_first_round(db) if view == "tabellone" else [],
        mine=mine, scorers=scorers, team_stats=team_stats,
        active_grp=active_group(g, tables, service.team(db, team_id) if team_id else None),
        per="tavolo" if per == "tavolo" else "turno", by_table=sorted(by_table.items()), plan=plan,
        shots=tracking.shot_stats(db) if view == "giocatori" and has_shots else None,
        has_shots=has_shots,
        tournament_liters=t_liters, total_cups_live=t_cups_live,
        live=True,
    )


@router.get("/tabellone")
def knockout(vista: str = "") -> Response:
    return redirect("/classifiche?vista=tabellone" + ("&per=tavolo" if vista == "tavoli" else ""))


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
    other = int_or_none(vs)
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
