"""Running the engine beside a live game.

Two jobs, both off the event loop. **Calibration** happens once, as soon as the
start-position frame lands, because both phones want to know whether the board
was found before the first move is played — the camera draws the quad it
detected and offers the manual corner drag if it did not, and the clock lights
its calibration dot.

**Tracking** runs incrementally: each capture is fed to a ``Tracker`` that
persists for the game, so the move list is current as the game goes on rather
than being computed from scratch at the end. The final ``clock.stop`` or
``clock.flag`` writes ``analysis.json`` and ``game.pgn``.

Everything here runs in a worker thread. OpenCV releases the GIL for the
expensive parts, and nothing in this module touches a WebSocket directly — the
caller awaits the result and does the sending.
"""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import storage


@dataclass
class GameAnalysis:
    """The engine state for one live game."""

    game_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    tracker: object | None = None
    calibration: dict | None = None
    last_seq: int = -1
    moves: list[str] = field(default_factory=list)
    error: str | None = None


_games: dict[str, GameAnalysis] = {}
_registry_lock = threading.Lock()


def for_game(game_id: str) -> GameAnalysis:
    with _registry_lock:
        state = _games.get(game_id)
        if state is None:
            state = GameAnalysis(game_id=game_id)
            _games[game_id] = state
        return state


def forget(game_id: str) -> None:
    with _registry_lock:
        _games.pop(game_id, None)


def _frames_for(game_id: str, seq: int) -> list[Path]:
    gdir = storage.game_dir(game_id)
    return sorted((gdir / "frames").glob(f"{seq:04d}_*.jpg"))


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

def calibrate_start_frame(game_id: str) -> dict:
    """Find the board in the start-position frame. Safe to call repeatedly."""
    from engine.calibrate import CHECKER_MIN, calibrate

    state = for_game(game_id)
    paths = _frames_for(game_id, 0)
    if not paths:
        return {"ok": False, "warning": "no start frame yet"}
    with state.lock:
        try:
            cal = calibrate(paths)
        except Exception as exc:                      # a bad frame is not fatal
            state.error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "warning": state.error}
        payload = cal.to_json()
        payload["threshold"] = CHECKER_MIN
        # The camera page draws this quad and, when ok is false, offers the
        # manual four-corner drag that Phase 1 left waiting behind exactly this.
        payload["manual_corners_needed"] = not cal.ok
        state.calibration = payload
        storage.game_dir(game_id).joinpath("calibration.json").write_text(
            json.dumps(payload, indent=1))
        state.tracker = None                          # rebuild on the next frame
        return payload


def set_manual_corners(game_id: str, corners) -> dict:
    """Adopt four corners a player dragged on the camera page."""
    import numpy as np

    from engine.calibrate import calibrate_image, load_frames
    from engine.rectify import homography

    state = for_game(game_id)
    paths = _frames_for(game_id, 0)
    if not paths:
        raise ValueError("no start frame to calibrate against")
    pts = np.asarray(corners, dtype=np.float64).reshape(4, 2)
    with state.lock:
        bgr = load_frames(paths)
        cal = calibrate_image(bgr, refine=False, climb=False)
        cal.corners = pts
        cal.corners_image = pts
        cal.H = homography(pts, cal.size)
        cal.ok = True
        cal.method = "manual"
        cal.warnings = list(cal.warnings) + ["manual-corners"]
        payload = cal.to_json()
        payload["manual_corners_needed"] = False
        state.calibration = payload
        state.tracker = None
        storage.game_dir(game_id).joinpath("calibration.json").write_text(
            json.dumps(payload, indent=1))
    return payload


# --------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------

def _build_tracker(game_id: str):
    import cv2

    from engine.calibrate import Calibration, calibrate
    from engine.features import load_params
    from engine.tracker import Tracker
    import numpy as np

    paths = _frames_for(game_id, 0)
    if not paths:
        return None
    state = for_game(game_id)
    saved = state.calibration
    frame0 = cv2.imread(str(paths[0]), cv2.IMREAD_COLOR)
    if frame0 is None:
        return None
    if saved is None:
        cal = calibrate(paths)
        state.calibration = cal.to_json()
    else:
        cal = calibrate(paths)
        cal.corners = np.asarray(saved["corners"], dtype=np.float64)
        from engine.rectify import homography
        cal.H = homography(cal.corners, cal.size)
    return Tracker(cal, frame0, load_params())


def track_frame(game_id: str, seq: int) -> dict:
    """Feed one capture to the game's tracker and return the move list so far."""
    state = for_game(game_id)
    with state.lock:
        if state.error:
            return {"ok": False, "error": state.error, "moves": state.moves}
        try:
            if state.tracker is None:
                state.tracker = _build_tracker(game_id)
                state.last_seq = 0
            if state.tracker is None:
                return {"ok": False, "error": "no start frame", "moves": []}
            for s in range(max(state.last_seq + 1, 1), seq + 1):
                paths = _frames_for(game_id, s)
                if paths:
                    state.tracker.add_frame(s, paths)
                    state.last_seq = s
            result = state.tracker.result()
        except Exception as exc:
            state.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            return {"ok": False, "error": state.error, "moves": state.moves}
        state.moves = [p.san for p in result.plies]
        return {"ok": True, "seq": seq, "moves": state.moves,
                "flagged": len(result.flagged)}


def finish(game_id: str) -> dict:
    """Write ``analysis.json`` and ``game.pgn``. Called on stop or flag."""
    from engine.pgn import build_pgn

    state = for_game(game_id)
    gdir = storage.game_dir(game_id)
    last = storage.max_seq(game_id)
    track_frame(game_id, last)
    with state.lock:
        if state.tracker is None:
            return {"ok": False, "error": state.error or "nothing tracked"}
        result = state.tracker.result()
        payload = result.to_json()
        payload["game_id"] = game_id
        pgn = build_pgn(result, gdir)
        gdir.joinpath("analysis.json").write_text(json.dumps(payload, indent=1))
        gdir.joinpath("game.pgn").write_text(pgn + "\n")
    return {"ok": True, "pgn": pgn, "flagged": len(result.flagged),
            "plies": len(result.plies)}
