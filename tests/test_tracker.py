"""The tracker on short games whose answer is known exactly.

These are not accuracy measurements — ``engine.evaluate`` on the synthetic
corpora does that. These pin the cases a corpus average would hide: the moves
that change more than two squares, and the three ways a capture sequence can
come apart when the clock and the board disagree about how many moves have been
played.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import chess
import cv2
import numpy as np
import pytest

from engine.features import load_params
from engine.tracker import track_game
from synth.profiles import PROFILES, make_scene
from synth.render import FrameOpts, Renderer

W, H = 1280, 720


def moves_from_san(sans: list[str]) -> list[chess.Move]:
    board = chess.Board()
    out = []
    for san in sans:
        mv = board.parse_san(san)
        out.append(mv)
        board.push(mv)
    return out


def build_game(out: Path, sans: list[str], *, profile: str = "clean", seed: int = 7,
               skip: set[int] = frozenset(), double: set[int] = frozenset(),
               bump_at: int | None = None, hand_at: int | None = None,
               bursts: int = 1) -> list[chess.Move]:
    """Render a short game into a BoardCam game directory.

    ``skip`` drops the capture after that ply, so the following frame advances
    two plies — a missed clock press. ``double`` repeats the capture after that
    ply, so one frame advances none — a double press.
    """
    rng = random.Random(seed)
    profile_obj = PROFILES[profile]
    scene = make_scene(profile_obj, rng, w=W, h=H)
    renderer = Renderer(scene)
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)

    moves = moves_from_san(sans)
    board = chess.Board()
    bump = None
    events = [{"type": "clock.config", "initial_ms": 600_000, "increment_ms": 5_000,
               "white_name": "White", "black_name": "Black", "t": 0, "server_t": 0}]
    seq = 0
    t = 0

    def write(seq: int, brd: chess.Board, hand_sq: int | None, white_moved: bool) -> None:
        for k in range(bursts):
            opt = FrameOpts(bump=bump, pose=0.4 * seq, noise_seed=seq * 31 + k,
                            hand=((hand_sq, 0.55, white_moved)
                                  if hand_sq is not None and k == 0 else None))
            img, _ = renderer.render(brd, opt)
            cv2.imwrite(str(frames / f"{seq:04d}_{k}.jpg"), img,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 85])

    write(0, board, None, True)
    for ply, mv in enumerate(moves, start=1):
        white_moved = board.turn == chess.WHITE
        board.push(mv)
        if bump_at is not None and ply == bump_at:
            bump = np.array([[0.9999, -0.011, 9.0], [0.011, 0.9999, -7.0]])
        if ply in skip:
            continue
        for _ in range(2 if ply in double else 1):
            seq += 1
            t += 20_000
            events.append({"type": "clock.start" if seq == 1 else "clock.press",
                           "seq": seq, "side": "white" if white_moved else "black",
                           "white_ms": 600_000 - 1000 * seq, "black_ms": 600_000 - 900 * seq,
                           "t": t, "server_t": t})
            write(seq, board, mv.to_square if ply == hand_at else None, white_moved)

    (out / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return moves


def track(out: Path) -> list[str]:
    params = load_params()
    return [m.uci() for m in track_game(out, params).moves]


def expect(tmp_path: Path, sans: list[str], **kw) -> None:
    moves = build_game(tmp_path / "g", sans, **kw)
    got = track(tmp_path / "g")
    want = [m.uci() for m in moves]
    assert got == want, f"\n  want {want}\n  got  {got}"


# The four moves that touch more than a from- and a to-square.
CASTLE = ["e4", "e5", "Nf3", "Nf6", "Bc4", "Bc5", "O-O", "O-O"]
EN_PASSANT = ["e4", "d5", "e5", "f5", "exf6"]
PROMOTION = ["d4", "e5", "dxe5", "d6", "exd6", "Be7", "dxc7", "Nf6", "cxb8=Q"]
CAPTURES = ["e4", "d5", "exd5", "Qxd5", "Nc3", "Qa5", "d4", "Nf6"]


def test_castling_both_sides(tmp_path):
    expect(tmp_path, CASTLE)


def test_en_passant(tmp_path):
    expect(tmp_path, EN_PASSANT)


def test_promotion_with_capture(tmp_path):
    expect(tmp_path, PROMOTION)


def test_captures(tmp_path):
    expect(tmp_path, CAPTURES)


def test_missed_press_puts_two_plies_in_one_frame(tmp_path):
    expect(tmp_path, CAPTURES, skip={4})


def test_double_press_puts_no_plies_in_one_frame(tmp_path):
    expect(tmp_path, CAPTURES, double={3})


def test_survives_a_tripod_bump(tmp_path):
    expect(tmp_path, CAPTURES, bump_at=3)


def test_survives_a_hand_over_the_board(tmp_path):
    expect(tmp_path, CAPTURES, hand_at=5, bursts=3)


def test_flags_every_promotion(tmp_path):
    build_game(tmp_path / "g", PROMOTION)
    res = track_game(tmp_path / "g", load_params())
    promo = [p for p in res.plies if p.uci.endswith("q")]
    assert promo, "no promotion tracked"
    assert all("promotion_unknown" in p.flags for p in promo)
