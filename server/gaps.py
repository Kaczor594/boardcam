"""The game between the photos: solve every gap, pin moves, evaluate the line.

Contract: ``engine/README.md`` (Lines → ``lines.json``) and ``docs/PROTOCOL.md``
§8. A *gap* runs between two consecutive anchors: the initial position (ply 0)
and every confirmed photo, in ply order. Each gap is solved with
:func:`engine.lines.solve` under the game's two players (``engine.humanlike``),
with the player's accepted moves (``pins.json``) forced. Solved gaps are cached
in ``lines.json`` under a key of everything the answer depends on, so
re-solving a game after one more photo only solves the new gap.

The *timeline* is the game as the review page steps through it: one entry per
ply along the most likely line of every solved gap. From it come the
evaluation graph (Stockfish, cached per FEN in ``evals.json``) and the PGN.

Everything here works on a game directory, not a game id, so
``scripts.replay_game`` can run it on a copied game. It is synchronous and
slow (seconds per gap); the server runs it in a worker thread, one game at a
time (lc0 and Stockfish are shared).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import chess

from engine import lines as lines_mod
from engine.pgn import build_pgn, read_events

START = chess.STARTING_FEN
EVAL_DEPTH = 12
TOP_ENGINE = 3

# Replaced in tests. MOVER(meta) -> object with .policy(board), .prefetch(boards),
# .bands {"white", "black"}, .prior {"white", "black"}.
MOVER = None
# EVALUATOR() -> object with .evaluate(board) -> {"cp", "mate"} and
# .top_moves(fen, k) -> [{"uci", "san", "cp", "mate"}].
EVALUATOR = None

_engine_lock = threading.Lock()     # one solve / evaluation at a time: shared lc0 + Stockfish


# --------------------------------------------------------------------------
# Small JSON helpers on a game directory
# --------------------------------------------------------------------------

def _read(gdir: Path, name: str, default):
    path = gdir / name
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write(gdir: Path, name: str, doc) -> None:
    path = gdir / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.replace(path)


def board_ply(fen: str) -> int:
    return chess.Board(fen).ply()


# --------------------------------------------------------------------------
# Players: the default mover (Maia through lc0, opening prior) and Stockfish
# --------------------------------------------------------------------------

_maia = None
_games = None
_movers: dict[tuple, object] = {}
_stockfish = None


class _GameMover:
    """``engine.humanlike.Mover`` plus the bands and prior flags lines.json records."""

    def __init__(self, mover):
        self._m = mover
        self.policy = mover.policy
        self.prefetch = mover.prefetch
        self.bands = {"white": mover.sides[chess.WHITE].band,
                      "black": mover.sides[chess.BLACK].band}
        self.prior = {"white": mover.sides[chess.WHITE].prior is not None,
                      "black": mover.sides[chess.BLACK].prior is not None}


def _default_mover(meta: dict):
    global _maia, _games
    from engine import humanlike

    key = (meta.get("white_rating", 1350), meta.get("black_rating", 1350),
           meta.get("white_name", ""), meta.get("black_name", ""))
    if key in _movers:
        return _movers[key]
    if _maia is None:
        _maia = humanlike.Maia(workers=3)
    if _games is None:
        try:
            _games = humanlike.load_games()
        except Exception:           # no chess-insights database: Maia only
            _games = []
    mover = _GameMover(humanlike.for_game(key[0], key[1], white_name=key[2],
                                          black_name=key[3], maia=_maia, games=_games))
    _movers[key] = mover
    return mover


def _default_evaluator():
    global _stockfish
    if _stockfish is None:
        from engine.evalgraph import Stockfish
        _stockfish = Stockfish(depth=EVAL_DEPTH)
    return _stockfish


def mover_for(meta: dict):
    return (MOVER or _default_mover)(meta)


def evaluator():
    return (EVALUATOR or _default_evaluator)()


# --------------------------------------------------------------------------
# Anchors and pins
# --------------------------------------------------------------------------

def anchors(positions: dict) -> list[dict]:
    """Confirmed photos in ply order; one per ply (the latest photo wins)."""
    by_ply: dict[int, dict] = {}
    for p in positions.get("photos", []):
        if p.get("status") != "confirmed" or not p.get("confirmed"):
            continue
        c = p["confirmed"]
        cur = by_ply.get(c["ply"])
        if cur is None or p["n"] > cur["photo"]:
            by_ply[c["ply"]] = {"photo": p["n"], "ply": c["ply"], "fen": c["fen"]}
    return [by_ply[k] for k in sorted(by_ply)]


def read_pins(gdir: Path) -> list[dict]:
    return list(_read(gdir, "pins.json", {}).get("pins", []))


def _pins_by_index(pins: list[dict]) -> dict[int, str]:
    """``{board.ply() before the move: uci}``; a pin's ``ply`` is the move's own ply (1-based)."""
    return {int(p["ply"]) - 1: p["uci"] for p in pins}


def pinned_policy(policy, pins: dict[int, str]):
    """``policy`` with the pinned plies forced: only the pinned move, with probability 1."""
    if not pins:
        return policy

    def pol(board: chess.Board) -> dict[str, float]:
        uci = pins.get(board.ply())
        if uci is None:
            return policy(board)
        return {uci: 1.0} if board.is_legal(chess.Move.from_uci(uci)) else {}
    return pol


# --------------------------------------------------------------------------
# Solving
# --------------------------------------------------------------------------

def _gap_key(fen_from: str, fen_to: str, n: int, bands: dict, prior: dict,
             pins: dict[int, str], solver: dict) -> str:
    blob = json.dumps([fen_from, fen_to.split()[0], n, bands, prior,
                       sorted(pins.items()), solver], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _solver_settings() -> dict:
    cfg = lines_mod.load_solver_config()
    return {k: cfg.get(k) for k in ("lam", "width", "time_box", "top", "c", "beam_div")}


def plan_gaps(positions: dict) -> list[tuple[dict, dict]]:
    """(from, to) anchor pairs; ``from`` of the first gap is the initial position."""
    out, prev = [], {"photo": None, "ply": 0, "fen": START}
    for a in anchors(positions):
        out.append((prev, a))
        prev = a
    return out


def solve_game(gdir: Path, *, on_gap=None) -> dict:
    """Solve every gap that is not already solved under the same inputs; write
    ``lines.json``, ``evals.json`` and ``game.pgn``. Returns the lines document.

    ``on_gap(doc)`` is called after each newly solved gap (progress pushes)."""
    gdir = Path(gdir)
    meta = _read(gdir, "meta.json", {})
    positions = _read(gdir, "positions.json", {"photos": []})
    pins = _pins_by_index(read_pins(gdir))
    old = {g.get("key"): g for g in _read(gdir, "lines.json", {}).get("gaps", [])}
    solver = _solver_settings()
    doc = {"game_id": meta.get("game_id"), "updated_at": time.time(), "gaps": []}

    with _engine_lock:
        mover = None
        fen_from = START
        from_anchor = {"photo": None, "ply": 0, "fen": START}
        for prev, to in plan_gaps(positions):
            # the next gap starts where the last line ended (castling and e.p.
            # from the path); after an unsolved gap, from the photo's own FEN
            frm = {**from_anchor, "fen": fen_from, "ply": board_ply(fen_from)}
            n = to["ply"] - frm["ply"]
            gap_pins = {i: u for i, u in pins.items() if frm["ply"] <= i < to["ply"]}
            if mover is None:
                mover = mover_for(meta)
            key = _gap_key(frm["fen"], to["fen"], n, mover.bands, mover.prior, gap_pins, solver)
            entry = old.get(key)
            if entry is None:
                entry = {"key": key, "from": frm, "to": to, "bands": mover.bands,
                         "prior": mover.prior, "solver": solver,
                         "pins": [{"ply": i + 1, "uci": u} for i, u in sorted(gap_pins.items())]}
                if n < 0:
                    entry["result"] = {"status": "order", "n_requested": n, "n_used": None,
                                       "complete": True, "lines": [], "marginals": [],
                                       "fen_a": frm["fen"], "placement_b": to["fen"].split()[0]}
                else:
                    res = lines_mod.solve(frm["fen"], to["fen"], n,
                                          pinned_policy(mover.policy, gap_pins),
                                          prefetch=mover.prefetch)
                    entry["result"] = res.to_json()
                doc["gaps"].append(entry)
                if on_gap:
                    _write(gdir, "lines.json", doc)
                    on_gap(doc)
            else:
                entry = {**entry, "from": frm, "to": to}
                doc["gaps"].append(entry)
            result = entry["result"]
            fen_from = result["lines"][0]["fen_end"] if result.get("lines") else to["fen"]
            from_anchor = {"photo": to["photo"], "ply": to["ply"], "fen": to["fen"]}

        _write(gdir, "lines.json", doc)
        tl = timeline(doc, pins)
        _evaluate(gdir, tl)
    write_pgn(gdir, doc)
    return doc


# --------------------------------------------------------------------------
# The game as one sequence of plies
# --------------------------------------------------------------------------

def timeline(doc: dict, pins: dict[int, str] | None = None) -> list[dict]:
    """One entry per known position, in ply order.

    ``{"ply", "fen", "uci", "san", "kind", "photo", "p", "certain", "gap", "alts"}``:
    ``kind`` is ``start`` | ``photo`` | ``inferred`` | ``pinned``; ``p`` is the
    marginal probability of the move that led here (``None`` for anchors reached
    through an unsolved gap); ``certain`` is true for photo positions and for
    positions reached from a certain one by a pinned or forced move; ``alts``
    are the marginals of the move *into* this position. A position after an
    unsolved gap has ``uci: None`` and ``break: true``.
    """
    pins = pins if pins is not None else {}
    out = [{"ply": 0, "fen": START, "uci": None, "san": None, "kind": "start",
            "photo": None, "p": None, "certain": True, "gap": None, "alts": [], "break": False}]
    for gi, g in enumerate(doc.get("gaps", [])):
        res = g["result"]
        to = g["to"]
        if res.get("status") != "found" or not res.get("lines"):
            out.append({"ply": to["ply"], "fen": to["fen"], "uci": None, "san": None,
                        "kind": "photo", "photo": to["photo"], "p": None, "certain": True,
                        "gap": gi, "alts": [], "break": True})
            continue
        top = res["lines"][0]
        b = chess.Board(res["fen_a"])
        exhaustive = res.get("complete") and len(res["lines"]) < (g.get("solver", {}).get("top") or 5)
        certain = True              # every gap starts at the start position or a photo
        for j, (u, s) in enumerate(zip(top["moves"], top["san"])):
            idx = b.ply()
            b.push(chess.Move.from_uci(u))
            marg = res["marginals"][j] if j < len(res["marginals"]) else []
            p = next((m["p"] for m in marg if m["uci"] == u), None)
            pinned = pins.get(idx) == u
            last = j == len(top["moves"]) - 1
            forced = exhaustive and p is not None and p >= 0.999
            certain = (certain and (pinned or forced)) or last
            out.append({"ply": idx + 1, "fen": b.fen(), "uci": u, "san": s,
                        "kind": "photo" if last else ("pinned" if pinned else "inferred"),
                        "photo": to["photo"] if last else None, "p": p,
                        "pinned": pinned, "certain": certain, "gap": gi,
                        "alts": marg, "break": False})
        if not top["moves"]:        # a zero-ply gap: a photo of the position already reached
            out[-1]["photo"] = to["photo"]
            if out[-1]["kind"] != "start":
                out[-1]["kind"] = "photo"
    return out


# --------------------------------------------------------------------------
# Stockfish: the graph and the engine arrows
# --------------------------------------------------------------------------

def _evaluate(gdir: Path, tl: list[dict]) -> dict:
    """Evaluate every timeline FEN not yet in ``evals.json``. Caller holds the lock."""
    cache = _read(gdir, "evals.json", {"depth": EVAL_DEPTH, "evals": {}, "top": {}})
    todo = [e["fen"] for e in tl if e["fen"] not in cache["evals"]]
    if todo:
        ev = evaluator()
        for fen in todo:
            cache["evals"][fen] = ev.evaluate(chess.Board(fen))
        _write(gdir, "evals.json", cache)
    return cache


def evals_for(gdir: Path, tl: list[dict]) -> list[dict | None]:
    cache = _read(gdir, "evals.json", {"evals": {}})["evals"]
    return [cache.get(e["fen"]) for e in tl]


def top_moves(gdir: Path, fen: str) -> list[dict]:
    """Stockfish's best moves at ``fen`` (cached per FEN)."""
    cache = _read(gdir, "evals.json", {"depth": EVAL_DEPTH, "evals": {}, "top": {}})
    cache.setdefault("top", {})
    if fen not in cache["top"]:
        with _engine_lock:
            cache["top"][fen] = evaluator().top_moves(fen, TOP_ENGINE)
            fresh = _read(gdir, "evals.json", {"depth": EVAL_DEPTH, "evals": {}, "top": {}})
            fresh.setdefault("top", {})[fen] = cache["top"][fen]
            _write(gdir, "evals.json", fresh)
    return cache["top"][fen]


