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

BOARD_NOT_FOUND = ("The board was never found in the start photo, so the moves cannot "
                   "be read. Mark the four corners on the camera phone before playing.")


def _saved_calibration(state: GameAnalysis, game_id: str) -> dict | None:
    if state.calibration is None:                 # survive a server restart
        state.calibration = storage.read_json(game_id, "calibration.json")
    return state.calibration


def calibration_usable(game_id: str) -> bool:
    """Whether the geometry can be trusted: detected with a passing score, or placed by hand.

    Calibrates on the spot if nothing has been saved yet, so a capture that
    races the start frame's own calibration is not refused for it. Must be
    called without the game's lock held.
    """
    state = for_game(game_id)
    if _saved_calibration(state, game_id) is None:
        calibrate_start_frame(game_id)
    cal = state.calibration or {}
    return bool(cal.get("ok")) or cal.get("method") == "manual"


def _decorate(payload: dict, bgr) -> dict:
    """What the phones need beyond the engine's own record of the calibration.

    ``corners_norm`` is the quad in 0..1 of the frame, which is what the camera
    page's overlay draws in; ``setup_warnings`` are problems with the camera
    placement that the players can still fix before the first move.
    """
    from engine.calibrate import frame_checks

    h, w = bgr.shape[:2]
    payload["frame_size"] = [w, h]
    usable = payload.get("ok") or payload.get("method") == "manual"
    payload["corners_norm"] = [[round(x / w, 5), round(y / h, 5)]
                               for x, y in payload["corners"]]
    payload["setup_warnings"] = frame_checks(bgr, payload["corners"] if usable else None)
    return payload


def calibrate_start_frame(game_id: str, force: bool = False) -> dict:
    """Find the board in the start-position frame. Safe to call repeatedly.

    Corners a player dragged by hand always win. Before the game starts the
    camera re-uploads frame 0 every couple of seconds, and each upload lands
    here — so without this guard an automatic detection that keeps failing
    overwrites the manual corners seconds after they are saved, and tells the
    camera to reopen the panel. From the phone that looks like the button did
    nothing.
    """
    from engine.calibrate import CHECKER_MIN, calibrate_image, load_frames

    state = for_game(game_id)
    _saved_calibration(state, game_id)
    if not force and (state.calibration or {}).get("method") == "manual":
        return state.calibration
    paths = _frames_for(game_id, 0)
    if not paths:
        return {"ok": False, "warning": "no start frame yet"}
    with state.lock:
        try:
            bgr = load_frames(paths)
            cal = calibrate_image(bgr)
        except Exception as exc:                      # a bad frame is not fatal
            state.error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "warning": state.error}
        payload = cal.to_json()
        payload["threshold"] = CHECKER_MIN
        # The camera page draws this quad and, when ok is false, offers the
        # manual four-corner drag that Phase 1 left waiting behind exactly this.
        payload["manual_corners_needed"] = not cal.ok
        _decorate(payload, bgr)
        state.calibration = payload
        storage.game_dir(game_id).joinpath("calibration.json").write_text(
            json.dumps(payload, indent=1))
        state.tracker = None                          # rebuild on the next frame
        return payload


def set_manual_corners(game_id: str, corners) -> dict:
    """Adopt four corners a player dragged on the camera page."""
    import numpy as np

    from engine.calibrate import _camera_side, calibrate_image, load_frames
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
        # The corners arrive in board order (a1, h1, h8, a8), so the orientation
        # is known from them alone — the detection that just failed has no say in
        # it, and its own guesses would be read as fact by the tracker.
        cal.camera_side_idx, cal.camera_side = _camera_side(pts)
        cal.square_is_light = np.array(
            [[(f + r) % 2 == 1 for r in range(8)] for f in range(8)])
        cal.ok = True
        cal.method = "manual"
        cal.warnings = list(cal.warnings) + ["manual-corners"]
        payload = cal.to_json()
        payload["manual_corners_needed"] = False
        payload["method"] = "manual"
        _decorate(payload, bgr)
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
        from engine.calibrate import _camera_side
        from engine.rectify import homography
        cal = calibrate(paths)
        cal.corners = np.asarray(saved["corners"], dtype=np.float64)
        cal.H = homography(cal.corners, cal.size)
        # The saved corners are in board order, so they alone say which side
        # the camera is on; a fresh detection's guess may disagree with them.
        cal.camera_side_idx, cal.camera_side = _camera_side(cal.corners)
        cal.ok = True
    return Tracker(cal, frame0, load_params())


