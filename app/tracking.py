"""Database side of live tracking: store taps, build the live view, derive tournament-wide shot stats."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import live, service
from .service import TournamentError

# Many phones poll the same match every couple of seconds: replay only when its reports change,
# and rebuild tournament-wide context only when any report or result changes.
_state_cache: dict[int, tuple[tuple[int, int], live.LiveState]] = {}
_context_cache: list[tuple[tuple, dict]] = []


def reports(db: sqlite3.Connection, match_id: int) -> list[live.Report]:
    return [
        live.Report(r["id"], r["device"], r["kind"], r["side"], r["player"], r["cup"], r["at"])
        for r in db.execute("SELECT * FROM reports WHERE match_id = ? ORDER BY at", (match_id,))
    ]


def state(db: sqlite3.Connection, match_id: int) -> live.LiveState:
    """Replayed live state, cached until a report of this match is added or removed."""
    reracks = [(r["side"], r["at"]) for r in db.execute("SELECT side, at FROM reracks WHERE match_id = ? ORDER BY at, id", (match_id,))]
    key = tuple(db.execute(
        "SELECT COUNT(r.id), COALESCE(MAX(r.id), 0), m.started_at, m.first_side, m.stage"
        " FROM matches m LEFT JOIN reports r ON r.match_id = m.id WHERE m.id = ?",
        (match_id,),
    ).fetchone()) + (service.cups_in_play(db, match_id), tuple(reracks))
    cached = _state_cache.get(match_id)
    if cached is None or cached[0] != key:
        _, _, started, first, stage, cups, _ = key
        # The tiro della salvezza is a knockout rule (the final included).
        st = live.replay(reports(db, match_id), cups, first or 1, salvezza=stage == "ko", reracks=reracks)
        st.started_at = started  # the referee's start is the match clock
        cached = (key, st)
        _state_cache[match_id] = cached
    return cached[1]


def _context(db: sqlite3.Connection) -> dict:
    key = tuple(db.execute(
        "SELECT (SELECT COUNT(*) FROM reports), (SELECT COALESCE(MAX(id), 0) FROM reports),"
        " (SELECT COUNT(*) FROM matches WHERE done = 1),"
        " (SELECT COALESCE(SUM(id * (score1 * 31 + score2)), 0) FROM matches WHERE done = 1),"
        " (SELECT COUNT(*) FROM matches)"
    ).fetchone())
    if not _context_cache or _context_cache[0][0] != key:
        context = {
            "labels": service.standing_labels(db),
            "records": {r.team: r for table in service.group_tables(db) for r in table.rows},
            "totals": _team_side_stats(db),
        }
        _context_cache[:] = [(key, context)]
    return _context_cache[0][1]


def add_shot(db: sqlite3.Connection, match_id: int, side: int, player: int, cup: int, now: float) -> int:
    """Stores one of the referee's shots (cup 0 = miss): the only source of shot data."""
    m = service.match(db, match_id)
    if m is None or m.bye or m.t1 is None or m.t2 is None:
        raise TournamentError("Partita non disponibile.")
    if m.done:
        raise TournamentError("La partita è già conclusa.")
    if side not in (1, 2) or player not in (1, 2) or not 0 <= cup <= live.CUPS:
        raise TournamentError("Tiro non valido.")
    with db:
        cur = db.execute(
            "INSERT INTO reports(match_id, device, kind, side, player, cup, at) VALUES (?, ?, 'shot', ?, ?, ?, ?)",
            (match_id, live.REFEREE, side, player, cup, now),
        )
    return cur.lastrowid


@dataclass
class RefereeView:
    """What the referee's scoring screen needs: whose turn it is and who still has to shoot in it."""

    state: live.LiveState
    turn_side: int
    shot_in_turn: set[int]
    next_player: int
    last_shot: live.Shot | None
    rows: dict[int, list[list[int]]]  # rack side -> its layout now (re-racks change it)
    hit_by: dict[int, dict[int, int]]  # rack side -> cup taken -> player of the other couple who hit it
    last_rerack: int | None  # the rack side of the referee's last action, when that was a re-rack
    last_blown: bool = False  # the referee's last shot was blown out by the defenders (rule 5)
    blows: tuple[int, int] = (0, 0)  # saves by blowing, per couple


