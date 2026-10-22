from __future__ import annotations

from dataclasses import dataclass

MIN_GROUP = 3
MAX_GROUP = 6
TARGET_GROUP = 5


@dataclass(frozen=True)
class Fixture:
    grp: int
    round: int
    team1: int
    team2: int


def next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def bracket_plan(group_count: int) -> tuple[int, int]:
    """Return (bracket size, qualifiers): top 2 per group plus best thirds, byes for the rest."""
    size = max(2, next_pow2(2 * group_count))
    return size, min(size, 3 * group_count)


def choose_group_count(n_teams: int) -> int:
    if n_teams < 4:
        raise ValueError("Servono almeno 4 squadre.")
    candidates = [
        g
        for g in range(1, n_teams + 1)
        if -(-n_teams // g) <= MAX_GROUP and n_teams // g >= MIN_GROUP
    ]
    # Prefer counts whose bracket can be filled with thirds alone, without byes.
    full = [g for g in candidates if bracket_plan(g)[0] <= 3 * g]
    pool = full or candidates
    return min(pool, key=lambda g: (abs(n_teams / g - TARGET_GROUP), g))


def group_sizes(n_teams: int) -> list[int]:
    g = choose_group_count(n_teams)
    base, extra = divmod(n_teams, g)
    return [base + 1] * extra + [base] * (g - extra)


def round_robin(teams: list[int]) -> list[list[tuple[int, int]]]:
    ring: list[int | None] = list(teams)
    if len(ring) % 2:
        ring.append(None)
    n = len(ring)
    rounds = []
    for _ in range(n - 1):
        pairs = [(ring[i], ring[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) for a, b in pairs if a is not None and b is not None])
        ring = [ring[0], ring[-1], *ring[1:-1]]
    return rounds


def build_fixtures(groups: list[list[int]]) -> list[Fixture]:
    per_group = [round_robin(g) for g in groups]
    max_rounds = max(len(r) for r in per_group)
    fixtures = []
    for r in range(max_rounds):
        for gi, rounds in enumerate(per_group):
            if r < len(rounds):
                fixtures += [Fixture(gi, r + 1, a, b) for a, b in rounds[r]]
    return fixtures


def pack_slots(fixtures: list[Fixture], fields: int) -> list[list[Fixture]]:
    """Fill time slots greedily, favouring matches whose teams have rested longest."""
    remaining = list(enumerate(fixtures))
    last_played: dict[int, int] = {}
    slots: list[list[Fixture]] = []
    while remaining:
        ranked = sorted(
            remaining,
            key=lambda p: (max(last_played.get(p[1].team1, -9), last_played.get(p[1].team2, -9)), p[0]),
        )
        busy: set[int] = set()
        chosen: list[tuple[int, Fixture]] = []
        for i, f in ranked:
            if len(chosen) == fields:
                break
            if f.team1 in busy or f.team2 in busy:
                continue
            chosen.append((i, f))
            busy |= {f.team1, f.team2}
        for _, f in chosen:
            last_played[f.team1] = last_played[f.team2] = len(slots)
        chosen_ids = {i for i, _ in chosen}
        remaining = [p for p in remaining if p[0] not in chosen_ids]
        slots.append([f for _, f in chosen])
    return slots
