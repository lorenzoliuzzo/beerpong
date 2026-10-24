"""Entering and checking in: phone number, venue code, invites, guests and the beer voucher."""
from __future__ import annotations

import io
import secrets
import sqlite3
import time

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from .. import service
from ..db import get_phase, get_setting, set_setting
from ..service import TournamentError
from .common import device_id, enter_team, flash, get_db, int_or_none, invite_url, is_demo, my_player, my_team_id, page, record_failure, redirect, throttled

router = APIRouter()


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
    if throttled(request, "players"):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/checkin")
    found = service.teams_by_phone(db, phone)
    if int_or_none(team_id) is not None:
        found = [t for t in found if t["id"] == int_or_none(team_id)]
    if not found:
        if service.is_unpaired_guest(db, phone):
            flash(request, "Sei iscritto «solo serata»: se giocate in coppia, completate qui.")
            return redirect("/checkin/nuova")
        record_failure(request, "players")
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
        if throttled(request, "players"):
            flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
            return redirect("/io")
        if not service.venue_code_ok(db, codice):
            record_failure(request, "players")
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
    claim_demo_table(db, field_no)
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


def claim_demo_table(db: sqlite3.Connection, field_no: int | None) -> None:
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
    if throttled(request, "players"):
        flash(request, "Troppi tentativi: riprova tra qualche minuto.", "err")
        return redirect("/checkin/nuova")
    try:
        team_id = service.register_guest_team(db, phone1, name, p1, p2, phone2, payment)
    except TournamentError as e:
        record_failure(request, "players")
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
