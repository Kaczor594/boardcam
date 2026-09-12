"""Score the tracker against a corpus of games whose moves are known.

Run it on a synthetic profile or on ``data/real``; the directory layout is the
same either way, so the numbers are comparable. Four things matter, in order:

``plies correct``
    index-wise agreement with ``truth.pgn``. Harsh on purpose — a desynchronised
    game is a wrong game, not a partially right one.
``final position``
    whether the game ends on the right board. A PGN that is right at the end is
    usually right everywhere.
``wrong-ply recall``
    of the plies the tracker got wrong, the share it *flagged*. This is the one
    that decides whether the vision-LLM fallback and the review page can save
    the game, and a low number here is worse than a low accuracy.
``flagged per game``
    what recall costs. Every flagged ply is an LLM call or a human glance.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import chess
import chess.pgn


def truth_moves(game_dir: Path) -> list[str]:
    with open(game_dir / "truth.pgn") as fh:
        game = chess.pgn.read_game(fh)
    return [m.uci() for m in game.mainline_moves()]


def game_dirs(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "truth.pgn").exists())


def score_game(args) -> dict:
    game_dir, params_path, use_llm = args
    from engine.features import load_params
    from engine.tracker import track_game
    game_dir = Path(game_dir)
    params = load_params(params_path)
    params["use_llm"] = use_llm
    t0 = time.time()
    try:
        res = track_game(game_dir, params)
    except Exception as exc:                       # a crashed game is a failed game
        return {"game": game_dir.name, "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=3), "plies": 0, "correct": 0,
                "truth_plies": len(truth_moves(game_dir)), "final_ok": False,
                "wrong": 0, "wrong_flagged": 0, "flagged": 0, "frames": 0,
                "seconds": time.time() - t0}

    truth = truth_moves(game_dir)
    pred = [m.uci() for m in res.moves]
    correct = sum(1 for i in range(min(len(truth), len(pred))) if truth[i] == pred[i])

    board = chess.Board()
    for u in truth:
        board.push(chess.Move.from_uci(u))
    final_ok = board.board_fen() == res.board.board_fen()

    wrong = wrong_flagged = 0
    for i in range(len(truth)):
        bad = i >= len(pred) or pred[i] != truth[i]
        if not bad:
            continue
        wrong += 1
        if i < len(res.plies) and res.plies[i].flags:
            wrong_flagged += 1
    llm_cost = sum(f.get("llm_cost", 0.0) for f in res.frames)
    return {"game": game_dir.name, "plies": len(pred), "truth_plies": len(truth),
            "correct": correct, "final_ok": final_ok, "wrong": wrong,
            "wrong_flagged": wrong_flagged, "flagged": len(res.flagged),
            "frames": len(res.frames), "seconds": time.time() - t0,
            "llm_cost": llm_cost, "warnings": res.warnings}


def summarise(rows: list[dict]) -> dict:
    truth = sum(r["truth_plies"] for r in rows) or 1
    frames = sum(r["frames"] for r in rows) or 1
    wrong = sum(r["wrong"] for r in rows)
    return {
        "games": len(rows),
        "plies_correct": 100.0 * sum(r["correct"] for r in rows) / truth,
        "final_correct": 100.0 * sum(1 for r in rows if r["final_ok"]) / max(len(rows), 1),
        "wrong_recall": (100.0 * sum(r["wrong_flagged"] for r in rows) / wrong
                         if wrong else 100.0),
        "wrong_plies": wrong,
        "flagged_per_game": sum(r["flagged"] for r in rows) / max(len(rows), 1),
        "sec_per_frame": sum(r["seconds"] for r in rows) / frames,
        "llm_cost_per_game": sum(r.get("llm_cost", 0.0) for r in rows) / max(len(rows), 1),
        "errors": sum(1 for r in rows if r.get("error")),
    }


def print_table(name: str, s: dict) -> None:
    print(f"\n{name}  ({s['games']} games)")
    print(f"  plies correct        {s['plies_correct']:6.2f} %")
    print(f"  final position ok    {s['final_correct']:6.2f} % of games")
    print(f"  wrong-ply recall     {s['wrong_recall']:6.2f} %  ({s['wrong_plies']} wrong plies)")
    print(f"  flagged per game     {s['flagged_per_game']:6.2f}")
    print(f"  seconds per frame    {s['sec_per_frame']:6.3f}")
    if s["llm_cost_per_game"]:
        print(f"  LLM cost per game    ${s['llm_cost_per_game']:.4f}")
    if s["errors"]:
        print(f"  CRASHED GAMES        {s['errors']}")


def evaluate(root: Path, params_path: str | None = None, use_llm: bool = True,
             workers: int | None = None, limit: int | None = None) -> tuple[dict, list[dict]]:
    dirs = game_dirs(root)
    if limit:
        dirs = dirs[:limit]
    if not dirs:
        raise SystemExit(f"no game directories with truth.pgn under {root}")
    jobs = [(str(d), params_path, use_llm) for d in dirs]
    workers = workers or min(os.cpu_count() or 4, 9)
    if workers <= 1:
        rows = [score_game(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(score_game, jobs))
    return summarise(rows), rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir", type=Path)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--params", default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", type=Path, default=None, help="write per-game rows here")
    ap.add_argument("--worst", type=int, default=0, help="list the N worst games")
    a = ap.parse_args(argv)

    summary, rows = evaluate(a.dir, a.params, not a.no_llm, a.workers, a.limit)
    print_table(str(a.dir), summary)
    if a.worst:
        bad = sorted(rows, key=lambda r: (r["correct"] - r["truth_plies"]))[:a.worst]
        print("\n  worst games:")
        for r in bad:
            tag = r.get("error", "")
            print(f"    {r['game']:38s} {r['correct']:3d}/{r['truth_plies']:3d}"
                  f"  flagged {r['flagged']:2d}  {tag}")
    if a.json:
        a.json.write_text(json.dumps({"summary": summary, "games": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
