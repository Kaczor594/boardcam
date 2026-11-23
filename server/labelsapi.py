"""Label page API (PROTOCOL.md §10): browse a label set, draft a board with the
reader, correct it, tap the four corners, add photos from the phone.

Corner truth is entered by hand and never pre-filled from ``engine.gridfind``,
so the grid finder's evaluation is not anchored on its own output.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps
from pydantic import BaseModel

from engine import labels as L
from engine import read_eval
from engine.reader import DEFAULT_MODEL, ReadError, check_rows, load_image

from . import photos

router = APIRouter()
VIEW_EDGE = 2000           # long edge of the photo the page shows


def _set(name: str):
    try:
        d = L.set_dir(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not (d / "truth.json").exists():
        raise HTTPException(404, f"no label set {name}")
    return d


def _entry(d, name: str) -> dict:
    if not L.NAME_RE.match(name):
        raise HTTPException(404, "bad photo name")
    e = L.load(d).get(name)
    if e is None:
        raise HTTPException(404, f"no photo {name}")
    return e


def _size(path) -> list[int]:
    with Image.open(path) as im:
        w, h = ImageOps.exif_transpose(im).size
    return [w, h]


@router.get("/api/labels")
async def api_sets():
    out = []
    for s in L.list_sets():
        t = L.load(L.set_dir(s))
        out.append({"set": s, "photos": len(t),
                    "labelled": sum(bool(e.get("done")) and not e.get("excluded") for e in t.values()),
                    "corners": sum(bool(e.get("corners")) for e in t.values()),
                    "unchecked": sum(e.get("source", "").startswith("game:") and not e.get("checked")
                                     for e in t.values())})
    return {"sets": out}


@router.get("/api/labels/{set_name}")
async def api_set(set_name: str):
    d = _set(set_name)
    t = L.load(d)
    return {"set": set_name, "photos": [
        {"name": n, "split": e.get("split"), "session": e.get("session"),
         "done": bool(e.get("done")), "excluded": e.get("excluded"),
         "source": e.get("source"), "checked": bool(e.get("checked")),
         "corners": bool(e.get("corners"))}
        for n, e in sorted(t.items(), key=lambda kv: (kv[1].get("session") or "", kv[0]))]}


@router.get("/api/labels/{set_name}/{name}")
async def api_photo(set_name: str, name: str):
    d = _set(set_name)
    e = _entry(d, name)
    home = e.get("home") or L.cached_home(d, name)
    rows = e.get("rows")
    if not rows and e.get("fen"):
        home = home or "bottom"
        rows = L.rows_from_fen(e["fen"], home)
    return {"name": name, **e, "rows": rows, "home": home,
            "size": _size(d / "photos" / f"{name}.jpg"), "view_edge": VIEW_EDGE}


@router.get("/labels/{set_name}/photos/{name}")
async def api_photo_image(set_name: str, name: str):
    """Upright, downscaled, EXIF-free: tap coordinates on the page are in this frame."""
    d = _set(set_name)
    _entry(d, name)

    def render() -> bytes:
        img = load_image(d / "photos" / f"{name}.jpg")
        img.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue()

    return Response(await asyncio.to_thread(render), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


@router.post("/api/labels/{set_name}/{name}/draft")
async def api_draft(set_name: str, name: str):
    """The reader's board for this photo (cached per model, like read_eval)."""
    d = _set(set_name)
    _entry(d, name)
    out = await asyncio.to_thread(read_eval.read_one, d, name, DEFAULT_MODEL, "photo", False, None)
    if "error" in out:
        raise HTTPException(502, f"reader: {out['error']} {out.get('detail', '')}".strip())
    return {"rows": out["rows"], "home": out["home"], "model": out["model"],
            "cost": out.get("cost", 0.0)}


class LabelBody(BaseModel):
    rows: list[str] | None = None
    home: Literal["top", "bottom", "left", "right"] | None = None
    corners: list[list[float]] | None = None     # a8, h8, h1, a1; full-res upright px
    done: bool = False
    excluded: str | None = None


@router.put("/api/labels/{set_name}/{name}")
async def api_save(set_name: str, name: str, body: LabelBody):
    d = _set(set_name)
    _entry(d, name)
    if body.rows is not None:
        try:
            check_rows(body.rows)
        except ReadError as exc:
            raise HTTPException(400, f"bad board: {exc}") from exc
        if body.home is None:
            raise HTTPException(400, "a board needs White's home side")
    if body.done and body.rows is None and not body.excluded:
        raise HTTPException(400, "a done label needs a board")
    if body.corners is not None and (len(body.corners) != 4 or any(len(p) != 2 for p in body.corners)):
        raise HTTPException(400, "corners are four [x, y] points: a8, h8, h1, a1")

    def apply(truth):
        e = truth[name]
        if body.rows is not None:
            e["rows"], e["home"] = body.rows, body.home
            e["fen"] = L.fen_from_rows(body.rows, body.home)
        if body.corners is not None:
            e["corners"] = [[round(x, 1), round(y, 1)] for x, y in body.corners]
        e["done"] = body.done
        e["excluded"] = body.excluded or None
        e["checked"] = True
        if e.get("split") == "unlabelled" and body.done:
            e["split"] = "upload"
        return e

    return L.update(d, apply)


def _session_of(blob: bytes) -> str:
    try:
        with Image.open(io.BytesIO(blob)) as im:
            raw = im.getexif().get_ifd(0x8769).get(36867) or im.getexif().get(306)
        if raw:
            return dt.datetime.strptime(str(raw)[:10], "%Y:%m:%d").date().isoformat()
    except (OSError, ValueError, AttributeError):
        pass
    return dt.date.today().isoformat()


@router.post("/api/labels/{set_name}/upload")
async def api_upload(set_name: str, files: list[UploadFile] = File(...),
                     split: Literal["staged", "diagonal", "upload"] = Form("staged")):
    d = _set(set_name)
    names = []
    for f in files:
        blob = await f.read()
        try:
            jpeg = photos.as_jpeg(blob)
        except photos.PhotoError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        session = _session_of(blob)
        names.append(L.add_upload(d, jpeg, split=split, session=session,
                                  stem=f"{split}-{session.replace('-', '')}"))
    return {"added": names}
