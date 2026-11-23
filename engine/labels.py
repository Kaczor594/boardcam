"""Label store: ``data/labels/<set>/`` — photos, truth, corners, crops.

    python -m engine.labels --ingest-games            # backfill confirmed game photos
    python -m engine.labels --crops data/labels/green-mat

One set per physical board and piece set (``green-mat``, ``wood-board``).
``truth.json`` maps a photo name to::

    {"session": "2026-11-21",        # capture date; splits are by session, never by square
     "split": "pilot|holdout|games|staged|diagonal|upload|unlabelled",
     "rows": [8 x 8] | null,         # photo frame (engine.reader), with
     "home": "top|bottom|left|right" | null,
     "fen": placement | null,        # board frame; hold-out labels were entered this way
     "done": bool, "excluded": reason | null,
     "corners": [[x, y] x 4] | null, # outer corners of a8, h8, h1, a1 (upright full-res px)
     "source": "pilot|holdout|game:<id>/<n>|upload",
     "checked": bool}                # looked at on the label page (game photos arrive unchecked)

Truth is either ``rows`` + ``home`` or ``fen``; the label page writes both. A
game photo arrives as the player confirmed it during play — which is not ground
truth (the 2026-11-21 field test confirmed a misread) — so it carries
``checked: false`` until it has been looked at on the label page.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import threading
from pathlib import Path

import chess

from .position import square_of

SET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
DEFAULT_SET = "green-mat"
_lock = threading.Lock()


def labels_root() -> Path:
    return Path(os.environ.get("BOARDCAM_DATA", "data")).resolve() / "labels"


def set_dir(name: str) -> Path:
    if not SET_RE.match(name):
        raise ValueError(f"bad label set name {name!r}")
    return labels_root() / name


def load(d: Path) -> dict:
    p = d / "truth.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save(d: Path, truth: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "truth.json.tmp"
    tmp.write_text(json.dumps(truth, indent=1, sort_keys=True))
    tmp.replace(d / "truth.json")


def update(d: Path, fn):
    """Read-modify-write ``truth.json`` under a lock; ``fn(truth)`` returns the result."""
    with _lock:
        truth = load(d)
        out = fn(truth)
        save(d, truth)
        return out


def list_sets() -> list[str]:
    root = labels_root()
    return sorted(p.name for p in root.iterdir() if (p / "truth.json").exists()) if root.exists() else []


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

def rows_from_fen(fen: str, home: str) -> list[str]:
    """Photo-frame rows of a board-frame placement for a White home side."""
    board = chess.Board(None)
    board.set_board_fen(fen.split()[0])
    rows = []
    for r in range(8):
        row = ""
        for c in range(8):
            p = board.piece_at(square_of(r, c, home))
            row += p.symbol() if p else "."
        rows.append(row)
    return rows


def fen_from_rows(rows: list[str], home: str) -> str:
    board = chess.Board(None)
    for r in range(8):
        for c in range(8):
            ch = rows[r][c]
            if ch != ".":
                board.set_piece_at(square_of(r, c, home), chess.Piece.from_symbol(ch))
    return board.board_fen()


def placement(e: dict) -> str | None:
    """Board-frame placement of an entry's truth, or None if unlabelled."""
    if e.get("fen"):
        return e["fen"].split()[0]
    if e.get("rows") and e.get("home") in ("top", "bottom", "left", "right"):
        return fen_from_rows(e["rows"], e["home"])
    return None


def cached_home(d: Path, name: str) -> str | None:
    """White's home side from any cached reader read of this photo."""
    for p in sorted((d / "reads").glob(f"*/photo/{name}.json")):
        try:
            h = json.loads(p.read_text()).get("home")
        except (OSError, ValueError):
            continue
        if h in ("top", "bottom", "left", "right"):
            return h
    return None


def board_corner_order(home: str) -> list[int]:
    """Indices into image corners [TL, TR, BR, BL] (photo frame) giving a8, h8, h1, a1."""
    cell = {0: (0, 0), 1: (0, 7), 2: (7, 7), 3: (7, 0)}     # photo corner -> its cell
    want = [chess.A8, chess.H8, chess.H1, chess.A1]
    by_square = {square_of(r, c, home): k for k, (r, c) in cell.items()}
    return [by_square[w] for w in want]


