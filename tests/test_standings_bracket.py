from app import bracket
from app.standings import Result, Row, compute_table


def test_table_orders_by_points_then_cups() -> None:
    results = [Result(1, 2, 6, 3), Result(1, 3, 2, 5), Result(2, 3, 4, 4)]
    table = compute_table([1, 2, 3], results)
    assert [(r.team, r.points, r.diff) for r in table] == [(3, 4, 3), (1, 3, 0), (2, 1, -3)]


def test_head_to_head_beats_cup_difference() -> None:
    # 1 and 2 tie on points; 2 has the better difference but lost the direct match.
    results = [Result(1, 2, 3, 2), Result(1, 3, 1, 9), Result(2, 3, 9, 1), Result(3, 4, 5, 0)]
    table = compute_table([1, 2, 3, 4], results)
    assert [r.team for r in table] == [3, 1, 2, 4]
    assert table[1].diff < table[2].diff


def test_seed_order() -> None:
    assert bracket.seed_order(2) == [1, 2]
    assert bracket.seed_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]


def test_round_names() -> None:
    assert [bracket.round_name(r, 4) for r in range(1, 5)] == ["Ottavi di finale", "Quarti di finale", "Semifinali", "Finale"]


def _row(team: int, points: int, played: int = 4) -> Row:
    return Row(team, played=played, won=points // 3, drawn=points % 3, lost=played - points // 3 - points % 3)


def test_qualifiers_use_best_thirds_per_match() -> None:
    tables = {
        0: [_row(1, 12), _row(2, 9), _row(3, 6), _row(4, 0)],
        1: [_row(5, 12), _row(6, 6), _row(7, 4, played=3), _row(8, 0)],  # 3-match group: 1.33 pts/match
        2: [_row(9, 9), _row(10, 6), _row(11, 3), _row(12, 0)],
    }
    seeds = bracket.qualifier_seeds(tables, qualifiers=8)
    assert [s.team for s in seeds] == [1, 5, 9, 2, 6, 10, 3, 7]


def test_first_round_byes_go_to_top_seeds_and_avoid_group_rematch() -> None:
    seeds = [bracket.Seed(t, g) for t, g in [(1, 0), (2, 1), (3, 0), (4, 1), (5, 1), (6, 0)]]
    pairs = bracket.first_round(seeds, 8)
    assert pairs[0] == (seeds[0], None)
    assert sum(1 for a, b in pairs if b is None) == 2
    for a, b in pairs:
        if a and b:
            assert a.grp != b.grp
