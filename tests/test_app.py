import random
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app import live, service
from app.db import connect, get_setting, init_db
from app.main import create_app, get_db

ADMIN_PIN = "123456"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("BP_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("BP_ADMIN_PIN", ADMIN_PIN)
    app_main._failures.clear()  # the login/phone throttle is per process: each test starts unthrottled
    with TestClient(create_app()) as c:
        yield c
    app_main._failures.clear()


def db_call(fn, *args):
    db = connect()
    try:
        return fn(db, *args)
    finally:
        db.close()


def record(c: TestClient, match_id: int, s1: int, s2: int, winner: int | str | None = None, **kw):
    data: dict = {"c1": [str(i) for i in range(1, s1 + 1)], "c2": [str(i) for i in range(1, s2 + 1)]}
    if winner is not None:
        data["vincitore"] = str(winner)
    return c.post(f"/risultato/{match_id}", data=data, **kw)


def login(c: TestClient, pin: str) -> None:
    r = c.post("/login", data={"pin": pin}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != "/login"


def import_teams(c: TestClient, n: int) -> None:
    login(c, ADMIN_PIN)
    c.post("/admin/impostazioni", data={"title": "Test", "fields": 3, "group_min": 5, "ko_min": 6, "buffer_min": 1})
    c.post("/admin/squadre/importa", data={"testo": "\n".join(f"Squadra {i}; Ada {i}; Bruno {i}" for i in range(n))})


def checkin_form(t, **overrides) -> dict:
    form = {"name": t["name"], "p1": t["p1"], "p2": t["p2"], "phone1": "+39 333 1234567", "phone2": "", "payment": "contanti"}
    return {**form, **overrides}


def setup_tournament(c: TestClient, n_teams: int, fields: int = 3) -> None:
    import_teams(c, n_teams)
    c.post("/admin/impostazioni", data={"title": "Test", "fields": fields, "group_min": 5, "ko_min": 6, "buffer_min": 1})
    for t in db_call(service.teams).values():
        c.post(f"/admin/squadre/{t['id']}/presente", data={"presente": 1})
    c.post("/admin/avvia")


def play_all(c: TestClient, stage: str, rng: random.Random) -> None:
    ids = [m.id for m in db_call(service.matches) if m.stage == stage and not m.done and m.t1 and m.t2]
    for mid in ids:
        cups = db_call(service.cups_in_play, mid)
        r = record(c, mid, rng.randint(0, cups), rng.randint(0, cups), "1", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/gironi"), r.headers


def test_db_dependency_closes_from_another_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BP_DB", str(tmp_path / "t.db"))
    init_db()
    gen = get_db()
    next(gen)
    errors: list[BaseException] = []

    def close() -> None:
        try:
            gen.close()
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=close)
    t.start()
    t.join()
    assert errors == []


def test_public_pages_render(client: TestClient) -> None:
    import_teams(client, 5)
    client.post("/logout")
    for path in ["/", "/gironi", "/programma", "/classifiche", "/classifiche?vista=giocatori", "/classifiche?vista=coppie",
                 "/tabellone", "/login", "/checkin", "/checkin/1", "/squadra/1"]:
        assert client.get(path).status_code == 200, path
    # Live redirect
    assert client.get("/live", follow_redirects=False).status_code == 303


def test_navigation_tabs_structure(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    r = client.get("/")
    assert r.status_code == 200
    # 3 tabs only: Squadra, Tavoli, Classifica; Live tab is removed; staff reach admin from the header
    for tab in ('href="/io"', ">Squadra", ">Tavoli", ">Classifica"):
        assert tab in r.text
    assert ">Live</a>" not in r.text
    assert 'class="staff" href="/login">Staff' in r.text and ">Admin\n" not in r.text
    # Programma is no longer in tabbar
    assert '<a href="/programma"' not in r.text


def test_gironi_single_and_all_modes(client: TestClient) -> None:
    setup_tournament(client, 8)
    # Default view without ?g=all is mode-single (shows single group with matches)
    r_single = client.get("/gironi")
    assert r_single.status_code == 200
    assert 'class="mode-single"' in r_single.text

    # View all groups (?g=all) is mode-all (standings only, matches hidden via CSS)
    r_all = client.get("/gironi?g=all")
    assert r_all.status_code == 200
    assert 'class="mode-all"' in r_all.text


def test_asynchronous_tables_and_group_mapping(client: TestClient) -> None:
    setup_tournament(client, 32, fields=4)
    t_views = db_call(service.table_views)
    assert len(t_views) == 4
    assert [tv.group_letters for tv in t_views] == ["A · B", "C · D", "E · F", "G · H"]
    assert all(tv.total_count == 12 for tv in t_views)

    # Complete 2 matches on Table 1 only; Tables 2, 3, 4 are still on their first match (slot 0)
    t1_first = t_views[0].current
    t1_second = t_views[0].next_match
    assert t1_first is not None and t1_second is not None
    record(client, t1_first.id, 10, 6)

    now_ms, next_ms = db_call(lambda db: service.live(service.matches(db)))
    now_by_field = {m.field: m for m in now_ms}
    # Table 1 immediately advanced to t1_second while Tables 2, 3, 4 are still at slot 0
    assert now_by_field[1].id == t1_second.id
    assert now_by_field[2].slot == 0
    assert now_by_field[3].slot == 0
    assert now_by_field[4].slot == 0


def test_phase_transition_and_team_profile(client: TestClient) -> None:
    rng = random.Random(77)
    setup_tournament(client, 8, fields=2)
    r_profile = client.get("/squadra/1")
    assert r_profile.status_code == 200
    assert "compariranno dopo la prima partita" in r_profile.text and "Posizionamento nei Ranking" not in r_profile.text

    # Finish all group matches -> /gironi (Tavoli) now shows the active knockout round directly
    play_all(client, "group", rng)
    r_profile = client.get("/squadra/1").text
    assert "Posizionamento nei Ranking" in r_profile and "Statistiche Personali Giocatori" in r_profile
    assert "Litri bevuti" in r_profile and "Scontri diretti" not in r_profile
    r_gironi = client.get("/gironi", follow_redirects=False)
    assert r_gironi.status_code == 200 and "Semifinali" in r_gironi.text
    assert client.get("/gironi?archivio=1").status_code == 200


def test_admin_and_qr_require_login(client: TestClient) -> None:
    for path in ["/admin", "/admin/qr", "/admin/qr.svg"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.post("/admin/avvia", follow_redirects=False).status_code == 303
    client.post("/login", data={"pin": "000000"})
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_import_accepts_optional_phones(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2\nB\tb1\tb2\t333 111 2222\nC; c1; c2; 3331112223; +39 333 4445556"})
    rows = {t["name"]: t for t in db_call(service.teams).values()}
    assert rows["A"]["phone1"] == "" and rows["B"]["phone1"] == "3331112222"
    assert rows["C"]["phone2"] == "+393334445556"
    client.post("/admin/squadre/importa", data={"testo": "squadra;giocatore1;giocatore2;telefono1;telefono2;da_verificare\nD;d1;d2;3331112224;;controllare | nota"})
    assert {t["name"]: t for t in db_call(service.teams).values()}["D"]["phone1"] == "3331112224"
    r = client.post("/admin/squadre/importa", data={"testo": "solo un campo"})
    assert "Riga 1" in r.text


def test_checkin_needs_qr_key_and_mandatory_answers(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    t = db_call(service.team, 1)

    client.post("/checkin/1", data=checkin_form(t))
    assert not db_call(service.team, 1)["checked_in"]

    client.get("/checkin?k=sbagliata")
    client.post("/checkin/1", data=checkin_form(t))
    assert not db_call(service.team, 1)["checked_in"]

    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    assert "Telefono (obbligatorio)" in client.get("/checkin/1").text
    assert "telefono valido" in client.post("/checkin/1", data=checkin_form(t, phone1="12")).text
    assert "domanda sul ticket" in client.post("/checkin/1", data=checkin_form(t, payment="")).text

    r = client.post("/checkin/1", data=checkin_form(t, name="Le Spugne", payment="cassa"))
    assert "Check-in fatto" in r.text and "Le Spugne" in r.text
    row = db_call(service.team, 1)
    assert (row["checked_in"], row["payment"], row["paid"], row["phone1"]) == (1, "cassa", 0, "+393331234567")


def test_partner_joins_with_phone(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    client.post("/checkin/2", data=checkin_form(db_call(service.team, 2), phone2="347 9876543"))

    with TestClient(client.app) as partner:
        page = partner.get("/checkin/2").text
        assert "già fatto il check-in" in page
        assert "non corrisponde" in partner.post("/checkin/2/entra", data={"phone": "000 0000000"}).text
        # Someone who is not on the team cannot overwrite the check-in.
        partner.post("/checkin/2", data=checkin_form(db_call(service.team, 2), name="Hacker"))
        assert db_call(service.team, 2)["name"] == "Squadra 1"
        r = partner.post("/checkin/2/entra", data={"phone": "+39 347 987 6543"})
        assert "Benvenuto" in r.text and "Squadra 1" in r.text


def test_start_uses_only_present_teams_and_admin_sees_missing(client: TestClient) -> None:
    import_teams(client, 10)
    for team_id in range(1, 9):
        client.post(f"/admin/squadre/{team_id}/presente", data={"presente": 1})
    admin_page = client.get("/admin").text
    assert "Mancano (2)" in admin_page and "Squadra 9" in admin_page

    client.post("/admin/avvia")
    playing = {t for m in db_call(service.matches) for t in (m.t1, m.t2)}
    assert playing == set(range(1, 9))
    assert {s.team_id for s in db_call(service.scorers)} == set(range(1, 9))


def test_payment_verification(client: TestClient) -> None:
    import_teams(client, 4)
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    client.post("/checkin/1", data=checkin_form(db_call(service.team, 1), payment="online"))
    # Tickets are paid by bank transfer before the night: every registered couple counts, arrived or not.
    page = client.get("/admin").text
    assert '<div class="big">0/4</div><span class="muted small">pagate</span>' in page
    assert page.count('aria-label="Pagato:') == 4
    client.post("/admin/squadre/1/pagato", data={"pagato": 1})
    client.post("/admin/squadre/3/pagato", data={"pagato": 1})
    assert db_call(service.team, 1)["paid"] == 1
    assert '<div class="big">2/4</div><span class="muted small">pagate</span>' in client.get("/admin").text
    client.post("/admin/squadre/3/pagato", data={"pagato": 0})
    assert '<div class="big">1/4</div><span class="muted small">pagate</span>' in client.get("/admin").text


def test_start_requires_four_present(client: TestClient) -> None:
    import_teams(client, 6)
    client.post("/admin/squadre/1/presente", data={"presente": 1})
    assert "almeno 4 coppie presenti" in client.post("/admin/avvia").text
    assert db_call(get_setting, "phase") == "setup"


@pytest.mark.parametrize("n_teams", [30, 48, 13])
def test_full_tournament(client: TestClient, n_teams: int) -> None:
    rng = random.Random(n_teams)
    setup_tournament(client, n_teams)
    assert db_call(get_setting, "phase") == "groups"

    play_all(client, "group", rng)
    assert db_call(get_setting, "phase") == "knockout"
    for _ in range(6):
        play_all(client, "ko", rng)

    assert db_call(get_setting, "phase") == "finished"
    ko = db_call(service.knockout_rounds)
    final = ko[-1][1][0]
    assert final.label == "Finale" and final.duration is None and final.winner
    assert all(m.duration == 6 for _, ms in ko[:-1] for m in ms if not m.bye)
    total = sum(m.s1 + m.s2 for m in db_call(service.matches) if not m.bye)
    assert sum(s.cups_for for s in db_call(service.team_ranking)) == total

    for path in ["/", "/programma", "/classifiche", "/classifiche?vista=coppie", "/tabellone",
                 "/squadra/1?vs=2", "/programma?team=3"]:
        assert client.get(path).status_code == 200, path
    assert "Campioni" in client.get("/").text


def test_team_splits_cups_for_player_ranking(client: TestClient) -> None:
    import_teams(client, 8)
    client.post("/logout")
    key = db_call(get_setting, "reg_key")
    phones = {}
    for t in db_call(service.teams).values():
        with TestClient(client.app) as phone:
            phone.get(f"/checkin?k={key}")
            phones[t["id"]] = f"33300000{t['id']:02d}"
            phone.post(f"/checkin/{t['id']}", data=checkin_form(t, phone1=phones[t["id"]]))
    login(client, ADMIN_PIN)
    client.post("/admin/avvia")
    m = db_call(service.matches)[0]
    record(client, m.id, 5, 4)
    team1 = db_call(service.team, m.t1)

    with TestClient(client.app) as phone:
        phone.post(f"/checkin/{m.t1}/entra", data={"phone": phones[m.t1]})
        assert "Dividete i canestri" in phone.get("/").text
        phone.post(f"/canestri/{m.id}", data={"side": 2, "cup1": "1"})  # not their side
        assert db_call(service.match, m.id).split2 is None
        assert "Indica chi ha centrato" in phone.post(f"/canestri/{m.id}", data={"side": 1, "cup1": "1"}).text
        phone.post(f"/canestri/{m.id}", data={"side": 1, "cup1": "1", "cup2": "1", "cup3": "2", "cup4": "1", "cup5": "2"})

    view = db_call(service.match, m.id)
    assert view.split1 == (3, 2) and view.split2 is None
    top = db_call(service.scorers)[:2]
    assert [(s.player, s.cups) for s in top] == [(team1["p1"], 3), (team1["p2"], 2)]

    record(client, m.id, 4, 4)  # cup 5 was not sunk after all: the other cups keep their player
    assert db_call(service.match, m.id).split1 == (3, 1)
    record(client, m.id, 6, 4)  # two more cups nobody has claimed yet
    assert db_call(service.match, m.id).split1 is None


def test_knockout_tie_requires_winner(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    play_all(client, "group", random.Random(1))
    semi = next(m for m in db_call(service.matches) if m.stage == "ko")
    assert "overtime" in record(client, semi.id, 4, 4).text
    record(client, semi.id, 4, 4, "2")
    final = next(m for m in db_call(service.matches) if m.label == "Finale")
    assert final.t1 == semi.t2


def test_reopen_groups_until_knockout_played(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    play_all(client, "group", random.Random(2))
    last_group = [m for m in db_call(service.matches) if m.stage == "group"][-1]

    assert "Gironi chiusi" in record(client, last_group.id, 9, 0).text
    client.post("/admin/riapri")
    assert db_call(get_setting, "phase") == "groups"
    record(client, last_group.id, 9, 0)
    assert db_call(get_setting, "phase") == "knockout"

    play_all(client, "ko", random.Random(3))
    assert "Si è già giocata" in client.post("/admin/riapri").text


def test_shift_schedule_moves_only_pending(client: TestClient) -> None:
    setup_tournament(client, 8, fields=1)
    first, second = db_call(service.matches)[:2]
    record(client, first.id, 2, 0)
    client.post("/admin/sposta", data={"minuti": 15})
    after = {m.id: m.time for m in db_call(service.matches)}
    assert after[first.id] == first.time
    assert after[second.id] != second.time


def test_partial_couple_completes_at_checkin(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "Solitari; Ada; ; 3331112222\nVeri; Bea; Carlo; 3331113333"})
    partial = next(t for t in db_call(service.teams).values() if t["name"] == "Solitari")
    assert partial["p2"] == ""
    assert "manca il compagno" in client.get("/checkin").text
    assert "entrambi i giocatori" in client.post(f"/admin/squadre/{partial['id']}/presente", data={"presente": 1}).text
    assert not db_call(service.team, partial["id"])["checked_in"]

    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    assert "Servono nome" in client.post(f"/checkin/{partial['id']}", data=checkin_form(partial)).text
    client.post(f"/checkin/{partial['id']}", data=checkin_form(partial, p2="Dario"))
    row = db_call(service.team, partial["id"])
    assert (row["p2"], row["checked_in"]) == ("Dario", 1)


def test_admin_adds_partial_couple_without_team_name(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/nuova", data={"name": "", "p1": "Elena", "p2": "", "phone1": ""})
    (t,) = db_call(service.teams).values()
    assert (t["name"], t["p2"]) == ("Squadra di Elena", "")


def test_guest_from_party_list_enters_as_couple(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    r = client.post("/admin/squadre/importa", data={"testo": (
        "squadra;giocatore1;giocatore2;telefono1;telefono2;da_verificare\n"
        "Veri; Bea; Carlo; 3331113333;;\n"
        ";Ottavia;;333 000 0101;;solo serata\n"
    )})
    assert "1 coppie e 1 iscritti solo serata" in r.text
    client.post("/logout")

    form = {"phone1": "+39 333 0000101", "name": "Pinte Pazze", "p1": "Ottavia", "p2": "Fulvio", "phone2": "", "payment": "online"}
    assert "Si fa alla serata" in client.post("/checkin/nuova", data=form).text
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    assert "non risulta tra gli iscritti" in client.post("/checkin/nuova", data={**form, "phone1": "3330000000"}).text
    r = client.post("/checkin/nuova", data=form)
    assert "Siete in gara" in r.text and "Pinte Pazze" in r.text
    t = next(t for t in db_call(service.teams).values() if t["name"] == "Pinte Pazze")
    assert (t["checked_in"], t["phone1"], t["payment"]) == (1, "3330000101", "online")

    # The same phone cannot create a second team.
    with TestClient(client.app) as other:
        other.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
        assert "non risulta" in other.post("/checkin/nuova", data={**form, "name": "Bis"}).text

    login(client, ADMIN_PIN)
    assert "da «solo serata»" in client.get("/admin").text


def test_admin_adds_and_edits_team_from_phone_form(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    r = client.post("/admin/squadre/nuova", data={
        "name": "Ultimi Arrivati", "p1": "Gino", "p2": "Pina", "phone1": "333 555 6666", "phone2": "",
        "payment": "cassa", "presente": "1",
    })
    assert "Coppia «Ultimi Arrivati» aggiunta" in r.text
    t = next(iter(db_call(service.teams).values()))
    assert (t["checked_in"], t["payment"], t["paid"], t["phone1"]) == (1, "cassa", 0, "3335556666")

    assert "Ultimi Arrivati" in client.get(f"/admin/squadre/{t['id']}").text
    client.post(f"/admin/squadre/{t['id']}", data={
        "name": "Ultimi ma Primi", "p1": "Gino", "p2": "Pina", "phone1": "3335556666", "phone2": "347 1112223",
        "payment": "cassa", "pagato": "1", "presente": "1",
    })
    t = db_call(service.team, t["id"])
    assert (t["name"], t["phone2"], t["paid"], t["checked_in"]) == ("Ultimi ma Primi", "3471112223", 1, 1)

    r = client.post(f"/admin/squadre/{t['id']}", data={"name": "X", "p1": "Gino", "p2": "", "presente": "1"})
    assert "entrambi i giocatori" in r.text
    assert "numero di telefono non valido" in client.post(
        f"/admin/squadre/{t['id']}", data={"name": "X", "p1": "Gino", "p2": "Pina", "phone1": "12"}
    ).text.lower()


def test_after_draw_admin_can_fix_names_but_not_roster(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    t = db_call(service.team, 1)
    client.post("/admin/squadre/1", data={"name": "Nome Corretto", "p1": t["p1"], "p2": t["p2"], "phone1": "3339998887"})
    t = db_call(service.team, 1)
    assert (t["name"], t["checked_in"]) == ("Nome Corretto", 1)  # presence untouched though the box was not sent
    assert "Nome Corretto" in client.get("/classifiche").text
    assert "non si possono aggiungere" in client.post("/admin/squadre/nuova", data={"p1": "A", "p2": "B"}).text
    assert "Il torneo è già iniziato" in client.post("/admin/squadre/1/elimina").text


def test_draw_preview_with_registered_and_start_with_previewed_groups(client: TestClient) -> None:
    import_teams(client, 14)
    client.post("/admin/squadre/importa", data={"testo": "Parziale; Solo; ; 3331234000"})
    r = client.get("/admin/gironi?base=iscritte&seed=7")
    assert "14" in r.text and "non ancora arrivata" in r.text and "Avvia con questi gironi" not in r.text
    assert "almeno 4 coppie presenti" in client.get("/admin/gironi?base=presenti&seed=7").text
    assert client.get("/admin/gironi", follow_redirects=False).headers["location"].startswith("/admin/gironi?base=presenti&seed=")

    for team_id in range(1, 11):
        client.post(f"/admin/squadre/{team_id}/presente", data={"presente": 1})
    preview = db_call(service.preview_draw, True, 42)
    assert sorted(t["id"] for g in preview.groups for t in g) == list(range(1, 11))
    assert "Avvia con questi gironi" in client.get("/admin/gironi?base=presenti&seed=42").text

    client.post("/admin/avvia", data={"seed": "42"})
    started = {t["id"]: t["grp"] for t in db_call(service.teams).values() if t["grp"] is not None}
    assert [sorted(t["id"] for t in g) for g in preview.groups] == [
        sorted(tid for tid, grp in started.items() if grp == gi) for gi in range(len(preview.groups))
    ]


OFFICIAL = (
    "I gufi, Sole mare spritz, los tapos, ZETA9, Le vipere, l’orso bruno, cappuccì, QWtech, Tux"
)


def test_reconcile_official_list_and_apply(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": (
        "I Gufi; Aldo; Sergio; 3330000102\n"
        "Sole Mare Spritz; A; B; 3330000103\n"
        "Los Taposs; Leo; Dario; 3330000104\n"
        "ZETA9; Kurt; Vera; 3330000105\n"
        "Le iene; Sara; Mia; 3330000106\n"
        "L'Orso Bruno; Clara; Elsa; 3330000107\n"
    )})
    rec = db_call(service.reconcile, OFFICIAL)
    assert {name for name, _ in rec.matched} == {"I gufi", "Sole mare spritz", "los tapos", "ZETA9", "l’orso bruno"}
    assert rec.new == ["Le vipere", "cappuccì", "QWtech", "Tux"]
    assert [t["name"] for t in rec.extra] == ["Le iene"]
    assert "Registrate ma non nell'elenco (1)" in client.post("/admin/mancanti/confronta", data={"elenco": OFFICIAL}).text

    iene_id = rec.extra[0]["id"]
    r = client.post("/admin/mancanti/applica", data={
        "aggiungi": ["Le vipere", "cappuccì", "QWtech", "Tux"], f"rinomina_{iene_id}": "Le vipere",
    })
    assert "3 coppie aggiunte da completare, 1 rinominate" in r.text
    names = {t["name"]: t for t in db_call(service.teams).values()}
    assert names["Le vipere"]["p1"] == "Sara" and "Le iene" not in names
    assert (names["QWtech"]["p1"], names["QWtech"]["p2"]) == ("", "")
    assert "giocatori da definire" in client.get("/admin/mancanti").text
    rec = db_call(service.reconcile, OFFICIAL)
    assert rec.new == [] and rec.extra == []


def test_reimport_skips_known_teams_and_backfills_guest_times(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2; 3331110000\n;Ottavia;;3330000101;;solo serata"})
    r = client.post("/admin/squadre/importa", data={"testo": (
        "squadra;giocatore1;giocatore2;telefono1;telefono2;da_verificare;iscritto_il\n"
        "A;a1;a2;3331110000;;\n"
        ";Ottavia;;3330000101;;solo serata;2026-10-22T15:37\n"
    )})
    assert "0 coppie e 0 iscritti solo serata importati. 1 già presenti, saltate." in r.text
    assert db_call(service.unassigned_guests)[0]["registered_at"] == "2026-10-22T15:37"


def test_guest_hints_link_and_new_team(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": (
        "Tux; ; ; \n"
        "Amici di Bobo; Bobo; ; 3330001111\n"
        ";;;3330000108;;solo serata;2026-10-19T21:37\n"
        ";;;3330000109;;solo serata;2026-10-19T21:39\n"
        ";Bobo;;3330000110;;solo serata;2026-10-22T16:33\n"
        ";Ottavia;;3330000101;;solo serata;2026-10-22T18:00\n"
    )})
    hints = {gv.guest["phone"]: gv.hints for gv in db_call(service.guest_views)}
    assert any("2 min" in h for h in hints["3330000108"])
    assert any("Amici di Bobo" in h for h in hints["3330000110"])
    assert hints["3330000101"] == []

    page = client.get("/admin/mancanti").text
    assert "forse sono in coppia" in page and "Profili da completare o verificare (2)" in page

    teams = {t["name"]: t["id"] for t in db_call(service.teams).values()}
    guests = {g["phone"]: g["id"] for g in db_call(service.unassigned_guests)}
    client.post(f"/admin/iscritti/{guests['3330000101']}/collega", data={"team_id": teams["Tux"]})
    tux = db_call(service.team, teams["Tux"])
    assert (tux["p1"], tux["phone1"]) == ("Ottavia", "3330000101")

    r = client.post(f"/admin/iscritti/{guests['3330000108']}/nuova-coppia", data={"name": "Coppia 21:37", "compagno": ""})
    assert "Coppia creata" in r.text
    new = next(t for t in db_call(service.teams).values() if t["name"] == "Coppia 21:37")
    assert new["phone1"] == "3330000108"
    assert {g["phone"] for g in db_call(service.unassigned_guests)} == {"3330000109", "3330000110"}
    assert "già in una coppia" in client.post(
        f"/admin/iscritti/{guests['3330000101']}/collega", data={"team_id": teams["Tux"]}
    ).text


def test_old_database_gains_guest_time_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE guests (id INTEGER PRIMARY KEY, phone TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '', team_id INTEGER)")
    old.execute("INSERT INTO guests(phone, name) VALUES ('3331234567', 'Vecchio')")
    old.commit()
    old.close()
    monkeypatch.setenv("BP_DB", str(path))
    init_db()
    rows = db_call(lambda db: db.execute("SELECT name, registered_at FROM guests").fetchall())
    assert [(r["name"], r["registered_at"]) for r in rows] == [("Vecchio", None)]


def test_progress_and_live_bracket_preview(client: TestClient) -> None:
    setup_tournament(client, 13, fields=3)
    steps = db_call(service.tournament_progress)
    assert [s.label for s in steps] == ["Gironi", "Quarti di finale", "Semifinali", "Finale"]
    assert steps[0].done == 0 and steps[0].total > 0

    projection = db_call(service.projected_first_round)
    assert len(projection) == 4
    page = client.get("/tabellone").text
    assert "Se i gironi finissero adesso" in page and "1° A" in page
    cls_gironi = client.get("/classifiche?vista=gironi", follow_redirects=False)
    assert cls_gironi.status_code == 200 and "Girone A" in cls_gironi.text

    play_all(client, "group", random.Random(5))
    steps = db_call(service.tournament_progress)
    assert steps[0].complete and steps[1].total == 4
    assert db_call(service.projected_first_round) == []
    assert "°" in client.get("/tabellone").text  # origin labels on knockout matches


def test_official_list_adds_couples_to_verify_with_inferred_players(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "Le iene; Sara; Mia; 3330000106\nZETA9; Kurt; Vera; 3330000105"})
    iene_id = next(t["id"] for t in db_call(service.teams).values() if t["name"] == "Le iene")
    client.post("/admin/mancanti/applica", data={
        "aggiungi": ["ada e bice", "pino pallino", "QWtech"], "fuori_elenco": [str(iene_id)], f"rinomina_{iene_id}": "",
    })
    t = {x["name"]: x for x in db_call(service.teams).values()}
    assert (t["ada e bice"]["p1"], t["ada e bice"]["p2"]) == ("Ada", "Bice")
    assert "dedotti dal nome" in t["ada e bice"]["note"]
    assert (t["pino pallino"]["p1"], t["QWtech"]["p1"]) == ("", "")
    assert "da completare" in t["QWtech"]["note"]
    assert "Non presente nell'elenco" in t["Le iene"]["note"]
    assert t["ZETA9"]["note"] == ""

    page = client.get("/admin/mancanti").text
    assert "da verificare" in page and "Profili da completare o verificare (4)" in page

    gid = t["ada e bice"]["id"]
    client.post(f"/admin/squadre/{gid}", data={"name": "ada e bice", "p1": "Ada", "p2": "Beatrice", "note": "x", "verificata": "1"})
    row = db_call(service.team, gid)
    assert (row["p2"], row["note"]) == ("Beatrice", "")


def test_import_keeps_verification_note(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "Stella;Ugo bassi;Ines Rota;3330000111;;divisione da controllare"})
    assert next(iter(db_call(service.teams).values()))["note"] == "divisione da controllare"


def test_paid_list_marks_checked_teams(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": (
        "I Gufi; A; B\nLos Taposs; C; D\nLago Monte; E; F\nada e bice; Ada; Bice\nL'Orso Bruno; G; H"
    )})
    r = client.post("/admin/pagamenti/elenco", data={"elenco": (
        "1.  I gufi ✅ \n 3. los tapos ✅ \n 5. Lago Monte\n"
        " 25. l’orso bruno ✅ \n 27. Neri Neri (ada e bice) ✅ \n 24. le vipere ✅ "
    )})
    assert "4 pagamenti segnati come verificati. Non trovate: le vipere" in r.text
    paid = {t["name"] for t in db_call(service.teams).values() if t["paid"]}
    assert paid == {"I Gufi", "Los Taposs", "L'Orso Bruno", "ada e bice"}
    assert "lo erano già" in client.post("/admin/pagamenti/elenco", data={"elenco": "I gufi ✅"}).text


def test_da_sistemare_lists_unpaid_with_quick_pay(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2; 3331110000\nB; b1; b2; 3331110001"})
    a = next(t["id"] for t in db_call(service.teams).values() if t["name"] == "A")
    page = client.get("/admin/mancanti").text
    assert "Non hanno ancora pagato (2)" in page and "tel:3331110000" in page
    r = client.post(f"/admin/squadre/{a}/pagato", data={"pagato": 1, "back": "/admin/mancanti#non-pagato"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/mancanti#non-pagato"
    assert "Non hanno ancora pagato (1)" in client.get("/admin/mancanti").text
    assert "Non pagate 1</button>" in client.get("/admin").text


def test_board_shows_provisional_groups_then_live_then_bracket(client: TestClient) -> None:
    import_teams(client, 30)
    client.post("/admin/squadre/importa", data={"testo": "QWtech; ; ;\nTux; ; ;"})  # incomplete: shown as confirmed
    groups, provisional = db_call(service.board_groups)
    assert provisional and sum(len(g.teams) for g in groups) == 32 and [len(g.teams) for g in groups] == [6, 6, 5, 5, 5, 5]
    page = client.get("/cartellone").text
    assert "Gironi provvisori" in page and "QWtech" in page and "giocatori da definire" in page
    assert "Ottavi" in page and 'class="tabbar"' not in page
    assert client.get("/qr-sito.svg").headers["content-type"] == "image/svg+xml"

    before = [[t["id"] for t in g.teams] for g in groups]
    client.post("/admin/cartellone/rimescola")
    after = [[t["id"] for t in g.teams] for g in db_call(service.board_groups)[0]]
    assert before != after

    # Everyone present: the tournament starts with exactly the groups shown on the board.
    for t in db_call(service.teams).values():
        if not t["p1"]:
            client.post(f"/admin/squadre/{t['id']}", data={"name": t["name"], "p1": "X", "p2": "Y"})
        client.post(f"/admin/squadre/{t['id']}/presente", data={"presente": 1})
    client.post("/admin/avvia")
    started = {}
    for t in db_call(service.teams).values():
        started.setdefault(t["grp"], set()).add(t["id"])
    assert sorted(map(sorted, started.values())) == sorted(map(sorted, after))

    page = client.get("/cartellone").text
    assert "Fase a gironi" in page and "anteprima" in page
    assert "<span class=\"tnum\">Tavolo 1</span>" in page and "<span class=\"lbl\">Ora</span>" in page
    play_all(client, "group", random.Random(9))
    page = client.get("/cartellone").text
    assert "Eliminazione diretta" in page and "Classifiche finali dei gironi" in page
    for _ in range(5):
        play_all(client, "ko", random.Random(1))
    assert "Campioni" in client.get("/cartellone").text


def test_chat_list_marks_couples_by_phone(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": (
        "A; a1; a2; 3331234567\nB; b1; b2; 3331110001; 3331110002\nC; c1; c2; 3331110003"
    )})
    r = client.post("/admin/gruppo/elenco", data={"elenco": (
        "+39 333 123 4567\n~ Bice: +39 333 111-0002\nTu\n+39 333 111 0001\n+39 320 000 0000"
    )})
    assert "2 coppie segnate nel gruppo. Numeri senza coppia: +393200000000" in r.text
    in_chat = {t["name"] for t in db_call(service.teams).values() if t["in_chat"]}
    assert in_chat == {"A", "B"}
    assert "Fuori dal gruppo 1</button>" in r.text
    assert "lo erano già" in client.post("/admin/gruppo/elenco", data={"elenco": "3331234567"}).text


def test_chat_flag_toggles_and_survives_team_edit(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2; 3331110000"})
    a = next(iter(db_call(service.teams)))
    r = client.post(f"/admin/squadre/{a}/gruppo", data={"gruppo": 1, "back": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/mancanti#fuori-gruppo"
    assert db_call(service.team, a)["in_chat"] == 1
    assert "Fuori dal gruppo 0</button>" in client.get("/admin").text
    form = {"name": "A", "p1": "a1", "p2": "a2", "phone1": "3331110000", "gruppo": "1"}
    client.post(f"/admin/squadre/{a}", data=form)
    assert db_call(service.team, a)["in_chat"] == 1
    client.post(f"/admin/squadre/{a}", data={k: v for k, v in form.items() if k != "gruppo"})
    assert db_call(service.team, a)["in_chat"] == 0


def test_couples_list_has_paid_and_chat_toggles(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2; 3331110000\nB; b1; b2"})
    a = next(t["id"] for t in db_call(service.teams).values() if t["name"] == "A")
    client.post(f"/admin/squadre/{a}/pagato", data={"pagato": 1})
    page = client.get("/admin").text
    assert '<div class="big">1/2</div><span class="muted small">pagate</span>' in page
    assert '<div class="big">0/2</div><span class="muted small">nel gruppo</span>' in page
    assert f'<input type="hidden" name="pagato" value="0"><input type="hidden" name="back" value="/admin#t{a}">' in page
    assert 'class="check on" aria-pressed="true" aria-label="Pagato: A"' in page
    assert 'class="check " aria-pressed="false" aria-label="Nel gruppo: A"' in page
    assert "senza telefono" in page  # B has no phone
    client.post(f"/admin/squadre/{a}/gruppo", data={"gruppo": 1})
    client.post(f"/admin/squadre/{a}/pagato", data={"pagato": 0})
    t = db_call(service.team, a)
    assert (t["paid"], t["in_chat"]) == (0, 1)
    assert 'aria-pressed="true" aria-label="Nel gruppo: A"' in client.get("/admin/mancanti").text


def test_shared_tables_never_show_a_couple_twice(client: TestClient) -> None:
    # 5 couples, 2 tables: one group shares both tables, so table 1 must not run ahead of table 2.
    setup_tournament(client, 5, fields=2)
    for _ in range(6):
        now, _ = db_call(lambda db: service.live(service.matches(db)))
        playing = [t for m in now for t in (m.t1, m.t2)]
        assert len(playing) == len(set(playing))
        record(client, min(now, key=lambda m: m.field).id, 10, 4)


def test_31_couples_on_4_tables_balance_the_queues(client: TestClient) -> None:
    setup_tournament(client, 31, fields=4)
    assert [tv.total_count for tv in db_call(service.table_views)] == [12, 12, 12, 9]


def test_solo_serata_list(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "; ; ; 3330000001\n; Ada; ; 3330000002\nA; a1; a2; 3331110000"})
    page = client.get("/admin").text
    assert 'Solo serata <span class="pill">2</span>' in page
    assert "Ada" in page and "Senza nome" in page and "tel:3330000001" in page


def test_organizers_are_not_counted_in_checks(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2; 3331110000\nOrg; o1; o2"})
    org = next(t["id"] for t in db_call(service.teams).values() if t["name"] == "Org")
    client.post(f"/admin/squadre/{org}", data={"name": "Org", "p1": "o1", "p2": "o2", "organizzatori": "1"})
    page = client.get("/admin").text
    assert '<div class="big">0/1</div><span class="muted small">pagate</span>' in page
    assert "Senza contare le 1 coppie degli organizzatori" in page and "Senza telefono 0</button>" in page
    assert f'id="t{org}" data-f=""' in page and "organizzatori</span>" in page
    assert "Non hanno ancora pagato (1)" in client.get("/admin/mancanti").text


def test_provisional_schedule_starts_at_the_planned_time(client: TestClient) -> None:
    import_teams(client, 16)
    client.post("/admin/impostazioni", data={"title": "T", "fields": 4, "group_min": 5, "ko_min": 6, "buffer_min": 1,
                                             "start_time": "21:30"})
    plan = db_call(lambda db: service.planned_schedule(db, datetime(2026, 10, 23, 18, 0)))
    assert plan.start == "21:30" and plan.slots[0][0] == "21:30" and plan.slots[1][0] == "21:36"
    assert len(plan.slots[0][1]) == 4 and plan.groups_end == "22:06"
    page = client.get("/programma").text
    assert "Programma provvisorio" in page and "Tavolo 1 · Girone A" in page
    assert "Orario di inizio non valido" in client.post("/admin/impostazioni", data={
        "title": "T", "fields": 4, "group_min": 5, "ko_min": 6, "buffer_min": 1, "start_time": "25:00"}).text
    assert "Si comincia alle 21:30" in client.get("/cartellone").text


def test_start_uses_planned_time_unless_late(client: TestClient) -> None:
    import_teams(client, 8)
    for t in db_call(service.teams).values():
        client.post(f"/admin/squadre/{t['id']}/presente", data={"presente": 1})
    db_call(lambda db: service.start_tournament(db, datetime(2026, 10, 23, 21, 10)))
    assert min(m.time for m in db_call(service.matches) if m.time) == "21:30"
    db_call(service.reset_tournament)
    db_call(lambda db: service.start_tournament(db, datetime(2026, 10, 23, 21, 47, 20)))
    assert min(m.time for m in db_call(service.matches) if m.time) == "21:48"


def test_player_journey_checkin_then_group_presence(client: TestClient) -> None:
    import_teams(client, 16)
    client.post("/admin/impostazioni", data={"title": "T", "fields": 4, "group_min": 5, "ko_min": 6, "buffer_min": 1,
                                             "start_time": "21:30"})
    client.post("/logout")
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    me = db_call(service.team, 1)
    client.post("/checkin/1", data=checkin_form(me, phone1=f"+39 333 000 {1:04d}"))

    assert "★ Squadra 0 · il vostro profilo" in client.get("/").text
    home = client.get("/io").text
    spot = db_call(lambda db: service.provisional_spot(db, 1, datetime(2026, 10, 23, 18, 0)))
    assert f"Girone {spot.group.letter} · 🏓 Tavolo {spot.table}" in home
    assert f"contro <strong>{spot.first_rival['name']}</strong>" in home and "✓ arrivata" in home

    gironi = client.get("/gironi").text
    assert "1/16</div>" in gironi and "coppie arrivate" in gironi
    assert f'href="/gironi?tavolo={spot.table}" class="on">Tavolo {spot.table} ★' in gironi
    assert "arrivate 1 coppie su 16" in client.get("/cartellone").text


def test_profile_stays_short_without_the_table_queue(client: TestClient) -> None:
    import_teams(client, 16)
    client.post("/admin/impostazioni", data={"title": "T", "fields": 4, "group_min": 5, "ko_min": 6, "buffer_min": 1})
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    for t in sorted(db_call(service.teams).values(), key=lambda t: t["id"] == 1):  # couple 1 last: its session stays
        client.post(f"/checkin/{t['id']}", data=checkin_form(t, phone1=f"+39 333 000 {t['id']:04d}"))
    client.post("/admin/avvia")
    home = client.get("/io").text
    field = next(m.field for m in db_call(service.matches) if m.side(1))
    assert "Al vostro tavolo" not in home  # the table's queue lives on the Tavoli page, the profile stays short
    assert f"Tavolo {field}" in home and "Partite di Squadra 0" in home


def test_rankings_are_stats_only(client: TestClient) -> None:
    import_teams(client, 8)
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    for t in sorted(db_call(service.teams).values(), key=lambda t: t["id"] == 1):
        client.post(f"/checkin/{t['id']}", data=checkin_form(t, phone1=f"+39 333 000 {t['id']:04d}"))
    client.post("/admin/avvia")
    page = client.get("/classifiche").text
    assert 'href="/classifiche?vista=gironi" class="on">Gironi' in page
    assert 'href="/classifiche?vista=eliminazione"' in page
    page_coppie = client.get("/classifiche?vista=coppie").text
    assert '<tr class="mine' in page_coppie and "Mostra tutta la classifica" not in page_coppie  # 8 couples: no cut yet
    mine = [m for m in db_call(service.matches) if m.side(1)]
    profile = client.get("/io").text
    assert "● LIVE" not in profile and profile.count("in chiamata") == sum(m.playing for m in mine) <= 1
    assert "Attiva le notifiche" in profile


def test_profile_page_logged_out_and_own_team_redirect(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    assert "Entrate nel profilo della vostra coppia" in client.get("/io").text
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    r = client.post("/checkin/1", data=checkin_form(db_call(service.team, 1)), follow_redirects=False)
    assert r.headers["location"] == "/io"
    assert client.get("/squadra/1", follow_redirects=False).headers["location"] == "/io"
    page = client.get("/io").text
    assert "★ VOI" in page and "Non siete voi? Esci" in page and "Modifica i vostri dati" in page
    assert client.get("/squadra/2").status_code == 200 and "★ VOI" not in client.get("/squadra/2").text
    client.post("/esci-squadra")
    assert "Entrate nel profilo" in client.get("/io").text


def test_knockout_waits_for_the_other_tables(client: TestClient) -> None:
    setup_tournament(client, 16, fields=4)
    play_all(client, "group", random.Random(3))
    ko = [m for m in db_call(service.matches) if m.stage == "ko"]
    before = {m.id: m.time for m in ko}
    first = next(m for m in ko if m.t1 and m.t2 and not m.done)
    record(client, first.id, 10, 2)
    after = {m.id: m.time for m in db_call(service.matches) if m.stage == "ko"}
    assert {k: v for k, v in after.items() if k != first.id} == {k: v for k, v in before.items() if k != first.id}
    now, upcoming = db_call(lambda db: service.live(service.matches(db)))
    assert all(m.t1 and m.t2 for m in now)


def test_pasted_numbers_on_separate_lines_stay_separate(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "A; a1; a2; 3331234567\nB; b1; b2; 3331110001"})
    r = client.post("/admin/gruppo/elenco", data={"elenco": "333 123 4567\n333 111 0001"})
    assert "2 coppie segnate nel gruppo" in r.text


def test_odd_query_values_and_quotes_in_names(client: TestClient) -> None:
    import_teams(client, 4)
    assert client.get("/gironi?tavolo=%C2%B2&g=%C2%B2").status_code == 200
    client.post("/logout")
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    t = db_call(service.team, 1)
    client.post("/checkin/1", data=checkin_form(t, name="L'Orso"))
    page = client.get("/io").text
    assert "confirm(\"Uscire dal profilo di L\\u0027Orso" in page and "di L'Orso su" not in page


def test_late_start_after_midnight_starts_now(client: TestClient) -> None:
    cfg = db_call(service.load_config)
    late = service.Config(**{**cfg.__dict__, "start_time": "23:30"})
    assert service.planned_start(late, datetime(2026, 10, 24, 0, 10)) == datetime(2026, 10, 24, 0, 10)
    assert service.planned_start(late, datetime(2026, 10, 23, 22, 0)) == datetime(2026, 10, 23, 23, 30)
    assert service.planned_start(late, datetime(2026, 10, 23, 8, 0)) == datetime(2026, 10, 23, 23, 30)


def test_shared_tables_views_never_show_a_couple_twice(client: TestClient) -> None:
    setup_tournament(client, 5, fields=2)
    for _ in range(2):
        first = next(m for m in db_call(service.matches) if m.field == 1 and not m.done)
        record(client, first.id, 10, 4)
        current = [tv.current for tv in db_call(service.table_views) if tv.current]
        playing = [t for m in current for t in (m.t1, m.t2)]
        assert len(playing) == len(set(playing))


def test_table_views_follow_the_matches_when_fields_change(client: TestClient) -> None:
    setup_tournament(client, 32, fields=4)
    def shrink(db):
        with db:
            service.set_setting(db, "fields", "2")
    db_call(shrink)
    assert [tv.field for tv in db_call(service.table_views)] == [1, 2, 3, 4]
    groups, _ = db_call(service.board_groups)
    assert [f for f, _ in db_call(lambda db: service.board_tables(db, groups))] == [1, 2, 3, 4]
    assert client.get("/gironi?tavolo=4").status_code == 200 and "Tavolo 4" in client.get("/cartellone").text


def test_draw_with_more_organizers_than_spread_slots(client: TestClient) -> None:
    from app.service import draw_groups
    for seed in range(30):
        groups = draw_groups(list(range(11)), seed, fields=1, organizers=frozenset(range(11)))
        assert sorted(t for g in groups for t in g) == list(range(11))


def test_qr_then_phone_checks_in_and_opens_the_profile(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": (
        "Gufi; Ada; Bice; 3331110001; 3331110002\nLontre; Ciro; Dora; 3331110003\nCastori; Elsa; Furio; 3331110001\n"
        "; Gina; ; 3339990000"
    )})
    client.post("/logout")
    ids = {t["name"]: t["id"] for t in db_call(service.teams).values()}

    # without the QR: no check-in, and no way in before the payment
    r = client.post("/checkin/accedi", data={"phone": "333 111 0003"})
    assert "si conferma con il pagamento" in r.text and not db_call(service.team, ids["Lontre"])["checked_in"]

    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    r = client.post("/checkin/accedi", data={"phone": "+39 333 111 0003"}, follow_redirects=False)
    assert r.headers["location"] == "/questionario" and db_call(service.team, ids["Lontre"])["checked_in"] == 1
    assert "Check-in fatto ✓ Benvenuti, Lontre!" in client.get("/questionario").text

    # the partner's number works too; a number in two couples asks which one
    r = client.post("/checkin/accedi", data={"phone": "3331110001"})
    assert "Quale coppia?" in r.text and "Gufi" in r.text and "Castori" in r.text
    client.post("/checkin/accedi", data={"phone": "3331110001", "team_id": ids["Castori"]})
    assert db_call(service.team, ids["Castori"])["checked_in"] == 1 and not db_call(service.team, ids["Gufi"])["checked_in"]

    # a party-only guest goes to the couple form; an unknown number gets a hint
    assert client.post("/checkin/accedi", data={"phone": "3339990000"}, follow_redirects=False).headers["location"] == "/checkin/nuova"
    assert "Numero non trovato" in client.post("/checkin/accedi", data={"phone": "3330000000"}).text


def test_phone_login_after_the_start(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/squadre/importa", data={"testo": "\n".join(f"S{i}; A{i}; B{i}; 33311100{i:02d}" for i in range(5))})
    for t in list(db_call(service.teams).values())[:4]:
        client.post(f"/admin/squadre/{t['id']}/presente", data={"presente": 1})
    client.post("/admin/avvia")
    client.post("/logout")
    with TestClient(client.app) as phone:
        assert "Bentornati, S0!" in phone.post("/checkin/accedi", data={"phone": "3331110000"}).text
        assert "Il torneo è già iniziato" in phone.post("/checkin/accedi", data={"phone": "3331110004"}).text


def test_wrong_numbers_are_throttled(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    for _ in range(app_main.ATTEMPT_MAX_FAILURES):
        client.post("/checkin/accedi", data={"phone": "3330000000"})
    assert "Troppi tentativi" in client.post("/checkin/accedi", data={"phone": "3330000000"}).text


def test_admin_control_room_during_the_groups(client: TestClient) -> None:
    setup_tournament(client, 16, fields=4)
    page = client.get("/admin").text
    assert page.index('id="torneo"') < page.index('id="coppie"')  # the tournament comes first once it runs
    assert '<span class="muted small">partite</span>' in page and "tavoli in ritardo" in page
    current = [m for m in db_call(service.matches) if m.playing]
    assert len(current) == 4 and all(f'href="/risultato/{m.id}">Risultato</a>' in page for m in current)

    m = current[0]
    form = client.get(f"/risultato/{m.id}").text
    assert f"{m.n1} <span class=\"muted\">vs</span> {m.n2}" in form and f"‹ Tavolo {m.field}" in form


def test_the_referee_couples_matches_are_spaced_out_on_its_table() -> None:
    from app import planning

    groups = [[1, 2, 3, 4], [5, 6, 7, 8]]  # both on one table: 12 matches, the referee couple 1 plays 3
    for ref in (1, 6):
        queue = planning.build_table_queues(groups, 1, frozenset({ref}))[1]
        assert len(queue) == 12
        spots = [i for i, f in enumerate(queue) if ref in (f.team1, f.team2)]
        assert len(spots) == 3 and min(b - a for a, b in zip(spots, spots[1:])) >= 3
        assert all({a.team1, a.team2}.isdisjoint({b.team1, b.team2}) for a, b in zip(queue, queue[1:]))


def test_questionnaire_after_check_in_sets_the_couples_prior(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/logout")
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    t = db_call(service.team, 1)
    client.post("/checkin/1", data=checkin_form(t, phone1="+39 333 000 0001"))
    assert "Due domande veloci" in client.get("/io").text  # not answered yet: the profile reminds them
    assert db_call(lambda db: service.prior_rate(service.team(db, 1))) is None

    assert "Rispondi a tutte" in client.post("/questionario", data={"livello": "x", "mira1": 3, "mira2": 3, "obiettivo": "quarti"}).text
    r = client.post("/questionario", data={"livello": "spesso", "mira1": 6, "mira2": 4, "obiettivo": "quarti"}, follow_redirects=False)
    assert r.headers["location"] == "/io"
    prior = db_call(lambda db: service.prior_rate(service.team(db, 1)))
    assert prior == pytest.approx((0.5 + 0.36) / 2)
    assert "Due domande veloci" not in client.get("/io").text
    # the prior moves the expected hit rate, and real shots soon outweigh it
    assert live.hit_rate(0, 0, prior) == pytest.approx(prior) and live.hit_rate(0, 0) == pytest.approx(0.3)
    assert abs(live.hit_rate(40, 100, prior) - 0.4) < abs(live.hit_rate(0, 0, prior) - 0.4)

    # a couple already answered goes straight to its profile at the next login
    client.post("/esci-squadra")
    assert client.post("/checkin/accedi", data={"phone": "3330000001"}, follow_redirects=False).headers["location"] == "/io"


def test_stylesheet_link_changes_with_the_file(client: TestClient) -> None:
    import hashlib

    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_bytes()
    assert f'/static/style.css?v={hashlib.sha256(css).hexdigest()[:10]}"' in client.get("/").text


def test_heatmaps_show_where_couples_hit(client: TestClient) -> None:
    setup_tournament(client, 8, fields=2)
    m = next(m for m in db_call(service.matches) if m.playing and m.field == 1)
    record(client, m.id, 3, 0)
    assert "Dove centrano" in client.get(f"/squadra/{m.t1}").text  # the couple's rack, before the players split it
    client.post(f"/canestri/{m.id}", data={"side": 1, "cup1": "1", "cup2": "2", "cup3": "1"})
    profile = client.get(f"/squadra/{m.t1}").text
    assert "Dove centrano" in profile and profile.count('class="heatmap-fig compact"') == 2
    assert 'title="1 colpo (' in profile and "<i>#" not in profile


def test_knockout_timetable_per_table_is_announced_during_the_groups(client: TestClient) -> None:
    setup_tournament(client, 16, fields=4)
    plan = db_call(service.planned_knockout)
    assert plan[-1].label == "Finale" and plan[-1].field == 1 and plan[-1].n1.startswith("Vincente semifinale")
    assert all("(1° " in p.n1 or "(2° " in p.n1 for p in plan if p.label == plan[0].label)  # who would play now
    assert "Dopo i gironi al Tavolo 1" in client.get("/gironi?tavolo=1").text
    page = client.get("/tabellone?vista=tavoli").text
    assert "Per tavolo" in page and "Tavolo 4" in page and "Vincente semifinale 1" in page

    play_all(client, "group", random.Random(3))
    real = [m for m in db_call(service.matches) if m.stage == "ko" and not m.bye]
    assert [(m.label, m.field) for m in real] == [(p.label, p.field) for p in plan]  # the same tables, in order
    # The times start from when the groups really ended, a minute from now.
    first = datetime.fromisoformat(real[0].start_iso)
    assert timedelta(0) < first - datetime.now() <= timedelta(minutes=1)
    assert db_call(service.planned_knockout) == []
    assert "Vincente semifinale 2" in client.get("/tabellone?vista=tavoli").text


def test_secondary_tables_removed_as_knockout_advances(client: TestClient) -> None:
    rng = random.Random(11)
    setup_tournament(client, 16, fields=4)
    assert [tv.field for tv in db_call(service.table_views)] == [1, 2, 3, 4]
    assert [tv.kind for tv in db_call(service.table_views)] == ["Principale", "Principale", "Secondario", "Secondario"]

    play_all(client, "group", rng)
    # Quarterfinals (4 matches) use all 4 tables; finish them to enter Semifinals (2 matches on main tables 1 & 2)
    qf_ids = [m.id for m in db_call(service.matches) if m.stage == "ko" and m.round == 1 and not m.done]
    for mid in qf_ids:
        record(client, mid, 6, 3)
    t_views = db_call(service.table_views)
    assert [tv.field for tv in t_views] == [1, 2] and all(tv.is_main for tv in t_views)
    page = client.get("/gironi").text
    assert "Tavolo 3, Tavolo 4 sono stati rimossi" in page


def test_salvezza_when_last_target_cup_is_hit_starts_overtime_immediately() -> None:
    # Side 1 has 1 cup left ({6}), Side 2 has 1 cup left ({6}) before Side 1 clears Side 2's rack:
    seq = [
        (1, 1, 1), (1, 2, 2), (2, 1, 1), (2, 2, 2),
        (1, 1, 3), (1, 2, 4), (2, 1, 3), (2, 2, 4),
        (1, 1, 5), (1, 2, 0), (2, 1, 5), (2, 2, 0),
        (1, 1, 6),  # clears Side 2's rack -> Side 2 enters salvezza against Side 1's single remaining cup {6}
        (2, 1, 6),  # Side 2's 1st player hits Side 1's last cup {6} -> overtime must start immediately
    ]
    reports = [live.Report(i, live.REFEREE, "shot", s, p, c, 100.0 + i) for i, (s, p, c) in enumerate(seq, 1)]
    st = live.replay(reports, 6, 1, salvezza=True)
    assert st.phase == "overtime" and st.racks[1].cups == {1, 2, 3} and st.racks[2].cups == {1, 2, 3}


def test_couples_enter_their_own_result_with_cup_positions(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    m = next(x for x in db_call(service.matches) if x.playing)
    db_call(lambda db: db.execute("UPDATE teams SET phone1 = '3330009999' WHERE id = ?", (m.t1,)) and db.commit())
    client.post("/logout")
    with TestClient(client.app) as outsider:
        assert outsider.get(f"/risultato/{m.id}", follow_redirects=False).status_code == 303
    with TestClient(client.app) as phone:
        phone.post("/checkin/accedi", data={"phone": "3330009999"})
        assert f'href="/risultato/{m.id}"' in phone.get("/io").text
        assert 'name="c1" value="6"' in phone.get(f"/risultato/{m.id}").text
        phone.post(f"/risultato/{m.id}", data={"c1": ["1", "4", "6"], "c2": ["2"]})
    done = db_call(service.match, m.id)
    assert done.done and (done.s1, done.s2, done.winner) == (3, 1, m.t1)
    cups = db_call(lambda db: sorted(tuple(r) for r in db.execute("SELECT side, cup FROM cup_hits WHERE match_id = ?", (m.id,))))
    assert cups == [(1, 1), (1, 4), (1, 6), (2, 2)]


TOURNAMENT = {
    "title": "Beer Pong d'autunno", "date": "2026-11-21", "start_time": "21:00", "target_teams": 4, "fields": 2,
    "fee": "12,50", "rules": "6 bicchieri per lato.\nSi passa in due.", "form_url": "https://forms.gle/abc",
    "pay_info": "Satispay al 333 0000000",
}


def test_admin_creates_the_tournament_and_shares_its_page(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    assert "da creare" in client.get("/admin").text
    assert "Scegli la data" in client.post("/admin/torneo", data={**TOURNAMENT, "date": ""}).text
    assert "https://" in client.post("/admin/torneo", data={**TOURNAMENT, "form_url": "javascript:alert(1)"}).text
    assert "Quota non valida" in client.post("/admin/torneo", data={**TOURNAMENT, "fee": "dieci"}).text
    assert "Torneo salvato" in client.post("/admin/torneo", data=TOURNAMENT).text
    info = db_call(service.load_tournament)
    assert (info.title, info.target_teams, info.fields, info.fee) == ("Beer Pong d'autunno", 4, 2, "12,50")
    assert "21/11 · 4 coppie" in client.get("/admin").text

    client.post("/logout")
    public = client.get("/regole").text
    assert "21/11/2026" in public and "12,50 €" in public and 'href="https://forms.gle/abc"' in public
    assert "Si passa in due." in public and "0/4" in public


def test_payments_are_confirmed_from_a_pasted_statement(client: TestClient) -> None:
    login(client, ADMIN_PIN)
    client.post("/admin/torneo", data=TOURNAMENT)
    client.post("/admin/squadre/importa", data={"testo": "\n".join(
        f"Squadra {i}; Ada {i}; Bruno {i}; 33300000{i:02d}" for i in range(6)
    )})
    ids = sorted(db_call(service.teams))
    reg = db_call(service.registration)
    assert len(reg.placed) == 4 and [t["id"] for t in reg.waiting] == ids[4:]  # 4 places, form order
    page = client.get("/admin").text
    assert service.pay_code(ids[0]) in page and "wa.me/393330000000?text=" in page and "Lista d'attesa (2)" in page
    groups, _ = db_call(service.board_groups)
    assert sorted(t["id"] for g in groups for t in g.teams) == ids[:4]  # the waiting list is not drawn

    statement = "\n".join([
        "Data;Descrizione;Importo",
        f"24/10/2026;Satispay da Ada 0 - causale {service.pay_code(ids[0]).lower()};12,50",
        f"24/10/2026;Bonifico BP-{ids[1]};12,50",
        "24/10/2026;Satispay da +39 333 000 0002;12,50",  # no code, but the couple's phone
        "24/10/2026;Satispay da sconosciuto;12,50",
    ])
    flash = client.post("/admin/pagamenti/estratto", data={"estratto": statement}).text
    assert "3 pagamenti confermati" in flash and "sconosciuto" in flash
    assert [t["paid"] for t in db_call(service.registration).placed] == [1, 1, 1, 0]
    assert '<div class="big">3/4</div><span class="muted small">pagate</span>' in client.get("/admin").text  # places only
    assert "1 già confermati" in client.post("/admin/pagamenti/estratto", data={"estratto": f"BP{ids[0]}"}).text


def test_new_tournament_archives_the_old_one_and_starts_empty(client: TestClient, tmp_path: Path) -> None:
    setup_tournament(client, 8, fields=2)
    client.post("/admin/torneo", data=TOURNAMENT)
    pin = db_call(lambda db: get_setting(db, "admin_pin"))
    assert "scrivi NUOVO" in client.post("/admin/torneo/nuovo", data={"conferma": "si"}).text
    assert db_call(service.teams)

    page = client.post("/admin/torneo/nuovo", data={"conferma": "nuovo"}).text
    assert "Torneo precedente archiviato in torneo-" in page and "Crea il torneo" in page
    assert not db_call(service.teams) and not db_call(service.matches)
    assert db_call(lambda db: (get_setting(db, "phase"), get_setting(db, "admin_pin"))) == ("setup", pin)
    (archived,) = (tmp_path / "archivio").glob("torneo-*.db")
    with closing(sqlite3.connect(archived)) as old:
        assert old.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 8


def test_each_player_gets_their_own_phone_and_invites_the_partner(client: TestClient) -> None:
    import re

    import_teams(client, 4)
    client.post("/logout")
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    t = db_call(service.team, 2)
    client.post("/checkin/2", data=checkin_form(t, phone1="333 0000001", phone2="347 0000002"))
    page = client.get("/io").text
    assert "Chi sta usando questo telefono?" in page and "/io/invito.svg" not in page  # filled the form: who?
    page = client.post("/io/chi-sono", data={"giocatore": 1}).text
    assert f"Ciao {t['p1']}!" in page and f"{t['p1']} · 📱 collegato" in page and f"{t['p2']} · non ancora" in page
    assert client.get("/io/invito.svg").headers["content-type"].startswith("image/svg+xml")
    invite = re.search(r'data-copy="([^"]+)"', page).group(1)
    assert "/entra/" in invite

    with TestClient(client.app) as partner:
        page = partner.get(invite[invite.index("/entra/"):]).text
        assert f"Ciao {t['p2']}!" in page and "questo telefono ora è il tuo" in page
        assert f"{t['p1']} · 📱 collegato" in page and f"{t['p2']} · 📱 collegato" in page
        assert "Invita" not in page.split("Mostra di nuovo il QR")[0].split('class="card phones-card"')[1]  # both in
        partner.post("/esci-squadra")
    assert db_call(service.team_devices, 2) == {1: 1, 2: 0}

    with TestClient(client.app) as stranger:
        token = invite[invite.index("/entra/") + 7:]
        assert "Invito non valido" in stranger.get(f"/entra/{token[:-2]}xx").text
        assert stranger.get("/io/invito.svg").status_code == 404


def test_the_phone_number_tells_which_player_logs_in(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import_teams(client, 4)
    client.post("/logout")
    client.get(f"/checkin?k={db_call(get_setting, 'reg_key')}")
    t = db_call(service.team, 3)
    client.post("/checkin/3", data=checkin_form(t, phone1="333 0000003", phone2="347 0000004"))
    client.post("/esci-squadra")
    with TestClient(client.app) as phone:
        phone.post("/checkin/accedi", data={"phone": "347 0000004"})
        page = phone.get("/io").text
        assert f"Ciao {t['p2']}!" in page

    monkeypatch.setattr(service, "INVITE_MAX_AGE_S", -1)
    token = db_call(service.invite_token, 3, 1)
    with TestClient(client.app) as late:
        assert "invito è scaduto" in late.get(f"/entra/{token}").text


def test_paid_couples_enter_from_home_and_check_in_at_the_venue(client: TestClient) -> None:
    import re
    from urllib.parse import unquote

    login(client, ADMIN_PIN)
    client.post("/admin/torneo", data=TOURNAMENT)
    client.post("/admin/squadre/importa", data={"testo": "Gufi; Ada; Bice; 3331110001\nLontre; Ciro; Dora; 3331110003"})
    ids = {t["name"]: t["id"] for t in db_call(service.teams).values()}
    client.post("/admin/pagamenti/estratto", data={"estratto": f"Satispay {service.pay_code(ids['Gufi'])} 12,50"})
    admin = client.get("/admin").text
    assert "Confermate (1)" in admin and "Manda accesso" in admin
    link = re.search(r"/accesso/[\w.\-]+", unquote(admin)).group(0)
    client.post("/logout")
    tv = client.get("/cartellone").text
    assert "/qr-sito.svg" in tv and "/admin/qr.svg" not in tv  # from home: no check-in QR on the board

    with TestClient(client.app) as home:
        page = home.get(link).text
        assert "Ciao Ada!" in page and "Siete arrivati alla serata?" in page
        assert not db_call(service.team, ids["Gufi"])["checked_in"]
        wrong = "1111" if db_call(service.venue_code) == "0000" else "0000"
        assert "Codice sbagliato" in home.post("/io/arrivati", data={"codice": wrong}).text
        page = home.post("/io/arrivati", data={"codice": db_call(service.venue_code)}).text
        assert "Check-in fatto ✓" in page and db_call(service.team, ids["Gufi"])["checked_in"] == 1

    with TestClient(client.app) as other:
        assert "non valido" in other.get(link[:-3] + "xyz").text
        assert "si conferma con il pagamento" in other.get(f"/accesso/{db_call(service.access_token, ids['Lontre'])}").text


def test_the_venue_qr_checks_in_a_couple_already_in_its_profile(client: TestClient) -> None:
    import_teams(client, 4)
    client.post("/admin/squadre/1/pagato", data={"pagato": 1})
    tv = client.get("/cartellone").text
    assert "/admin/qr.svg" in tv and db_call(service.venue_code) in tv  # the organizers' TV: one QR at the venue
    client.post("/logout")
    with TestClient(client.app) as phone:
        phone.get(f"/accesso/{db_call(service.access_token, 1)}")
        assert not db_call(service.team, 1)["checked_in"]
        r = phone.get(f"/checkin?k={db_call(get_setting, 'reg_key')}", follow_redirects=False)
        assert r.headers["location"] == "/io" and db_call(service.team, 1)["checked_in"] == 1
