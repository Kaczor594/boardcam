"""Vision-LLM fallback: schema, escalation, call cap, cache — all mocked — plus
one real smoke test (``-m live``) against flagged plies from a synthetic game.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from engine import vision_llm

FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
RECT = np.zeros((512, 512, 3), dtype=np.uint8)


class _DummyCalib:
    corners = np.array([[50.0, 400.0], [400.0, 400.0], [400.0, 50.0], [50.0, 50.0]])
    corners_image = None


CALIB = _DummyCalib()
CANDS = [{"label": "Nf3", "moves": ("g1f3",)},
         {"label": "no move happened", "moves": ()}]


class _FakeBlock:
    def __init__(self, type_, input_):
        self.type = type_
        self.input = input_


class _FakeResponse:
    def __init__(self, choice, confidence, in_tok=100, out_tok=20):
        self.content = [_FakeBlock("tool_use", {"choice": choice, "confidence": confidence})]
        self.usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _resolve(client, candidates=CANDS):
    return vision_llm.resolve_ply(
        [FRAME], [FRAME], RECT, RECT, candidates,
        side_to_move="white", calib=CALIB, client=client,
    )


# --------------------------------------------------------------------------
# Schema and forced tool use
# --------------------------------------------------------------------------

def test_forces_tool_and_builds_enum_from_candidates():
    client = _FakeClient([_FakeResponse("Nf3", 0.95)])
    result = _resolve(client)

    assert result["san"] == "Nf3"
    assert result["confidence"] == pytest.approx(0.95)
    assert result["model"] == vision_llm.DEFAULT_MODEL
    assert result["cost"] > 0

    call = client.messages.calls[0]
    assert call["model"] == vision_llm.DEFAULT_MODEL
    assert call["tool_choice"] == {"type": "tool", "name": "choose_move"}
    enum = call["tools"][0]["input_schema"]["properties"]["choice"]["enum"]
    assert enum == ["Nf3", "no move happened", "none_of_these"]


def test_duplicate_labels_are_rejected():
    client = _FakeClient([])
    with pytest.raises(ValueError):
        _resolve(client, candidates=[{"label": "Nf3", "moves": ("g1f3",)},
                                     {"label": "Nf3", "moves": ()}])


def test_confidence_is_clamped_to_unit_interval():
    client = _FakeClient([_FakeResponse("Nf3", 1.4)])
    result = _resolve(client)
    assert result["confidence"] == 1.0


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------

def test_no_escalation_when_confident_and_matched():
    client = _FakeClient([_FakeResponse("Nf3", 0.95)])
    _resolve(client)
    assert len(client.messages.calls) == 1


def test_escalates_on_none_of_these():
    client = _FakeClient([_FakeResponse("none_of_these", 0.9),
                          _FakeResponse("Nf3", 0.8)])
    result = _resolve(client)
    assert result["san"] == "Nf3"
    assert result["model"] == vision_llm.ESCALATION_MODEL
    assert len(client.messages.calls) == 2
    assert client.messages.calls[1]["model"] == vision_llm.ESCALATION_MODEL


def test_escalates_on_low_confidence():
    client = _FakeClient([_FakeResponse("Nf3", 0.4), _FakeResponse("Nf3", 0.9)])
    result = _resolve(client)
    assert result["confidence"] == pytest.approx(0.9)
    assert len(client.messages.calls) == 2


def test_escalation_cost_is_summed():
    client = _FakeClient([_FakeResponse("none_of_these", 0.9, in_tok=1000, out_tok=100),
                          _FakeResponse("Nf3", 0.9, in_tok=1000, out_tok=100)])
    result = _resolve(client)
    price_in, price_out = vision_llm._PRICE[vision_llm.DEFAULT_MODEL]
    epr_in, epr_out = vision_llm._PRICE[vision_llm.ESCALATION_MODEL]
    expected = (1000 * price_in + 100 * price_out) / 1e6 + (1000 * epr_in + 100 * epr_out) / 1e6
    assert result["cost"] == pytest.approx(expected)


# --------------------------------------------------------------------------
# Resolver: cache + call cap
# --------------------------------------------------------------------------

def _fake_answer(**kwargs):
    return {"san": "Nf3", "confidence": 0.9, "model": "claude-sonnet-5", "cost": 0.001}


def test_resolver_caches_by_key(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vision_llm, "resolve_ply",
                        lambda **kw: (calls.append(kw), _fake_answer())[1])

    kwargs = dict(before_paths=[FRAME], after_paths=[FRAME], rect_before=RECT,
                  rect_after=RECT, candidates=CANDS, side_to_move="white", calib=CALIB)
    resolver = vision_llm.LLMResolver(tmp_path)
    first = resolver.resolve("5", **kwargs)
    second = resolver.resolve("5", **kwargs)

    assert first == second
    assert len(calls) == 1
    assert resolver.calls_made == 1
    assert resolver.path.exists()

    resolver2 = vision_llm.LLMResolver(tmp_path)
    third = resolver2.resolve("5", **kwargs)
    assert third == first
    assert len(calls) == 1                        # the second resolver made no new call


def test_resolver_respects_call_cap(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vision_llm, "resolve_ply",
                        lambda **kw: (calls.append(kw), _fake_answer())[1])

    kwargs = dict(before_paths=[FRAME], after_paths=[FRAME], rect_before=RECT,
                  rect_after=RECT, candidates=CANDS, side_to_move="white", calib=CALIB)
    resolver = vision_llm.LLMResolver(tmp_path, max_calls=1)
    first = resolver.resolve("1", **kwargs)
    second = resolver.resolve("2", **kwargs)       # different key, budget already spent

    assert first is not None
    assert second is None
    assert len(calls) == 1


def test_resolver_survives_a_corrupt_cache_file(tmp_path, monkeypatch):
    (tmp_path / "llm_cache.json").write_text("not json")
    monkeypatch.setattr(vision_llm, "resolve_ply", lambda **kw: _fake_answer())
    resolver = vision_llm.LLMResolver(tmp_path)
    kwargs = dict(before_paths=[FRAME], after_paths=[FRAME], rect_before=RECT,
                  rect_after=RECT, candidates=CANDS, side_to_move="white", calib=CALIB)
    assert resolver.resolve("1", **kwargs) is not None


# --------------------------------------------------------------------------
# Live smoke test
# --------------------------------------------------------------------------

@pytest.mark.live
def test_live_resolve_flagged_plies():
    """3 real calls on flagged plies from data/synth/hard; ≥ 2/3 should match truth."""
    import chess

    from engine.evaluate import truth_moves
    from engine.features import load_params
    from engine.rectify import rectify
    from engine.tracker import frame_paths, track_game

    root = Path(__file__).resolve().parent.parent / "data" / "synth" / "hard"
    game_dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.exists() else []
    if not game_dirs:
        pytest.skip("data/synth/hard not generated — see engine/README.md")

    params = load_params()
    params["use_llm"] = False

    flagged = []
    game_dir = None
    result = None
    for candidate_dir in game_dirs:
        result = track_game(candidate_dir, params)
        flagged = [p for p in result.plies if p.flags and len(p.candidates) >= 2]
        if len(flagged) >= 3:
            game_dir = candidate_dir
            break
    if game_dir is None:
        pytest.skip("no game in data/synth/hard had 3 flagged, answerable plies")

    truth = truth_moves(game_dir)
    frames = dict(frame_paths(game_dir))

    correct = 0
    answered = 0
    for ply in flagged[:3]:
        seen: set[str] = set()
        candidates = []
        for c in ply.candidates:
            if c["san"] in seen:
                continue
            seen.add(c["san"])
            candidates.append({"label": c["san"],
                               "moves": (c["uci"],) if c["uci"] else ()})

        before_seq = max(ply.seq - 1, min(frames))
        before_paths = frames.get(before_seq, frames[min(frames)])
        after_paths = frames[ply.seq]
        rect_before = rectify(cv2.imread(str(before_paths[0])), result.calibration)
        rect_after = rectify(cv2.imread(str(after_paths[0])), result.calibration)

        board_before = chess.Board()
        for m in result.moves[:ply.index]:
            board_before.push(m)
        side = "white" if board_before.turn == chess.WHITE else "black"

        answer = vision_llm.resolve_ply(before_paths, after_paths, rect_before,
                                        rect_after, candidates, side, result.calibration)
        answered += 1
        assert answer["san"] in ({c["label"] for c in candidates} | {"none_of_these"})

        truth_san = None
        if ply.index < len(truth):
            tb = chess.Board()
            for u in truth[:ply.index]:
                tb.push(chess.Move.from_uci(u))
            truth_san = tb.san(chess.Move.from_uci(truth[ply.index]))
        if truth_san is not None and answer["san"] == truth_san:
            correct += 1

    assert answered == 3
    assert correct >= 2
