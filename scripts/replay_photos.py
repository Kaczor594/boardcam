"""Push photos through the real intake path, as the clock phone would.

    python -m scripts.replay_photos data/labels/green-mat/photos/2777.jpg --ply 24
    python -m scripts.replay_photos IMG_1.jpg IMG_2.jpg --ply 10 --ply 31

Each photo is uploaded to a throwaway game (temporary data dir) through the
FastAPI app — `POST /games/{id}/photos`, stamped with its ply — and the FEN the
server returns is printed. A photo inside a label set (`data/labels/<set>/`)
reads through that set's cache, `reads/<model>/photo/<name>.json`, so a photo
already read costs nothing; `--fresh` forces a paid read. When the set has
truth for the photo, the board is compared with it and a mismatch exits 1.

`data/labels/<set>/<name>.jpg` is accepted for `.../photos/<name>.jpg`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import chess

from engine import read_eval
from engine.reader import DEFAULT_MODEL, BoardRead, ReadError, read_position


def resolve(path: Path) -> Path:
    if path.exists():
        return path
    alt = path.parent / "photos" / path.name
    if alt.exists():
        return alt
    raise SystemExit(f"no such photo: {path}")


def label_set(photo: Path) -> Path | None:
    d = photo.parent.parent if photo.parent.name == "photos" else photo.parent
    return d if (d / "truth.json").exists() else None


def make_reader(photo: Path, fresh: bool):
    labels = label_set(photo)

    def read(blob: bytes, model: str) -> BoardRead:
        if labels is None:
            return read_position(blob, model=model)
        got = read_eval.read_one(labels, photo.stem, model, "photo", fresh, None)
        if "error" in got:
            raise ReadError(got["error"], got.get("detail", ""))
        return BoardRead(**got)
    return read


def truth_fen(photo: Path) -> str | None:
    labels = label_set(photo)
    if labels is None:
        return None
    truth, _ = read_eval.load_truth(labels, "all")
    e = truth.get(photo.stem)
    if e is None:
        return None
    board = chess.Board(None)
    for sq, sym in read_eval.truth_placement(e).items():
        board.set_piece_at(sq, chess.Piece.from_symbol(sym))
    return board.board_fen()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("photos", nargs="+", type=Path)
    ap.add_argument("--ply", type=int, action="append", required=True,
                    help="clock ply count at each photo (one per photo, or one for all)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--fresh", action="store_true", help="ignore cached reads (costs money)")
    args = ap.parse_args(argv)
    plies = args.ply * len(args.photos) if len(args.ply) == 1 else args.ply
    if len(plies) != len(args.photos):
        ap.error("give one --ply per photo, or a single --ply for all")

    photos_in = [resolve(p) for p in args.photos]
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOARDCAM_DATA"] = tmp
        from fastapi.testclient import TestClient

        from server import app as app_module
        from server import photos as photos_mod

        bad = 0
        with TestClient(app_module.app) as client:
            gid = client.post("/games", json={"reader_model": args.model}).json()["game_id"]
            for photo, ply in zip(photos_in, plies):
                photos_mod.READER = make_reader(photo, args.fresh)
                resp = client.post(f"/games/{gid}/photos",
                                   data={"ply": str(ply),
                                         "turn": "white" if ply % 2 == 0 else "black"},
                                   files={"file": (photo.name, photo.read_bytes(), "image/jpeg")})
                if resp.status_code != 200:
                    print(f"{photo.name}: HTTP {resp.status_code} {resp.text}")
                    bad += 1
                    continue
                e = resp.json()
                if e["status"] == "error":
                    print(f"{photo.name}: read failed: {e['error']['reason']} {e['error']['detail']}")
                    bad += 1
                    continue
                pos = e["position"]
                print(pos["fen"])
                note = f"  home {pos['home']}{' (guessed)' if pos['home_guessed'] else ''}"
                if pos["flags"]:
                    note += f", flags {', '.join(pos['flags'])}"
                want = truth_fen(photo)
                if want is not None:
                    ok = pos["fen"].split()[0] == want
                    note += ", matches the label" if ok else f", MISMATCH: label is {want}"
                    bad += not ok
                print(f"{photo.name} ply {ply}:{note}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