def track_frame(game_id: str, seq: int) -> dict:
    """Feed one capture to the game's tracker and return the move list so far."""
    state = for_game(game_id)
    usable = calibration_usable(game_id)
    with state.lock:
        if state.error:
            return {"ok": False, "error": state.error, "moves": state.moves}
        if not usable:
            # Tracking against a grid that is not the board still yields a
            # legal game, and it looks exactly as confident as a real one —
            # that is how the third field test produced eight rook shuffles.
            return {"ok": False, "error": BOARD_NOT_FOUND, "moves": []}
        try:
            if state.tracker is None:
                state.tracker = _build_tracker(game_id)
                state.last_seq = 0
            if state.tracker is None:
                return {"ok": False, "error": "no start frame", "moves": []}
            from engine.tracker import press_sides
            sides = press_sides(storage.game_dir(game_id))
            for s in range(max(state.last_seq + 1, 1), seq + 1):
                paths = _frames_for(game_id, s)
                if paths:
                    state.tracker.add_frame(s, paths, sides.get(s))
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
    if not calibration_usable(game_id):
        return {"ok": False, "error": BOARD_NOT_FOUND, "board_not_found": True}
    track_frame(game_id, last)
    with state.lock:
        if state.tracker is None:
            return {"ok": False, "error": state.error or "nothing tracked"}
        result = state.tracker.result()
        payload = result.to_json()
        payload["game_id"] = game_id
        pgn = build_pgn(result, gdir)
        gdir.joinpath("analysis.json").write_text(json.dumps(payload, indent=1))
        gdir.joinpath("game.pgn").write_text(pgn.rstrip("\n") + "\n")
    return {"ok": True, "pgn": pgn, "flagged": len(result.flagged),
            "plies": len(result.plies)}


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------

def match_start_orientation(game_id: str, blob: bytes) -> bytes:
    """Rotate a capture to the start frame's orientation if the phone turned.

    The camera page works held either way, and the browser hands over frames
    upright for however the phone is held *now*. Every engine stage assumes the
    board sits where the start frame put it, so a phone that auto-rotates
    mid-game would otherwise end the game's tracking there. The right rotation
    is the one that makes the frame look most like the start frame, which does
    not depend on which way a given browser reports its angle.
    """
    import cv2
    import numpy as np

    ref_paths = _frames_for(game_id, 0)
    if not ref_paths:
        return blob
    img = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
    ref = cv2.imread(str(ref_paths[0]), cv2.IMREAD_GRAYSCALE)
    if img is None or ref is None:
        return blob
    rh, rw = ref.shape[:2]
    h, w = img.shape[:2]
    if (h > w) == (rh > rw) and h != w:
        options = [None, cv2.ROTATE_180]
    else:
        options = [cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE]

    def thumb(g):
        t = cv2.resize(g, (48, 48), interpolation=cv2.INTER_AREA).astype(np.float32)
        return (t - t.mean()) / (t.std() + 1e-6)

    ref_t = thumb(ref)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    cost = [float(np.abs(thumb(gray if o is None else cv2.rotate(gray, o)) - ref_t).mean())
            for o in options]
    best = int(np.argmin(cost))
    # Upright is the default; a half-turn has to win clearly, since a board seen
    # from above is close to symmetric under one.
    if options[0] is None and not cost[1] < 0.7 * cost[0]:
        return blob
    ok, enc = cv2.imencode(".jpg", cv2.rotate(img, options[best]),
                           [cv2.IMWRITE_JPEG_QUALITY, 92])
    return enc.tobytes() if ok else blob
