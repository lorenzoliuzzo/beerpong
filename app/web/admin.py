"""The organizers' pages under /admin."""
from __future__ import annotations

import io
import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from .. import push, service
from ..db import db_path, get_setting, set_setting
from ..states import Scoring
from ..service import TournamentError
from .common import checkin_url, flash, get_db, int_or_none, page, public_site_url, redirect, require_admin
from .public import photo_dir


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
        "referee_player": int_or_none(arbitro),
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
    seed_value = int_or_none(seed)
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
        service.start_tournament(db, datetime.now(), int_or_none(seed))
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
