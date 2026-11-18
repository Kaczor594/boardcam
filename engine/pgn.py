"""PGN of a photographed game: inferred moves marked, clock times on every ply.

Input is the game's *timeline* (``server.gaps.timeline``): one entry per ply
along the most likely line between the photos. Every move gets ``[%clk]`` from
the clock press with the same ply number (the press count is the ply count);
a move the solver inferred carries ``inferred NN%`` (its marginal probability);
a move the player accepted on the review page carries nothing extra; the
position of every photo is given as a FEN comment on the move that reaches it.

A gap the solver could not fill ends the move list: lichess only imports legal
games. The remaining photo positions follow as a comment, and the review page
offers them as a FEN list.
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


def headers_for(meta: dict, events: list[dict], result: str | None = None) -> dict:
    stop = next((e for e in reversed(events) if e.get("type") == "clock.stop"), {})
    # server_t is Unix seconds, the clock's own t is milliseconds.
    created = next((e["server_t"] if e.get("server_t") else (e.get("t") or 0) / 1000
                    for e in events if e.get("server_t") or e.get("t")), None)
    created = created or meta.get("created_at")
    h = {
        "Event": "BoardCam",
        "Date": datetime.fromtimestamp(created).strftime("%Y.%m.%d") if created else "????.??.??",
        "White": meta.get("white_name") or "White",
        "Black": meta.get("black_name") or "Black",
        "Result": result or meta.get("result") or stop.get("result") or "*",
    }
    if meta.get("initial_ms"):
        h["TimeControl"] = (f"{int(meta['initial_ms']) // 1000}"
                            f"+{int(meta.get('increment_ms', 0)) // 1000}")
    if meta.get("white_rating"):
        h["WhiteElo"] = str(meta["white_rating"])
    if meta.get("black_rating"):
        h["BlackElo"] = str(meta["black_rating"])
    h["Annotator"] = "BoardCam (moves between photos are inferred)"
    return h


def build_pgn(meta: dict, events: list[dict], timeline: list[dict],
              result: str | None = None) -> str:
    """A PGN lichess can import, from the timeline of a photographed game."""
    game = chess.pgn.Game()
    game.headers.update(headers_for(meta, events, result))
    by_seq = {e["seq"]: e for e in events
              if e.get("type") in ("clock.start", "clock.press") and isinstance(e.get("seq"), int)}

    node = game
    board = chess.Board()
    rest: list[dict] = []
    for i, e in enumerate(timeline[1:], 1):
        if e.get("break") or e.get("uci") is None:
            rest = [x for x in timeline[i:] if x.get("kind") == "photo"]
            break
        move = chess.Move.from_uci(e["uci"])
        if move not in board.legal_moves:      # never expected: lines are legal by construction
            rest = [x for x in timeline[i:] if x.get("kind") == "photo"]
            break
        board.push(move)
        node = node.add_variation(move)
        parts = []
        ev = by_seq.get(e["ply"])
        if ev:
            ms = ev.get("white_ms") if ev.get("side") == "white" else ev.get("black_ms")
            clk = _clk(ms)
            if clk:
                parts.append(f"[%clk {clk}]")
        if e.get("kind") == "inferred":
            p = e.get("p")
            # a share among the lines found, never certainty
            parts.append("inferred " + ("?" if p is None else ">99" if p >= 0.995 else
                                        str(round(100 * p))) + "%")
        if e.get("photo") is not None:
            parts.append(f"photo {e['photo']}: {e['fen']}")
        if parts:
            node.comment = " ".join(parts)

    if timeline and timeline[0].get("photo") is not None:
        game.comment = f"photo {timeline[0]['photo']}: {timeline[0]['fen']}"
    if rest:
        tail = "; ".join(f"photo {x['photo']} at ply {x['ply']}: {x['fen']}" for x in rest)
        note = f"The moves to the next photo could not be inferred. Later positions: {tail}"
        if node is game:
            game.comment = (game.comment + " " if game.comment else "") + note
        else:
            node.comment = (node.comment + " " if node.comment else "") + note

    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    return game.accept(exporter)
