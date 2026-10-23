from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import live, service, tracking
from app import main as app_main
from app.db import connect
from app.live import Report
from app.main import create_app

from .test_app import ADMIN_PIN, db_call, setup_tournament


def shot(i: int, device: str, at: float, side: int = 1, player: int = 1, cup: int = 3) -> Report:
    return Report(i, device, "shot", side, player, cup, at)


def test_only_the_referees_shots_count() -> None:
    fan = Report(1, "some-phone", "shot", 1, 1, 3, 10.0)
    ref = Report(2, live.REFEREE, "shot", 1, 2, 4, 11.0)
    assert [(s.player, s.cup) for s in live.accepted_shots([fan, ref])] == [(2, 4)]
    assert live.accepted_shots([fan]) == []


def test_replay_removes_cups_and_handles_a_cup_already_gone() -> None:
    taps = [Report(1, live.REFEREE, "shot", 1, 1, 3, 10.0), Report(2, live.REFEREE, "shot", 1, 2, 3, 11.0),
            Report(3, live.REFEREE, "shot", 2, 1, 0, 12.0)]
    st = live.replay(taps)
    assert st.racks[2].cups == set(range(1, 11)) - {1, 3}  # the second "3" was already gone: the closest one went
    assert st.hits == {(1, 1): 1, (1, 2): 1} and st.attempts == {(1, 1): 1, (1, 2): 1, (2, 1): 1}


