"""The synthetic corpus: reproducible, legal, and shaped like a real game dir."""

from __future__ import annotations

import json
import random
from pathlib import Path

import chess
import chess.pgn
import numpy as np
import pytest

from synth import generate
from synth.games import MAX_PLIES, MIN_PLIES, build_corpus, classic_games
from synth.profiles import PROFILES, make_scene
from synth.render import FrameOpts, Renderer

# The seeds and sizes the Phase 2 gate uses, so the tests speak about the same
# corpora the thresholds were measured on.
GATE = {"clean": (30, 1), "shallow": (40, 4), "hard": (30, 2), "nightmare": (10, 3)}


def test_classic_games_are_legal_and_plentiful():
    games = classic_games()
    assert len(games) >= 20, f"only {len(games)} usable classic games"
    for g in games:
        board = chess.Board()
        for mv in g.moves:
            assert mv in board.legal_moves, f"{g.name}: {mv} illegal"
            board.push(mv)
        assert MIN_PLIES <= len(g) <= MAX_PLIES


@pytest.mark.parametrize("profile,n,seed", [(p, n, s) for p, (n, s) in GATE.items()])
def test_corpus_covers_castling_and_promotion(profile, n, seed):
    corpus = build_corpus(n, random.Random(seed))
    assert len(corpus) == n
    assert any("castling" in g.features for g in corpus)
    assert any("promotion" in g.features for g in corpus)
    assert any("en_passant" in g.features for g in corpus)
    assert any("underpromotion" in g.features for g in corpus)
    assert all(MIN_PLIES <= len(g) <= MAX_PLIES for g in corpus)


def test_corpus_is_reproducible():
    a = build_corpus(6, random.Random(11))
    b = build_corpus(6, random.Random(11))
    assert [g.name for g in a] == [g.name for g in b]
    assert [[m.uci() for m in g.moves] for g in a] == [[m.uci() for m in g.moves] for g in b]


def test_render_is_deterministic_under_seed():
    board = chess.Board()
    imgs = []
    for _ in range(2):
        scene = make_scene(PROFILES["shallow"], random.Random(99), w=480, h=270)
        img, corners = Renderer(scene).render(board, FrameOpts(noise_seed=5, captured=3))
        imgs.append((img, corners))
    assert np.array_equal(imgs[0][0], imgs[1][0])
    assert np.allclose(imgs[0][1], imgs[1][1])


def test_render_keeps_the_board_and_its_far_strip_in_frame():
    """The analysis mask is the board plus a strip above its far edge."""
    board = chess.Board()
    for name in PROFILES:
        for seed in (1, 2, 3, 4, 5):
            scene = make_scene(PROFILES[name], random.Random(seed), w=640, h=360)
            _img, corners = Renderer(scene).render(board, FrameOpts())
            assert np.isfinite(corners).all(), name
            assert corners[:, 0].min() > -10 and corners[:, 0].max() < 650, name
            assert corners[:, 1].min() > -10 and corners[:, 1].max() < 370, name


def test_generated_game_dir_matches_the_protocol(tmp_path: Path):
    rc = generate.main(["--games", "2", "--profile", "clean", "--seed", "1",
                        "--out", str(tmp_path), "--bursts", "1", "--max-plies", "32",
                        "--width", "480", "--height", "270", "--workers", "2"])
    assert rc == 0
    dirs = sorted(p for p in tmp_path.iterdir() if p.is_dir())
    assert len(dirs) == 2

    for d in dirs:
        truth = json.loads((d / "truth.json").read_text())
        n = truth["plies"]
        assert n == 32

        game = chess.pgn.read_game((d / "truth.pgn").open())
        assert game is not None
        assert len(list(game.mainline_moves())) == n

        events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]
        assert events[0]["type"] == "clock.config"
        assert events[1]["type"] == "clock.start" and events[1]["seq"] == 1
        presses = [e for e in events if e["type"] in ("clock.start", "clock.press")]
        assert [e["seq"] for e in presses] == list(range(1, n + 1))
        assert [e["side"] for e in presses][:2] == ["white", "black"]
        assert events[-1]["type"] == "clock.stop"
        assert events[-1]["seq"] == truth["stop_seq"] == n + 1
        for e in events:
            assert "server_t" in e

        # A frame for every capture, seq 0 through the stop capture.
        for seq in range(0, n + 2):
            assert (d / "frames" / f"{seq:04d}_0.jpg").exists(), (d.name, seq)

        corners = np.array(truth["corners"])
        assert corners.shape == (4, 2)
        assert (corners > -10).all() and (corners[:, 0] < 490).all()
        assert truth["camera_side"] in ("rank1", "fileh", "rank8", "filea")
        sil = np.array(truth["square_is_light"])
        assert sil.shape == (8, 8)
        assert not sil[0][0] and sil[7][0]      # a1 dark, h1 light
