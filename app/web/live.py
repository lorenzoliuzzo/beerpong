"""Following a match recorded shot by shot: the live page, its state and the cheers."""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .. import service, tracking
from ..service import TournamentError
from .common import device_id, get_db, my_team_id, page, playing_now

router = APIRouter()


REPORT_MIN_GAP_S = 0.7
REPORT_MAX_PER_MINUTE = 40
_report_times: dict[str, deque[float]] = defaultdict(deque)


def server_now() -> float:
    """The one clock every phone follows (wall time on the Pi)."""
    return time.time()


def _report_allowed(request: Request, device: str) -> bool:
    """Any phone may report, so cap the rate per device and per address."""
    now = time.monotonic()
    for key in (f"d:{device}", f"ip:{request.client.host if request.client else '?'}"):
        times = _report_times[key]
        while times and times[0] < now - 60:
            times.popleft()
        limit = REPORT_MAX_PER_MINUTE * (1 if key.startswith("d:") else 3)
        if len(times) >= limit or (key.startswith("d:") and times and now - times[-1] < REPORT_MIN_GAP_S):
            return False
    for key in (f"d:{device}", f"ip:{request.client.host if request.client else '?'}"):
        _report_times[key].append(now)
    return True


@router.get("/live/{match_id}")
def live_page(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> Response:
    m = service.match(db, match_id)
    if m is None or m.bye or m.t1 is None or m.t2 is None:
        raise HTTPException(404)
    device_id(request)
    return page(request, db, "live.html", m=m)


def cheer_account(request: Request) -> str:
    """Who is cheering: the couple when logged in (all its phones count once), else this browser."""
    team_id = my_team_id(request)
    return f"team:{team_id}" if team_id is not None else f"device:{device_id(request)}"


PLAYING_MSG = "State giocando: occhi sul tavolo! 🏓 Il live lo seguite dopo."


def _live_json(request: Request, db: sqlite3.Connection, match_id: int, device: str | None) -> JSONResponse:
    data = tracking.live_view(db, match_id, server_now())
    data["my_cheers"] = tracking.my_cheers(db, match_id, cheer_account(request) if device else None)
    data["you_play"] = playing_now(request, db)
    return JSONResponse(data)


@router.get("/api/live/{match_id}")
def live_state(request: Request, match_id: int, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    try:
        return _live_json(request, db, match_id, request.session.get("device"))
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


class CheerIn(BaseModel):
    side: int
    emoji: str = Field(max_length=8)


@router.post("/api/live/{match_id}/tifo")
def live_cheer(request: Request, match_id: int, body: CheerIn, db: sqlite3.Connection = Depends(get_db)) -> JSONResponse:
    device = device_id(request)
    if playing_now(request, db):
        return JSONResponse({"error": PLAYING_MSG}, status_code=403)
    if not _report_allowed(request, device):
        return JSONResponse({"error": "Piano! 🍻"}, status_code=429)
    try:
        tracking.cheer(db, match_id, body.side, body.emoji, cheer_account(request), server_now())
        return _live_json(request, db, match_id, device)
    except TournamentError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
