"""Phase 1 gate: the physical workflow end to end, with no engine.

Covers pairing, the press → capture → upload → ack round trip, event
persistence, frame storage, and reconnect-replaces-stale-socket.
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

# Point storage at a temp tree before the app imports it.
os.environ.setdefault("BOARDCAM_DATA", "")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDCAM_DATA", str(tmp_path))
    from server import app as app_module
    from server import storage

    # storage reads BOARDCAM_DATA lazily, so the env var is enough.
    assert storage.data_root() == tmp_path.resolve()
    with TestClient(app_module.app) as c:
        yield c


def jpeg_bytes(size: int = 800) -> bytes:
    """A body that passes the SOI check without pulling in an encoder."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * size + b"\xff\xd9"


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


# ---------------------------------------------------------------- pairing

def test_create_game_returns_room_and_id(client):
    game = make_game(client, initial_ms=600_000, increment_ms=5_000)
    assert len(game["room"]) == 4
    assert game["room"].isupper()
    assert game["initial_ms"] == 600_000
    assert game["increment_ms"] == 5_000

    by_room = client.get(f"/api/games/by-room/{game['room']}")
    assert by_room.status_code == 200
    assert by_room.json()["game_id"] == game["game_id"]

    # Lookup is case-insensitive: the clock phone's input may be lowercase.
    assert client.get(f"/api/games/by-room/{game['room'].lower()}").status_code == 200
    assert client.get("/api/games/by-room/ZZZZ").status_code == 404


