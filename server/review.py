"""After the game: the review payload, corrections, and the rectified overlays.

A correction is not an edit to the move list. The player pins one ply to the
move they actually played and the **whole game is tracked again** with that ply
constrained, because a ply read wrongly usually poisons the plies after it — the
board the tracker was matching against was wrong from there on. Re-running is
what puts those back, and it is why `labels.json` stores the pins rather than
the corrected move list alone: a later engine can be re-constrained from the
same human input without anyone labelling the game twice.

Everything here is synchronous and slow enough to belong in a worker thread;
`server.app` calls it with `asyncio.to_thread`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import chess

from . import analysis, storage

RESULTS = ("1-0", "0-1", "1/2-1/2", "*")


# --------------------------------------------------------------------------
# labels.json
# --------------------------------------------------------------------------

def read_labels(game_id: str) -> dict:
    saved = storage.read_json(game_id, "labels.json") or {}
    return {
        "game_id": game_id,
        "updated_at": saved.get("updated_at"),
        "corrections": list(saved.get("corrections") or []),
        "result": saved.get("result"),
        "verified": bool(saved.get("verified")),
        "moves": list(saved.get("moves") or []),
    }


def write_labels(game_id: str, labels: dict) -> dict:
    labels = dict(labels)
    labels["game_id"] = game_id
    labels["updated_at"] = time.time()
    path = storage.game_dir(game_id) / "labels.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(labels, indent=1))
    tmp.replace(path)
    return labels


def constraints_of(labels: dict) -> dict[int, chess.Move]:
    """The pinned plies, as the tracker wants them."""
    out: dict[int, chess.Move] = {}
    for c in labels.get("corrections") or []:
        try:
            out[int(c["ply"])] = chess.Move.from_uci(c["uci"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------
# Reading a game
# --------------------------------------------------------------------------

def _pgn_text(game_id: str) -> str | None:
    path = storage.game_dir(game_id) / "game.pgn"
    return path.read_text() if path.exists() else None


def payload(game_id: str) -> dict:
    """Everything the review page renders, in one object (PROTOCOL.md §8)."""
    meta = storage.read_meta(game_id)
    saved = storage.read_json(game_id, "analysis.json")
    state = analysis.for_game(game_id)
    return {
        "game_id": game_id,
        "room": meta.get("room"),
        "status": meta.get("status", "ready"),
        "created_at": meta.get("created_at"),
        "white_name": meta.get("white_name", "White"),
        "black_name": meta.get("black_name", "Black"),
        "result": meta.get("result"),
        "lichess_url": meta.get("lichess_url"),
        "pending": saved is None,
        "error": state.error,
        "pgn": _pgn_text(game_id),
        "frames": storage.frame_inventory(game_id),
        "labels": read_labels(game_id),
        "analysis": saved,
    }


# --------------------------------------------------------------------------
# Corrections
# --------------------------------------------------------------------------

def _moves_of(payload_analysis: dict | None) -> list[str]:
    return list((payload_analysis or {}).get("moves") or [])


def parse_move(game_id: str, ply: int, san: str | None, uci: str | None) -> chess.Move:
    """Read a correction against the position the player is looking at.

    The SAN on the review page is read off the board as it stands after the
    plies before this one — which are themselves the corrected ones — so that is
    the position it has to be parsed in.
    """
    saved = storage.read_json(game_id, "analysis.json")
    moves = _moves_of(saved)
    if ply < 0 or ply > len(moves):
        raise ValueError(f"ply {ply} is outside this game (0..{len(moves)})")
    board = chess.Board()
    for u in moves[:ply]:
        board.push(chess.Move.from_uci(u))
    if uci:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f"{uci} is not legal in this position")
        return move
    if not san:
        raise ValueError("a correction needs either san or uci")
    try:
        return board.parse_san(san)
    except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError) as exc:
        raise ValueError(f"{san!r} is not a legal move here: {exc}") from exc


def _rerun(game_id: str, labels: dict) -> dict:
    """Track the game again with every pinned ply constrained."""
    from engine.features import load_params
    from engine.pgn import build_pgn
    from engine.tracker import track_game

    gdir = storage.game_dir(game_id)
    result = track_game(gdir, load_params(), constraints=constraints_of(labels))
    payload_json = result.to_json()
    payload_json["game_id"] = game_id
    gdir.joinpath("analysis.json").write_text(json.dumps(payload_json, indent=1))
    pgn = build_pgn(result, gdir, result_override=labels.get("result")
                    or storage.read_meta(game_id).get("result"))
    gdir.joinpath("game.pgn").write_text(pgn.rstrip("\n") + "\n")
    labels["moves"] = [p.san for p in result.plies]
    write_labels(game_id, labels)
    return payload_json


def apply_correction(game_id: str, ply: int, san: str | None = None,
                     uci: str | None = None) -> dict:
    """Pin one ply and re-run. Returns the review payload plus `changed`."""
    if storage.read_json(game_id, "analysis.json") is None:
        raise LookupError("this game has no analysis to correct")
    move = parse_move(game_id, ply, san, uci)
    before = _moves_of(storage.read_json(game_id, "analysis.json"))

    labels = read_labels(game_id)
    board = chess.Board()
    for u in before[:ply]:
        board.push(chess.Move.from_uci(u))
    labels["corrections"] = [c for c in labels["corrections"] if int(c["ply"]) != ply]
    labels["corrections"].append({"ply": ply, "san": board.san(move),
                                  "uci": move.uci(), "t": time.time()})
    labels["corrections"].sort(key=lambda c: c["ply"])

    _rerun(game_id, labels)
    out = payload(game_id)
    out["changed"] = _changed(before, _moves_of(out["analysis"]))
    return out


def drop_correction(game_id: str, ply: int) -> dict:
    """Unpin one ply and re-run."""
    labels = read_labels(game_id)
    kept = [c for c in labels["corrections"] if int(c["ply"]) != ply]
    if len(kept) == len(labels["corrections"]):
        raise LookupError(f"ply {ply} is not pinned")
    before = _moves_of(storage.read_json(game_id, "analysis.json"))
    labels["corrections"] = kept
    _rerun(game_id, labels)
    out = payload(game_id)
    out["changed"] = _changed(before, _moves_of(out["analysis"]))
    return out


def _changed(before: list[str], after: list[str]) -> list[int]:
    n = max(len(before), len(after))
    return [i for i in range(n)
            if (before[i] if i < len(before) else None)
            != (after[i] if i < len(after) else None)]


# --------------------------------------------------------------------------
# Result and verification
# --------------------------------------------------------------------------

def set_result(game_id: str, result: str) -> dict:
    if result not in RESULTS:
        raise ValueError(f"result must be one of {', '.join(RESULTS)}")
    from engine.pgn import build_pgn

    storage.update_meta(game_id, result=result)
    labels = read_labels(game_id)
    labels["result"] = result
    write_labels(game_id, labels)

    gdir = storage.game_dir(game_id)
    saved = storage.read_json(game_id, "analysis.json")
    pgn = None
    if saved is not None:
        pgn = build_pgn(_ReplayedResult(saved), gdir, result_override=result)
        gdir.joinpath("game.pgn").write_text(pgn.rstrip("\n") + "\n")
    return {"ok": True, "result": result, "pgn": pgn}


def set_verified(game_id: str, verified: bool) -> dict:
    """Mark the move list as truth, which is what puts it in front of tune.py."""
    labels = read_labels(game_id)
    labels["verified"] = bool(verified)
    if verified and not labels["moves"]:
        labels["moves"] = [p["san"] for p in
                           (storage.read_json(game_id, "analysis.json") or {}).get("plies", [])]
    write_labels(game_id, labels)
    return {"ok": True, "verified": bool(verified)}


class _ReplayedResult:
    """Just enough of a ``TrackResult`` for ``build_pgn`` to read a saved game.

    Rewriting the PGN after a result override should not cost a re-track, and
    everything ``build_pgn`` touches is already in ``analysis.json``.
    """

    class _Ply:
        def __init__(self, d: dict):
            self.index = d["index"]
            self.san = d["san"]
            self.uci = d["uci"]
            self.seq = d["seq"]
            self.flags = d.get("flags") or []

    def __init__(self, saved: dict):
        self.plies = [self._Ply(p) for p in saved.get("plies", [])]
        self.moves = [chess.Move.from_uci(p.uci) for p in self.plies]


# --------------------------------------------------------------------------
# Rectified overlays
# --------------------------------------------------------------------------

_CHANGE_MIN = 14.0          # mean |Δgrey| over a square, on 0..255


def _calibration_for(game_id: str):
    from engine.calibrate import calibrate

    gdir = storage.game_dir(game_id)
    paths = sorted((gdir / "frames").glob("0000_*.jpg"))
    if not paths:
        raise LookupError("no start frame to calibrate against")
    saved = storage.read_json(game_id, "calibration.json")
    cal = calibrate(paths)
    if saved and saved.get("corners"):
        import numpy as np

        from engine.rectify import homography
        cal.corners = np.asarray(saved["corners"], dtype=np.float64)
        cal.H = homography(cal.corners, cal.size)
    return cal


def rect_jpeg(game_id: str, seq: int, k: int = 0, prev: int | None = None,
              squares: str | None = None, size: int = 512) -> bytes:
    """One capture, rectified, with the squares worth looking at outlined."""
    import cv2
    import numpy as np

    from engine.rectify import rectify, square_box

    size = max(128, min(int(size), 1024))
    path = storage.frame_path(game_id, seq, k)
    if not path.exists():
        raise LookupError(f"no frame {seq}/{k}")
    cal = _calibration_for(game_id)
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise LookupError(f"frame {seq}/{k} is not readable")
    rect = rectify(bgr, cal, size=size)

    if prev is not None:
        prev_path = storage.frame_path(game_id, prev, 0)
        prev_bgr = cv2.imread(str(prev_path), cv2.IMREAD_COLOR) if prev_path.exists() else None
        if prev_bgr is not None:
            a = cv2.cvtColor(rectify(prev_bgr, cal, size=size), cv2.COLOR_BGR2GRAY)
            b = cv2.cvtColor(rect, cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(a.astype(np.float32), b.astype(np.float32))
            for file in range(8):
                for rank in range(8):
                    x0, y0, x1, y1 = square_box(size, file, rank)
                    if float(diff[y0:y1, x0:x1].mean()) >= _CHANGE_MIN:
                        cv2.rectangle(rect, (x0 + 1, y0 + 1), (x1 - 2, y1 - 2),
                                      (60, 150, 210), 2)          # amber, BGR

    for name in _square_names(squares):
        file, rank = name
        x0, y0, x1, y1 = square_box(size, file, rank)
        cv2.rectangle(rect, (x0 + 3, y0 + 3), (x1 - 4, y1 - 4), (48, 56, 200), 3)  # red

    ok, buf = cv2.imencode(".jpg", rect, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        raise LookupError("could not encode the rectified view")
    return buf.tobytes()


def _square_names(squares: str | None) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for token in (squares or "").split(","):
        token = token.strip().lower()
        if len(token) != 2 or token[0] not in "abcdefgh" or token[1] not in "12345678":
            continue
        out.append((ord(token[0]) - 97, int(token[1]) - 1))
    return out