def referee_view(db: sqlite3.Connection, match_id: int) -> RefereeView:
    st = state(db, match_id)
    official = [s for s in st.shots if live.REFEREE in s.devices]
    first = db.execute("SELECT first_side FROM matches WHERE id = ?", (match_id,)).fetchone()[0] or 1
    side = st.turn_side if official else first  # the replay follows the turns (salvezza and overtime included)
    in_turn = set(st.in_turn) if official else set()
    nxt = next((p for p in (1, 2) if p not in in_turn), 1)
    hit_by: dict[int, dict[int, int]] = {1: {}, 2: {}}
    for rack_side in (1, 2):  # only the racks as they stand on the table now
        for s in st.shots[st.rack_start.get(rack_side, 0):]:
            if s.removed is not None and 3 - s.side == rack_side:
                hit_by[rack_side][s.removed] = s.player
    last = _last_referee_action(db, match_id)
    blown = bool(last and last[0] == "shot" and db.execute("SELECT 1 FROM blows WHERE report_id = ?", (last[1],)).fetchone())
    return RefereeView(
        st, side, in_turn, nxt, official[-1] if official else None, st.rows, hit_by,
        last[2] if last and last[0] == "rerack" else None, blown, blow_counts(db, match_id),
    )


def blow_counts(db: sqlite3.Connection, match_id: int) -> tuple[int, int]:
    counts = dict(db.execute("SELECT side, COUNT(*) FROM blows WHERE match_id = ? GROUP BY side", (match_id,)).fetchall())
    return counts.get(1, 0), counts.get(2, 0)


def _last_referee_action(db: sqlite3.Connection, match_id: int) -> tuple[str, int, int | None] | None:
    shot = db.execute(
        "SELECT id, at FROM reports WHERE match_id = ? AND device = ? AND kind = 'shot' ORDER BY at DESC, id DESC LIMIT 1",
        (match_id, live.REFEREE),
    ).fetchone()
    rerack = db.execute("SELECT id, at, side FROM reracks WHERE match_id = ? ORDER BY at DESC, id DESC LIMIT 1", (match_id,)).fetchone()
    if rerack and (shot is None or rerack["at"] >= shot["at"]):
        return ("rerack", rerack["id"], rerack["side"])
    return ("shot", shot["id"], None) if shot else None


def referee_rerack(db: sqlite3.Connection, match_id: int, side: int, now: float) -> None:
    """The shooters asked: the remaining cups of `side` are put back into a compact formation."""
    m = service.match(db, match_id)
    if m is None or m.started_at is None or m.done:
        raise TournamentError("Avvia prima la partita.")
    st = state(db, match_id)
    if st.winner_side or side not in (1, 2):
        raise TournamentError("Riordino non possibile.")
    if len(st.racks[side].cups) not in live.FORMATIONS:
        raise TournamentError("Si riordina con 6 bicchieri o meno.")
    with db:
        db.execute("INSERT INTO reracks(match_id, side, at) VALUES (?, ?, ?)", (match_id, side, now))


def official_count(view: RefereeView) -> int:
    return sum(1 for s in view.state.shots if live.REFEREE in s.devices)


def referee_shot(
    db: sqlite3.Connection, match_id: int, player: int, cup: int, now: float, number: int | None = None,
    blown: bool = False,
) -> bool:
    """The referee records a shot of the couple whose turn it is: a cup of the opponents' rack, or 0 for a miss.

    `blown`: the ball was spinning in a cup and the defenders blew it out (rule 5): a miss for the shooter,
    a save for the defenders.

    `number` is the shot the screen was showing (0 = first). A double tap or a retry after a lost answer
    sends the same number again: it is ignored instead of recording the shot twice. Returns whether it
    was recorded."""
    m = service.match(db, match_id)
    if m is None or m.started_at is None:
        raise TournamentError("Avvia prima la partita.")
    view = referee_view(db, match_id)
    if number is not None:
        recorded = official_count(view)
        last = view.last_shot
        if number == recorded - 1 and last and (last.player, last.cup, view.last_blown) == (player, cup, blown):
            return False  # this very shot is already in
        if number != recorded:
            raise TournamentError("La schermata non era aggiornata: controlla l'ultimo tiro e riprova.")
    if view.state.winner_side:
        raise TournamentError("I bicchieri sono finiti: conferma il risultato.")
    if player not in (1, 2):
        raise TournamentError("Scegli chi ha tirato.")
    if cup and cup not in view.state.racks[3 - view.turn_side].cups:
        raise TournamentError("Quel bicchiere non c'è più.")
    report_id = add_shot(db, match_id, view.turn_side, player, 0 if blown else cup, now)
    if blown:
        with db:
            db.execute("INSERT INTO blows(report_id, match_id, side) VALUES (?, ?, ?)", (report_id, match_id, 3 - view.turn_side))
    return True


def referee_undo(db: sqlite3.Connection, match_id: int) -> None:
    """Takes back the referee's last action: a shot or a re-rack."""
    last = _last_referee_action(db, match_id)
    if last is None:
        raise TournamentError("Nessun tiro da annullare.")
    with db:
        db.execute(f"DELETE FROM {'reracks' if last[0] == 'rerack' else 'reports'} WHERE id = ?", (last[1],))


CHEERS = ("🍺", "🔥", "👏")


