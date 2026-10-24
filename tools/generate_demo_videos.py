"""Record the demo through the four phases of a night, as short phone-sized videos.

1. Lounge: couples arriving, the profile's "Chi c'è già", the provisional groups.
2. Gironi: halfway through the groups, matches on every table.
3. Eliminazione: the bracket being played.
4. Finale: the champion.

It moves the tournament itself (reusing the demo's own setup and simulator), serves it from a server
of its own and films each page in headless Chrome, then joins the frames with ffmpeg: one MP4 per
phase plus the whole night. It resets the tournament, so only for a scratch database.

Usage: BP_DB=data/demo-tour.db python -m tools.generate_demo_videos [--out demo-videos] [--seconds 2.5]
Needs google-chrome or chromium, and ffmpeg. aiohttp comes with pywebpush.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from app import db, service
from app.states import Phase
from tools import demo_sim
from tools.populate_demo import add_demo_teams, setup_demo

PHONE = {"width": 430, "height": 932, "deviceScaleFactor": 2, "mobile": True}
ARRIVED = 22  # couples already at the venue in the lounge


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Chrome:
    """Headless Chrome driven over the DevTools protocol: one tab, phone-sized."""

    def __init__(self, binary: str, profile: Path) -> None:
        self.binary, self.profile = binary, profile
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> Chrome:
        self.proc = subprocess.Popen(
            [self.binary, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
             "--hide-scrollbars", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port_file = self.profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            await asyncio.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        self.http = aiohttp.ClientSession()
        async with self.http.get(f"http://127.0.0.1:{port}/json/list") as r:
            tab = next(t for t in await r.json() if t["type"] == "page")
        self.ws = await self.http.ws_connect(tab["webSocketDebuggerUrl"], max_msg_size=0)
        self.reader = asyncio.create_task(self._read())
        await self.send("Page.enable")
        await self.send("Emulation.setDeviceMetricsOverride", **PHONE)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.reader.cancel()
        await self.ws.close()
        await self.http.close()
        self.proc.terminate()
        self.proc.wait()

    async def _read(self) -> None:
        async for msg in self.ws:
            data = msg.json()
            if "id" in data:
                self.pending.pop(data["id"]).set_result(data)
            else:
                self.events.put_nowait(data["method"])

    async def send(self, method: str, **params: object) -> dict:
        msg_id = next(self.ids)
        self.pending[msg_id] = asyncio.get_running_loop().create_future()
        await self.ws.send_json({"id": msg_id, "method": method, "params": params})
        reply = await asyncio.wait_for(self.pending[msg_id], 30)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    async def open(self, url: str) -> None:
        while not self.events.empty():
            self.events.get_nowait()
        if "loaderId" in await self.send("Page.navigate", url=url):  # absent when only the #fragment changes
            while await asyncio.wait_for(self.events.get(), 30) != "Page.loadEventFired":
                pass
        await asyncio.sleep(0.8)  # countdowns and the live page's first fetch

    async def run(self, js: str) -> None:
        await self.send("Runtime.evaluate", expression=js, awaitPromise=True)

    async def shot(self, url: str, out: Path) -> Path:
        await self.open(url)
        out.write_bytes(base64.b64decode((await self.send("Page.captureScreenshot", format="png"))["data"]))
        return out


def lounge(conn: sqlite3.Connection) -> None:
    if not service.teams(conn):
        add_demo_teams(conn)
    db.set_setting(conn, "title", "DEMO · Torneo di Beer Pong")
    db.set_setting(conn, "demo", "1")
    conn.commit()
    if not service.load_config(conn).phase.is_setup:
        service.reset_tournament(conn)
    ids = sorted(service.teams(conn))
    now = datetime.now()
    with conn:
        conn.execute("UPDATE teams SET checked_in = 0, checked_in_at = NULL")
        conn.executemany(
            "UPDATE teams SET checked_in = 1, checked_in_at = ? WHERE id = ?",
            [((now - timedelta(minutes=3 * (ARRIVED - i))).isoformat(sep=" ", timespec="seconds"), t) for i, t in enumerate(ids[:ARRIVED])],
        )


def play_until(conn: sqlite3.Connection, rng: random.Random, done: object) -> None:
    """Runs the demo's simulator on a fast clock until `done(conn)`; every match lasts a few minutes."""
    now = time.time()
    for _ in range(2000):
        if done(conn):
            return
        now += 300
        demo_sim.tick(conn, rng, now)
    raise RuntimeError("The tournament did not get there: is the demo database broken?")


