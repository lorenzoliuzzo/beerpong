from __future__ import annotations

from dataclasses import dataclass

from .standings import Row

ROUND_NAMES = {2: "Finale", 4: "Semifinali", 8: "Quarti di finale", 16: "Ottavi di finale", 32: "Sedicesimi di finale"}


@dataclass(frozen=True)
class Seed:
    team: int
    grp: int


def round_name(round_no: int, total_rounds: int) -> str:
    return ROUND_NAMES.get(2 ** (total_rounds - round_no + 1), f"Turno {round_no}")


def seed_order(size: int) -> list[int]:
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def _per_game(row: Row) -> tuple[float, float, float, int]:
    # Groups can differ in size, so cross-group comparisons use per-match averages.
    played = row.played or 1
    return (-row.points / played, -row.diff / played, -row.cups_for / played, row.team)


def qualifier_seeds(tables: dict[int, list[Row]], qualifiers: int) -> list[Seed]:
    by_place = []
    for place in range(3):
        ranked = sorted(((g, t[place]) for g, t in tables.items() if len(t) > place), key=lambda x: _per_game(x[1]))
        by_place.append(ranked)
    thirds_needed = qualifiers - 2 * len(tables)
    picked = by_place[0] + by_place[1] + by_place[2][:thirds_needed]
    return [Seed(row.team, g) for g, row in picked]


def first_round(seeds: list[Seed], size: int) -> list[tuple[Seed | None, Seed | None]]:
    def at(s: int) -> Seed | None:
        return seeds[s - 1] if s <= len(seeds) else None

    order = seed_order(size)
    pairs = [[at(order[i]), at(order[i + 1])] for i in range(0, size, 2)]
    # Avoid replaying a group match in the first round by swapping the lower seeds of two matches.
    for i, (a, b) in enumerate(pairs):
        if not (a and b and a.grp == b.grp):
            continue
        for j, (c, d) in enumerate(pairs):
            if j != i and d and d.grp != a.grp and (c is None or c.grp != b.grp):
                pairs[i][1], pairs[j][1] = d, b
                break
    return [(a, b) for a, b in pairs]