# --------------------------------------------------------------------------
# PGN
# --------------------------------------------------------------------------

def write_pgn(gdir: Path, doc: dict | None = None) -> str:
    gdir = Path(gdir)
    doc = doc if doc is not None else _read(gdir, "lines.json", {"gaps": []})
    meta = _read(gdir, "meta.json", {})
    tl = timeline(doc, _pins_by_index(read_pins(gdir)))
    events = read_events(gdir)
    pgn = build_pgn(meta, events, tl)
    (gdir / "game.pgn").write_text(pgn.rstrip("\n") + "\n")
    return pgn


def photo_fens(positions: dict) -> list[dict]:
    """``[{"photo", "ply", "fen"}]`` of the confirmed photos, for copying."""
    return [{"photo": a["photo"], "ply": a["ply"], "fen": a["fen"]} for a in anchors(positions)]


# --------------------------------------------------------------------------
# Pins
# --------------------------------------------------------------------------

def add_pin(gdir: Path, ply: int, uci: str) -> list[dict]:
    """Accept ``uci`` as the move played at ``ply`` (1-based). The move must be
    legal on the timeline's board before that ply."""
    gdir = Path(gdir)
    doc = _read(gdir, "lines.json", {"gaps": []})
    tl = timeline(doc, _pins_by_index(read_pins(gdir)))
    before = next((e for e in tl if e["ply"] == ply - 1), None)
    if before is None:
        raise LookupError(f"ply {ply} is not inside a solved gap")
    if not any(e["kind"] == "photo" and e["ply"] >= ply for e in tl):
        raise LookupError(f"ply {ply} is after the last photo")
    board = chess.Board(before["fen"])
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise ValueError(f"bad move {uci!r}") from exc
    if move not in board.legal_moves:
        raise ValueError(f"{uci} is not legal at ply {ply}")
    pins = [p for p in read_pins(gdir) if int(p["ply"]) != ply]
    pins.append({"ply": ply, "uci": uci, "san": board.san(move), "t": time.time()})
    pins.sort(key=lambda p: p["ply"])
    _write(gdir, "pins.json", {"pins": pins})
    return pins


def drop_pin(gdir: Path, ply: int) -> list[dict]:
    pins = read_pins(gdir)
    kept = [p for p in pins if int(p["ply"]) != ply]
    if len(kept) == len(pins):
        raise LookupError(f"ply {ply} is not pinned")
    _write(Path(gdir), "pins.json", {"pins": kept})
    return kept


def shutdown() -> None:
    """Close Stockfish and lc0. Needed before exit: python-chess runs the engine
    on a non-daemon thread, which keeps the interpreter alive otherwise."""
    global _stockfish, _maia
    if _stockfish is not None:
        _stockfish.close()
        _stockfish = None
    if _maia is not None:
        _maia.close()
        _maia = None
    _movers.clear()