def final_done(conn: sqlite3.Connection) -> bool:
    rounds = service.knockout_rounds(conn)
    return bool(rounds) and rounds[-1][1][0].done


def assemble(frames: list[Path], out: Path, seconds: float) -> None:
    listing = out.with_suffix(".txt")
    lines = [f"file '{f.resolve()}'\nduration {seconds}\n" for f in frames] + [f"file '{frames[-1].resolve()}'\n"]
    listing.write_text("".join(lines))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-c:v", "libx264", "-r", "24", str(out)],
            check=True,
        )
    finally:
        listing.unlink()


async def record(base: str, conn: sqlite3.Connection, out_dir: Path, seconds: float, chrome_bin: str) -> list[Path]:
    rng = random.Random(7)
    frames_dir = Path(tempfile.mkdtemp(prefix="demo-tour-"))
    videos: list[Path] = []
    every: list[Path] = []
    try:
        async with Chrome(chrome_bin, frames_dir / "chrome") as chrome:
            async def film(name: str, urls: list[str]) -> None:
                frames = [await chrome.shot(base + u, frames_dir / f"{name}-{i}.png") for i, u in enumerate(urls)]
                videos.append(out_dir / f"{name}.mp4")
                assemble(frames, videos[-1], seconds)
                every.extend(frames)
                print(f"  {videos[-1]}", flush=True)

            lounge(conn)
            await chrome.open(base + "/")
            await chrome.run("fetch('/demo/coppia', {method: 'POST', credentials: 'same-origin'})")
            await film("1-lounge", ["/io", "/io#lounge", "/arrivi", "/"])

            setup_demo(conn)
            demo_sim.tick(conn, rng, time.time())  # the couples reach their tables
            running = next((m for m in service.matches(conn) if m.started_at and not m.done), None)
            await film("2-gironi", ["/", "/io", "/classifiche?vista=gironi"] + ([f"/live/{running.id}"] if running else []))

            play_until(conn, rng, lambda c: service.load_config(c).phase is Phase.KNOCKOUT)
            demo_sim.tick(conn, rng, time.time())
            await film("3-eliminazione", ["/classifiche?vista=tabellone", "/classifiche?vista=tabellone&per=tavolo", "/"])

            play_until(conn, rng, final_done)
            await film("4-finale", ["/", "/classifiche?vista=tabellone", "/io"])

        videos.append(out_dir / "torneo-completo.mp4")
        assemble(every, videos[-1], seconds * 0.8)
        print(f"  {videos[-1]}", flush=True)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return videos


async def serve_and_record(out_dir: Path, seconds: float, chrome_bin: str) -> None:
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "--factory", "app.main:create_app", "--port", str(port), "--log-level", "warning"],
        env=os.environ,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(100):
                try:
                    async with http.get(base + "/"):
                        break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
        conn = db.connect()
        try:
            await record(base, conn, out_dir, seconds, chrome_bin)
        finally:
            conn.close()
    finally:
        server.terminate()
        server.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("demo-videos"), help="where the MP4s go")
    parser.add_argument("--seconds", type=float, default=2.5, help="how long each page stays on screen")
    args = parser.parse_args()
    if db.db_path().resolve() == db.DEFAULT_DB.resolve():
        raise SystemExit("It resets the tournament: set BP_DB to a scratch database, e.g. BP_DB=data/demo-tour.db")
    chrome_bin = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if chrome_bin is None or shutil.which("ffmpeg") is None:
        raise SystemExit("Needs google-chrome (or chromium) and ffmpeg on the PATH.")
    db.init_db()
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"Recording the demo into {args.out}/ …", flush=True)
    asyncio.run(serve_and_record(args.out, args.seconds, chrome_bin))


if __name__ == "__main__":
    main()
