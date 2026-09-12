"""Game directories on disk. Layout is docs/PROTOCOL.md §5."""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .models import GameConfig

# Unambiguous alphabet: no B/8, I/1, O/0, S/5, Z/2.
ROOM_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"
ROOM_LEN = 4

_GAME_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_lock = threading.Lock()


def data_root() -> Path:
    """Root of the data tree. Overridable via BOARDCAM_DATA for tests."""
    return Path(os.environ.get("BOARDCAM_DATA", "data")).resolve()


def games_root() -> Path:
    root = data_root() / "games"
    root.mkdir(parents=True, exist_ok=True)
    return root


def game_dir(game_id: str) -> Path:
    """Resolve a game directory, refusing anything that is not a valid id.

    The id comes off the wire, so pattern-matching it is what keeps
    `../` out of the path.
    """
    if not _GAME_ID_RE.match(game_id):
        raise ValueError(f"invalid game_id: {game_id!r}")
    return games_root() / game_id


def exists(game_id: str) -> bool:
    try:
        return game_dir(game_id).is_dir()
    except ValueError:
        return False


def _new_game_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _active_rooms() -> set[str]:
    rooms: set[str] = set()
    for meta_path in games_root().glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") != "finished":
            rooms.add(meta.get("room", ""))
    return rooms


def _new_room() -> str:
    taken = _active_rooms()
    for _ in range(200):
        room = "".join(random.choice(ROOM_ALPHABET) for _ in range(ROOM_LEN))
        if room not in taken:
            return room
    raise RuntimeError("could not allocate a free room code")


def create_game(config: GameConfig) -> dict:
    with _lock:
        game_id = _new_game_id()
        room = _new_room()
        d = games_root() / game_id
        (d / "frames").mkdir(parents=True, exist_ok=True)
        meta = {
            "game_id": game_id,
            "room": room,
            "created_at": time.time(),
            "status": "ready",
            "result": None,
            **config.model_dump(),
        }
        write_meta(game_id, meta)
        (d / "events.jsonl").touch()
        return meta


def read_meta(game_id: str) -> dict:
    return json.loads((game_dir(game_id) / "meta.json").read_text())


def write_meta(game_id: str, meta: dict) -> None:
    path = game_dir(game_id) / "meta.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(path)


def update_meta(game_id: str, **fields) -> dict:
    with _lock:
        meta = read_meta(game_id)
        meta.update(fields)
        write_meta(game_id, meta)
        return meta


def find_by_room(room: str) -> dict | None:
    """Newest non-finished game with this room code, else newest with it."""
    room = room.upper()
    best: dict | None = None
    for meta_path in games_root().glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("room") != room:
            continue
        if best is None:
            best = meta
            continue
        # Prefer a live game; among equals prefer the newer.
        best_live = best.get("status") != "finished"
        meta_live = meta.get("status") != "finished"
        if (meta_live, meta.get("created_at", 0)) > (best_live, best.get("created_at", 0)):
            best = meta
    return best


def append_event(game_id: str, event: dict) -> dict:
    """Append one event to events.jsonl, stamping server time."""
    event = {**event, "server_t": time.time()}
    with _lock:
        with (game_dir(game_id) / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
    return event


def read_events(game_id: str) -> list[dict]:
    path = game_dir(game_id) / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def frame_path(game_id: str, seq: int, k: int) -> Path:
    if not (0 <= seq <= 9999 and 0 <= k <= 9):
        raise ValueError(f"frame out of range: seq={seq} k={k}")
    return game_dir(game_id) / "frames" / f"{seq:04d}_{k}.jpg"


def write_frame(game_id: str, seq: int, k: int, blob: bytes) -> Path:
    path = frame_path(game_id, seq, k)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jpg.tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return path


def frame_inventory(game_id: str) -> list[dict]:
    """[{'seq': 1, 'k': [0,1,2]}, ...] sorted by seq."""
    by_seq: dict[int, list[int]] = {}
    for p in sorted((game_dir(game_id) / "frames").glob("[0-9]" * 4 + "_?.jpg")):
        seq_s, k_s = p.stem.split("_")
        by_seq.setdefault(int(seq_s), []).append(int(k_s))
    return [{"seq": s, "k": sorted(ks)} for s, ks in sorted(by_seq.items())]


def count_frames(game_id: str) -> int:
    """Number of distinct capture sequences with at least one frame."""
    return len(frame_inventory(game_id))


def count_plies(game_id: str) -> int:
    """Highest press sequence seen. Exact ply count is the engine's job."""
    best = 0
    for ev in read_events(game_id):
        if ev.get("type") in ("clock.start", "clock.press") and isinstance(ev.get("seq"), int):
            best = max(best, ev["seq"])
    return best


def max_seq(game_id: str) -> int:
    """Highest capture seq in the event log, over every capturing event."""
    best = 0
    for ev in read_events(game_id):
        seq = ev.get("seq")
        if isinstance(seq, int):
            best = max(best, seq)
    return best


def summarize(game_id: str) -> dict:
    meta = read_meta(game_id)
    return {
        "game_id": meta["game_id"],
        "room": meta["room"],
        "created_at": meta["created_at"],
        "status": meta.get("status", "ready"),
        "result": meta.get("result"),
        "frames": count_frames(game_id),
        "plies": count_plies(game_id),
        "white_name": meta.get("white_name", "White"),
        "black_name": meta.get("black_name", "Black"),
    }


def list_games() -> list[dict]:
    out = []
    for meta_path in games_root().glob("*/meta.json"):
        gid = meta_path.parent.name
        if not _GAME_ID_RE.match(gid):
            continue
        try:
            out.append(summarize(gid))
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    out.sort(key=lambda g: g["created_at"], reverse=True)
    return out


def delete_game(game_id: str) -> None:
    shutil.rmtree(game_dir(game_id))


def read_json(game_id: str, name: str) -> dict | None:
    path = game_dir(game_id) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