def cheer(db: sqlite3.Connection, match_id: int, side: int, emoji: str, account: str, now: float) -> bool:
    """One emoji of each kind per account and couple: a second tap takes it back. Returns whether it is on."""
    m = service.match(db, match_id)
    if m is None or m.bye or m.t1 is None or m.t2 is None:
        raise TournamentError("Partita non disponibile.")
    if side not in (1, 2) or emoji not in CHEERS:
        raise TournamentError("Tifo non valido.")
    with db:
        gone = db.execute(
            "DELETE FROM cheers WHERE match_id = ? AND side = ? AND emoji = ? AND device = ?", (match_id, side, emoji, account)
        ).rowcount
        if not gone:
            db.execute("INSERT INTO cheers(match_id, side, emoji, device, at) VALUES (?, ?, ?, ?, ?)", (match_id, side, emoji, account, now))
    return not gone


def my_cheers(db: sqlite3.Connection, match_id: int, account: str | None) -> list[list]:
    if not account:
        return []
    return [[r["side"], r["emoji"]] for r in db.execute("SELECT side, emoji FROM cheers WHERE match_id = ? AND device = ?", (match_id, account))]


def _cheers(db: sqlite3.Connection, match_id: int) -> dict[int, dict[str, int]]:
    out = {1: dict.fromkeys(CHEERS, 0), 2: dict.fromkeys(CHEERS, 0)}
    for r in db.execute("SELECT side, emoji, COUNT(*) AS n FROM cheers WHERE match_id = ? GROUP BY side, emoji", (match_id,)):
        if r["emoji"] in out[r["side"]]:
            out[r["side"]][r["emoji"]] = r["n"]
    return out


_others_cache: tuple[float, list[dict]] = (0.0, [])


def _on_tables(db: sqlite3.Connection, now: float) -> list[dict]:
    """Every match on a table now, for the strip on top of the live page. Shared for a few seconds: every
    viewer polls every two seconds, and the full match list is the costly part."""
    global _others_cache
    if now - _others_cache[0] > 5:
        _others_cache = (now, [
            {"id": m.id, "field": m.field, "n1": m.n1, "n2": m.n2, "score": list(m.live_score) if m.live_score else None,
             "label": m.label}
            for m in service.matches(db) if m.playing and m.t1 and m.t2
        ])
    return _others_cache[1]


def _team_side_stats(db: sqlite3.Connection) -> dict[int, list[int]]:
    """team id -> [hits, shots] over every tracked match."""
    stats: dict[int, list[int]] = {}
    for m in service.matches(db):
        if m.t1 is None or m.t2 is None:
            continue
        st = state(db, m.id)
        for side, team in ((1, m.t1), (2, m.t2)):
            s = stats.setdefault(team, [0, 0])
            s[0] += st.side_hits(side)
            s[1] += st.side_attempts(side)
    return stats


def live_view(db: sqlite3.Connection, match_id: int, now: float) -> dict:
    m = service.match(db, match_id)
    if m is None or m.bye or m.t1 is None or m.t2 is None:
        raise TournamentError("Partita non disponibile.")
    st = state(db, match_id)
    team_rows = service.teams(db)
    context = _context(db)
    labels, records, totals = context["labels"], context["records"], context["totals"]
    duration = m.duration * 60 if m.duration else None

    rates = tuple(live.hit_rate(*totals.get(t, [0, 0]), service.prior_rate(team_rows[t])) for t in (m.t1, m.t2))
    cups_to_hit = (len(st.racks[2].cups), len(st.racks[1].cups))
    if m.done:
        prob1 = 1.0 if m.winner == m.t1 else 0.0 if m.winner == m.t2 else 0.5
    elif st.winner_side:
        prob1 = 1.0 if st.winner_side == 1 else 0.0
    else:
        left = live.shots_left_per_team(st, now, duration)
        prob1 = live.win_probability(cups_to_hit, rates, left, seed=match_id)

    def team_block(side: int, team_id: int) -> dict:
        t = team_rows[team_id]
        rec = records.get(team_id)
        hits, shots = totals.get(team_id, [0, 0])
        return {
            "id": team_id,
            "name": t["name"],
            "players": [t["p1"], t["p2"]],
            "label": labels.get(team_id, ""),
            "record": f"{rec.won}V {rec.drawn}N {rec.lost}P" if rec else "",
            "accuracy": round(100 * hits / shots) if shots else None,
            "rack": sorted(st.racks[side].cups),
            "rows": st.rows.get(side, live.RACK_ROWS[st.rack_cups]),
            "hits": st.side_hits(side),
            "player_stats": [
                {"hits": st.hits.get((side, p), 0), "shots": st.attempts.get((side, p), 0)} for p in (1, 2)
            ],
        }

    cheers = _cheers(db, match_id)
    return {
        "now": now,
        "official": True,  # only the table's referee scores matches
        "phase": st.phase,
        "started_at": st.started_at,
        "duration": duration,
        "done": m.done,
        "label": m.label,
        "field": m.field,
        "score": [m.s1, m.s2] if m.done else None,
        "winner_side": 1 if m.done and m.winner == m.t1 else 2 if m.done and m.winner == m.t2 else st.winner_side,
        "prob": [round(prob1, 3), round(1 - prob1, 3)],
        "teams": [team_block(1, m.t1), team_block(2, m.t2)],
        "shots": [
            {
                "at": s.at, "side": s.side, "player": s.player, "cup": s.cup, "removed": s.removed,
                "votes": s.votes,
            }
            for s in reversed(st.shots[-30:])
        ],
        "timeline": _cached_timeline(st, rates, duration, service.cups_in_play(db, match_id), match_id),
        "streak": [live.streak(st, 1), live.streak(st, 2)],
        "blows": list(blow_counts(db, match_id)),
        "cheers": [cheers[1], cheers[2]],
        "others": [o for o in _on_tables(db, now) if o["id"] != match_id],
    }


