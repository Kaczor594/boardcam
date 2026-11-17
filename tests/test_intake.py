"""Photo intake with a stubbed reader (photo-positions spec, Phase 2 gate).

upload → stamped position → correction → confirmed FEN, plus the ways a photo
is refused or fails. No network: ``photos.READER`` is replaced per test.
"""

from __future__ import annotations

import io

import chess
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from engine.reader import BoardRead, ReadError

# The start position after 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 (ply 6), photographed
# from Black's side: White's home rank is at the top of the photo.
AFTER_6 = chess.Board()
for san in ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]:
    AFTER_6.push_san(san)


def rows_from_top(board: chess.Board) -> list[str]:
    """Photo-frame rows for a camera on Black's side (home = top)."""
    # engine.position.square_of(r, c, "top") is file 7 - c, rank r.
    return ["".join(str(board.piece_at(chess.square(7 - c, r)) or ".") for c in range(8))
            for r in range(8)]


TRUE_ROWS = rows_from_top(AFTER_6)


def misread(rows: list[str]) -> list[str]:
    """The reader's classic slip: one bishop read as a pawn."""
    out = [list(r) for r in rows]
    for r in range(8):
        for c in range(8):
            if out[r][c] == "B":
                out[r][c] = "P"
                return ["".join(x) for x in out]
    raise AssertionError("no white bishop to misread")


def jpeg(color=(40, 120, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 200, 200)).save(buf, "PNG")
    return buf.getvalue()


class StubReader:
    def __init__(self, rows=None, home="top", error: ReadError | None = None):
        self.rows = rows if rows is not None else TRUE_ROWS
        self.home = home
        self.error = error
        self.calls: list[tuple[int, str]] = []

    def __call__(self, blob: bytes, model: str) -> BoardRead:
        self.calls.append((len(blob), model))
        if self.error is not None:
            raise self.error
        return BoardRead(rows=self.rows, home=self.home, backend="claude", model=model,
                         cost=0.1, secs=1.0)


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


def new_game(client, **cfg) -> str:
    return client.post("/games", json=cfg or None).json()["game_id"]


def upload(client, gid, blob=None, ply=6, turn="white", **extra):
    data = {"ply": str(ply), "turn": turn, "white_ms": "581000", "black_ms": "590500",
            **{k: str(v) for k, v in extra.items()}}
    return client.post(f"/games/{gid}/photos", data=data,
                       files={"file": ("IMG_0001.jpg", blob or jpeg(), "image/jpeg")})


# ----------------------------------------------------------- the happy path

def test_upload_read_correct_confirm(client, stub):
    stub.rows = misread(TRUE_ROWS)
    gid = new_game(client, reader_model="claude-sonnet-5-5")

    resp = upload(client, gid, ply=6, turn="white")
    assert resp.status_code == 200, resp.text
    e = resp.json()
    assert e["n"] == 1 and e["file"] == "photos/001.jpg"
    assert e["status"] == "read"
    assert e["ply"] == 6 and e["turn"] == "white"
    assert (e["white_ms"], e["black_ms"]) == (581000, 590500)
    assert e["model"] == "claude-sonnet-5-5"
    assert stub.calls and stub.calls[0][1] == "claude-sonnet-5-5"
    assert e["read"]["rows"] == stub.rows and e["read"]["home"] == "top"
    # Ply 6 → White to move, move 4.
    assert e["position"]["fen"].split()[1:2] == ["w"]
    assert e["position"]["fen"].split()[5] == "4"

    from server import storage
    assert storage.photo_path(gid, 1).read_bytes()[:2] == b"\xff\xd8"
    assert client.get(f"/games/{gid}/photos/1").headers["content-type"] == "image/jpeg"

    # The player fixes the bishop and confirms.
    resp = client.post(f"/api/games/{gid}/photos/1/confirm",
                       json={"rows": TRUE_ROWS, "home": "top", "ply": 6})
    assert resp.status_code == 200, resp.text
    c = resp.json()
    assert c["status"] == "confirmed"
    assert c["confirmed"]["fen"].split()[0] == AFTER_6.board_fen()
    assert c["confirmed"]["fen"].split()[1] == "w"
    assert c["confirmed"]["corrections"] == [
        {"r": r, "c": col, "read": "P", "now": "B"}
        for r in range(8) for col in range(8)
        if stub.rows[r][col] != TRUE_ROWS[r][col]]
    assert len(c["confirmed"]["corrections"]) == 1
    assert c["confirmed"]["home_changed"] is False and c["confirmed"]["ply_changed"] is False
    # The original read is kept beside the correction: that is the label.
    assert c["read"]["rows"] == stub.rows

    doc = client.get(f"/api/games/{gid}/positions").json()
    assert [p["status"] for p in doc["photos"]] == ["confirmed"]
    assert client.get("/api/games").json()["games"][0]["confirmed"] == 1