# --------------------------------------------------------------------------
# Adding photos
# --------------------------------------------------------------------------

def _place_photo(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def add_from_game(game_dir: Path, n: int, board_set: str | None = None) -> str | None:
    """Copy a confirmed game photo into its board's label set. Returns the name.

    Re-confirming updates the truth unless the label page has checked it since:
    a checked label is the better one.
    """
    pos = json.loads((game_dir / "positions.json").read_text())
    e = next((p for p in pos["photos"] if p["n"] == n), None)
    if not e or e.get("status") != "confirmed" or not e.get("confirmed"):
        return None
    meta_p = game_dir / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    if meta.get("room") == "DEMO":
        return None
    gid = game_dir.name
    d = set_dir(board_set or meta.get("board_set") or DEFAULT_SET)
    name = f"g{gid}-{n:03d}"
    _place_photo(game_dir / e["file"], d / "photos" / f"{name}.jpg")
    c = e["confirmed"]
    session = f"{gid[:4]}-{gid[4:6]}-{gid[6:8]}"

    def apply(truth):
        old = truth.get(name, {})
        if old.get("checked"):
            return name
        truth[name] = {
            "session": session, "split": "games", "rows": c["rows"], "home": c["home"],
            "fen": fen_from_rows(c["rows"], c["home"]), "done": True, "excluded": None,
            "corners": old.get("corners"), "source": f"game:{gid}/{n}", "checked": False,
        }
        return name

    return update(d, apply)


def ingest_games(games_root: Path) -> list[str]:
    out = []
    for g in sorted(p for p in games_root.iterdir() if (p / "positions.json").exists()):
        pos = json.loads((g / "positions.json").read_text())
        for p in pos["photos"]:
            if p.get("status") == "confirmed":
                name = add_from_game(g, p["n"])
                if name:
                    out.append(name)
    return out


def add_upload(d: Path, jpeg: bytes, *, split: str, session: str, stem: str) -> str:
    """A photo added on the label page (staged or diagonal shots)."""
    def apply(truth):
        k = 1
        while f"{stem}-{k:02d}" in truth:
            k += 1
        name = f"{stem}-{k:02d}"
        (d / "photos").mkdir(parents=True, exist_ok=True)
        (d / "photos" / f"{name}.jpg").write_bytes(jpeg)
        truth[name] = {"session": session, "split": split, "rows": None, "home": None,
                       "fen": None, "done": False, "excluded": None, "corners": None,
                       "source": "upload", "checked": False}
        return name
    return update(d, apply)


# --------------------------------------------------------------------------
# Crops
# --------------------------------------------------------------------------

def write_crops(d: Path) -> int:
    """``crops/<name>.npz`` (crops uint8 [64, H, W, 3], squares, labels) for every
    labelled photo with corner truth, plus ``crops/index.json``."""
    import numpy as np

    from .gridfind import square_crops

    truth = load(d)
    out = d / "crops"
    out.mkdir(parents=True, exist_ok=True)
    index = {}
    for name, e in sorted(truth.items()):
        fen = placement(e)
        if not (e.get("done") and not e.get("excluded") and fen and e.get("corners")):
            continue
        board = chess.Board(None)
        board.set_board_fen(fen)
        crops = square_crops(d / "photos" / f"{name}.jpg", e["corners"])
        squares = sorted(crops)
        labels = []
        for sq in squares:
            p = board.piece_at(chess.parse_square(sq))
            labels.append(p.symbol() if p else ".")
        np.savez_compressed(out / f"{name}.npz", crops=np.stack([crops[s] for s in squares]),
                            squares=np.array(squares), labels=np.array(labels))
        index[name] = {"session": e.get("session"), "split": e.get("split")}
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return len(index)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ingest-games", action="store_true")
    ap.add_argument("--crops", type=Path)
    a = ap.parse_args(argv)
    if a.ingest_games:
        root = Path(os.environ.get("BOARDCAM_DATA", "data")).resolve() / "games"
        names = ingest_games(root)
        print(f"{len(names)} confirmed game photos in the label store")
        return 0
    if a.crops:
        print(f"wrote crops for {write_crops(a.crops)} photos")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