def test_unknown_room_is_refused_at_the_socket(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/ZZZZ?role=clock") as ws:
            ws.receive_json()


def test_both_roles_see_each_other(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        hello = cam.receive_json()
        assert hello["type"] == "hello"
        assert hello["role"] == "camera"
        assert hello["peer_connected"] is False
        assert hello["config"]["initial_ms"] == 600_000

        with client.websocket_connect(f"/ws/{room}?role=clock") as clk:
            clock_hello = clk.receive_json()
            assert clock_hello["type"] == "hello"
            assert clock_hello["peer_connected"] is True

            peer = drain(cam, "peer")
            assert peer["role"] == "clock" and peer["connected"] is True


# ------------------------------------------------------- press → capture

def test_press_reaches_the_camera_quickly(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        cam.receive_json()
        with client.websocket_connect(f"/ws/{room}?role=clock") as clk:
            clk.receive_json()
            drain(cam, "peer")

            t0 = time.perf_counter()
            clk.send_json({
                "type": "clock.start", "seq": 1, "side": "white",
                "white_ms": 600_000, "black_ms": 600_000, "t": time.time() * 1000,
            })
            cap = drain(cam, "capture")
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert cap["seq"] == 1
            assert cap["reason"] == "start"
            assert elapsed_ms < 200, f"capture took {elapsed_ms:.0f} ms"

            clk.send_json({
                "type": "clock.press", "seq": 2, "side": "black",
                "white_ms": 598_000, "black_ms": 597_000, "t": time.time() * 1000,
            })
            cap2 = drain(cam, "capture")
            assert cap2["seq"] == 2 and cap2["reason"] == "press"

    from server import storage
    events = storage.read_events(game["game_id"])
    assert [e["type"] for e in events] == ["clock.start", "clock.press"]
    assert all("server_t" in e for e in events)
    assert storage.read_meta(game["game_id"])["status"] == "running"


def test_pause_and_resume_do_not_capture(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        cam.receive_json()
        with client.websocket_connect(f"/ws/{room}?role=clock") as clk:
            clk.receive_json()
            drain(cam, "peer")
            clk.send_json({"type": "clock.pause", "t": time.time() * 1000})
            clk.send_json({"type": "clock.resume", "t": time.time() * 1000})
            clk.send_json({
                "type": "clock.press", "seq": 1, "side": "white",
                "white_ms": 1, "black_ms": 1, "t": time.time() * 1000,
            })
            # The only capture that arrives is the press, proving the two
            # clock-state events produced none.
            cap = drain(cam, "capture")
            assert cap["reason"] == "press" and cap["seq"] == 1

    from server import storage
    types = [e["type"] for e in storage.read_events(game["game_id"])]
    assert types == ["clock.pause", "clock.resume", "clock.press"]


def test_stop_captures_a_final_frame_and_sets_result(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        cam.receive_json()
        with client.websocket_connect(f"/ws/{room}?role=clock") as clk:
            clk.receive_json()
            drain(cam, "peer")
            clk.send_json({
                "type": "clock.stop", "seq": 7, "result": "1-0",
                "t": time.time() * 1000,
            })
            cap = drain(cam, "capture")
            assert cap["seq"] == 7 and cap["reason"] == "stop"

    from server import storage
    meta = storage.read_meta(game["game_id"])
    assert meta["status"] == "finished" and meta["result"] == "1-0"


# ------------------------------------------------------------- uploads

def test_frame_upload_acks_the_clock_and_lands_on_disk(client):
    game = make_game(client)
    gid, room = game["game_id"], game["room"]
    with client.websocket_connect(f"/ws/{room}?role=clock") as clk:
        clk.receive_json()

        for k in range(3):
            resp = client.post(
                f"/games/{gid}/frames/3/{k}",
                files={"file": (f"f{k}.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["seq"] == 3 and body["k"] == k
            assert body["path"] == f"frames/0003_{k}.jpg"

            ack = drain(clk, "frame.ok")
            assert ack["seq"] == 3 and ack["k"] == k and ack["bytes"] > 0

    from server import storage
    for k in range(3):
        assert storage.frame_path(gid, 3, k).exists()
    assert storage.frame_inventory(gid) == [{"seq": 3, "k": [0, 1, 2]}]

    served = client.get(f"/games/{gid}/frames/3/0")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert client.get(f"/games/{gid}/frames/9/0").status_code == 404


def test_frame_zero_is_overwritten_not_appended(client):
    game = make_game(client)
    gid = game["game_id"]
    for size in (400, 900, 1500):
        resp = client.post(
            f"/games/{gid}/frames/0/0",
            files={"file": ("f.jpg", io.BytesIO(jpeg_bytes(size)), "image/jpeg")},
        )
        assert resp.status_code == 200

    from server import storage
    assert storage.frame_inventory(gid) == [{"seq": 0, "k": [0]}]
    assert storage.frame_path(gid, 0, 0).stat().st_size == 1500 + 6


def test_non_jpeg_and_unknown_game_are_refused(client):
    game = make_game(client)
    gid = game["game_id"]
    bad = client.post(
        f"/games/{gid}/frames/1/0",
        files={"file": ("x.html", io.BytesIO(b"<html>ngrok</html>"), "text/html")},
    )
    assert bad.status_code == 415

    missing = client.post(
        "/games/20200101-000000-abcd/frames/1/0",
        files={"file": ("f.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
    )
    assert missing.status_code == 404

    traversal = client.get("/api/games/..%2f..%2fetc")
    assert traversal.status_code == 404


# ------------------------------------------------------------ reconnect

def test_rejoining_replaces_the_stale_socket(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        cam.receive_json()
        with client.websocket_connect(f"/ws/{room}?role=clock") as first:
            first.receive_json()
            drain(cam, "peer")

            # Same role joins again: the new socket must win.
            with client.websocket_connect(f"/ws/{room}?role=clock") as second:
                hello = second.receive_json()
                assert hello["type"] == "hello"

                # The replacement, not the replaced socket, receives the press ack.
                client.post(
                    f"/games/{game['game_id']}/frames/1/0",
                    files={"file": ("f.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
                )
                ack = drain(second, "frame.ok")
                assert ack["seq"] == 1


def test_heartbeat_replies(client):
    game = make_game(client)
    with client.websocket_connect(f"/ws/{game['room']}?role=clock") as clk:
        clk.receive_json()
        clk.send_json({"type": "ping"})
        pong = drain(clk, "pong")
        assert "t" in pong


def test_camera_cannot_forge_clock_events(client):
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        cam.receive_json()
        cam.send_json({
            "type": "clock.press", "seq": 1, "side": "white",
            "white_ms": 1, "black_ms": 1, "t": 0,
        })
        cam.send_json({"type": "ping"})
        drain(cam, "pong")

    from server import storage
    assert storage.read_events(game["game_id"]) == []


# --------------------------------------------------------------- listing

def test_games_listing_and_delete(client):
    a = make_game(client, white_name="Isaac", black_name="Friend")
    b = make_game(client)
    client.post(
        f"/games/{a['game_id']}/frames/0/0",
        files={"file": ("f.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")},
    )

    listing = client.get("/api/games").json()["games"]
    ids = [g["game_id"] for g in listing]
    assert a["game_id"] in ids and b["game_id"] in ids
    assert listing[0]["created_at"] >= listing[-1]["created_at"]

    row = next(g for g in listing if g["game_id"] == a["game_id"])
    assert row["frames"] == 1 and row["white_name"] == "Isaac" and row["status"] == "ready"

    detail = client.get(f"/api/games/{a['game_id']}").json()
    assert detail["room"] == a["room"]
    assert detail["analysis"] is None
    assert detail["frames"] == [{"seq": 0, "k": [0]}]

    assert client.delete(f"/api/games/{a['game_id']}").status_code == 200
    assert client.get(f"/api/games/{a['game_id']}").status_code == 404


def test_stop_without_seq_still_captures(client):
    """A stale cached clock page may omit seq; the final frame must survive."""
    game = make_game(client)
    room = game["room"]
    with client.websocket_connect(f"/ws/{room}?role=camera") as cam:
        cam.receive_json()
        with client.websocket_connect(f"/ws/{room}?role=clock") as clk:
            clk.receive_json()
            drain(cam, "peer")
            for seq in (1, 2, 3):
                clk.send_json({
                    "type": "clock.press", "seq": seq, "side": "white",
                    "white_ms": 1, "black_ms": 1, "t": 0,
                })
                drain(cam, "capture")

            clk.send_json({"type": "clock.stop", "result": "1-0", "t": 0})
            cap = drain(cam, "capture")
            assert cap["seq"] == 4 and cap["reason"] == "stop"
