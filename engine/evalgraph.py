"""Stockfish evaluations for the review page.

* :meth:`Stockfish.line` — the evaluation after every ply of a line (and of its
  start), White's point of view, for the evaluation graph.
* :meth:`Stockfish.top_moves` — Stockfish's best moves at one position, for the
  engine arrows.

Scores are centipawns from White's side; a forced mate is reported as ``mate``
(plies to mate, signed for White) with ``cp`` clamped to ±10 000 so a graph can
plot it.
"""

from __future__ import annotations

import argparse
import json
import shutil

import chess
import chess.engine

MATE_CP = 10_000


def _score(pov: chess.engine.PovScore) -> dict:
    s = pov.white()
    mate = s.mate()
    return {"cp": s.score(mate_score=MATE_CP), "mate": mate}


class Stockfish:
    def __init__(self, path: str | None = None, depth: int = 14, threads: int = 1,
                 hash_mb: int = 64):
        path = path or shutil.which("stockfish")
        if not path:
            raise FileNotFoundError("stockfish not found on PATH")
        self.depth = depth
        self.engine = chess.engine.SimpleEngine.popen_uci(path)
        self.engine.configure({"Threads": threads, "Hash": hash_mb})

    def evaluate(self, board: chess.Board, depth: int | None = None) -> dict:
        if board.is_game_over():
            out = board.outcome()
            cp = 0 if out.winner is None else (MATE_CP if out.winner else -MATE_CP)
            return {"cp": cp, "mate": None if out.winner is None else 0}
        info = self.engine.analyse(board, chess.engine.Limit(depth=depth or self.depth))
        return _score(info["score"])

    def line(self, fen: str, moves: list[str], depth: int | None = None) -> list[dict]:
        """``[{"ply", "fen", "cp", "mate"}]`` for the start and after every move;
        ``ply`` counts from the start of the game (the FEN's fullmove/turn)."""
        b = chess.Board(fen)
        ply0 = (b.fullmove_number - 1) * 2 + (0 if b.turn == chess.WHITE else 1)
        out = [{"ply": ply0, "fen": b.fen(), **self.evaluate(b, depth)}]
        for i, u in enumerate(moves, 1):
            b.push(chess.Move.from_uci(u))
            out.append({"ply": ply0 + i, "fen": b.fen(), **self.evaluate(b, depth)})
        return out

    def top_moves(self, fen: str, k: int = 3, depth: int | None = None) -> list[dict]:
        """Stockfish's ``k`` best moves at ``fen``: ``[{"uci", "san", "cp", "mate"}]``."""
        b = chess.Board(fen)
        if b.is_game_over():
            return []
        infos = self.engine.analyse(b, chess.engine.Limit(depth=depth or self.depth),
                                    multipv=k)
        out = []
        for info in infos:
            pv = info.get("pv")
            if not pv:
                continue
            out.append({"uci": pv[0].uci(), "san": b.san(pv[0]), **_score(info["score"])})
        return out

    def close(self) -> None:
        try:
            self.engine.quit()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fen")
    ap.add_argument("moves", nargs="*", help="UCI moves of a line")
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--top", type=int, default=0, help="also print the top-k moves at the start")
    a = ap.parse_args(argv)
    with Stockfish(depth=a.depth) as sf:
        print(json.dumps(sf.line(a.fen, a.moves)))
        if a.top:
            print(json.dumps(sf.top_moves(a.fen, a.top)))


if __name__ == "__main__":
    main()
