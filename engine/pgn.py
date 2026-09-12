"""PGN, with the clock times the phone already recorded.

The clock sends its remaining times on every capturing event, so the PGN can
carry ``%clk`` tags without the engine having to time anything itself. The
association is by capture sequence: the ply read from capture ``seq`` gets the
time the clock reported in the event carrying that ``seq``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import chess
import chess.pgn


def read_events(game_dir: str | Path) -> list[dict]:
    path = Path(game_dir) / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _clk(ms: float | None) -> str | None:
    if ms is None:
        return None
    total = max(int(ms), 0) // 1000
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def build_pgn(result, game_dir: str | Path | None = None,
              events: list[dict] | None = None) -> str:
    """A complete PGN for a tracked game."""
    events = events if events is not None else (read_events(game_dir) if game_dir else [])
    cfg = next((e for e in events if e.get("type") == "clock.config"), {})
    stop = next((e for e in reversed(events) if e.get("type") == "clock.stop"), {})
    by_seq = {e["seq"]: e for e in events if "seq" in e}

    game = chess.pgn.Game()
    game.headers["Event"] = "BoardCam"
    created = next((e.get("server_t") or e.get("t") for e in events), None)
    game.headers["Date"] = (datetime.fromtimestamp(created / 1000).strftime("%Y.%m.%d")
                            if created else "????.??.??")
    game.headers["White"] = cfg.get("white_name") or "White"
    game.headers["Black"] = cfg.get("black_name") or "Black"
    game.headers["Result"] = stop.get("result") or "*"
    if cfg.get("initial_ms"):
        game.headers["TimeControl"] = (f"{int(cfg['initial_ms']) // 1000}"
                                       f"+{int(cfg.get('increment_ms', 0)) // 1000}")
    game.headers["Annotator"] = "BoardCam engine"

    node = game
    for ply in result.plies:
        move = chess.Move.from_uci(ply.uci)
        node = node.add_variation(move)
        ev = by_seq.get(ply.seq)
        if ev:
            side = ev.get("side")
            ms = ev.get("white_ms") if side == "white" else ev.get("black_ms")
            clk = _clk(ms)
            if clk:
                node.comment = f"[%clk {clk}]"
        if ply.flags:
            node.nags.add(chess.pgn.NAG_DUBIOUS_MOVE if "low_margin" in ply.flags
                          else chess.pgn.NAG_SPECULATIVE_MOVE)

    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    return game.accept(exporter)