def test_position_read_is_pushed_to_the_clock(client, stub):
    created = client.post("/games", json={}).json()
    with client.websocket_connect(f"/ws/{created['room']}?role=clock") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert upload(client, created["game_id"]).status_code == 200
        for _ in range(6):
            msg = ws.receive_json()
            if msg["type"] == "position.read":
                break
        else:
            raise AssertionError("no position.read push")
        assert msg["photo"]["n"] == 1 and msg["photo"]["status"] == "read"


# ------------------------------------------------------- ply and orientation

def test_ply_is_adjustable_by_one_and_flips_side_to_move(client, stub):
    gid = new_game(client)
    upload(client, gid, ply=6)
    # Photo taken after Black's 3...a6 but the clock showed ply 5: confirm at 6.
    before = client.post(f"/api/games/{gid}/photos/1/confirm",
                         json={"rows": TRUE_ROWS, "home": "top", "ply": 5})
    assert before.status_code == 200
    assert before.json()["confirmed"]["fen"].split()[1] == "b"
    assert before.json()["confirmed"]["ply_changed"] is True

    far = client.post(f"/api/games/{gid}/photos/1/confirm",
                      json={"rows": TRUE_ROWS, "home": "top", "ply": 8})
    assert far.status_code == 400


def test_rotating_the_board_is_recorded_as_a_home_change(client, stub):
    stub.home = "bottom"            # the reader got White's side wrong
    gid = new_game(client)
    e = upload(client, gid).json()
    assert e["position"]["fen"].split()[0] != AFTER_6.board_fen()
    c = client.post(f"/api/games/{gid}/photos/1/confirm",
                    json={"rows": TRUE_ROWS, "home": "top", "ply": 6}).json()
    assert c["confirmed"]["home_changed"] is True
    assert c["confirmed"]["corrections"] == []
    assert c["confirmed"]["fen"].split()[0] == AFTER_6.board_fen()


# ------------------------------------------------- nothing silently accepted

def test_an_impossible_position_is_refused_on_confirm(client, stub):
    gid = new_game(client)
    upload(client, gid)
    no_king = [r.replace("k", ".") for r in TRUE_ROWS]
    resp = client.post(f"/api/games/{gid}/photos/1/confirm",
                       json={"rows": no_king, "home": "top", "ply": 6})
    assert resp.status_code == 422
    assert "no_black_king" in resp.json()["detail"]["flags"]
    assert client.get(f"/api/games/{gid}/positions").json()["photos"][0]["status"] == "read"


def test_an_impossible_read_is_flagged_not_hidden(client, stub):
    stub.rows = [r.replace("k", ".") for r in TRUE_ROWS]
    gid = new_game(client)
    e = upload(client, gid).json()
    assert e["status"] == "read"
    assert e["position"]["legal"] is False
    assert "no_black_king" in e["position"]["flags"]