def _cached_timeline(
    st: live.LiveState, rates: tuple[float, float], duration: float | None, cups: int, match_id: int
) -> list[dict]:
    # Kept on the cached state, so it is simulated once per new shot, not once per viewer poll.
    key = (rates, duration, cups)
    cached = getattr(st, "_timeline", None)
    if cached is None or cached[0] != key:
        cached = (key, live.timeline(st, rates, duration, cups, match_id))
        st._timeline = cached
    return cached[1]


def live_score(db: sqlite3.Connection, match_id: int) -> tuple[int, int] | None:
    st = state(db, match_id)
    if not st.shots:
        return None
    return st.side_hits(1), st.side_hits(2)


def apply_live_splits(db: sqlite3.Connection, match_id: int) -> None:
    """After the referee confirms: if the tracked hits match the score, fill in the players' split for them."""
    m = db.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    st = state(db, match_id)
    if m is None or not m["done"] or not st.shots:
        return
    with db:
        for side, (a, b) in ((1, ("c1a", "c1b")), (2, ("c2a", "c2b"))):
            if m[a] is None and st.side_hits(side) == m[f"score{side}"]:
                db.execute(
                    f"UPDATE matches SET {a} = ?, {b} = ? WHERE id = ?",
                    (st.hits.get((side, 1), 0), st.hits.get((side, 2), 0), match_id),
                )


# --- Tournament-wide shot statistics ------------------------------------------------------


@dataclass
class PlayerShots:
    player: str
    team: str
    team_id: int
    hits: int = 0
    shots: int = 0
    finishers: int = 0  # hits that cleared the opponent's last cup

    @property
    def accuracy(self) -> float:
        return self.hits / self.shots if self.shots else 0.0


@dataclass
class ShotStats:
    players: list[PlayerShots]
    heatmap: dict[int, int]  # cup position -> hits
    total_shots: int
    awards: list[tuple[str, str, PlayerShots]] = field(default_factory=list)


MIN_SHOTS_FOR_ACCURACY = 8


def shot_stats(db: sqlite3.Connection) -> ShotStats:
    team_rows = service.teams(db)
    by_player: dict[tuple[int, int], PlayerShots] = {}
    heat: Counter[int] = Counter()
    total = 0
    for m in service.matches(db):
        if m.t1 is None or m.t2 is None:
            continue
        st = state(db, m.id)
        last_hit = next((s for s in reversed(st.shots) if s.removed is not None), None) if st.winner_side else None
        for s in st.shots:
            team_id = m.t1 if s.side == 1 else m.t2
            t = team_rows[team_id]
            p = by_player.setdefault((team_id, s.player), PlayerShots(t[f"p{s.player}"], t["name"], team_id))
            p.shots += 1
            total += 1
            if s.removed is not None:
                p.hits += 1
                heat[s.removed] += 1
                if s is last_hit:
                    p.finishers += 1
    players = sorted(by_player.values(), key=lambda p: (-p.hits, -p.accuracy, p.player.lower()))
    stats = ShotStats(players, {c: heat.get(c, 0) for c in range(1, live.CUPS + 1)}, total)
    if players:
        stats.awards.append(("🎯", "Bomber", players[0]))
        qualified = [p for p in players if p.shots >= MIN_SHOTS_FOR_ACCURACY]
        if qualified:
            stats.awards.append(("🔭", "Cecchino", max(qualified, key=lambda p: (p.accuracy, p.hits))))
        finisher = max(players, key=lambda p: (p.finishers, p.hits))
        if finisher.finishers:
            stats.awards.append(("🏁", "Re dell'ultimo bicchiere", finisher))
    return stats
