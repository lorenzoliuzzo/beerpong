from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import live, service, tracking
from app.db import connect
from app.live import Report
from app.main import create_app

from .test_app import ADMIN_PIN, db_call, setup_tournament


def shot(i: int, device: str, at: float, side: int = 1, player: int = 1, cup: int = 3) -> Report:
    return Report(i, device, "shot", side, player, cup, at)


def test_start_time_is_median_of_taps() -> None:
    taps = [Report(i, f"d{i}", "start", None, None, None, at) for i, at in enumerate([100.0, 101.0, 140.0])]
    assert live.start_time(taps) == 101.0
    assert live.start_time([]) is None


def test_reports_close_together_are_one_shot_with_majority_cup() -> None:
    reports = [shot(1, "a", 10.0, cup=3), shot(2, "b", 11.5, cup=3), shot(3, "c", 12.0, cup=5)]
    (s,) = live.cluster_shots(reports)
    assert (s.cup, s.votes) == (3, 3)


def test_same_device_twice_means_two_shots() -> None:
    shots = live.cluster_shots([shot(1, "a", 10.0), shot(2, "a", 12.0, cup=0)])
    assert [s.cup for s in shots] == [3, 0]


def test_isolated_prank_is_rejected_by_majority() -> None:
    watchers = [shot(i, d, 10.0 + i * 0.3) for i, d in enumerate("abcd")]
    prank = shot(9, "x", 30.0, side=2, cup=1)
    accepted = live.accepted_shots(watchers + [prank])
    assert len(accepted) == 1 and accepted[0].side == 1


def test_single_watcher_counts() -> None:
    assert len(live.accepted_shots([shot(1, "a", 10.0)])) == 1


def test_replay_removes_cups_and_handles_mislabeled_cup() -> None:
    reports = [shot(1, "a", 10.0, cup=3), shot(2, "a", 30.0, cup=3), shot(3, "a", 50.0, side=2, cup=0)]
    st = live.replay(reports)
    assert st.racks[2].cups == set(range(1, 11)) - {1, 3}  # second "3" was already gone: closest cup taken
    assert st.hits == {(1, 1): 2} and st.attempts == {(1, 1): 2, (2, 1): 1}


def test_clearing_the_rack_ends_the_match() -> None:
    reports = [shot(i, "a", 10.0 * i, cup=i) for i in range(1, 11)] + [shot(99, "a", 500.0, side=2)]
    st = live.replay(reports)
    assert st.winner_side == 1 and not st.racks[2].cups and st.side_attempts(2) == 0


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
    with TestClient(create_app()) as c:
        yield c


def test_live_flow_with_several_phones(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_tournament(client, 8, fields=4)
    m = db_call(service.matches)[0]
    clock = [1000.0]
    monkeypatch.setattr("app.main.server_now", lambda: clock[0])
    monkeypatch.setattr("app.main.REPORT_MIN_GAP_S", 0)

    assert "in chiamata" in client.get("/").text  # called to the table, nobody has started it yet
    assert client.get(f"/live/{m.id}").status_code == 200
    phones = [TestClient(client.app) for _ in range(3)]
    for p in phones:
        p.get(f"/live/{m.id}")
        clock[0] += 0.5
        assert p.post(f"/api/live/{m.id}/segnala", json={"kind": "start"}).status_code == 200
    assert phones[0].post(f"/api/live/{m.id}/segnala", json={"kind": "start"}).json()["error"].startswith("Hai già")

    clock[0] = 1010.0
    for p in phones[:2]:
        p.post(f"/api/live/{m.id}/segnala", json={"kind": "shot", "side": 1, "player": 2, "cup": 4})
    state = phones[2].post(f"/api/live/{m.id}/segnala", json={"kind": "shot", "side": 1, "player": 2, "cup": 7}).json()
    assert state["started_at"] == 1001.0  # median of the three taps
    assert state["shots"][0]["removed"] == 4 and state["shots"][0]["votes"] == 3
    assert 4 not in state["teams"][1]["rack"] and len(state["teams"][1]["rack"]) == 5  # 6 cups, one taken
    assert state["prob"][0] > 0.5
    assert state["duration"] == 300

    clock[0] = 1020.0
    wrong = phones[0].post(f"/api/live/{m.id}/segnala", json={"kind": "shot", "side": 2, "player": 1, "cup": 2}).json()
    mine = wrong["mine"][0]["id"]
    assert phones[1].post(f"/api/live/{m.id}/annulla", json={"id": mine}).status_code == 400  # not their report
    after = phones[0].post(f"/api/live/{m.id}/annulla", json={"id": mine}).json()
    assert len(after["shots"]) == 1

    assert client.post(f"/api/live/{m.id}/segnala", json={"kind": "shot", "side": 3, "player": 1, "cup": 1}).status_code == 400

    # The referee confirms 1-0: tracked hits match, so the players' split is filled in automatically.
    client.post(f"/partita/{m.id}", data={"s1": 1, "s2": 0})
    view = db_call(service.match, m.id)
    assert view.split1 == (0, 1) and view.split2 == (0, 0)
    assert client.post(f"/api/live/{m.id}/segnala", json={"kind": "start"}).json()["error"] == "La partita è già conclusa."

    page = client.get("/classifiche?vista=tiri").text
    assert "Bomber" in page and "Mappa dei bicchieri" in page
    stats = db_call(tracking.shot_stats)
    assert stats.total_shots == 1 and stats.heatmap[4] == 1


def test_report_rate_limit(client: TestClient) -> None:
    setup_tournament(client, 8, fields=4)
    m = db_call(service.matches)[0]
    client.get(f"/live/{m.id}")
    assert client.post(f"/api/live/{m.id}/segnala", json={"kind": "shot", "side": 1, "player": 1, "cup": 1}).status_code == 200
    assert client.post(f"/api/live/{m.id}/segnala", json={"kind": "shot", "side": 1, "player": 1, "cup": 2}).status_code == 429


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
