"""Label store and label page API (photo-positions spec, Phase 5).

A confirmed game photo lands in its board's label set on its own; the label
page drafts, corrects, stores corners in board order, and takes uploads.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from engine import labels as L
from engine import train_local
from engine.reader import BoardRead

from test_intake import TRUE_ROWS, StubReader, jpeg


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDCAM_DATA", str(tmp_path))
    from server import app as app_module
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def stub(monkeypatch):
    from server import photos
    s = StubReader()
    monkeypatch.setattr(photos, "READER", s)
    return s


def new_game(client) -> str:
    return client.post("/games", json={}).json()["game_id"]


def confirm_one(client, gid) -> dict:
    r = client.post(f"/games/{gid}/photos", files={"file": ("p.jpg", jpeg(), "image/jpeg")},
                    data={"ply": 6, "turn": "white"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/games/{gid}/photos/1/confirm",
                    json={"rows": TRUE_ROWS, "home": "top", "ply": 6})
    assert r.status_code == 200, r.text
    return r.json()


def test_confirmed_game_photo_flows_into_the_label_store(client, stub, tmp_path):
    gid = new_game(client)
    confirm_one(client, gid)
    truth = json.loads((tmp_path / "labels" / "green-mat" / "truth.json").read_text())
    name = f"g{gid}-001"
    e = truth[name]
    assert e["rows"] == TRUE_ROWS and e["home"] == "top" and e["done"] and not e["checked"]
    assert e["split"] == "games" and e["source"] == f"game:{gid}/1"
    assert e["session"] == f"{gid[:4]}-{gid[4:6]}-{gid[6:8]}"
    assert (tmp_path / "labels" / "green-mat" / "photos" / f"{name}.jpg").exists()
    # The label's FEN is the confirmed position's placement.
    pos = client.get(f"/api/games/{gid}/positions").json()["photos"][0]
    assert e["fen"] == pos["confirmed"]["fen"].split()[0]


def test_a_checked_label_is_not_overwritten_by_a_later_confirm(client, stub, tmp_path):
    gid = new_game(client)
    confirm_one(client, gid)
    name = f"g{gid}-001"
    r = client.put(f"/api/labels/green-mat/{name}", json={"rows": TRUE_ROWS, "home": "top", "done": True})
    assert r.status_code == 200 and r.json()["checked"]
    client.post(f"/api/games/{gid}/photos/1/confirm", json={"rows": TRUE_ROWS, "home": "top", "ply": 7})
    e = L.load(tmp_path / "labels" / "green-mat")[name]
    assert e["checked"] and e["rows"] == TRUE_ROWS


def test_label_api_saves_board_and_corners(client, stub, tmp_path):
    gid = new_game(client)
    confirm_one(client, gid)
    name = f"g{gid}-001"
    sets = client.get("/api/labels").json()["sets"]
    assert sets == [{"set": "green-mat", "photos": 1, "labelled": 1, "corners": 0, "unchecked": 1}]
    one = client.get(f"/api/labels/green-mat/{name}").json()
    assert one["rows"] == TRUE_ROWS and one["size"] == [64, 48]
    img = client.get(f"/labels/green-mat/photos/{name}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    corners = [[10, 5], [50, 6], [55, 40], [8, 42]]
    r = client.put(f"/api/labels/green-mat/{name}",
                   json={"rows": TRUE_ROWS, "home": "top", "corners": corners, "done": True})
    assert r.status_code == 200
    e = L.load(tmp_path / "labels" / "green-mat")[name]
    assert e["corners"] == [[float(x), float(y)] for x, y in corners] and e["checked"]

    assert client.put(f"/api/labels/green-mat/{name}",
                      json={"rows": ["...."], "home": "top", "done": True}).status_code == 400
    assert client.put(f"/api/labels/green-mat/{name}",
                      json={"rows": TRUE_ROWS, "home": "top", "corners": [[1, 2]]}).status_code == 400
    assert client.get("/api/labels/no-such-set").status_code == 404
    assert client.get("/api/labels/green-mat/..%2Fx").status_code == 404


def test_upload_then_draft_then_label(client, stub, tmp_path, monkeypatch):
    gid = new_game(client)
    confirm_one(client, gid)                      # creates the set
    buf = io.BytesIO()
    Image.new("RGB", (80, 60), (30, 110, 50)).save(buf, "PNG")
    r = client.post("/api/labels/green-mat/upload",
                    files=[("files", ("a.png", buf.getvalue(), "image/png")),
                           ("files", ("b.jpg", jpeg(), "image/jpeg"))],
                    data={"split": "staged"})
    assert r.status_code == 200, r.text
    added = r.json()["added"]
    assert len(added) == 2 and all(n.startswith("staged-") for n in added)

    from engine import read_eval

    def fake_read(image, model, grid_image=None, effort=None):
        return BoardRead(rows=TRUE_ROWS, home="top", backend="claude", model=model, cost=0.2)
    monkeypatch.setattr(read_eval, "read_position", fake_read)
    d = client.post(f"/api/labels/green-mat/{added[0]}/draft").json()
    assert d["rows"] == TRUE_ROWS and d["home"] == "top"
    # Cached like read_eval's reads, so a re-draft costs nothing.
    assert (tmp_path / "labels" / "green-mat" / "reads" / "claude-fable-5-2" / "photo"
            / f"{added[0]}.json").exists()

    client.put(f"/api/labels/green-mat/{added[0]}", json={"rows": TRUE_ROWS, "home": "top", "done": True})
    inv = train_local.inventory(tmp_path / "labels" / "green-mat")
    assert inv["labelled"] == 2 and inv["splits"] == {"games": 1, "staged": 1}
    assert sum(inv["classes"].values()) == 128


def test_rows_fen_round_trip_for_every_home():
    fen = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R"
    for home in ("bottom", "top", "left", "right"):
        assert L.fen_from_rows(L.rows_from_fen(fen, home), home) == fen
