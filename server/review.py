"""After (or during) the game: the review payload, pins, result and PGN.

Contract: docs/PROTOCOL.md §8. The moves between photos come from
``server.gaps``; this module only assembles what the review page renders and
applies the player's edits. Everything is synchronous; ``server.app`` runs it
in a worker thread.
"""

from __future__ import annotations

from . import gaps, storage

RESULTS = ("1-0", "0-1", "1/2-1/2", "*")


def _pgn_text(game_id: str) -> str | None:
    path = storage.game_dir(game_id) / "game.pgn"
    return path.read_text() if path.exists() else None


def payload(game_id: str, solving: bool = False) -> dict:
    """Everything the review page renders, in one object (PROTOCOL.md §8)."""
    meta = storage.read_meta(game_id)
    gdir = storage.game_dir(game_id)
    positions = storage.read_positions(game_id)
    doc = storage.read_json(game_id, "lines.json") or {"gaps": []}
    pins = gaps.read_pins(gdir)
    tl = gaps.timeline(doc, gaps._pins_by_index(pins))
    for e, ev in zip(tl, gaps.evals_for(gdir, tl)):
        e["eval"] = ev
    anchors = gaps.anchors(positions)
    solved_to = max((a["ply"] for a in anchors), default=0)
    stale = len(doc["gaps"]) != len(anchors) or any(
        g["to"]["photo"] != a["photo"] or g["to"]["fen"] != a["fen"]
        for g, a in zip(doc["gaps"], anchors))
    return {
        "game_id": game_id,
        "room": meta.get("room"),
        "status": meta.get("status", "ready"),
        "created_at": meta.get("created_at"),
        **{k: meta.get(k) for k in ("white_name", "black_name", "white_rating",
                                    "black_rating", "reader_model")},
        "result": meta.get("result"),
        "lichess_url": meta.get("lichess_url"),
        "plies": storage.count_plies(game_id),
        "last_photo_ply": solved_to,
        "solving": solving,
        "stale": stale,
        "positions": positions,
        "gaps": [_gap_summary(g) for g in doc["gaps"]],
        "timeline": tl,
        "pins": pins,
        "fens": gaps.photo_fens(positions),
        "pgn": _pgn_text(game_id),
    }


def _gap_summary(g: dict) -> dict:
    r = g["result"]
    return {"from": g["from"], "to": g["to"], "status": r.get("status"),
            "complete": r.get("complete"), "n_requested": r.get("n_requested"),
            "n_used": r.get("n_used"), "lines": len(r.get("lines") or []),
            "top_prob": (r.get("lines") or [{}])[0].get("prob"),
            "secs": r.get("secs"), "bands": g.get("bands"), "prior": g.get("prior"),
            "pins": len(g.get("pins") or [])}


def top_moves(game_id: str, ply: int) -> dict:
    """Stockfish's best moves at the timeline position of ``ply``."""
    gdir = storage.game_dir(game_id)
    doc = storage.read_json(game_id, "lines.json") or {"gaps": []}
    tl = gaps.timeline(doc, gaps._pins_by_index(gaps.read_pins(gdir)))
    e = next((x for x in tl if x["ply"] == ply), None)
    if e is None:
        raise LookupError(f"no known position at ply {ply}")
    return {"ply": ply, "fen": e["fen"], "moves": gaps.top_moves(gdir, e["fen"])}


def add_pin(game_id: str, ply: int, uci: str) -> list[dict]:
    return gaps.add_pin(storage.game_dir(game_id), ply, uci)


def drop_pin(game_id: str, ply: int) -> list[dict]:
    return gaps.drop_pin(storage.game_dir(game_id), ply)


def set_result(game_id: str, result: str) -> dict:
    if result not in RESULTS:
        raise ValueError(f"result must be one of {', '.join(RESULTS)}")
    storage.update_meta(game_id, result=result)
    pgn = gaps.write_pgn(storage.game_dir(game_id))
    return {"ok": True, "result": result, "pgn": pgn}


def ensure_pgn(game_id: str) -> str:
    """The PGN as it stands (written from lines.json; no solving)."""
    return gaps.write_pgn(storage.game_dir(game_id))
