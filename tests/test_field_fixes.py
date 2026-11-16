"""Regressions for what the September field-test games exposed that still apply."""

from __future__ import annotations

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from engine.calibrate import frame_checks
from engine.pgn import build_pgn


def _jpeg(img: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", img)[1].tobytes()


def _scene(h: int = 640, w: int = 360) -> np.ndarray:
    """No board, but asymmetric enough to tell one rotation from another."""
    img = np.full((h, w, 3), 180, np.uint8)
    cv2.rectangle(img, (20, 30), (200, 120), (40, 40, 40), -1)
    cv2.circle(img, (w - 60, h - 90), 50, (90, 30, 200), -1)
    cv2.line(img, (0, h // 2), (w, h // 3), (10, 10, 10), 9)
    return img


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
