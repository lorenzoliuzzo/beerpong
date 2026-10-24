from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import live
from app import main as app_main
from app.live import Report
from app.main import create_app

from .test_app import ADMIN_PIN


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