def test_a_failed_read_is_an_error_and_can_be_retaken(client, stub):
    from engine.reader import ReadError
    stub.error = ReadError("no_board", "stop_reason=end_turn")
    gid = new_game(client)
    e = upload(client, gid).json()
    assert e["status"] == "error" and e["error"]["reason"] == "no_board"
    assert e["error"]["retry"] is False

    stub.error = None
    retake = upload(client, gid, retake_of=1).json()
    assert retake["n"] == 2 and retake["status"] == "read" and retake["retake_of"] == 1
    photos_ = client.get(f"/api/games/{gid}/positions").json()["photos"]
    assert [p["status"] for p in photos_] == ["replaced", "read"]
    assert photos_[0]["replaced_by"] == 2
    # A replaced photo cannot be confirmed any more.
    assert client.post(f"/api/games/{gid}/photos/1/confirm",
                       json={"rows": TRUE_ROWS, "home": "top", "ply": 6}).status_code == 409


def test_a_transport_failure_can_be_retried_without_a_new_photo(client, stub):
    from engine.reader import ReadError
    stub.error = ReadError("rate_limited", "429")
    gid = new_game(client)
    e = upload(client, gid).json()
    assert e["status"] == "error" and e["error"]["retry"] is True

    stub.error = None
    again = client.post(f"/api/games/{gid}/photos/1/read")
    assert again.status_code == 200 and again.json()["status"] == "read"
    # Retrying something that did not fail is refused.
    assert client.post(f"/api/games/{gid}/photos/1/read").status_code == 409


def test_a_reader_crash_is_an_error_not_a_stuck_photo(client, monkeypatch):
    from server import photos

    def boom(blob, model):
        raise KeyError("oops")
    monkeypatch.setattr(photos, "READER", boom)
    gid = new_game(client)
    e = upload(client, gid).json()
    assert e["status"] == "error" and e["error"]["reason"] == "server_error"


def test_a_failed_read_can_still_be_entered_by_hand(client, stub):
    from engine.reader import ReadError
    stub.error = ReadError("max_tokens", "after 64000")
    gid = new_game(client)
    upload(client, gid)
    c = client.post(f"/api/games/{gid}/photos/1/confirm",
                    json={"rows": TRUE_ROWS, "home": "top", "ply": 6}).json()
    assert c["status"] == "confirmed"
    assert len(c["confirmed"]["corrections"]) == 32      # every piece entered


def test_non_photos_and_bad_requests_are_refused(client, stub):
    gid = new_game(client)
    html = client.post(f"/games/{gid}/photos", data={"ply": "0"},
                       files={"file": ("x.html", b"<html>ngrok</html>", "text/html")})
    assert html.status_code == 415
    assert upload(client, gid, ply=-1).status_code == 400
    assert upload(client, gid, turn="green").status_code == 400
    assert upload(client, "20200101-000000-abcd").status_code == 404
    bad = client.post(f"/api/games/{gid}/photos/1/confirm",
                      json={"rows": ["........"] * 7, "home": "top", "ply": 0})
    assert bad.status_code in (400, 404)
    assert stub.calls == []


def test_a_png_is_stored_as_jpeg(client, stub):
    gid = new_game(client)
    assert upload(client, gid, blob=png()).status_code == 200
    from server import storage
    assert storage.photo_path(gid, 1).read_bytes()[:2] == b"\xff\xd8"


def test_a_finished_game_still_takes_a_photo_of_the_final_position(client, stub):
    created = client.post("/games", json={}).json()
    with client.websocket_connect(f"/ws/{created['room']}?role=clock") as ws:
        ws.receive_json()
        ws.send_json({"type": "clock.stop", "ply": 6, "result": "1-0", "t": 1})
        ws.send_json({"type": "ping"})
        while ws.receive_json()["type"] != "pong":
            pass
    assert upload(client, created["game_id"], ply=6).json()["status"] == "read"
