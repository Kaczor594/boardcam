"""Regressions for what the three field-test games exposed (spec Phase 6)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from engine.calibrate import frame_checks
from engine.pgn import build_pgn
from server.models import GameConfig


@pytest.fixture()
def game(tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDCAM_DATA", str(tmp_path))
    from server import analysis, storage

    meta = storage.create_game(GameConfig())
    yield meta["game_id"]
    analysis.forget(meta["game_id"])


def _jpeg(img: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", img)[1].tobytes()


def _scene(h: int = 640, w: int = 360) -> np.ndarray:
    """No board, but asymmetric enough to tell one rotation from another."""
    img = np.full((h, w, 3), 180, np.uint8)
    cv2.rectangle(img, (20, 30), (200, 120), (40, 40, 40), -1)
    cv2.circle(img, (w - 60, h - 90), 50, (90, 30, 200), -1)
    cv2.line(img, (0, h // 2), (w, h // 3), (10, 10, 10), 9)
    return img


def test_unfound_board_is_refused_not_tracked(game):
    """Game 3: calibration failed and the tracker ran anyway, emitting rook shuffles."""
    from server import analysis, storage

    storage.write_frame(game, 0, 0, _jpeg(_scene()))
    storage.write_frame(game, 1, 0, _jpeg(_scene()))
    assert analysis.calibration_usable(game) is False
    got = analysis.track_frame(game, 1)
    assert got["ok"] is False and "corners" in got["error"]
    done = analysis.finish(game)
    assert done["ok"] is False and done["board_not_found"] is True
    assert not storage.game_dir(game).joinpath("game.pgn").exists()


def test_manual_corners_make_a_game_readable(game):
    from server import analysis, storage

    storage.write_frame(game, 0, 0, _jpeg(_scene()))
    payload = analysis.set_manual_corners(game, [[60, 500], [300, 500], [300, 200], [60, 200]])
    assert analysis.calibration_usable(game) is True
    # The overlay is drawn in 0..1 of the frame, not in pixels.
    assert payload["frame_size"] == [360, 640]
    assert payload["corners_norm"][0] == [pytest.approx(60 / 360, abs=1e-4),
                                          pytest.approx(500 / 640, abs=1e-4)]


@pytest.mark.parametrize("rot", [None, cv2.ROTATE_90_CLOCKWISE,
                                 cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180])
def test_a_turned_phone_is_turned_back(game, rot):
    from server import analysis, storage

    ref = _scene()
    storage.write_frame(game, 0, 0, _jpeg(ref))
    sent = ref if rot is None else cv2.rotate(ref, rot)
    out = cv2.imdecode(np.frombuffer(analysis.match_start_orientation(game, _jpeg(sent)),
                                     np.uint8), cv2.IMREAD_COLOR)
    assert out.shape == ref.shape
    assert np.abs(out.astype(int) - ref.astype(int)).mean() < 4


def test_frame_checks_flag_a_blocked_lens_and_a_small_board():
    img = _scene()
    assert frame_checks(img, None) == []
    img[int(0.6 * img.shape[0]):] = 0
    small_board = np.array([[100, 300], [300, 300], [300, 100], [100, 100]], float)
    assert frame_checks(img, small_board) == ["lens-blocked", "board-small"]


def test_pgn_date_and_time_control_come_from_the_real_game(tmp_path):
    """Real games have no clock.config event and stamp server_t in seconds."""
    (tmp_path / "meta.json").write_text(json.dumps(
        {"initial_ms": 600000, "increment_ms": 5000,
         "white_name": "Isaac", "black_name": "Guest"}))
    events = [{"type": "clock.start", "seq": 1, "side": "white", "white_ms": 605000,
               "black_ms": 600000, "t": 1789242078277, "server_t": 1789242078.36}]
    result = SimpleNamespace(plies=[])
    pgn = build_pgn(result, tmp_path, events=events)
    assert '[Date "2026.09.12"]' in pgn
    assert '[TimeControl "600+5"]' in pgn
    assert '[White "Isaac"]' in pgn
