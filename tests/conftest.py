"""Keep the tests that spend money out of a plain ``pytest`` run.

A ``live`` test calls the real Anthropic API. Marking it is not enough — pytest
runs marked tests by default — so a plain run would bill on every invocation.
Select them deliberately with ``pytest -m live``.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    if "live" in (config.getoption("-m") or ""):
        return
    skip_live = pytest.mark.skip(reason="live: real API calls — run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# --------------------------------------------------------------------------
# No lc0 or Stockfish in server tests: confirming a photo starts a background
# solve, so every test gets a uniform mover and a material-count evaluator.
# --------------------------------------------------------------------------

import chess  # noqa: E402

_VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300, chess.ROOK: 500,
           chess.QUEEN: 900, chess.KING: 0}


class UniformMover:
    """Every legal move equally likely, unless ``prefer`` names a move to weight."""

    def __init__(self, prefer: dict[str, float] | None = None):
        self.prefer = prefer or {}
        self.bands = {"white": 1600, "black": 1600}
        self.prior = {"white": False, "black": False}
        self.calls = 0

    def policy(self, board: chess.Board) -> dict[str, float]:
        self.calls += 1
        w = {m.uci(): self.prefer.get(m.uci(), 1.0) for m in board.legal_moves}
        z = sum(w.values())
        return {u: v / z for u, v in w.items()}

    def prefetch(self, boards) -> None:
        pass


class MaterialEval:
    def evaluate(self, board: chess.Board) -> dict:
        cp = sum(_VALUES[p.piece_type] * (1 if p.color else -1)
                 for p in board.piece_map().values())
        return {"cp": cp, "mate": None}

    def top_moves(self, fen: str, k: int = 3) -> list[dict]:
        b = chess.Board(fen)
        return [{"uci": m.uci(), "san": b.san(m), "cp": 0, "mate": None}
                for m in list(b.legal_moves)[:k]]


@pytest.fixture(autouse=True)
def _no_engines(monkeypatch):
    from server import gaps
    mover = UniformMover()
    monkeypatch.setattr(gaps, "MOVER", lambda meta: mover)
    monkeypatch.setattr(gaps, "EVALUATOR", lambda: MaterialEval())
    return mover
