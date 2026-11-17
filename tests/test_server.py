"""The one-phone server surface: game creation, settings, the clock socket.

Photo intake has its own file (tests/test_intake.py). There is no camera role
any more: one phone is clock and camera (photo-positions spec, Phase 2).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDCAM_DATA", str(tmp_path))
    from server import app as app_module
    from server import storage

    # storage reads BOARDCAM_DATA lazily, so the env var is enough.
    assert storage.data_root() == tmp_path.resolve()
    with TestClient(app_module.app) as c:
        yield c


def make_game(client, **config) -> dict:
    resp = client.post("/games", json=config or None)
    assert resp.status_code == 200, resp.text
    return resp.json()


def drain(ws, wanted: str, limit: int = 12) -> dict:
    """Read messages until one of type `wanted` appears."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == wanted:
            return msg
    raise AssertionError(f"never saw {wanted!r}")


# ------------------------------------------------------- creation, settings

def test_create_game_returns_id_and_settings(client):
    game = make_game(client, initial_ms=600_000, increment_ms=5_000)
    assert len(game["room"]) == 4
    assert game["initial_ms"] == 600_000 and game["increment_ms"] == 5_000
    assert game["white_rating"] == 1350 and game["black_rating"] == 1350
    assert game["reader_model"] == "claude-fable-5-2"

    from server import storage
    meta = storage.read_meta(game["game_id"])
    assert meta["reader_model"] == "claude-fable-5-2"
    assert (storage.game_dir(game["game_id"]) / "photos").is_dir()


def test_last_used_settings_become_the_defaults(client):
    first = client.get("/api/settings").json()
    assert first["defaults"]["white_rating"] == 1350
    assert first["reader_models"] == ["claude-fable-5-2", "claude-sonnet-5-5", "claude-opus-5-5"]

    make_game(client, white_name="Isaac", black_name="Friend", white_rating=1500,
              black_rating=1200, reader_model="claude-sonnet-5-5", initial_ms=300_000,
              increment_ms=0)
    d = client.get("/api/settings").json()["defaults"]
    assert (d["white_name"], d["black_name"]) == ("Isaac", "Friend")
    assert (d["white_rating"], d["black_rating"]) == (1500, 1200)
    assert d["reader_model"] == "claude-sonnet-5-5"
    assert (d["initial_ms"], d["increment_ms"]) == (300_000, 0)


def test_settings_are_editable_after_creation(client):
    game = make_game(client)
    gid = game["game_id"]
    resp = client.patch(f"/api/games/{gid}/settings",
                        json={"black_name": "Anna", "black_rating": 1720,
                              "reader_model": "claude-opus-5-5"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["black_name"] == "Anna"

    from server import storage
    meta = storage.read_meta(gid)
    assert meta["black_rating"] == 1720 and meta["reader_model"] == "claude-opus-5-5"
    assert meta["white_name"] == "White"            # untouched keys stay

    assert client.patch(f"/api/games/{gid}/settings",
                        json={"reader_model": "gpt-4"}).status_code == 422
    assert client.patch(f"/api/games/{gid}/settings",
                        json={"white_rating": 99_999}).status_code == 422


def test_bad_settings_refuse_creation(client):
    assert client.post("/games", json={"reader_model": "nope"}).status_code == 422
    assert client.post("/games", json={"white_rating": 5}).status_code == 422


# ------------------------------------------------------------- the socket

def test_clock_socket_says_hello_with_settings(client):
    game = make_game(client, white_name="Isaac")
    with client.websocket_connect(f"/ws/{game['room']}?role=clock") as clk:
        hello = clk.receive_json()
        assert hello["type"] == "hello" and hello["game_id"] == game["game_id"]
        assert hello["config"]["white_name"] == "Isaac"
        assert hello["config"]["reader_model"] == "claude-fable-5-2"
        assert hello["seq"] == 0


def test_role_defaults_to_clock_and_camera_is_gone(client):
    game = make_game(client)
    with client.websocket_connect(f"/ws/{game['room']}") as clk:
        assert clk.receive_json()["type"] == "hello"
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{game['room']}?role=camera") as cam:
            cam.receive_json()


def test_unknown_room_is_refused_at_the_socket(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/ZZZZ?role=clock") as ws:
            ws.receive_json()


def test_clock_events_are_persisted_and_set_status(client):
    game = make_game(client)
    gid = game["game_id"]
    with client.websocket_connect(f"/ws/{game['room']}?role=clock") as clk:
        clk.receive_json()
        clk.send_json({"type": "clock.start", "seq": 1, "side": "white",
                       "white_ms": 600_000, "black_ms": 600_000, "t": 1})
        clk.send_json({"type": "clock.press", "seq": 2, "side": "black",
                       "white_ms": 598_000, "black_ms": 597_000, "t": 2})
        clk.send_json({"type": "clock.pause", "t": 3})
        clk.send_json({"type": "ping"})
        drain(clk, "pong")

        from server import storage
        assert storage.read_meta(gid)["status"] == "paused"
        clk.send_json({"type": "clock.stop", "ply": 2, "result": "1-0", "t": 4})
        clk.send_json({"type": "ping"})
        drain(clk, "pong")

    events = storage.read_events(gid)
    assert [e["type"] for e in events] == ["clock.start", "clock.press", "clock.pause",
                                           "clock.stop"]
    assert all("server_t" in e for e in events)
    meta = storage.read_meta(gid)
    assert meta["status"] == "finished" and meta["result"] == "1-0"

    # A reconnecting clock learns the press count, so its numbering continues.
    with client.websocket_connect(f"/ws/{game['room']}?role=clock") as clk:
        hello = clk.receive_json()
        assert hello["seq"] == 2 and hello["status"] == "finished"


def test_rejoining_replaces_the_stale_socket(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=clock") as first:
        first.receive_json()
        with client.websocket_connect(f"/ws/{room}?role=clock") as second:
            assert second.receive_json()["type"] == "hello"
            second.send_json({"type": "ping"})
            assert drain(second, "pong")


def test_heartbeat_replies(client):
    game = make_game(client)
    with client.websocket_connect(f"/ws/{game['room']}?role=clock") as clk:
        clk.receive_json()
        clk.send_json({"type": "ping"})
        assert "t" in drain(clk, "pong")


# --------------------------------------------------------------- listing

def test_games_listing_and_delete(client):
    a = make_game(client, white_name="Isaac", black_name="Friend")
    b = make_game(client)

    listing = client.get("/api/games").json()["games"]
    ids = [g["game_id"] for g in listing]
    assert a["game_id"] in ids and b["game_id"] in ids
    assert listing[0]["created_at"] >= listing[-1]["created_at"]

    row = next(g for g in listing if g["game_id"] == a["game_id"])
    assert row["photos"] == 0 and row["white_name"] == "Isaac" and row["status"] == "ready"

    detail = client.get(f"/api/games/{a['game_id']}").json()
    assert detail["room"] == a["room"]
    assert detail["positions"] == {"game_id": a["game_id"], "photos": []}

    assert client.delete(f"/api/games/{a['game_id']}").status_code == 200
    assert client.get(f"/api/games/{a['game_id']}").status_code == 404
    assert client.get("/api/games/..%2f..%2fetc").status_code == 404
