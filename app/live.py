"""Live shot tracking: crowd-sourced reports reconciled by majority, a shared clock, win odds.

Pure functions over plain data, so the consensus rules can be tested without a database.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from dataclasses import dataclass, field

CUPS = 10
SHOT_WINDOW_S = 6.0  # reports of the same player this close together describe one shot
ACTIVE_WINDOW_S = 60.0  # a device "is watching" a shot if it reported anything this close to it
DEFAULT_SECONDS_PER_SHOT = 12.0  # per team, until the match itself gives a better estimate
PRIOR_HITS, PRIOR_SHOTS = 3, 10  # Beta prior: an unknown team hits about 30% of its shots
SIMULATIONS = 1000


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


def start_time(reports: list[Report]) -> float | None:
    """The median of everyone's "start" taps, so one early or late tap barely moves the clock."""
    starts = sorted(r.at for r in reports if r.kind == "start")
    return statistics.median(starts) if starts else None


def cluster_shots(reports: list[Report]) -> list[Shot]:
    clusters: list[list[Report]] = []
    open_by_player: dict[tuple[int, int], list[Report]] = {}
    for r in sorted((r for r in reports if r.kind == "shot"), key=lambda r: r.at):
        key = (r.side, r.player)
        current = open_by_player.get(key)
        if current is None or r.at - current[0].at > SHOT_WINDOW_S or any(x.device == r.device for x in current):
            current = [r]
            clusters.append(current)
            open_by_player[key] = current
        else:
            current.append(r)
    shots = []
    for c in clusters:
        votes = Counter(r.cup for r in c)
        top = max(votes.values())
        # Ties go to the most recent report among the tied answers.
        cup = next(r.cup for r in reversed(c) if votes[r.cup] == top)
        shots.append(Shot(c[0].side, c[0].player, cup, statistics.median(r.at for r in c), len(c), frozenset(r.device for r in c)))
    return sorted(shots, key=lambda s: s.at)


def accepted_shots(reports: list[Report]) -> list[Shot]:
    """A shot counts when at least half of the devices following the match around that moment reported it."""
    accepted = []
    for shot in cluster_shots(reports):
        watching = {r.device for r in reports if abs(r.at - shot.at) <= ACTIVE_WINDOW_S}
        if shot.votes >= math.ceil(len(watching) / 2):
            accepted.append(shot)
    return accepted


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
    winner_side: int | None  # a rack was cleared

    def side_hits(self, side: int) -> int:
        return sum(v for (s, _), v in self.hits.items() if s == side)

    def side_attempts(self, side: int) -> int:
        return sum(v for (s, _), v in self.attempts.items() if s == side)


def replay(reports: list[Report]) -> LiveState:
    racks = {1: Rack(), 2: Rack()}
    hits: Counter[tuple[int, int]] = Counter()
    attempts: Counter[tuple[int, int]] = Counter()
    shots: list[Shot] = []
    winner = None
    for shot in accepted_shots(reports):
        if winner is not None:
            break
        target = racks[3 - shot.side]
        attempts[(shot.side, shot.player)] += 1
        if shot.cup:
            # A cup already gone means the reporters mislabeled it: still a hit, take the closest one left.
            removed = shot.cup if shot.cup in target.cups else min(target.cups, default=None)
            if removed is not None:
                target.cups.discard(removed)
                hits[(shot.side, shot.player)] += 1
                shot.removed = removed
            if not target.cups:
                winner = shot.side
        shots.append(shot)
    return LiveState(start_time(reports), shots, racks, dict(hits), dict(attempts), winner)


def hit_rate(hits: int, shots: int) -> float:
    return (hits + PRIOR_HITS) / (shots + PRIOR_SHOTS)


def win_probability(
    cups_to_hit: tuple[int, int], rates: tuple[float, float], shots_left: int | None, seed: int = 0
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
    for _ in range(SIMULATIONS):
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
    return wins / SIMULATIONS


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
