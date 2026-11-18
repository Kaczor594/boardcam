"""Solve a recorded game's gaps with the real engines and write its PGN.

    python -m scripts.replay_game data/games/<id>            # or data/real/<id>, or just <id>
    python -m scripts.replay_game <dir> --fresh               # ignore cached gaps
    python -m scripts.replay_game --demo game.pgn --photos 12,23,35 [--rating 1350]

The first form runs ``server.gaps.solve_game`` (lc0 + Maia, Stockfish) on a
game directory as the server would, prints one line per gap, writes
``lines.json``, ``evals.json`` and ``game.pgn`` into that directory, and checks
that the PGN parses. ``--demo`` builds a game directory under ``data/games/``
from a PGN instead: confirmed photo positions at the given plies (no images),
clock presses with made-up times, so the review page can be tried without a
real game. Its last photo ply is also the game's length.
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parent.parent


def _game_dir(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p.resolve()
    cand = ROOT / "data" / "games" / arg
    if cand.is_dir():
        return cand
    raise SystemExit(f"no game directory {arg}")


def demo(pgn_path: Path, photo_plies: list[int], rating: int) -> Path:
    game = chess.pgn.read_game(io.StringIO(pgn_path.read_text()))
    if game is None:
        raise SystemExit("no game in that PGN")
    moves = list(game.mainline_moves())
    last = max(photo_plies)
    if last > len(moves):
        raise SystemExit(f"the PGN has only {len(moves)} plies")
    gid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    gdir = ROOT / "data" / "games" / gid
    (gdir / "photos").mkdir(parents=True)
    now = time.time()
    meta = {"game_id": gid, "room": "DEMO", "created_at": now, "status": "finished",
            "result": game.headers.get("Result", "*"), "initial_ms": 600_000,
            "increment_ms": 5_000, "white_name": game.headers.get("White", "White"),
            "black_name": game.headers.get("Black", "Black"), "white_rating": rating,
            "black_rating": rating, "reader_model": "claude-fable-5-2"}
    (gdir / "meta.json").write_text(json.dumps(meta, indent=2))
    ms = {"white": 600_000, "black": 600_000}
    events = []
    board = chess.Board()
    fens = {0: board.fen()}
    for i, mv in enumerate(moves[:last], 1):
        side = "white" if i % 2 else "black"
        ms[side] = ms[side] - 9_000 + 5_000
        board.push(mv)
        fens[i] = board.fen()
        events.append({"type": "clock.start" if i == 1 else "clock.press", "seq": i,
                       "side": side, "white_ms": ms["white"], "black_ms": ms["black"],
                       "t": int(now * 1000) + i * 9000, "server_t": now + i * 9})
    events.append({"type": "clock.stop", "ply": last, "result": meta["result"],
                   "white_ms": ms["white"], "black_ms": ms["black"], "t": 0, "server_t": now})
    (gdir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    photos = []
    for n, ply in enumerate(sorted(photo_plies), 1):
        b = chess.Board(fens[ply])
        photos.append({"n": n, "file": f"photos/{n:03d}.jpg", "t": now, "ply": ply,
                       "turn": "white" if ply % 2 == 0 else "black", "white_ms": None,
                       "black_ms": None, "model": "demo", "status": "confirmed",
                       "retake_of": None, "read": None, "error": None, "position": None,
                       "confirmed": {"rows": [], "home": "bottom", "ply": ply, "fen": b.fen(),
                                     "corrections": [], "home_changed": False,
                                     "ply_changed": False, "t": now}})
    (gdir / "positions.json").write_text(json.dumps({"game_id": gid, "photos": photos}, indent=1))
    (gdir / "truth.json").write_text(json.dumps({"moves": [m.uci() for m in moves[:last]]}))
    print(f"demo game {gid}: photos at plies {sorted(photo_plies)}")
    return gdir


def replay(gdir: Path, fresh: bool = False) -> int:
    from server import gaps

    if fresh:
        for name in ("lines.json", "evals.json"):
            (gdir / name).unlink(missing_ok=True)
    t0 = time.time()

    def progress(doc):
        g = doc["gaps"][-1]
        r = g["result"]
        top = r["lines"][0]["prob"] if r.get("lines") else None
        print(f"  gap ply {g['from']['ply']:>3} → {g['to']['ply']:>3} (photo {g['to']['photo']}): "
              f"{r['status']:<7} n_used {r.get('n_used')}  lines {len(r.get('lines') or [])}"
              f"  top {top if top is None else round(top, 2)}  {r.get('secs', 0):.1f} s", flush=True)

    doc = gaps.solve_game(gdir, on_gap=progress)
    print(f"{len(doc['gaps'])} gaps in {time.time() - t0:.1f} s")
    truth = gdir / "truth.json"
    if truth.exists():
        want = json.loads(truth.read_text())["moves"]
        tl = gaps.timeline(doc, gaps._pins_by_index(gaps.read_pins(gdir)))
        got = [e["uci"] for e in tl[1:]]
        same = sum(a == b for a, b in zip(got, want))
        print(f"truth: {same}/{len(want)} plies match the most likely line")
    pgn = (gdir / "game.pgn").read_text()
    g = chess.pgn.read_game(io.StringIO(pgn))
    if g is None or g.errors:
        print(f"PGN does not parse: {g.errors if g else 'empty'}")
        return 1
    print(f"wrote {gdir / 'game.pgn'} ({len(list(g.mainline_moves()))} plies)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("game", nargs="?", help="game directory or id")
    ap.add_argument("--fresh", action="store_true", help="drop cached gaps and evaluations")
    ap.add_argument("--demo", type=Path, help="build a demo game from this PGN")
    ap.add_argument("--photos", default="10,20,30", help="photo plies for --demo")
    ap.add_argument("--rating", type=int, default=1350)
    a = ap.parse_args(argv)
    if a.demo:
        gdir = demo(a.demo, [int(x) for x in a.photos.split(",")], a.rating)
    elif a.game:
        gdir = _game_dir(a.game)
    else:
        ap.error("give a game, or --demo")
    from server import gaps
    try:
        return replay(gdir, a.fresh)
    finally:
        gaps.shutdown()


if __name__ == "__main__":
    sys.exit(main())