def test_win_probability_behaves() -> None:
    even = live.win_probability((10, 10), (0.3, 0.3), None)
    assert 0.4 < even < 0.65  # side 1 shoots first
    assert live.win_probability((1, 10), (0.3, 0.3), None) > 0.9
    assert live.win_probability((10, 1), (0.3, 0.3), None) < 0.1
    assert live.win_probability((3, 5), (0.3, 0.3), 0) == 1.0  # time is up: fewer cups left wins
    assert live.win_probability((0, 4), (0.3, 0.3), None) == 1.0


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("BP_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("BP_ADMIN_PIN", ADMIN_PIN)
    app_main._report_times.clear()
    with TestClient(create_app()) as c:
        yield c


def test_the_live_page_is_for_watching(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    m = db_call(service.matches)[0]
    assert "in chiamata" in client.get("/").text  # called to the table, the referee has not started it yet
    page = client.get(f"/live/{m.id}").text
    assert "Chi sta tirando" not in page and 'id="sheet"' not in page and "segna l'arbitro" in page
    for path in ("segnala", "annulla"):  # nobody but the referee records shots
        r = client.post(f"/api/live/{m.id}/{path}", json={"kind": "shot", "side": 1, "player": 1, "cup": 1})
        assert r.status_code in (404, 405)

    # The referee scores, and that is what the live page and the statistics show.
    db_call(lambda db: service.start_match(db, m.id, 1000.0, 1))
    db_call(lambda db: tracking.referee_shot(db, m.id, 2, 4, 1010.0, 0))
    state = client.get(f"/api/live/{m.id}").json()
    assert state["started_at"] == 1000.0 and state["duration"] == 300
    assert 4 not in state["teams"][1]["rack"] and len(state["teams"][1]["rack"]) == 5 and state["prob"][0] > 0.5
    client.post(f"/partita/{m.id}", data={"s1": 1, "s2": 0})
    assert db_call(service.match, m.id).split1 == (0, 1)  # the referee's hits fill in the players' split
    stats = db_call(tracking.shot_stats)
    assert stats.total_shots == 1 and stats.heatmap[4] == 1
    assert "Bomber" in client.get("/classifiche?vista=tiri").text


def test_cheer_rate_limit(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    m = db_call(service.matches)[0]
    assert client.post(f"/api/live/{m.id}/tifo", json={"side": 1, "emoji": "🍺"}).status_code == 200
    assert client.post(f"/api/live/{m.id}/tifo", json={"side": 1, "emoji": "🔥"}).status_code == 429  # too close


def _ref(cups_seq: list[tuple[int, int, int]]) -> list[live.Report]:
    """(side, player, cup) referee taps, one second apart."""
    return [live.Report(i, live.REFEREE, "shot", s, p, c, 100.0 + i) for i, (s, p, c) in enumerate(cups_seq)]


def test_group_match_ends_when_a_rack_is_cleared() -> None:
    seq = []
    for k in range(3):  # couple 1 hits both shots of each turn, couple 2 misses
        seq += [(1, 1, 2 * k + 1), (1, 2, 2 * k + 2), (2, 1, 0), (2, 2, 0)]
    st = live.replay(_ref(seq[:-2]), 6, 1, salvezza=False)
    assert st.winner_side == 1 and st.phase == "over"


def test_salvezza_missed_ends_the_knockout_match() -> None:
    seq = []
    for k in range(3):
        seq += [(1, 1, 2 * k + 1), (1, 2, 2 * k + 2)] + ([(2, 1, 0), (2, 2, 0)] if k < 2 else [])
    st = live.replay(_ref(seq), 6, 1, salvezza=True)
    assert st.winner_side is None and st.phase == "salvezza" and st.turn_side == 2 and not st.in_turn
    st = live.replay(_ref(seq + [(2, 1, 0), (2, 2, 0)]), 6, 1, salvezza=True)
    assert st.winner_side == 1 and st.phase == "over"


def test_salvezza_hit_goes_to_overtime_with_three_cups() -> None:
    seq = []
    for k in range(3):
        seq += [(1, 1, 2 * k + 1), (1, 2, 2 * k + 2)] + ([(2, 1, 0), (2, 2, 0)] if k < 2 else [])
    seq += [(2, 1, 4), (2, 2, 0)]  # saved with one hit
    st = live.replay(_ref(seq), 6, 1, salvezza=True)
    assert st.phase == "overtime" and st.rack_cups == 3 and st.turn_side == 1 and st.winner_side is None
    assert st.racks[1].cups == {1, 2, 3} and st.racks[2].cups == {1, 2, 3} and st.phase_start == len(seq)
    # the rack is cleared at the first shot of a turn: the salvezza starts at once, the turn is over
    st = live.replay(_ref(seq + [(1, 1, 1), (1, 2, 2), (2, 1, 0), (2, 2, 0), (1, 1, 3)]), 6, 1, salvezza=True)
    assert st.phase == "salvezza" and st.turn_side == 2
    assert st.side_hits(1) == 9 and st.side_hits(2) == 1  # the final score counts every cup


def test_rerack_compacts_the_remaining_cups() -> None:
    seq = [(1, 1, 1), (1, 2, 4), (2, 1, 0), (2, 2, 0), (1, 1, 6), (1, 2, 0)]  # couple 2 keeps cups 2, 3, 5
    reports = _ref(seq)
    st = live.replay(reports, 6, 1, reracks=[(2, 106.0)])
    assert st.racks[2].cups == {1, 2, 3} and st.rows[2] == live.FORMATIONS[3] and st.rack_start[2] == len(seq)
    assert st.rows[1] == live.RACK_ROWS[6]  # the other rack is untouched
    st = live.replay(_ref(seq + [(2, 1, 0), (2, 2, 0), (1, 1, 3)]), 6, 1, reracks=[(2, 106.0)])
    assert st.racks[2].cups == {1, 2} and st.side_hits(1) == 4


def test_live_view_has_trend_streak_cheers_and_other_tables(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.REPORT_MIN_GAP_S", 0)
    setup_tournament(client, 8, fields=2)
    m = next(x for x in db_call(service.matches) if x.playing and x.field == 1)
    other = next(x for x in db_call(service.matches) if x.playing and x.field == 2)
    tracking._others_cache = (0.0, [])  # a fresh tournament: do not reuse another test's strip
    db_call(lambda db: service.start_match(db, m.id, 1000.0, 1))
    for n, (pl, cup) in enumerate([(1, 1), (2, 2), (1, 0), (2, 0), (1, 3)]):
        db_call(lambda db: tracking.referee_shot(db, m.id, pl, cup, 1001.0 + n, n))
    data = client.get(f"/api/live/{m.id}").json()
    assert len(data["timeline"]) == 1 + 3 and all(0 <= p["p"] <= 1 for p in data["timeline"])
    assert data["timeline"][-1]["p"] > data["timeline"][0]["p"]  # couple 1 leads 3-0
    assert data["streak"] == [3, 0]  # couple 1 hit all three of its shots; the misses were couple 2
    assert [o["id"] for o in data["others"]] == [other.id]

    r = client.post(f"/api/live/{m.id}/tifo", json={"side": 2, "emoji": "🔥"})
    assert r.status_code == 200 and r.json()["cheers"][1]["🔥"] == 1
    assert client.post(f"/api/live/{m.id}/tifo", json={"side": 2, "emoji": "💩"}).status_code == 400
    page = client.get(f"/live/{m.id}").text
    assert 'id="trend"' in page and 'id="cheer1"' in page and 'id="others"' in page


def test_one_cheer_of_each_kind_per_account_and_none_while_playing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.REPORT_MIN_GAP_S", 0)
    client.post("/login", data={"pin": ADMIN_PIN})
    client.post("/admin/squadre/importa", data={"testo": "\n".join(f"S{i}; A{i}; B{i}; 33311100{i:02d}" for i in range(8))})
    client.post("/admin/impostazioni", data={"title": "T", "fields": 2, "group_min": 5, "ko_min": 7, "buffer_min": 1})
    for t in db_call(service.teams).values():
        client.post(f"/admin/squadre/{t['id']}/presente", data={"presente": 1})
    client.post("/admin/avvia")
    client.post("/logout")
    m = next(x for x in db_call(service.matches) if x.playing)

    def tifo(c: TestClient, side: int = 1, emo: str = "🍺"):
        return c.post(f"/api/live/{m.id}/tifo", json={"side": side, "emoji": emo})

    r = tifo(client)
    assert r.json()["cheers"][0]["🍺"] == 1 and r.json()["my_cheers"] == [[1, "🍺"]]
    assert tifo(client, 1, "🔥").json()["cheers"][0]["🔥"] == 1  # another kind counts on its own
    r = tifo(client)  # the same one again takes it back
    assert r.json()["cheers"][0]["🍺"] == 0 and r.json()["my_cheers"] == [[1, "🔥"]]

    # a couple cheers once whatever the number of its phones
    waiting = next(t for t in db_call(service.teams).values() if t["id"] not in (m.t1, m.t2))
    phones = [TestClient(client.app) for _ in range(2)]
    for ph in phones:
        assert "Bentornati" in ph.post("/checkin/accedi", data={"phone": waiting["phone1"]}).text
    assert tifo(phones[0], 2, "👏").json()["cheers"][1]["👏"] == 1
    assert tifo(phones[1], 2, "👏").json()["cheers"][1]["👏"] == 0  # same couple: the second tap took it back

    # a couple whose match is on can neither cheer nor report shots, and the page knows it
    db_call(lambda db: service.start_match(db, m.id, 1000.0, 1))
    with TestClient(client.app) as at_table:
        at_table.post("/checkin/accedi", data={"phone": db_call(service.team, m.t1)["phone1"]})
        r = tifo(at_table)
        assert r.status_code == 403 and "occhi sul tavolo" in r.json()["error"]
        assert at_table.get(f"/api/live/{m.id}").json()["you_play"] is True
    assert client.get(f"/api/live/{m.id}").json()["you_play"] is False
