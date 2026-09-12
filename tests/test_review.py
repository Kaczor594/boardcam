"""Phase 5 gate: the correction loop, the result override, and the exports.

The interesting claim here is not that an endpoint returns 200 — it is that a
correction actually re-tracks the game. So these run the real tracker over a
short rendered game rather than mocking it, and assert on the PGN that comes out
the other side. Only lichess is mocked; it is someone else's server.
"""

from __future__ import annotations

import json

import chess
import pytest
from fastapi.testclient import TestClient

from test_tracker import build_game

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
    """A real game on disk: rendered frames, events, analysis.json, game.pgn."""
    from server import analysis, storage

    created = client.post("/games", json={}).json()
    gid = created["game_id"]
    gdir = storage.game_dir(gid)
    build_game(gdir, SHORT, profile="clean", seed=11)
    got = analysis.finish(gid)
    assert got["ok"], got
    analysis.forget(gid)
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

def test_correction_reruns_the_game_and_changes_the_pgn(client, game):
    gid = game["game_id"]
    before = client.get(f"/api/games/{gid}/review").json()
    before_pgn = before["pgn"]

    # A legal first move the engine did not choose: pinning it must survive the
    # re-run, which is the whole point of a correction.
    played = before["analysis"]["plies"][0]["san"]
    wanted = "d4" if played != "d4" else "c4"

    resp = client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "san": wanted})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["analysis"]["plies"][0]["san"] == wanted
    assert body["pgn"] != before_pgn
    assert wanted in body["pgn"]
    assert 0 in body["changed"]

    labels = read_labels(client, gid)
    assert labels["corrections"] == [
        {"ply": 0, "san": wanted, "uci": labels["corrections"][0]["uci"],
         "t": labels["corrections"][0]["t"]}]
    assert labels["moves"][0] == wanted

    # The file on disk is what the clock page and lichess read, not the response.
    from server import storage
    assert (storage.game_dir(gid) / "game.pgn").read_text().strip() == body["pgn"].strip()
    assert json.loads((storage.game_dir(gid) / "analysis.json").read_text())["plies"][0]["san"] == wanted


def test_correction_accepts_uci_too(client, game):
    gid = game["game_id"]
    resp = client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "uci": "d2d4"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["analysis"]["plies"][0]["uci"] == "d2d4"


def test_an_illegal_correction_is_refused(client, game):
    gid = game["game_id"]
    resp = client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "san": "Qh5"})
    assert resp.status_code == 400
    assert "legal" in resp.json()["detail"]
    assert not (client.get(f"/api/games/{gid}/review").json()["labels"]["corrections"])


def test_a_correction_outside_the_game_is_refused(client, game):
    resp = client.post(f"/api/games/{game['game_id']}/corrections",
                       json={"ply": 999, "san": "e4"})
    assert resp.status_code == 400
    assert "outside" in resp.json()["detail"]


def test_correcting_a_game_with_no_analysis_is_409(client):
    created = client.post("/games", json={}).json()
    resp = client.post(f"/api/games/{created['game_id']}/corrections",
                       json={"ply": 0, "san": "e4"})
    assert resp.status_code == 409


def test_unpinning_drops_the_constraint(client, game):
    gid = game["game_id"]
    client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "san": "d4"})
    resp = client.delete(f"/api/games/{gid}/corrections/0")
    assert resp.status_code == 200, resp.text
    assert resp.json()["labels"]["corrections"] == []
    assert client.delete(f"/api/games/{gid}/corrections/0").status_code == 404


# ------------------------------------------------------------ result, verify

def test_result_override_rewrites_the_pgn(client, game):
    gid = game["game_id"]
    resp = client.post(f"/api/games/{gid}/result", json={"result": "1/2-1/2"})
    assert resp.status_code == 200, resp.text
    assert '[Result "1/2-1/2"]' in resp.json()["pgn"]
    assert client.get(f"/api/games/{gid}/review").json()["result"] == "1/2-1/2"
    assert read_labels(client, gid)["result"] == "1/2-1/2"

    assert client.post(f"/api/games/{gid}/result", json={"result": "won"}).status_code == 400


def test_result_override_survives_a_later_correction(client, game):
    gid = game["game_id"]
    client.post(f"/api/games/{gid}/result", json={"result": "0-1"})
    body = client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "san": "d4"}).json()
    assert '[Result "0-1"]' in body["pgn"]


def test_verify_marks_the_move_list_as_truth(client, game):
    gid = game["game_id"]
    assert client.post(f"/api/games/{gid}/verify", json={"verified": True}).json()["verified"]
    labels = read_labels(client, gid)
    assert labels["verified"] is True
    assert labels["moves"], "verifying an analysed game must record its move list"
    assert not client.post(f"/api/games/{gid}/verify", json={"verified": False}).json()["verified"]


# ---------------------------------------------------------------- rectified

def test_rect_endpoint_returns_a_jpeg(client, game):
    gid = game["game_id"]
    resp = client.get(f"/api/games/{gid}/rect/1", params={"prev": 0, "squares": "e2,e4"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:2] == b"\xff\xd8"
    assert client.get(f"/api/games/{gid}/rect/99").status_code == 404


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


# ------------------------------------------------------------ label export

def test_reviewed_games_are_exported_as_training_truth(client, game, tmp_path):
    import sys
    sys.path.insert(0, "scripts")
    import tune

    gid = game["game_id"]
    client.post(f"/api/games/{gid}/corrections", json={"ply": 0, "san": "d4"})

    real = tmp_path / "real"
    exported = tune.export_labelled(tmp_path / "games", real)
    assert exported == [gid]

    out = real / gid
    with open(out / "truth.pgn") as fh:
        import chess.pgn
        truth = chess.pgn.read_game(fh)
    sans = []
    board = chess.Board()
    for m in truth.mainline_moves():
        sans.append(board.san(m))
        board.push(m)
    assert sans == read_labels(client, gid)["moves"]
    assert (out / "events.jsonl").exists()
    # Frames are symlinked: evaluate reads them, nothing copies a game twice.
    assert (out / "frames").is_symlink()
    assert sorted((out / "frames").glob("*.jpg"))


def test_an_untouched_game_is_not_exported(client, game, tmp_path):
    import sys
    sys.path.insert(0, "scripts")
    import tune

    assert tune.export_labelled(tmp_path / "games", tmp_path / "real") == []


# ---------------------------------------------------- manual calibration

def test_manual_corners_survive_the_prestart_recalibration(client, game):
    """The camera re-uploads frame 0 every two seconds until the game starts.

    Each upload re-runs detection, so without a guard the corners a player
    dragged are overwritten seconds later and the phone is told to ask again.
    """
    from server import analysis, storage

    gid = game["game_id"]
    corners = [[10.0, 10.0], [90.0, 12.0], [92.0, 88.0], [8.0, 90.0]]
    manual = analysis.set_manual_corners(gid, corners)
    assert manual["method"] == "manual"

    again = analysis.calibrate_start_frame(gid)
    assert again["method"] == "manual"
    assert again["corners"] == manual["corners"]
    on_disk = storage.read_json(gid, "calibration.json")
    assert on_disk["method"] == "manual"

    # A fresh process (no in-memory state) must reach the same conclusion.
    analysis.forget(gid)
    assert analysis.calibrate_start_frame(gid)["method"] == "manual"
    # And an explicit re-detect is still possible.
    assert analysis.calibrate_start_frame(gid, force=True)["method"] != "manual"


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
                break                                       # no frames to track

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
