"""Line solver: legality, exact length, end on B, admissible pruning."""

import random
import shutil

import chess
import pytest

from engine.lines import Bound, solve


def uniform(board: chess.Board) -> dict[str, float]:
    moves = [m.uci() for m in board.legal_moves]
    return {u: 1.0 / len(moves) for u in moves} if moves else {}


def play(fen: str, sans: list[str]) -> chess.Board:
    b = chess.Board(fen)
    for s in sans:
        b.push_san(s)
    return b


def check_lines(res, fen_a, placement, n):
    assert res.status == "found"
    assert res.lines
    for ln in res.lines:
        assert len(ln.moves) == n
        b = chess.Board(fen_a)
        for u in ln.moves:
            mv = chess.Move.from_uci(u)
            assert mv in b.legal_moves
            b.push(mv)
        assert b.board_fen() == placement
        assert b.fen() == ln.fen_end
    assert abs(sum(ln.prob for ln in res.lines) - 1) < 1e-9
    for ply in res.marginals:
        assert abs(sum(p for _, p in ply) - 1) < 1e-9


@pytest.mark.parametrize("sans", [
    ["e4", "e5", "Nf3"],
    ["d4", "d5", "c4", "e6"],
    ["e4", "c5", "Nf3", "d6", "d4", "cxd4"],
])
def test_paths_are_legal_exact_and_end_on_b(sans):
    a = chess.Board()
    b = play(a.fen(), sans)
    res = solve(a.fen(), b.board_fen(), len(sans), uniform, top=5, time_box=20)
    check_lines(res, a.fen(), b.board_fen(), len(sans))
    assert res.n_used == len(sans)
    assert any(ln.moves == [m.uci() for m in b.move_stack] for ln in res.lines) or res.complete


