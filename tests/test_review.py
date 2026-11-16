"""The review payload, the result override, and the lichess export.

Moves used to come from the per-press reader, which was removed on 2026-11-16;
until the photo-positions spec's line solver exists (Phase 3) a correction is
refused with 409. The fixture writes a saved analysis directly so the rest of
the review surface stays covered. Only lichess is mocked.
"""

from __future__ import annotations

import json

import chess
import pytest
from fastapi.testclient import TestClient

from engine.pgn import build_pgn

SHORT = ["e4", "e5", "Nf3", "Nc6"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDCAM_DATA", str(tmp_path))
    from server import app as app_module
    from server import storage

    assert storage.data_root() == tmp_path.resolve()
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def game(client):
    """A finished game on disk with analysis.json and game.pgn written directly."""
    from server import review, storage

    created = client.post("/games", json={}).json()
    gid = created["game_id"]
    gdir = storage.game_dir(gid)
    board, plies = chess.Board(), []
    for i, san in enumerate(SHORT):
        move = board.parse_san(san)
        plies.append({"index": i, "san": san, "uci": move.uci(), "seq": i + 1,
                      "margin": 1.0, "flags": [], "candidates": []})
        board.push(move)
    saved = {"game_id": gid, "plies": plies, "moves": [p["uci"] for p in plies]}
    gdir.joinpath("analysis.json").write_text(json.dumps(saved))
    pgn = build_pgn(review._ReplayedResult(saved), gdir)
    gdir.joinpath("game.pgn").write_text(pgn)
    storage.update_meta(gid, status="finished")
    return created


def read_labels(client, gid: str) -> dict:
    from server import storage
    return json.loads((storage.game_dir(gid) / "labels.json").read_text())


# ------------------------------------------------------------------ payload

def test_review_payload_has_what_the_page_renders(client, game):
    body = client.get(f"/api/games/{game['game_id']}/review").json()
    assert body["pending"] is False
    assert body["pgn"].startswith("[Event ")
    assert body["analysis"]["plies"], "no plies in the analysis"
    assert {"corrections", "verified", "moves"} <= set(body["labels"])
    ply = body["analysis"]["plies"][0]
    assert {"index", "san", "uci", "seq", "margin", "flags", "candidates"} <= set(ply)


def test_a_game_without_analysis_is_pending(client):
    created = client.post("/games", json={}).json()
    body = client.get(f"/api/games/{created['game_id']}/review").json()
    assert body["pending"] is True
    assert body["analysis"] is None


def test_unknown_game_is_404(client):
    assert client.get("/api/games/20260101-000000-abcd/review").status_code == 404


# -------------------------------------------------------------- corrections

def test_a_correction_waits_for_the_solver(client, game):
    gid = game["game_id"]
    resp = client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "san": "d4"})
    assert resp.status_code == 409
    assert "solver" in resp.json()["detail"]


def test_an_illegal_correction_is_still_refused_first(client, game):
    resp = client.post(f"/api/games/{game['game_id']}/corrections",
                       json={"ply": 0, "san": "Qh5"})
    assert resp.status_code == 400
    assert "legal" in resp.json()["detail"]


def test_correcting_a_game_with_no_analysis_is_409(client):
    created = client.post("/games", json={}).json()
    resp = client.post(f"/api/games/{created['game_id']}/corrections",
                       json={"ply": 0, "san": "e4"})
    assert resp.status_code == 409


# ------------------------------------------------------------ result, verify

def test_result_override_rewrites_the_pgn(client, game):
    gid = game["game_id"]
    resp = client.post(f"/api/games/{gid}/result", json={"result": "1/2-1/2"})
    assert resp.status_code == 200, resp.text
    assert '[Result "1/2-1/2"]' in resp.json()["pgn"]
    assert client.get(f"/api/games/{gid}/review").json()["result"] == "1/2-1/2"
    assert read_labels(client, gid)["result"] == "1/2-1/2"

    assert client.post(f"/api/games/{gid}/result", json={"result": "won"}).status_code == 400


def test_verify_marks_the_move_list_as_truth(client, game):
    gid = game["game_id"]
    assert client.post(f"/api/games/{gid}/verify", json={"verified": True}).json()["verified"]
    labels = read_labels(client, gid)
    assert labels["verified"] is True
    assert labels["moves"], "verifying an analysed game must record its move list"
    assert not client.post(f"/api/games/{gid}/verify", json={"verified": False}).json()["verified"]


# ----------------------------------------------------------------- lichess

def test_lichess_import_is_cached(client, game, monkeypatch):
    from server import app as app_module

    calls = []

    async def fake_import(pgn: str, timeout: float = 15.0):
        calls.append(pgn)
        return "https://lichess.org/abcd1234"

    monkeypatch.setattr(app_module, "import_pgn", fake_import)
    gid = game["game_id"]
    first = client.post(f"/api/games/{gid}/lichess").json()
    second = client.post(f"/api/games/{gid}/lichess").json()
    assert first["url"] == second["url"] == "https://lichess.org/abcd1234"
    assert len(calls) == 1, "a second press must not import the game twice"
    assert client.get(f"/api/games/{gid}/review").json()["lichess_url"] == first["url"]


def test_lichess_refusal_is_not_an_error(client, game, monkeypatch):
    from server import app as app_module

    async def fake_import(pgn: str, timeout: float = 15.0):
        return None

    monkeypatch.setattr(app_module, "import_pgn", fake_import)
    resp = client.post(f"/api/games/{game['game_id']}/lichess")
    assert resp.status_code == 200
    assert resp.json()["url"] is None


# ------------------------------------------------- a finished game is closed

def test_a_finished_game_refuses_new_play(client):
    """The second field test recorded two games into one directory.

    A clock phone that never reloaded keeps the old room and carries its press
    counter on from the finished game, so its presses interleave a new game with
    the old one's frames. Both ends of that are refused now.
    """
    from server import storage

    created = client.post("/games", json={}).json()
    gid, room = created["game_id"], created["room"]

    with client.websocket_connect(f"/ws/{room}?role=clock") as ws:
        ws.receive_json()                                   # hello
        ws.send_json({"type": "clock.start", "seq": 1, "side": "white",
                      "white_ms": 600_000, "black_ms": 600_000, "t": 1})
        ws.send_json({"type": "clock.stop", "seq": 2, "result": "1-0", "t": 2})
        for _ in range(12):
            msg = ws.receive_json()
            if msg.get("type") == "analysis.error":
                break                                       # nothing reads moves yet

        assert storage.read_meta(gid)["status"] == "finished"

        # A press after the game is over is refused, with a reason.
        ws.send_json({"type": "clock.press", "seq": 3, "side": "white",
                      "white_ms": 590_000, "black_ms": 590_000, "t": 3})
        for _ in range(12):
            msg = ws.receive_json()
            if msg.get("type") == "game.finished":
                break
        else:
            raise AssertionError("never told the clock the game was finished")
        assert msg["result"] == "1-0"

    events = storage.read_events(gid)
    assert [e["type"] for e in events] == ["clock.start", "clock.stop"]

    # The start frame of a finished game cannot be overwritten either, but the
    # burst the final stop commanded is still allowed in.
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    assert client.post(f"/games/{gid}/frames/0/0",
                       files={"file": ("f.jpg", jpeg, "image/jpeg")}).status_code == 409
    assert client.post(f"/games/{gid}/frames/99/0",
                       files={"file": ("f.jpg", jpeg, "image/jpeg")}).status_code == 409
    assert client.post(f"/games/{gid}/frames/2/0",
                       files={"file": ("f.jpg", jpeg, "image/jpeg")}).status_code == 200
