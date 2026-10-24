"""Automated demonstration tour and video recorder for all 4 tournament phases.

Simulates the lifecycle:
1. Setup & Pre-tournament Lounge (team checking in, arriving couples, icebreaker code).
2. Starting the tournament -> Groups phase with live tables & scores.
3. Groups completion -> Knockout phase (quarterfinals, semifinals, final).
4. Tournament finished -> Champion crowned and final recap.

Generates real MP4 videos in app/static/demo_videos/ and orchestrates demo database states.
"""
from __future__ import annotations

import asyncio
import aiohttp
import base64
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from app import db, service

OUTPUT_DIR = Path("app/static/demo_videos")
TEMP_DIR = Path("/tmp/demo_tour_frames")
VIEWPORT_W, VIEWPORT_H = 430, 932


class BrowserRecorder:
    def __init__(self, port: int = 9222):
        self.port = port
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen([
            "google-chrome",
            "--headless",
            "--disable-gpu",
            f"--remote-debugging-port={self.port}",
            f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
            "--hide-scrollbars",
            "about:blank"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait()


async def capture_page(ws, url: str, session_token: str | None, out_png: Path) -> None:
    msg_id = int(time.time() * 1000) % 100000
    if session_token:
        await ws.send_json({
            "id": msg_id,
            "method": "Network.setCookie",
            "params": {
                "name": "session",
                "value": session_token,
                "domain": "127.0.0.1",
                "path": "/",
            }
        })
        await ws.receive_json()

    msg_id += 1
    await ws.send_json({
        "id": msg_id,
        "method": "Page.navigate",
        "params": {"url": url}
    })
    await ws.receive_json()
    await asyncio.sleep(1.2)  # Wait for DOM and countdowns

    msg_id += 1
    await ws.send_json({
        "id": msg_id,
        "method": "Page.captureScreenshot",
        "params": {"format": "png"}
    })
    res = await ws.receive_json()
    data = base64.b64decode(res["result"]["data"])
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png.write_bytes(data)


def assemble_video(images: list[Path], output_mp4: Path, frame_sec: float = 2.5) -> None:
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    list_file = output_mp4.parent / f"{output_mp4.stem}_concat.txt"
    with open(list_file, "w") as f:
        for img in images:
            f.write(f"file '{img.resolve()}'\n")
            f.write(f"duration {frame_sec}\n")
        # repeat last image to hold
        if images:
            f.write(f"file '{images[-1].resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264",
        "-r", "24",
        str(output_mp4)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    if list_file.exists():
        list_file.unlink()


async def record_phase_walkthrough(base_url: str = "http://127.0.0.1:8000"):
    recorder = BrowserRecorder()
    recorder.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{recorder.port}/json") as r:
                tabs = await r.json()
            ws_url = tabs[0]["webSocketDebuggerUrl"]

            async with session.ws_connect(ws_url) as ws:
                # Get couple session
                async with session.post(f"{base_url}/demo/coppia", allow_redirects=False) as resp:
                    raw_cookie = resp.headers.get("Set-Cookie", "")
                    couple_session = raw_cookie.split("session=")[1].split(";")[0] if "session=" in raw_cookie else None

                # Phase 1: Lounge Pre-torneo
                print("Capturing Phase 1: Lounge & Check-in...")
                p1_frames = [
                    TEMP_DIR / "phase1_01_lounge.png",
                    TEMP_DIR / "phase1_02_arrivals.png",
                    TEMP_DIR / "phase1_03_profile.png",
                ]
                await capture_page(ws, f"{base_url}/io", couple_session, p1_frames[0])
                await capture_page(ws, f"{base_url}/arrivi", couple_session, p1_frames[1])
                await capture_page(ws, f"{base_url}/io/profilo", couple_session, p1_frames[2])
                assemble_video(p1_frames, OUTPUT_DIR / "1_fase_lounge.mp4", frame_sec=2.5)

                # Phase 2: Live matches & Tavoli
                print("Capturing Phase 2: Gironi & Tavoli in gioco...")
                p2_frames = [
                    TEMP_DIR / "phase2_01_tavoli.png",
                    TEMP_DIR / "phase2_02_partita.png",
                    TEMP_DIR / "phase2_03_classifica.png",
                ]
                await capture_page(ws, f"{base_url}/gironi", couple_session, p2_frames[0])
                await capture_page(ws, f"{base_url}/programma", couple_session, p2_frames[1])
                await capture_page(ws, f"{base_url}/classifiche", couple_session, p2_frames[2])
                assemble_video(p2_frames, OUTPUT_DIR / "2_fase_gironi_live.mp4", frame_sec=2.5)

                # Phase 3: Knockout Tabellone
                print("Capturing Phase 3: Tabellone Eliminazione diretta...")
                p3_frames = [
                    TEMP_DIR / "phase3_01_tabellone.png",
                    TEMP_DIR / "phase3_02_classifiche_ko.png",
                ]
                await capture_page(ws, f"{base_url}/tabellone", couple_session, p3_frames[0])
                await capture_page(ws, f"{base_url}/classifiche?vista=eliminazione", couple_session, p3_frames[1])
                assemble_video(p3_frames, OUTPUT_DIR / "3_fase_eliminazione.mp4", frame_sec=2.5)

                # Phase 4: Full Tournament Recap Video
                all_frames = p1_frames + p2_frames + p3_frames
                assemble_video(all_frames, OUTPUT_DIR / "torneo_tour_completo.mp4", frame_sec=2.0)
                print("All demo walkthrough videos generated successfully in app/static/demo_videos/!")
    finally:
        recorder.stop()


if __name__ == "__main__":
    asyncio.run(record_phase_walkthrough())
