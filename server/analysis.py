"""Per-game engine state between the clock and the review page.

The per-press move reading that used to live here was removed with the
2026-11-16 photo-positions spec. Until Phase 2 wires photo intake and Phase 3
the line solver, a finished game has no move reading, and ``finish`` says so
rather than inventing one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

NOT_YET = ("Moves are no longer read from per-press captures; reading positions "
           "from hand-held photos arrives with the photo-positions spec.")


@dataclass
class GameAnalysis:
    """What the server holds in memory for one game."""

    game_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)
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


def finish(game_id: str) -> dict:
    """Called on stop or flag. There is nothing to read the moves with yet."""
    return {"ok": False, "error": NOT_YET}
