"""BoardCam server: static pages, REST, and the clock/camera WebSocket bridge.

Contract: docs/PROTOCOL.md. The server is deliberately thin — it stores events
and frames and forwards capture commands. All clock authority lives on the clock
phone, all engine work lands in later phases.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import analysis, storage
from .models import CAPTURE_REASONS, CLOCK_EVENTS, GameConfig
from .ws import hub, now_ms

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="BoardCam", version="0.1.0")


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


@app.get("/camera")
async def camera_page():
    return _page("camera.html")


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

@app.post("/games")
async def create_game(config: GameConfig | None = None):
    meta = storage.create_game(config or GameConfig())
    return {
        "game_id": meta["game_id"],
        "room": meta["room"],
        "created_at": meta["created_at"],
        "initial_ms": meta["initial_ms"],
        "increment_ms": meta["increment_ms"],
    }


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
        "frames": storage.frame_inventory(game_id),
        "events": storage.read_events(game_id),
        "analysis": storage.read_json(game_id, "analysis.json"),
        "calibration": storage.read_json(game_id, "calibration.json"),
    }


@app.delete("/api/games/{game_id}")
async def api_delete_game(game_id: str):
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    storage.delete_game(game_id)
    return {"ok": True}


@app.post("/games/{game_id}/frames/{seq}/{k}")
async def upload_frame(game_id: str, seq: int, k: int, file: UploadFile = File(...)):
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    blob = await file.read()
    # JPEG SOI marker. Cheap, and it keeps a stray HTML error page off disk.
    if len(blob) < 4 or blob[:2] != b"\xff\xd8":
        raise HTTPException(status_code=415, detail="expected a JPEG body")
    try:
        path = storage.write_frame(game_id, seq, k, blob)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    meta = storage.read_meta(game_id)
    room = meta["room"]
    await hub.send(
        room, "clock",
        {"type": "frame.ok", "seq": seq, "k": k, "bytes": len(blob)},
    )
    if seq == 0:
        asyncio.create_task(_calibrate_and_push(game_id, room))
    else:
        asyncio.create_task(_track_and_push(game_id, room, seq))
    return {
        "ok": True, "seq": seq, "k": k, "bytes": len(blob),
        "path": f"frames/{path.name}",
    }


async def _calibrate_and_push(game_id: str, room: str) -> None:
    """Find the board and tell both phones, before the first move is played."""
    payload = await asyncio.to_thread(analysis.calibrate_start_frame, game_id)
    message = {"type": "calibration", **payload}
    await hub.send(room, "camera", message)
    await hub.send(room, "clock", {"type": "calibration", "ok": payload.get("ok", False),
                                   "score": payload.get("score"),
                                   "warning": (payload.get("warnings") or [None])[0]})


async def _track_and_push(game_id: str, room: str, seq: int) -> None:
    """Keep the move list current as the game goes on."""
    got = await asyncio.to_thread(analysis.track_frame, game_id, seq)
    if got.get("ok"):
        await hub.broadcast(room, {"type": "moves", "seq": seq,
                                   "moves": got["moves"], "flagged": got["flagged"]})


async def _finish_and_push(game_id: str, room: str) -> None:
    """Run the tracker to the end and hand both phones the PGN."""
    got = await asyncio.to_thread(analysis.finish, game_id)
    if not got.get("ok"):
        await hub.broadcast(room, {"type": "analysis.error",
                                   "error": got.get("error", "analysis failed")})
        return
    url = None
    if storage.read_meta(game_id).get("lichess_url") is None:
        from .lichess import import_pgn
        url = await asyncio.to_thread(import_pgn, got["pgn"])
        if url:
            storage.update_meta(game_id, lichess_url=url)
    await hub.broadcast(room, {"type": "analysis.ready", "pgn": got["pgn"],
                               "lichess_url": url, "flagged": got["flagged"]})
    analysis.forget(game_id)


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


@app.post("/api/games/{game_id}/corners")
async def api_manual_corners(game_id: str, body: dict):
    """Adopt four corners dragged on the camera page.

    The detector gets the board in all but a few per cent of start frames, and
    when it does not there is no point failing the game over it: the players can
    drag the quad themselves and the engine carries on.
    """
    if not storage.exists(game_id):
        raise HTTPException(status_code=404, detail="unknown game")
    corners = body.get("corners")
    if not isinstance(corners, list) or len(corners) != 4:
        raise HTTPException(status_code=400,
                            detail="corners must be four [x, y] pairs, board order")
    try:
        payload = await asyncio.to_thread(analysis.set_manual_corners, game_id, corners)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    meta = storage.read_meta(game_id)
    await hub.broadcast(meta["room"], {"type": "calibration", **payload})
    return payload


@app.get("/api/games/{game_id}/analysis")
async def api_analysis(game_id: str):
    """The tracked move list, its per-ply confidence and its flags."""
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
# WebSocket
# --------------------------------------------------------------------------

def _hello(game_id: str, room: str, role: str) -> dict:
    meta = storage.read_meta(game_id)
    peer = "camera" if role == "clock" else "clock"
    return {
        "type": "hello",
        "role": role,
        "game_id": game_id,
        "room": room,
        "status": meta.get("status", "ready"),
        "result": meta.get("result"),
        "seq": storage.count_plies(game_id),
        "frames": storage.count_frames(game_id),
        "config": {
            "initial_ms": meta["initial_ms"],
            "increment_ms": meta["increment_ms"],
            "white_name": meta.get("white_name", "White"),
            "black_name": meta.get("black_name", "Black"),
        },
        "peer_connected": hub.is_connected(room, peer),
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


async def _handle_clock_event(room: str, game_id: str, msg: dict) -> None:
    event_type = msg["type"]
    stored = storage.append_event(game_id, msg)

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
        for key in ("initial_ms", "increment_ms", "white_name", "black_name"):
            if key in msg:
                fields[key] = msg[key]
    if fields:
        storage.update_meta(game_id, **fields)

    reason = CAPTURE_REASONS.get(event_type)
    if reason:
        seq = msg.get("seq")
        if not isinstance(seq, int):
            # A stop or flag from a stale cached page may omit seq. Losing the
            # final frame of a game is worse than guessing its number, and the
            # tracker tolerates an extra frame that advanced no plies.
            seq = storage.max_seq(game_id) + 1
        await hub.send(room, "camera", {"type": "capture", "seq": seq, "reason": reason})

    # The camera mirrors clock state for its own status strip.
    await hub.send(room, "camera", {"type": "clock.event", "event": stored})

    if event_type in ("clock.stop", "clock.flag"):
        asyncio.create_task(_finish_and_push(game_id, room))


@app.websocket("/ws/{room}")
async def websocket_endpoint(
    websocket: WebSocket,
    room: str,
    role: str = Query(..., pattern="^(clock|camera)$"),
):
    room = room.upper()
    meta = storage.find_by_room(room)
    if meta is None:
        await websocket.close(code=4404, reason="unknown room")
        return
    game_id = meta["game_id"]

    await websocket.accept()
    await hub.join(room, game_id, role, websocket)
    peer = "camera" if role == "clock" else "clock"

    try:
        await websocket.send_json(_hello(game_id, room, role))
        await hub.send(room, peer, {"type": "peer", "role": role, "connected": True})

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
            elif msg_type in CLOCK_EVENTS and role == "clock":
                await _handle_clock_event(room, game_id, msg)
            elif msg_type == "camera.status" and role == "camera":
                status = {k: msg.get(k) for k in ("streaming", "w", "h", "error")}
                r = hub.peek(room)
                if r is not None:
                    r.camera_status = status
                await hub.send(
                    room, "clock",
                    {"type": "peer", "role": "camera", "connected": True, **status},
                )
            # Unknown types are ignored on purpose: later phases add events.
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(room, role, websocket)
        await hub.send(room, peer, {"type": "peer", "role": role, "connected": False})


# Static assets last so the routes above win.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
