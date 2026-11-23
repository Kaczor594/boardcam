"""Photo intake: store a photo, read it into a position, take a player's fixes.

Contract: docs/PROTOCOL.md §5. Every function here is synchronous and runs in
a worker thread; the app awaits it. The reader is a module attribute so tests
can stub it (``photos.READER = fake``) without touching the network.
"""

from __future__ import annotations

import io
import logging
import time

from PIL import Image, ImageOps

from engine import reader as reader_mod
from engine.position import build_position, fullmove_of

from . import storage

# (image_bytes, model) -> BoardRead. Replaced in tests.
READER = lambda blob, model: reader_mod.read_position(blob, model=model)  # noqa: E731

log = logging.getLogger(__name__)

EMPTY_ROWS = ["........"] * 8


class PhotoError(ValueError):
    """A request the photo endpoints refuse; ``status`` is the HTTP code."""

    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

def as_jpeg(blob: bytes) -> bytes:
    """The upload as JPEG bytes. A JPEG is stored untouched (full resolution,
    EXIF kept); anything else Pillow can open is re-encoded upright."""
    if blob[:2] == b"\xff\xd8":
        return blob
    try:
        img = Image.open(io.BytesIO(blob))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:  # Pillow raises a zoo of types on junk
        raise PhotoError(415, "not a photo the server can open") from exc
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def add_photo(game_id: str, blob: bytes, *, ply: int, turn: str | None,
              white_ms: int | None, black_ms: int | None,
              retake_of: int | None = None) -> dict:
    """Store the photo and a ``reading`` entry stamped with the clock's state."""
    if ply < 0:
        raise PhotoError(400, "ply must be >= 0")
    blob = as_jpeg(blob)
    model = storage.read_meta(game_id).get("reader_model", reader_mod.DEFAULT_MODEL)

    def add(doc):
        photos = doc["photos"]
        n = max((p["n"] for p in photos), default=0) + 1
        if retake_of is not None:
            old = next((p for p in photos if p["n"] == retake_of), None)
            if old is None:
                raise PhotoError(404, f"no photo {retake_of} to retake")
            if old["status"] == "confirmed":
                raise PhotoError(409, f"photo {retake_of} is already confirmed")
            old["status"] = "replaced"
            old["replaced_by"] = n
        storage.write_photo(game_id, n, blob)
        entry = {
            "n": n,
            "file": f"photos/{n:03d}.jpg",
            "t": time.time(),
            "ply": ply,
            "turn": turn,
            "white_ms": white_ms,
            "black_ms": black_ms,
            "model": model,
            "status": "reading",
            "retake_of": retake_of,
            "read": None,
            "error": None,
            "position": None,
            "confirmed": None,
        }
        photos.append(entry)
        return entry

    return storage.update_positions(game_id, add)


def _entry(doc: dict, n: int) -> dict:
    for p in doc["photos"]:
        if p["n"] == n:
            return p
    raise PhotoError(404, f"no photo {n}")


def get_photo(game_id: str, n: int) -> dict:
    return _entry(storage.read_positions(game_id), n)


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

def read_photo(game_id: str, n: int) -> dict:
    """Run the reader on photo ``n`` and record the result. Never raises for a
    failed read: the failure is the entry's ``error``, which the phone shows."""
    entry = get_photo(game_id, n)
    if entry["status"] in ("confirmed", "replaced"):
        return entry
    blob = storage.photo_path(game_id, n).read_bytes()
    model = entry["model"]
    read = error = position = None
    try:
        got = READER(blob, model)
        read = got.to_json()
        position = build_position(got.rows, got.home, white_to_move=entry["ply"] % 2 == 0,
                                  fullmove=fullmove_of(entry["ply"])).to_json()
    except reader_mod.ReadError as exc:
        error = {"reason": exc.reason, "detail": exc.detail,
                 "retry": exc.reason in ("rate_limited", "connection") or exc.reason.startswith("api_")}
    except Exception as exc:  # a reader bug must not leave the photo "reading" forever
        error = {"reason": "server_error", "detail": f"{type(exc).__name__}: {exc}"[:300],
                 "retry": True}

    def record(doc):
        e = _entry(doc, n)
        if e["status"] in ("confirmed", "replaced"):
            return e        # the player moved on while the read was running
        e.update(read=read, error=error, position=position,
                 status="error" if error else "read")
        return e

    return storage.update_positions(game_id, record)


def mark_reading(game_id: str, n: int) -> dict:
    """Put a failed photo back to ``reading`` before a retry."""
    def mark(doc):
        e = _entry(doc, n)
        if e["status"] != "error":
            raise PhotoError(409, f"photo {n} is {e['status']}, not error")
        e.update(status="reading", error=None)
        return e
    return storage.update_positions(game_id, mark)


# --------------------------------------------------------------------------
# Confirm
# --------------------------------------------------------------------------

def confirm(game_id: str, n: int, rows: list[str], home: str, ply: int) -> dict:
    """Store the player's board for photo ``n`` beside the reader's original.

    The original read stays in ``read``; ``confirmed`` holds the corrected
    photo-frame rows and home side, which is the Phase 5 training label, plus
    the squares that changed. An illegal position is refused, never stored.
    """
    try:
        rows = reader_mod.check_rows(rows)
    except reader_mod.ReadError as exc:
        raise PhotoError(400, f"bad board: {exc}") from exc

    def apply(doc):
        e = _entry(doc, n)
        if e["status"] == "replaced":
            raise PhotoError(409, f"photo {n} was replaced by a retake")
        if abs(ply - e["ply"]) > 1:
            raise PhotoError(400, f"ply {ply} is more than one away from the clock's {e['ply']}")
        pos = build_position(rows, home, white_to_move=ply % 2 == 0,
                             fullmove=fullmove_of(ply))
        if not pos.legal:
            raise PhotoError(422, {"message": "that position is impossible",
                                   "flags": pos.flags, "fen": pos.fen})
        read_rows = e["read"]["rows"] if e["read"] else EMPTY_ROWS
        read_home = e["read"]["home"] if e["read"] else None
        corrections = [{"r": r, "c": c, "read": read_rows[r][c], "now": rows[r][c]}
                       for r in range(8) for c in range(8) if read_rows[r][c] != rows[r][c]]
        e["status"] = "confirmed"
        e["confirmed"] = {
            "rows": rows,
            "home": home,
            "ply": ply,
            "fen": pos.fen,
            "corrections": corrections,
            "home_changed": read_home != home,
            "ply_changed": ply != e["ply"],
            "t": time.time(),
        }
        return e

    entry = storage.update_positions(game_id, apply)
    _to_label_store(game_id, n)
    return entry


def _to_label_store(game_id: str, n: int) -> None:
    """Every confirmed photo is a training label for the local reader (spec
    Phase 5). A failure here must never fail the confirmation."""
    try:
        from engine import labels
        labels.add_from_game(storage.game_dir(game_id), n)
    except Exception:  # noqa: BLE001
        log.exception("label store: could not add %s photo %d", game_id, n)
