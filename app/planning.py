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


def choose_group_count(n_teams: int, fields: int = 1) -> int:
    if n_teams < 4:
        raise ValueError("Servono almeno 4 squadre.")
    candidates = [
        g
        for g in range(1, n_teams + 1)
        if -(-n_teams // g) <= MAX_GROUP and n_teams // g >= MIN_GROUP
    ]
    # Prefer counts whose bracket can be filled with thirds alone, without byes.
    full = [g for g in candidates if bracket_plan(g)[0] <= 3 * g]
    # Above that, the same number of groups on every table: each table then runs its own queue
    # (see build_table_queues) and none of them finishes far earlier than the others.
    even = [g for g in candidates if g % fields == 0]
    pool = [g for g in full if g in even] or even or full or candidates
    return min(pool, key=lambda g: (abs(n_teams / g - TARGET_GROUP), g))


def group_sizes(n_teams: int, fields: int = 1) -> list[int]:
    g = choose_group_count(n_teams, fields)
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


def table_for_group(grp: int, n_groups: int, fields: int) -> int:
    """Assign contiguous groups to tables 1..fields (e.g. 8 groups on 4 tables -> A-B:1, C-D:2, E-F:3, G-H:4)."""
    if fields <= 1 or n_groups <= 0:
        return 1
    if n_groups >= fields:
        return min(fields, (grp * fields) // n_groups + 1)
    return (grp % fields) + 1


def build_table_queues(groups: list[list[int]], fields: int) -> dict[int, list[Fixture]]:
    """Build an independent match queue for each table (1..fields) from its assigned groups."""
    n_groups = len(groups)
    by_table: dict[int, dict[int, list[int]]] = {f: {} for f in range(1, fields + 1)}
    for gi, members in enumerate(groups):
        t_no = table_for_group(gi, n_groups, fields)
        by_table.setdefault(t_no, {})[gi] = members

    queues: dict[int, list[Fixture]] = {}
    for f, grp_map in sorted(by_table.items()):
        if not grp_map:
            queues[f] = []
            continue
        per_group = {gi: round_robin(members) for gi, members in grp_map.items()}
        max_rounds = max((len(r) for r in per_group.values()), default=0)
        raw_fixtures: list[Fixture] = []
        for r in range(max_rounds):
            for gi, rounds in per_group.items():
                if r < len(rounds):
                    raw_fixtures += [Fixture(gi, r + 1, a, b) for a, b in rounds[r]]
        # Order single-table queue so no team plays back-to-back and everyone rests evenly
        queues[f] = [slot[0] for slot in pack_slots(raw_fixtures, 1)]
    return queues