def test_true_line_found_when_policy_knows_it():
    a = chess.Board()
    b = play(a.fen(), ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"])
    true = [m.uci() for m in b.move_stack]

    def peaked(board):
        pol = uniform(board)
        ply = (board.fullmove_number - 1) * 2 + (0 if board.turn else 1)
        if ply < len(true) and true[ply] in pol:
            pol = {u: (0.5 if u == true[ply] else 0.5 / (len(pol) - 1)) for u in pol}
        return pol

    res = solve(a.fen(), b.board_fen(), 6, peaked, top=3, time_box=20)
    check_lines(res, a.fen(), b.board_fen(), 6)
    assert res.lines[0].moves == true


def test_castling_rights_come_from_the_path():
    a = play(chess.STARTING_FEN, ["e4", "e5"])
    b = play(a.fen(), ["Ke2", "Ke7", "Ke1", "Ke8"])
    res = solve(a.fen(), b.board_fen(), 4, uniform, top=50, time_box=20)
    check_lines(res, a.fen(), b.board_fen(), 4)
    walk = [ln for ln in res.lines if ln.moves[0] == "e1e2"]
    assert walk, "the king walk is one of the 4-ply lines"
    assert all(" - " in ln.fen_end or "KQ" not in ln.fen_end.split()[2] for ln in walk)
    shuffle = [ln for ln in res.lines
               if not {"e1e2", "e8e7", "e2e1", "e7e8"} & set(ln.moves)]
    assert shuffle and all(ln.fen_end.split()[2] == "KQkq" for ln in shuffle)


def test_en_passant_comes_from_the_path():
    a = play(chess.STARTING_FEN, ["e4", "a6", "e5"])
    b = play(a.fen(), ["d5"])
    res = solve(a.fen(), b.board_fen(), 1, uniform, top=3, time_box=10)
    check_lines(res, a.fen(), b.board_fen(), 1)
    assert res.lines[0].fen_end.split()[3] == "d6"


def test_neighbouring_ply_count_is_tried():
    a = chess.Board()
    b = play(a.fen(), ["e4", "e5", "Nf3"])
    res = solve(a.fen(), b.board_fen(), 2, uniform, top=3, time_box=20)
    assert res.n_used == 3
    check_lines(res, a.fen(), b.board_fen(), 3)
    assert [t["n"] for t in res.tried] == [2, 1, 3]


def test_unreachable_target_is_none_and_complete():
    a = chess.Board()
    b = chess.Board()
    b.set_piece_at(chess.E4, chess.Piece(chess.PAWN, chess.WHITE))   # a ninth pawn
    res = solve(a.fen(), b.board_fen(), 4, uniform, time_box=10)
    assert res.status == "none" and res.complete and not res.lines


def test_zero_plies():
    a = chess.Board()
    res = solve(a.fen(), a.board_fen(), 0, uniform, neighbours=False)
    assert res.status == "found" and res.lines[0].moves == []


def _random_game(rng, plies):
    b = chess.Board()
    for _ in range(plies):
        moves = list(b.legal_moves)
        if not moves:
            break
        caps = [m for m in moves if b.is_capture(m) or m.promotion]
        b.push(rng.choice(caps) if caps and rng.random() < 0.5 else rng.choice(moves))
    return b


def test_bound_is_admissible_on_random_games():
    """need() never exceeds the moves each side actually used."""
    rng = random.Random(7)
    checked = 0
    for _ in range(600):
        g = _random_game(rng, rng.randint(10, 120))
        stack = list(g.move_stack)
        if len(stack) < 4:
            continue
        i = rng.randrange(0, len(stack) - 1)
        j = rng.randrange(i + 1, len(stack) + 1)
        a = chess.Board()
        for m in stack[:i]:
            a.push(m)
        b = a.copy()
        for m in stack[i:j]:
            b.push(m)
        need = Bound(b).need(a)
        assert need is not None
        moved = {chess.WHITE: 0, chess.BLACK: 0}
        t = a.turn
        for _ in range(j - i):
            moved[t] += 1
            t = not t
        assert need[0] <= moved[chess.WHITE] and need[1] <= moved[chess.BLACK], (a.fen(), b.fen())
        assert Bound(b).feasible(a, j - i)
        checked += 1
    assert checked > 450


@pytest.mark.skipif(shutil.which("lc0") is None, reason="lc0 not installed")
def test_maia_policy_is_a_distribution():
    from engine.humanlike import MAIA_DIR, Maia
    if not (MAIA_DIR / "maia-1500.pb.gz").exists():
        pytest.skip("no Maia weights")
    m = Maia()
    b = chess.Board()
    pol = m.policy(b, 1500)
    assert set(pol) == {mv.uci() for mv in b.legal_moves}
    assert abs(sum(pol.values()) - 1) < 1e-9
    assert max(pol, key=pol.get) in {"e2e4", "d2d4", "c2c4", "g1f3"}
    m.close()


def test_band_for_rounds_to_nearest_band_and_clamps():
    from engine.humanlike import band_for
    assert band_for(1350, 250) == 1600
    assert band_for(1340, 250) == 1600
    assert band_for(400, 250) == 1100
    assert band_for(2600, 250) == 1900


def test_opening_prior_blends_counts_with_maia():
    from collections import Counter
    from engine.humanlike import OpeningPrior, position_key
    b = chess.Board()
    prior = OpeningPrior({position_key(b): Counter({"d2d4": 9, "e2e4": 1})})
    maia = {"e2e4": 0.6, "d2d4": 0.3, "g1f3": 0.1}
    out = prior.blend(b, maia, k=5)
    assert abs(sum(out.values()) - 1) < 1e-9
    assert abs(out["d2d4"] - (9 + 5 * 0.3) / 15) < 1e-12
    assert out["d2d4"] > out["e2e4"] > out["g1f3"]
    b.push_uci("e2e4")
    assert prior.blend(b, maia, k=5) is maia        # unseen position → Maia unchanged


@pytest.mark.skipif(shutil.which("stockfish") is None, reason="stockfish not installed")
def test_evalgraph_line_and_top_moves():
    from engine.evalgraph import MATE_CP, Stockfish
    with Stockfish(depth=8) as sf:
        line = sf.line(chess.STARTING_FEN, ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"])
        assert [p["ply"] for p in line] == list(range(8))
        assert line[-1]["cp"] == MATE_CP            # White has mated
        top = sf.top_moves(chess.STARTING_FEN, 3)
        assert len(top) == 3 and all(chess.Move.from_uci(t["uci"]) in chess.Board().legal_moves
                                     for t in top)
