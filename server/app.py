"""BoardCam server: static pages, REST, photo intake and the clock WebSocket.

Contract: docs/PROTOCOL.md. One phone is clock and camera. The server stores
the clock's events and the photos it uploads, reads each photo into a position
(engine.reader) and keeps the player's corrections. All clock authority lives
on the phone.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analysis, photos, review, storage
from .lichess import import_pgn
from .models import CLOCK_EVENTS, READER_MODELS, SETTING_KEYS, Confirm, GameConfig, GameSettings
from .ws import hub, now_ms

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="BoardCam", version="0.2.0")


@app.middleware("http")
async def skip_ngrok_interstitial(request: Request, call_next):
    """Tell ngrok not to serve its warning page for our own responses."""
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "1"
    return response


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def _page(name: str) -> FileResponse:
    path = STATIC_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not built yet")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/")
async def index():
    return _page("index.html")


@app.get("/clock")
async def clock_page():
    return _page("clock.html")


@app.get("/games")
async def games_page():
    return _page("games.html")


@app.get("/review")
async def review_page():
    return _page("review.html")


@app.get("/health")
async def health():
    return {"ok": True, "t": now_ms()}


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

@app.get("/api/settings")
async def api_settings_defaults():
    """The values the new-game form starts from: the last game's, else built-ins."""
    base = GameConfig().model_dump()
    saved = storage.read_defaults()
    try:
        merged = GameConfig(**{**base, **saved}).model_dump()
    except ValueError:
        merged = base
    return {"defaults": merged, "reader_models": list(READER_MODELS)}


@app.post("/games")
async def create_game(config: GameConfig | None = None):
    config = config or GameConfig()
    meta = storage.create_game(config)
    storage.write_defaults(config.model_dump())
    return {k: meta[k] for k in ("game_id", "room", "created_at", "initial_ms",
                                 "increment_ms", *SETTING_KEYS)}


@app.patch("/api/games/{game_id}/settings")
async def api_update_settings(game_id: str, body: GameSettings):
    _require(game_id)
    fields = body.model_dump(exclude_none=True)
    meta = storage.update_meta(game_id, **fields) if fields else storage.read_meta(game_id)
    if fields:
        storage.write_defaults(fields)
    return {k: meta.get(k) for k in SETTING_KEYS}


@app.get("/api/games")
async def api_list_games():
    return {"games": storage.list_games()}


@app.get("/api/games/by-room/{room}")
async def api_game_by_room(room: str):
    meta = storage.find_by_room(room)
    if meta is None:
        raise HTTPException(status_code=404, detail="no game with that room code")
    return storage.summarize(meta["game_id"])


@app.get("/api/games/{game_id}")
async def api_game_detail(game_id: str):
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    meta = storage.read_meta(game_id)
    return {
        **meta,
        "events": storage.read_events(game_id),
        "positions": storage.read_positions(game_id),
        "analysis": storage.read_json(game_id, "analysis.json"),
        "calibration": storage.read_json(game_id, "calibration.json"),
    }


