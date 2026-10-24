"""Plays a whole tournament on a throwaway instance while a crowd of phones uses the site.

32 invented couples check in by phone (64 players), 4 referees score every match shot by shot through the
scoring screen, from the first group match to the final, while the players and extra spectators browse,
watch the live pages (polling every 2 s like the real page) and cheer. Prints latency per kind of request,
errors, and whether every referee tap ended up in the statistics.

Never point it at the live site: it resets nothing, but it creates couples and starts a tournament.

    BP_DB=/tmp/stress.db BP_ADMIN_PIN=424242 uvicorn --factory app.main:create_app --port 8002
    python -m tools.loadtest --base http://HOST:8002 --pin 424242
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import httpx

COUPLES = 32
FIELDS = 4


@dataclass
class Stats:
    latency: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    status: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    failures: Counter = field(default_factory=Counter)
    taps: int = 0
    checkin_key: str = ""
    checked_in: int = 0
    conflicts: int = 0
    matches: int = 0
    done: bool = False

    async def call(self, kind: str, client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response | None:
        t0 = time.perf_counter()
        try:
            r = await client.request(method, url, **kw)
        except httpx.HTTPError as e:
            self.failures[f"{kind}: {type(e).__name__}"] += 1
            return None
        self.latency[kind].append(time.perf_counter() - t0)
        self.status[kind][r.status_code] += 1
        return r


def new_client(base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, follow_redirects=True, timeout=15)


def phone(i: int, p: int) -> str:
    return f"3339{i:04d}{p:02d}"


async def setup(base: str, pin: str, st: Stats) -> httpx.AsyncClient:
    admin = new_client(base)
    await st.call("admin", admin, "POST", "/login", data={"pin": pin})
    await st.call("admin", admin, "POST", "/admin/impostazioni", data={
        "title": "Stress test", "fields": FIELDS, "group_min": 5, "ko_min": 7, "buffer_min": 1,
    })
    lines = [f"Coppia {i}; Ada {i}; Bea {i}; {phone(i, 1)}; {phone(i, 2)}" for i in range(COUPLES)]
    await st.call("admin", admin, "POST", "/admin/squadre/importa", data={"testo": "\n".join(lines)})
    r = await st.call("admin", admin, "GET", "/admin/qr")
    st.checkin_key = re.search(r"checkin\?k=([\w-]+)", r.text).group(1)
    return admin


async def player(base: str, i: int, p: int, st: Stats, rng: random.Random) -> None:
    async with new_client(base) as c:
        await asyncio.sleep(rng.uniform(0, 20))  # the check-in rush at the door
        await st.call("page", c, "GET", f"/checkin?k={st.checkin_key}")  # the QR code at the door
        r = await st.call("checkin", c, "POST", "/checkin/accedi", data={"phone": phone(i, p)})
        if r is not None and ("Check-in fatto" in r.text or "Bentornati" in r.text):
            st.checked_in += 1
        await browse(c, st, rng, watch_share=0.35)


async def spectator(base: str, st: Stats, rng: random.Random) -> None:
    async with new_client(base) as c:
        await asyncio.sleep(rng.uniform(0, 30))
        await browse(c, st, rng, watch_share=0.8)


PAGES = ["/io", "/", "/gironi", "/tabellone", "/programma", "/classifiche", "/classifiche?vista=giocatori",
         "/classifiche?vista=tiri", "/live"]


async def browse(c: httpx.AsyncClient, st: Stats, rng: random.Random, watch_share: float) -> None:
    await st.call("page", c, "GET", "/static/style.css")
    while not st.done:
        if rng.random() < watch_share:
            r = await st.call("page", c, "GET", "/live")
            ids = re.findall(r'href="/live/(\d+)"', r.text) if r is not None else []
            if ids:
                await watch(c, rng.choice(ids), st, rng)
                continue
        url = rng.choice(PAGES)
        # A page left open re-fetches itself every 20 s; people switch page sooner.
        for _ in range(rng.randint(1, 3)):
            await st.call("page", c, "GET", url)
            await asyncio.sleep(rng.uniform(5, 20))


async def watch(c: httpx.AsyncClient, match_id: str, st: Stats, rng: random.Random) -> None:
    await st.call("page", c, "GET", f"/live/{match_id}")
    until = time.monotonic() + rng.uniform(30, 120)
    while time.monotonic() < until and not st.done:
        r = await st.call("api_live", c, "GET", f"/api/live/{match_id}")
        if r is not None and r.status_code == 200 and r.json().get("done"):
            return
        if rng.random() < 0.05:
            await st.call("cheer", c, "POST", f"/api/live/{match_id}/tifo",
                          json={"side": rng.choice([1, 2]), "emoji": rng.choice(["🍺", "🔥", "👏"])})
        await asyncio.sleep(2)


def form_fields(html: str, action: str, cls: str) -> dict[str, str] | None:
    m = re.search(rf'<form method="post" action="{re.escape(action)}" class="{cls}">(.*?)</form>', html, re.S)
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', m.group(1))) if m else None


async def referee(base: str, field_no: int, pin: str, st: Stats, rng: random.Random, tap_s: float) -> None:
    """One admin phone per table: with no table PINs, the admin referees a table from /arbitro?campo=N."""
    async with new_client(base) as c:
        await st.call("referee", c, "POST", "/login", data={"pin": pin})
        while not st.done:
            r = await st.call("referee", c, "GET", f"/arbitro?campo={field_no}")
            ids = re.findall(r'href="/segna/(\d+)"', r.text) if r is not None else []
            if not ids:
                await asyncio.sleep(2)
                continue
            mid = ids[0]
            page = await st.call("segna", c, "GET", f"/segna/{mid}")
            while page is not None and page.url.path == f"/segna/{mid}":
                html = page.text
                await asyncio.sleep(rng.uniform(0.5, 1.5) * tap_s)
                if f'action="/partita/{mid}/inizia"' in html:
                    page = await st.call("segna", c, "POST", f"/partita/{mid}/inizia", data={"primo": rng.choice([1, 2])})
                elif (win := form_fields(html, f"/partita/{mid}", "ref-win")) is not None:
                    page = await st.call("segna", c, "POST", f"/partita/{mid}", data=win)
                    st.matches += 1
                else:
                    n = re.search(r'name="n" value="(\d+)"', html)
                    who = re.search(r'name="giocatore" value="(\d)" checked', html)
                    cups = re.findall(r'name="bicchiere" value="(\d+)"', html)
                    if not (n and who):
                        st.failures["referee: unreadable scoring screen"] += 1
                        break
                    roll = rng.random()
                    standing = [c for c in cups if c != "0"]
                    cup = "soffio" if roll < 0.03 else rng.choice(standing) if roll < 0.45 and standing else "0"
                    page = await st.call("tap", c, "POST", f"/partita/{mid}/tiro",
                                         data={"giocatore": who.group(1), "bicchiere": cup, "n": n.group(1)})
                    st.taps += 1
                    if page is not None and "non era aggiornata" in page.text:
                        st.conflicts += 1


async def until_champion(admin: httpx.AsyncClient, st: Stats, minutes: float) -> str:
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        await asyncio.sleep(10)
        await st.call("admin", admin, "GET", "/admin")
        r = await st.call("page", admin, "GET", "/tabellone")
        if r is not None and "Campioni del Torneo" in r.text:
            return re.search(r'<div class="headline">🏆 ([^<]+)</div>', r.text).group(1)
        print(f"  … {st.matches} partite confermate, {st.taps} tiri", flush=True)
    return ""


def report(st: Stats, elapsed: float, champion: str, recorded: int | None) -> None:
    print(f"\nDurata {elapsed / 60:.1f} min · campione: {champion or 'NESSUNO (tempo scaduto)'} · partite {st.matches}")
    print(f"{'tipo':10} {'richieste':>9} {'p50 ms':>7} {'p95 ms':>7} {'p99 ms':>7} {'max ms':>7}  stati")
    total = sum(len(v) for v in st.latency.values())
    for kind, lat in sorted(st.latency.items()):
        q = statistics.quantiles(lat, n=100) if len(lat) > 1 else lat * 99
        print(f"{kind:10} {len(lat):9} {1000 * q[49]:7.0f} {1000 * q[94]:7.0f} {1000 * q[98]:7.0f} "
              f"{1000 * max(lat):7.0f}  {dict(st.status[kind])}")
    print(f"Totale {total} richieste, {total / elapsed:.1f}/s in media")
    errors = sum(n for k in st.status.values() for s, n in k.items() if s >= 500)
    print(f"Errori 5xx: {errors} · errori di rete/timeout: {dict(st.failures) or 0}")
    print(f"Tiri inviati dagli arbitri: {st.taps} · schermata non aggiornata: {st.conflicts} · "
          f"nelle statistiche: {recorded if recorded is not None else '?'}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--pin", required=True, help="admin PIN of the throwaway instance")
    ap.add_argument("--spectators", type=int, default=20, help="besides the 64 players")
    ap.add_argument("--tap", type=float, default=1.5, help="mean seconds between two referee taps")
    ap.add_argument("--minutes", type=float, default=40)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    st = Stats()
    t0 = time.monotonic()

    admin = await setup(args.base, args.pin, st)
    crowd = [asyncio.create_task(player(args.base, i, p, st, random.Random(rng.random())))
             for i in range(COUPLES) for p in (1, 2)]
    crowd += [asyncio.create_task(spectator(args.base, st, random.Random(rng.random()))) for _ in range(args.spectators)]
    # Everyone checks in at the door before the draw, as tonight.
    while True:
        await asyncio.sleep(3)
        await st.call("admin", admin, "GET", "/admin")
        print(f"  check-in: {st.checked_in}/{2 * COUPLES} telefoni", flush=True)
        if st.checked_in >= 2 * COUPLES:
            break
    await st.call("admin", admin, "POST", "/admin/avvia")
    refs = [asyncio.create_task(referee(args.base, f, args.pin, st, random.Random(rng.random()), args.tap))
            for f in range(1, FIELDS + 1)]
    print(f"Torneo avviato: {FIELDS} arbitri, {len(crowd)} telefoni", flush=True)

    champion = await until_champion(admin, st, args.minutes)
    st.done = True
    await asyncio.gather(*crowd, *refs, return_exceptions=True)
    r = await st.call("page", admin, "GET", "/classifiche?vista=tiri")
    got = re.search(r"Dai tiri segnati dagli arbitri \((\d+) tiri\)", r.text) if r is not None else None
    await admin.aclose()
    report(st, time.monotonic() - t0, champion, int(got.group(1)) if got else None)


if __name__ == "__main__":
    asyncio.run(main())
