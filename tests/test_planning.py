from collections import Counter

import pytest

from app import planning


@pytest.mark.parametrize(
    ("n", "sizes", "bracket"),
    [
        (4, [4], (2, 2)),
        (8, [4, 4], (4, 4)),
        (30, [5] * 6, (16, 16)),
        (32, [6, 6, 5, 5, 5, 5], (16, 16)),
        (48, [5] * 4 + [4] * 7, (32, 32)),
    ],
)
def test_group_sizes_and_bracket(n: int, sizes: list[int], bracket: tuple[int, int]) -> None:
    assert planning.group_sizes(n) == sizes
    assert planning.bracket_plan(len(sizes)) == bracket


@pytest.mark.parametrize("n", range(4, 49))
def test_group_sizes_always_valid(n: int) -> None:
    sizes = planning.group_sizes(n)
    assert sum(sizes) == n
    assert all(planning.MIN_GROUP <= s <= planning.MAX_GROUP for s in sizes)
    size, qualifiers = planning.bracket_plan(len(sizes))
    assert 2 * len(sizes) <= qualifiers <= size < 2 * qualifiers


def test_too_few_teams() -> None:
    with pytest.raises(ValueError):
        planning.group_sizes(3)


@pytest.mark.parametrize("n", [3, 4, 5, 6])
def test_round_robin_plays_everyone_once(n: int) -> None:
    teams = list(range(n))
    pairs = [frozenset(p) for rnd in planning.round_robin(teams) for p in rnd]
    assert len(pairs) == len(set(pairs)) == n * (n - 1) // 2
    for rnd in planning.round_robin(teams):
        flat = [t for p in rnd for t in p]
        assert len(flat) == len(set(flat))


@pytest.mark.parametrize("fields", [1, 2, 4, 6])
def test_pack_slots(fields: int) -> None:
    groups = [list(range(g * 5, g * 5 + 5)) for g in range(6)]
    fixtures = planning.build_fixtures(groups)
    slots = planning.pack_slots(fixtures, fields)

    assert Counter(f for s in slots for f in s) == Counter(fixtures)
    for s in slots:
        assert len(s) <= fields
        teams = [t for f in s for t in (f.team1, f.team2)]
        assert len(teams) == len(set(teams))
    # With 30 teams no slot should be left half empty except the tail.
    assert all(len(s) == fields for s in slots[:-2])
    if fields <= 4:
        back_to_back = sum(
            1
            for a, b in zip(slots, slots[1:])
            for t in {x for f in a for x in (f.team1, f.team2)} & {x for f in b for x in (f.team1, f.team2)}
        )
        assert back_to_back == 0
