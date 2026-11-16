"""Photo-frame reads to FEN: orientation, side to move, castling guess, legality flags."""

from __future__ import annotations

import chess
import pytest

from engine.position import build_position, fullmove_of, guess_home, rotate_rows

START = ["rnbqkbnr", "pppppppp", "........", "........",
         "........", "........", "PPPPPPPP", "RNBQKBNR"]
START_FEN = chess.Board().fen()

# Turning the photo clockwise moves White's home edge bottom -> left -> top -> right.
TURNS = [(0, "bottom"), (1, "left"), (2, "top"), (3, "right")]


@pytest.mark.parametrize("turns, home", TURNS)
def test_every_home_side_gives_the_same_position(turns, home):
    pos = build_position(rotate_rows(START, turns), home)
    assert pos.fen == START_FEN
    assert pos.legal and not pos.home_guessed


@pytest.mark.parametrize("turns, home", TURNS)
def test_an_unknown_home_side_is_guessed_and_marked(turns, home):
    rows = rotate_rows(START, turns)
    assert guess_home(rows) == home
    pos = build_position(rows, "unknown")
    assert pos.fen == START_FEN and pos.home == home and pos.home_guessed


def test_side_to_move_and_move_number():
    pos = build_position(START, "bottom", white_to_move=False, fullmove=fullmove_of(1))
    assert pos.fen.split()[1:] == ["b", "KQkq", "-", "0", "1"]
    assert fullmove_of(24) == 13


def test_castling_rights_need_king_and_rook_at_home():
    rows = list(START)
    rows[7] = "RNBQKBN."                 # h1 rook gone
    rows[0] = "rnbq.bnr"                 # black king gone from e8 ...
    rows[1] = "ppppkppp"                 # ... to e7
    pos = build_position(rows, "bottom")
    assert pos.fen.split()[2] == "Q"


def test_a_pilot_position_reads_as_labelled():
    # Pilot photo 2507, White at the bottom of the photo.
    rows = ["..r..rk.", "pp..bppp", "..n.pnb.", ".B..N...",
            "Q..q.B..", "......N.", "PP...PPP", "R....RK."]
    pos = build_position(rows, "bottom")
    assert pos.fen.split()[0] == "2r2rk1/pp2bppp/2n1pnb1/1B2N3/Q2q1B2/6N1/PP3PPP/R4RK1"
    assert pos.legal


@pytest.mark.parametrize("change, flag", [
    ((7, "RNBQKBNK"), "too_many_kings"),
    ((0, "rnbq.bnr"), "no_black_king"),
    ((7, "RNBQKBNP"), "pawns_on_back_rank"),
    ((2, "QQQQQQQQ"), "too_many_white_pieces"),
])
def test_impossible_positions_are_flagged(change, flag):
    rows = list(START)
    rows[change[0]] = change[1]
    pos = build_position(rows, "bottom")
    assert flag in pos.flags and not pos.legal


def test_the_side_not_to_move_in_check_is_flagged():
    rows = ["....k...", "........", "........", "........",
            "........", "........", "........", "....R..K"]
    assert build_position(rows, "bottom", white_to_move=False).legal
    pos = build_position(rows, "bottom", white_to_move=True)
    assert "side_not_to_move_in_check" in pos.flags
