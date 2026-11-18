"""Lines between photos, pins, the review payload, the PGN and the lichess export.

lc0 and Stockfish are stubbed (``tests/conftest.py``): every legal move is
equally likely and the evaluation is a material count. Only lichess is mocked
beyond that.
"""

from __future__ import annotations

import io
import time

import chess
import chess.pgn
import pytest
from fastapi.testclient import TestClient

OPENING = ["e4", "e5", "Nf3", "Nc6"]            # ply 4
LATER = ["Bc4", "Bc5"]                            # ply 6


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDCAM_DATA", str(tmp_path))
    from server import app as app_module
    from server import storage

    assert storage.data_root() == tmp_path.resolve()
    with TestClient(app_module.app) as c:
        yield c


def fen_after(sans: list[str]) -> str:
    b = chess.Board()
    for s in sans:
        b.push_san(s)
    return b.fen()


def add_confirmed(gid: str, n: int, ply: int, fen: str) -> None:
    """A confirmed photo, written straight into positions.json. Side to move and
    move number come from ``ply``, as in a real confirmation."""
    from server import storage

    f = fen.split()
    f[1] = "w" if ply % 2 == 0 else "b"
    f[5] = str(ply // 2 + 1)
    fen = " ".join(f)

    def add(doc):
        doc["photos"].append({"n": n, "file": f"photos/{n:03d}.jpg", "ply": ply,
                              "status": "confirmed",
                              "confirmed": {"ply": ply, "fen": fen, "rows": [], "home": "bottom",
                                            "corrections": []}})
    storage.update_positions(gid, add)


def press_events(gid: str, plies: int) -> None:
    from server import storage
    for seq in range(1, plies + 1):
        side = "white" if seq % 2 else "black"
        storage.append_event(gid, {"type": "clock.start" if seq == 1 else "clock.press",
                                   "seq": seq, "side": side,
                                   "white_ms": 600_000 - seq * 1000, "black_ms": 600_000 - seq * 2000,
                                   "t": seq * 1000})


@pytest.fixture()
def game(client):
    """A game with presses to ply 6 and photos confirmed at plies 4 and 6."""
    gid = client.post("/games", json={"white_name": "Isaac", "black_name": "Guest"}).json()["game_id"]
    press_events(gid, 6)
    add_confirmed(gid, 1, 4, fen_after(OPENING))
    add_confirmed(gid, 2, 6, fen_after(OPENING + LATER))
    return gid


def solved(client, gid: str) -> dict:
    """Ask for a solve and wait for the background job."""
    client.post(f"/api/games/{gid}/solve")
    for _ in range(200):
        body = client.get(f"/api/games/{gid}/review").json()
        if not body["solving"] and not body["stale"]:
            return body
        time.sleep(0.02)
    raise AssertionError("the solve never finished")


def pgn_game(text: str) -> chess.pgn.Game:
    g = chess.pgn.read_game(io.StringIO(text))
    assert g is not None and not g.errors, g.errors if g else "no game"
    return g


# ------------------------------------------------------------------ solving

def test_gaps_are_solved_into_one_timeline(client, game):
    body = solved(client, game)
    assert [g["status"] for g in body["gaps"]] == ["found", "found"]
    tl = body["timeline"]
    assert [e["ply"] for e in tl] == list(range(7))
    assert tl[0]["kind"] == "start"
    assert [e["photo"] for e in tl if e["kind"] == "photo"] == [1, 2]
    assert tl[4]["fen"].split()[0] == fen_after(OPENING).split()[0]
    assert tl[6]["fen"].split()[0] == fen_after(OPENING + LATER).split()[0]
    # four orders of e4/Nf3 and e5/Nc6 reach photo 1 equally likely: inferred
    assert tl[1]["kind"] == "inferred" and 0 < tl[1]["p"] < 1
    assert all(e["eval"] is not None for e in tl)
    # the second gap is one bishop each: forced, so certain
    assert tl[5]["certain"] and tl[6]["certain"]


def test_the_pgn_marks_inferred_moves_and_photos_and_has_clock_times(client, game):
    body = solved(client, game)
    g = pgn_game(body["pgn"])
    nodes = list(g.mainline())
    assert len(nodes) == 6
    assert all("[%clk" in n.comment for n in nodes)
    assert "inferred" in nodes[0].comment
    assert f"photo 1: {body['timeline'][4]['fen']}" in nodes[3].comment
    assert "photo 2:" in nodes[5].comment
    assert nodes[-1].board().board_fen() == fen_after(OPENING + LATER).split()[0]
    assert g.headers["White"] == "Isaac" and g.headers["TimeControl"] == "600+5"
    assert [f["photo"] for f in body["fens"]] == [1, 2]


def test_a_second_solve_reuses_cached_gaps(client, game, _no_engines):
    solved(client, game)
    calls = _no_engines.calls
    solved(client, game)
    assert _no_engines.calls == calls, "unchanged gaps must not be solved again"


def test_a_new_photo_only_solves_the_new_gap(client, game, _no_engines):
    solved(client, game)
    calls = _no_engines.calls
    add_confirmed(game, 3, 8, fen_after(OPENING + LATER + ["d3", "d6"]))
    body = solved(client, game)
    assert len(body["gaps"]) == 3 and body["gaps"][2]["status"] == "found"
    assert 0 < _no_engines.calls - calls < 200


def test_an_impossible_gap_breaks_the_line_and_the_pgn_stops_there(client):
    gid = client.post("/games", json={}).json()["game_id"]
    add_confirmed(gid, 1, 2, fen_after(["e4", "e5"]))
    # ply 4 cannot have three white moves' worth of change
    add_confirmed(gid, 2, 4, fen_after(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]))
    add_confirmed(gid, 3, 6, fen_after(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"]))
    body = solved(client, gid)
    assert [g["status"] for g in body["gaps"]] == ["found", "none", "none"]
    breaks = [e for e in body["timeline"] if e["break"]]
    assert [e["photo"] for e in breaks] == [2, 3]
    g = pgn_game(body["pgn"])
    assert len(list(g.mainline())) == 2
    assert "could not be inferred" in list(g.mainline())[-1].comment
    assert "photo 3 at ply 6" in body["pgn"]


def test_a_photo_of_the_start_position_is_an_empty_gap(client):
    gid = client.post("/games", json={}).json()["game_id"]
    add_confirmed(gid, 1, 0, chess.STARTING_FEN)
    body = solved(client, gid)
    assert [g["status"] for g in body["gaps"]] == ["found"]       # a photo of the start
    assert [e["kind"] for e in body["timeline"]] == ["start"]
    assert body["timeline"][0]["photo"] == 1


# --------------------------------------------------------------------- pins

def test_accepting_a_move_pins_it_and_resolves_the_gap(client, game):
    body = solved(client, game)
    assert body["timeline"][1]["uci"] in ("e2e4", "g1f3")
    resp = client.post(f"/api/games/{game}/pins", json={"ply": 1, "uci": "g1f3"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pins"][0]["san"] == "Nf3"
    body = solved(client, game)
    tl = body["timeline"]
    assert tl[1]["uci"] == "g1f3" and tl[1]["kind"] == "pinned" and tl[1]["certain"]
    assert tl[4]["fen"].split()[0] == fen_after(OPENING).split()[0]
    assert "inferred" not in list(pgn_game(body["pgn"]).mainline())[0].comment

    # undo
    assert client.delete(f"/api/games/{game}/pins/1").status_code == 200
    body = solved(client, game)
    assert body["pins"] == [] and body["timeline"][1]["kind"] == "inferred"
    assert client.delete(f"/api/games/{game}/pins/1").status_code == 404


def test_a_pin_must_be_legal_and_inside_the_photos(client, game):
    solved(client, game)
    assert client.post(f"/api/games/{game}/pins", json={"ply": 1, "uci": "e2e5"}).status_code == 400
    assert client.post(f"/api/games/{game}/pins", json={"ply": 9, "uci": "e2e4"}).status_code == 409
    assert client.post(f"/api/games/{game}/pins", json={"ply": "1"}).status_code == 400


def test_engine_moves_at_a_ply(client, game):
    solved(client, game)
    resp = client.get(f"/api/games/{game}/engine/4")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fen"].split()[0] == fen_after(OPENING).split()[0]
    assert len(body["moves"]) == 3
    assert client.get(f"/api/games/{game}/engine/40").status_code == 404


def test_hello_carries_the_photo_hint_threshold(client):
    room = client.post("/games", json={"white_rating": 1350, "black_rating": 1350}).json()["room"]
    with client.websocket_connect(f"/ws/{room}") as ws:
        hello = ws.receive_json()
    assert hello["hint_plies"] == 11        # 1350 → Maia-1600 → 11 plies (engine/solver.json)


# --------------------------------------------------------- result, lichess

def test_result_override_rewrites_the_pgn(client, game):
    solved(client, game)
    resp = client.post(f"/api/games/{game}/result", json={"result": "1/2-1/2"})
    assert resp.status_code == 200, resp.text
    assert '[Result "1/2-1/2"]' in resp.json()["pgn"]
    assert client.get(f"/api/games/{game}/review").json()["result"] == "1/2-1/2"
    assert client.post(f"/api/games/{game}/result", json={"result": "won"}).status_code == 400


def test_pgn_download(client, game):
    solved(client, game)
    resp = client.get(f"/games/{game}/pgn")
    assert resp.status_code == 200
    assert len(list(pgn_game(resp.text).mainline())) == 6


def test_lichess_import_is_cached(client, game, monkeypatch):
    from server import app as app_module

    calls = []

    async def fake_import(pgn: str, timeout: float = 15.0):
        calls.append(pgn)
        return "https://lichess.org/abcd1234"

    monkeypatch.setattr(app_module, "import_pgn", fake_import)
    solved(client, game)
    first = client.post(f"/api/games/{game}/lichess").json()
    second = client.post(f"/api/games/{game}/lichess").json()
    assert first["url"] == second["url"] == "https://lichess.org/abcd1234"
    assert len(calls) == 1, "a second press must not import the game twice"
    pgn_game(calls[0])
    assert client.get(f"/api/games/{game}/review").json()["lichess_url"] == first["url"]


def test_lichess_refusal_is_not_an_error(client, game, monkeypatch):
    from server import app as app_module

    async def fake_import(pgn: str, timeout: float = 15.0):
        return None

    monkeypatch.setattr(app_module, "import_pgn", fake_import)
    resp = client.post(f"/api/games/{game}/lichess")
    assert resp.status_code == 200
    assert resp.json()["url"] is None


def test_unknown_game_is_404(client):
    assert client.get("/api/games/20260101-000000-abcd/review").status_code == 404


# ------------------------------------------------- a finished game is closed

def test_a_finished_game_refuses_new_play(client):
    """A clock phone that never reloaded keeps the old room; its presses must
    not write a second game into the first one's log."""
    from server import storage

    created = client.post("/games", json={}).json()
    gid, room = created["game_id"], created["room"]

    with client.websocket_connect(f"/ws/{room}?role=clock") as ws:
        ws.receive_json()                                   # hello
        ws.send_json({"type": "clock.start", "seq": 1, "side": "white",
                      "white_ms": 600_000, "black_ms": 600_000, "t": 1})
        ws.send_json({"type": "clock.stop", "seq": 2, "result": "1-0", "t": 2})
        ws.send_json({"type": "ping"})
        for _ in range(12):
            if ws.receive_json().get("type") == "pong":
                break
        assert storage.read_meta(gid)["status"] == "finished"
        ws.send_json({"type": "clock.press", "seq": 3, "side": "white",
                      "white_ms": 590_000, "black_ms": 590_000, "t": 3})
        for _ in range(12):
            msg = ws.receive_json()
            if msg.get("type") == "game.finished":
                break
        else:
            raise AssertionError("never told the clock the game was finished")
        assert msg["result"] == "1-0"

    assert [e["type"] for e in storage.read_events(gid)] == ["clock.start", "clock.stop"]
