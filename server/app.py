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

from . import gaps, photos, review, storage
from .lichess import import_pgn
from .models import CLOCK_EVENTS, READER_MODELS, SETTING_KEYS, Confirm, GameConfig, GameSettings
from .ws import hub, now_ms

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="BoardCam", version="0.2.0")


@app.on_event("shutdown")
async def _close_engines():
    await asyncio.to_thread(gaps.shutdown)


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
        if {"white_rating", "black_rating", "white_name", "black_name"} & fields.keys():
            schedule_solve(game_id)      # the players changed, so the likely lines did
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
        entry = await asyncio.to_thread(photos.confirm, game_id, n, body.rows,
                                        body.home, body.ply)
    except photos.PhotoError as exc:
        raise _photo_error(exc) from exc
    schedule_solve(game_id)
    return entry


@app.get("/games/{game_id}/pgn")
async def get_pgn(game_id: str):
    _require(game_id)
    text = await asyncio.to_thread(review.ensure_pgn, game_id)
    return Response(content=text, media_type="application/x-chess-pgn",
                    headers={"Content-Disposition": f'attachment; filename="{game_id}.pgn"'})


# --------------------------------------------------------------------------
# Lines between photos (PROTOCOL.md §8)
# --------------------------------------------------------------------------

# One solver job per game; a request while it runs marks the game dirty and the
# job goes round again, so the last photo or pin always gets solved.
_solving: dict[str, asyncio.Task] = {}
_dirty: set[str] = set()


def _require(game_id: str) -> None:
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")


async def _push_lines(game_id: str, doc: dict) -> None:
    room = storage.read_meta(game_id)["room"]
    gaps_ = [review._gap_summary(g) for g in doc.get("gaps", [])]
    await hub.send(room, "clock", {"type": "lines.ready", "game_id": game_id,
                                   "gaps": gaps_})


async def _solve_loop(game_id: str) -> None:
    loop = asyncio.get_running_loop()
    try:
        while True:
            _dirty.discard(game_id)

            def progress(doc):
                asyncio.run_coroutine_threadsafe(_push_lines(game_id, doc), loop)

            try:
                doc = await asyncio.to_thread(gaps.solve_game, storage.game_dir(game_id),
                                              on_gap=progress)
                await _push_lines(game_id, doc)
            except Exception as exc:       # never let a solver bug kill the server
                print(f"[boardcam] solving {game_id} failed: {type(exc).__name__}: {exc}")
            if game_id not in _dirty or not storage.exists(game_id):
                break
    finally:
        _solving.pop(game_id, None)


def schedule_solve(game_id: str) -> None:
    """Solve the game's gaps in the background (cached gaps cost nothing)."""
    if game_id in _solving:
        _dirty.add(game_id)
        return
    _solving[game_id] = asyncio.create_task(_solve_loop(game_id))


@app.get("/api/games/{game_id}/review")
async def api_review(game_id: str):
    _require(game_id)
    return await asyncio.to_thread(review.payload, game_id, game_id in _solving)


@app.post("/api/games/{game_id}/solve")
async def api_solve(game_id: str):
    """Make sure every gap is solved; returns at once (poll the review payload)."""
    _require(game_id)
    schedule_solve(game_id)
    return {"ok": True, "solving": True}


@app.get("/api/games/{game_id}/engine/{ply}")
async def api_engine(game_id: str, ply: int):
    """Stockfish's top moves at the position of ``ply`` on the timeline."""
    _require(game_id)
    try:
        return await asyncio.to_thread(review.top_moves, game_id, ply)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/pins")
async def api_pin(game_id: str, body: dict):
    """Accept a move at one ply; the gap around it is solved again."""
    _require(game_id)
    ply, uci = body.get("ply"), body.get("uci")
    if not isinstance(ply, int) or not isinstance(uci, str):
        raise HTTPException(status_code=400, detail="needs ply (int) and uci (str)")
    try:
        pins = await asyncio.to_thread(review.add_pin, game_id, ply, uci)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    schedule_solve(game_id)
    return {"pins": pins}


@app.delete("/api/games/{game_id}/pins/{ply}")
async def api_unpin(game_id: str, ply: int):
    _require(game_id)
    try:
        pins = await asyncio.to_thread(review.drop_pin, game_id, ply)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    schedule_solve(game_id)
    return {"pins": pins}


@app.post("/api/games/{game_id}/result")
async def api_set_result(game_id: str, body: dict):
    _require(game_id)
    try:
        return await asyncio.to_thread(review.set_result, game_id, body.get("result"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/lichess")
async def api_lichess(game_id: str):
    """Import the current PGN, once. A second press returns the same game."""
    _require(game_id)
    meta = storage.read_meta(game_id)
    if meta.get("lichess_url"):
        return {"url": meta["lichess_url"]}
    text = await asyncio.to_thread(review.ensure_pgn, game_id)
    url = await import_pgn(text)
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
        "hint_plies": hint_plies(meta),
        "t": now_ms(),
    }


def hint_plies(meta: dict) -> int | None:
    """Plies since the last photo after which the camera button glows: the
    Phase 3 threshold for the band of the two players' mean rating."""
    from engine.humanlike import band_for
    from engine.lines import load_solver_config

    table = load_solver_config().get("hint_plies") or {}
    mean = (meta.get("white_rating", 1350) + meta.get("black_rating", 1350)) / 2
    return table.get(str(band_for(mean)))


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
