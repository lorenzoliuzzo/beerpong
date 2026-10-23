"""Live shot tracking: the referee's shots replayed turn by turn, a shared clock, win odds.

Pure functions over plain data, so the consensus rules can be tested without a database.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

CUPS = 10  # the highest cup number: the final's rack; other matches use the first 6 (see service.cups_in_play)
DEFAULT_SECONDS_PER_SHOT = 12.0  # per team, until the match itself gives a better estimate
PRIOR_HITS, PRIOR_SHOTS = 3, 10  # Beta prior: an unknown team hits about 30% of its shots
SIMULATIONS = 1000
REFEREE = "arbitro"  # device id of the table's referee: once they score a match, only their shots count


@dataclass(frozen=True)
class Report:
    id: int
    device: str
    kind: str  # "start" | "shot"
    side: int | None  # 1 or 2: the team shooting
    player: int | None  # 1 or 2 within that team
    cup: int | None  # 1..10 hit on the opponent's rack, 0 = miss
    at: float  # server time, seconds


@dataclass
class Shot:
    side: int
    player: int
    cup: int  # 0 = miss
    at: float
    votes: int
    devices: frozenset[str]
    removed: int | None = None  # the cup actually taken off the rack, after replay


def accepted_shots(reports: list[Report]) -> list[Shot]:
    """The referee's shots, in order: only the table's referee scores a match, every tap is one shot."""
    official = sorted((r for r in reports if r.kind == "shot" and r.device == REFEREE), key=lambda r: (r.at, r.id))
    return [Shot(r.side, r.player, r.cup, r.at, 1, frozenset({r.device})) for r in official]


# Rows as the shooter sees them, the far row first and the apex closest.
RACK_ROWS = {3: [[2, 3], [1]], 6: [[4, 5, 6], [2, 3], [1]], 10: [[7, 8, 9, 10], [4, 5, 6], [2, 3], [1]]}
OVERTIME_CUPS = 3
# What a re-rack turns the remaining cups into, numbered 1..n again (far row first, apex towards the shooters).
FORMATIONS = {1: [[1]], 2: [[2], [1]], 3: [[2, 3], [1]], 4: [[4], [2, 3], [1]], 5: [[4, 5], [2, 3], [1]], 6: [[4, 5, 6], [2, 3], [1]]}


@dataclass
class Rack:
    cups: set[int] = field(default_factory=lambda: set(range(1, CUPS + 1)))


@dataclass
class LiveState:
    started_at: float | None
    shots: list[Shot]
    racks: dict[int, Rack]  # side -> its own cups still standing
    hits: dict[tuple[int, int], int]  # (side, player) -> hits
    attempts: dict[tuple[int, int], int]
    winner_side: int | None  # a rack was cleared (and, in the knockout, the tiro della salvezza missed)
    # Only for matches the referee scores, who follows the turns:
    phase: str = "play"  # "play" | "salvezza" | "overtime" | "over"
    phase_start: int = 0  # index in shots where the racks on the table were set up
    turn_side: int | None = None
    in_turn: frozenset[int] = frozenset()  # players of turn_side who already shot this turn
    rack_cups: int = CUPS
    rows: dict[int, list[list[int]]] = field(default_factory=dict)  # side -> layout of its rack on the table now
    rack_start: dict[int, int] = field(default_factory=dict)  # side -> index in shots since its rack was set up

    def side_hits(self, side: int) -> int:
        return sum(v for (s, _), v in self.hits.items() if s == side)

    def side_attempts(self, side: int) -> int:
        return sum(v for (s, _), v in self.attempts.items() if s == side)


def _referee_replay(
    shots_in: list[Shot], started: float | None, cups: int, first_side: int, salvezza: bool,
    reracks: list[tuple[int, float]] = (),
) -> LiveState:
    """The referee's shots, turn by turn: two shots per couple (one per player), the couples alternating.

    Clearing a rack ends the match, except in the knockout: there the other couple gets the tiro della
    salvezza, one shot per player; one hit sends the match to overtime with OVERTIME_CUPS each."""
    def fresh(n: int) -> dict[int, Rack]:
        return {1: Rack(set(range(1, n + 1))), 2: Rack(set(range(1, n + 1)))}

    racks, rack_cups = fresh(cups), cups
    rows = {1: RACK_ROWS[cups], 2: RACK_ROWS[cups]}
    rack_start = {1: 0, 2: 0}
    pending_reracks = sorted(reracks, key=lambda r: r[1])
    hits: Counter[tuple[int, int]] = Counter()
    attempts: Counter[tuple[int, int]] = Counter()
    shots: list[Shot] = []
    phase, phase_start, winner = "play", 0, None
    side, in_turn, taken = first_side, [], 0  # taken: shots of the current turn
    pending, saving_hits = None, 0
    def apply_reracks(until: float) -> None:
        while pending_reracks and pending_reracks[0][1] <= until:
            rack_side, _ = pending_reracks.pop(0)
            n = len(racks[rack_side].cups)
            if n in FORMATIONS:
                racks[rack_side].cups = set(range(1, n + 1))
                rows[rack_side], rack_start[rack_side] = FORMATIONS[n], len(shots)

    for shot in shots_in:
        if winner is not None:
            break
        apply_reracks(shot.at)
        target = racks[3 - shot.side]
        attempts[(shot.side, shot.player)] += 1
        hit = False
        if shot.cup:
            removed = shot.cup if shot.cup in target.cups else min(target.cups, default=None)
            if removed is not None:
                target.cups.discard(removed)
                hits[(shot.side, shot.player)] += 1
                shot.removed = removed
                hit = True
        shots.append(shot)
        taken += 1
        in_turn.append(shot.player)
        if phase == "salvezza":
            saving_hits += hit
            if taken < 2 and target.cups:
                continue
            if not saving_hits:
                winner, phase = pending, "over"
            else:  # saved: overtime, the couple that had cleared the rack shoots first
                racks, rack_cups, phase, phase_start = fresh(OVERTIME_CUPS), OVERTIME_CUPS, "overtime", len(shots)
                rows = {1: RACK_ROWS[OVERTIME_CUPS], 2: RACK_ROWS[OVERTIME_CUPS]}
                rack_start = {1: len(shots), 2: len(shots)}
                side, in_turn, taken = pending, [], 0
            continue
        if not target.cups:
            if salvezza:
                phase, pending, saving_hits = "salvezza", shot.side, 0
                side, in_turn, taken = 3 - shot.side, [], 0
            else:
                winner, phase = shot.side, "over"
            continue
        if taken >= 2:
            side, in_turn, taken = 3 - side, [], 0
    if winner is None:
        apply_reracks(float("inf"))  # a re-rack after the last shot already shapes the rack on the table
    return LiveState(
        started, shots, racks, dict(hits), dict(attempts), winner,
        phase, phase_start, side, frozenset(in_turn), rack_cups, rows, rack_start,
    )


def replay(
    reports: list[Report], cups: int = CUPS, first_side: int = 1, salvezza: bool = False,
    reracks: list[tuple[int, float]] = (),
) -> LiveState:
    return _referee_replay(accepted_shots(reports), None, cups, first_side, salvezza, reracks)


def hit_rate(hits: int, shots: int, prior: float | None = None) -> float:
    """Expected hit rate: the prior (the couple's questionnaire, else 30%) worth PRIOR_SHOTS shots, then real shots."""
    prior_hits = PRIOR_HITS if prior is None else prior * PRIOR_SHOTS
    return (hits + prior_hits) / (shots + PRIOR_SHOTS)


def win_probability(
    cups_to_hit: tuple[int, int], rates: tuple[float, float], shots_left: int | None, seed: int = 0,
    simulations: int = SIMULATIONS,
) -> float:
    """Chance that side 1 wins: teams alternate two-shot turns until a rack is cleared or time runs out.

    `shots_left` is per team; None means no time limit (the final). On time-out the team with fewer cups
    left to hit wins; a tie is an overtime coin flip weighted by accuracy.
    """
    need1, need2 = cups_to_hit
    if need1 == 0:
        return 1.0
    if need2 == 0:
        return 0.0
    rng = random.Random(seed)
    wins = 0.0
    cap = shots_left if shots_left is not None else 400
    for _ in range(simulations):
        left1, left2, taken = need1, need2, 0
        while taken < cap and left1 > 0 and left2 > 0:
            for _ in range(2):
                left1 -= rng.random() < rates[0]
            if left1 <= 0:
                break
            for _ in range(2):
                left2 -= rng.random() < rates[1]
            taken += 2
        if left1 <= 0:
            wins += 1
        elif left2 <= 0:
            continue
        elif left1 < left2:
            wins += 1
        elif left1 == left2:
            wins += rates[0] / (rates[0] + rates[1])
    return wins / simulations


def shots_left_per_team(state: LiveState, now: float, duration_s: float | None) -> int | None:
    if duration_s is None:
        return None
    if state.started_at is None:
        elapsed = 0.0
        pace = DEFAULT_SECONDS_PER_SHOT
    else:
        elapsed = max(0.0, now - state.started_at)
        per_team = max(state.side_attempts(1), state.side_attempts(2))
        pace = elapsed / per_team if per_team >= 4 else DEFAULT_SECONDS_PER_SHOT
    return max(0, int((duration_s - elapsed) / max(pace, 3.0)))


def timeline(state: LiveState, rates: tuple[float, float], duration_s: float | None, cups: int, seed: int = 0) -> list[dict]:
    """Side 1's chance to win at the start and after every hit, for the live chart (misses barely move it)."""
    need = {1: cups, 2: cups}  # cups still standing in each side's rack
    points = [{"at": state.started_at, "p": round(win_probability((cups, cups), rates, None, seed, 300), 3)}]
    for i, shot in enumerate(state.shots):
        if state.phase_start and i == state.phase_start:
            need = {1: OVERTIME_CUPS, 2: OVERTIME_CUPS}
        if shot.removed is None:
            continue
        need[3 - shot.side] = max(0, need[3 - shot.side] - 1)
        left = None
        if duration_s is not None and state.started_at is not None:
            left = max(0, int((duration_s - (shot.at - state.started_at)) / DEFAULT_SECONDS_PER_SHOT))
        points.append({"at": shot.at, "p": round(win_probability((need[2], need[1]), rates, left, seed + i, 300), 3)})
    return points


def streak(state: LiveState, side: int) -> int:
    """Hits in a row of `side`, counting back from its last shot."""
    n = 0
    for shot in reversed([s for s in state.shots if s.side == side]):
        if shot.removed is None:
            break
        n += 1
    return n