@app.delete("/api/games/{game_id}")
async def api_delete_game(game_id: str):
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    storage.delete_game(game_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# Photos and positions (PROTOCOL.md §5)
# --------------------------------------------------------------------------

# Reads in flight. Held so the tasks are not garbage-collected, and so a read
# survives the phone dropping its request (iOS can discard the page).
_reads: set[asyncio.Task] = set()


async def _read_and_push(game_id: str, n: int) -> dict:
    entry = await asyncio.to_thread(photos.read_photo, game_id, n)
    room = storage.read_meta(game_id)["room"]
    await hub.send(room, "clock", {"type": "position.read", "game_id": game_id,
                                   "photo": entry})
    return entry


async def _start_read(game_id: str, n: int) -> dict:
    task = asyncio.create_task(_read_and_push(game_id, n))
    _reads.add(task)
    task.add_done_callback(_reads.discard)
    # Shielded: if the phone's request is cancelled the read still finishes and
    # lands in positions.json, where the page picks it up after a reload.
    return await asyncio.shield(task)


def _photo_error(exc: photos.PhotoError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@app.post("/games/{game_id}/photos")
async def upload_photo(game_id: str, file: UploadFile = File(...),
                       ply: int = Form(...), turn: str | None = Form(None),
                       white_ms: int | None = Form(None), black_ms: int | None = Form(None),
                       retake_of: int | None = Form(None)):
    """Store a photo stamped with the clock's ply count, read it, return the read.

    The response waits for the reader (seconds, up to minutes on a diagonal
    photo). The same entry is pushed as ``position.read`` on the socket.
    """
    _require(game_id)
    blob = await file.read()
    if turn not in (None, "", "white", "black"):
        raise HTTPException(status_code=400, detail="turn must be white or black")
    try:
        entry = await asyncio.to_thread(photos.add_photo, game_id, blob, ply=ply,
                                        turn=turn or None, white_ms=white_ms,
                                        black_ms=black_ms, retake_of=retake_of)
    except photos.PhotoError as exc:
        raise _photo_error(exc) from exc
    return await _start_read(game_id, entry["n"])


@app.post("/api/games/{game_id}/photos/{n}/read")
async def reread_photo(game_id: str, n: int):
    """Retry a read that failed for a transport reason (no new photo needed)."""
    _require(game_id)
    try:
        await asyncio.to_thread(photos.mark_reading, game_id, n)
    except photos.PhotoError as exc:
        raise _photo_error(exc) from exc
    return await _start_read(game_id, n)


@app.get("/api/games/{game_id}/positions")
async def api_positions(game_id: str):
    _require(game_id)
    return storage.read_positions(game_id)


@app.get("/games/{game_id}/photos/{n}")
async def get_photo(game_id: str, n: int):
    _require(game_id)
    try:
        path = storage.photo_path(game_id, n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="no such photo")
    return FileResponse(path, media_type="image/jpeg")


@app.post("/api/games/{game_id}/photos/{n}/confirm")
async def confirm_photo(game_id: str, n: int, body: Confirm):
    _require(game_id)
    try:
        return await asyncio.to_thread(photos.confirm, game_id, n, body.rows,
                                       body.home, body.ply)
    except photos.PhotoError as exc:
        raise _photo_error(exc) from exc


@app.get("/games/{game_id}/frames/{seq}/{k}")
async def get_frame(game_id: str, seq: int, k: int):
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    try:
        path = storage.frame_path(game_id, seq, k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="no such frame")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/games/{game_id}/analysis")
async def api_analysis(game_id: str):
    """The move list read for this game, once there is one."""
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    saved = storage.read_json(game_id, "analysis.json")
    if saved is not None:
        return saved
    state = analysis.for_game(game_id)
    return {"game_id": game_id, "plies": [], "moves": state.moves,
            "pending": True, "error": state.error}


@app.get("/games/{game_id}/pgn")
async def get_pgn(game_id: str):
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    path = storage.game_dir(game_id) / "game.pgn"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no analysis yet")
    return FileResponse(path, media_type="application/x-chess-pgn")


# --------------------------------------------------------------------------
# Review and corrections (PROTOCOL.md §8)
# --------------------------------------------------------------------------

def _require(game_id: str) -> None:
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")


@app.get("/api/games/{game_id}/review")
async def api_review(game_id: str):
    _require(game_id)
    return await asyncio.to_thread(review.payload, game_id)


@app.get("/api/games/{game_id}/rect/{seq}")
async def api_rect(game_id: str, seq: int, k: int = 0, prev: int | None = None,
                   squares: str | None = None, size: int = 512):
    """A capture rectified to a top-down board, with the changed squares drawn."""
    _require(game_id)
    try:
        blob = await asyncio.to_thread(review.rect_jpeg, game_id, seq, k, prev,
                                       squares, size)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(content=blob, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/games/{game_id}/corrections")
async def api_correct(game_id: str, body: dict):
    """Pin one ply to the move that was actually played and re-run the game."""
    _require(game_id)
    ply = body.get("ply")
    if not isinstance(ply, int):
        raise HTTPException(status_code=400, detail="ply must be an integer")
    try:
        return await asyncio.to_thread(review.apply_correction, game_id, ply,
                                       body.get("san"), body.get("uci"))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/games/{game_id}/corrections/{ply}")
async def api_uncorrect(game_id: str, ply: int):
    _require(game_id)
    try:
        return await asyncio.to_thread(review.drop_correction, game_id, ply)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/result")
async def api_set_result(game_id: str, body: dict):
    _require(game_id)
    try:
        return await asyncio.to_thread(review.set_result, game_id, body.get("result"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/verify")
async def api_verify(game_id: str, body: dict | None = None):
    """Mark the move list as truth."""
    _require(game_id)
    want = True if body is None else bool(body.get("verified", True))
    return await asyncio.to_thread(review.set_verified, game_id, want)


@app.post("/api/games/{game_id}/lichess")
async def api_lichess(game_id: str):
    """Import the current PGN, once. A second press returns the same game."""
    _require(game_id)
    meta = storage.read_meta(game_id)
    if meta.get("lichess_url"):
        return {"url": meta["lichess_url"]}
    path = storage.game_dir(game_id) / "game.pgn"
    if not path.exists():
        raise HTTPException(status_code=409, detail="no PGN to import yet")
    url = await import_pgn(path.read_text())
    if url:
        storage.update_meta(game_id, lichess_url=url)
    return {"url": url}


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

def _hello(game_id: str, room: str) -> dict:
    meta = storage.read_meta(game_id)
    return {
        "type": "hello",
        "role": "clock",
        "game_id": game_id,
        "room": room,
        "status": meta.get("status", "ready"),
        "result": meta.get("result"),
        "seq": storage.count_plies(game_id),
        "config": {
            "initial_ms": meta["initial_ms"],
            "increment_ms": meta["increment_ms"],
            **{k: meta.get(k) for k in SETTING_KEYS},
        },
        "t": now_ms(),
    }


def _status_for(event_type: str, current: str) -> str | None:
    """Game status implied by a clock event, or None to leave it alone."""
    return {
        "clock.start": "running",
        "clock.press": "running",
        "clock.resume": "running",
        "clock.pause": "paused",
        "clock.flag": "finished",
        "clock.stop": "finished",
    }.get(event_type)


# Events that would record a *new* game. `clock.config` and `clock.reset` are
# harmless on a finished game; these are not.
PLAY_EVENTS = {"clock.start", "clock.press", "clock.pause",
               "clock.resume", "clock.flag", "clock.stop"}


async def _handle_clock_event(room: str, game_id: str, msg: dict) -> None:
    event_type = msg["type"]

    # A clock phone that never reloaded still holds the old room, and its press
    # counter carries on from the finished game's. Left alone it writes a second
    # game into the first one's frames and event log, interleaved and with no
    # way to tell them apart afterwards. Refuse, and say why.
    meta = storage.read_meta(game_id)
    if event_type in PLAY_EVENTS and meta.get("status") == "finished":
        await hub.send(room, "clock", {"type": "game.finished",
                                       "game_id": game_id,
                                       "result": meta.get("result")})
        return

    storage.append_event(game_id, msg)

    fields: dict = {}
    status = _status_for(event_type, "")
    if status:
        fields["status"] = status
    if event_type == "clock.stop":
        fields["result"] = msg.get("result", "*")
    if event_type == "clock.flag":
        # Flagging decides the game unless the clock also sends a stop.
        side = msg.get("side")
        fields["result"] = "0-1" if side == "white" else "1-0"
    if event_type == "clock.config":
        for key in ("initial_ms", "increment_ms"):
            if key in msg:
                fields[key] = msg[key]
    if fields:
        storage.update_meta(game_id, **fields)


@app.websocket("/ws/{room}")
async def websocket_endpoint(
    websocket: WebSocket,
    room: str,
    role: str = Query("clock", pattern="^clock$"),
):
    room = room.upper()
    meta = storage.find_by_room(room)
    if meta is None:
        await websocket.close(code=4404, reason="unknown room")
        return
    game_id = meta["game_id"]

    await websocket.accept()
    await hub.join(room, game_id, role, websocket)

    try:
        await websocket.send_json(_hello(game_id, room))

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "t": now_ms()})
            elif msg_type in CLOCK_EVENTS:
                await _handle_clock_event(room, game_id, msg)
            # Unknown types are ignored on purpose: later phases add events.
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(room, role, websocket)


# Static assets last so the routes above win.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
