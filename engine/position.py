"""Turn a reader's photo-frame board into a chess position.

The reader reports squares as they appear in the photo (row 0 farthest from the
camera, column 0 leftmost) and which side of the photo White's home rank is on.
This module rotates that into board coordinates, sets the side to move, and
builds a FEN. It does not decide whether the position is *right*; it flags the
ways it can be impossible, so the phone can ask for a retake or a fix.

Castling rights are only a plausible guess here (king and rook still on their
home squares). The line solver (Phase 3) sets the real ones from the path that
led to the position. En passant is never set from a photo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import chess

HOME_SIDES = ("bottom", "top", "left", "right")

# python-chess status bits worth telling a player about. Castling and en-passant
# bits are left out because this module sets those fields itself.
_STATUS = {
    chess.STATUS_NO_WHITE_KING: "no_white_king",
    chess.STATUS_NO_BLACK_KING: "no_black_king",
    chess.STATUS_TOO_MANY_KINGS: "too_many_kings",
    chess.STATUS_TOO_MANY_WHITE_PAWNS: "too_many_white_pawns",
    chess.STATUS_TOO_MANY_BLACK_PAWNS: "too_many_black_pawns",
    chess.STATUS_PAWNS_ON_BACKRANK: "pawns_on_back_rank",
    chess.STATUS_TOO_MANY_WHITE_PIECES: "too_many_white_pieces",
    chess.STATUS_TOO_MANY_BLACK_PIECES: "too_many_black_pieces",
    chess.STATUS_OPPOSITE_CHECK: "side_not_to_move_in_check",
    chess.STATUS_TOO_MANY_CHECKERS: "too_many_checkers",
    chess.STATUS_IMPOSSIBLE_CHECK: "impossible_check",
}


@dataclass
class Position:
    fen: str
    home: str                       # the home side actually used
    home_guessed: bool              # True when the reader said "unknown"
    flags: list[str] = field(default_factory=list)

    @property
    def legal(self) -> bool:
        return not self.flags

    def to_json(self) -> dict:
        return {**asdict(self), "legal": self.legal}


def square_of(r: int, c: int, home: str) -> chess.Square:
    """The board square at photo row ``r``, column ``c`` for a White home side."""
    if home == "bottom":
        rank, file = 7 - r, c
    elif home == "top":
        rank, file = r, 7 - c
    elif home == "left":
        rank, file = c, r
    elif home == "right":
        rank, file = 7 - c, 7 - r
    else:
        raise ValueError(f"home side must be one of {HOME_SIDES}, not {home!r}")
    return chess.square(file, rank)


def to_board_map(rows: list[str], home: str) -> dict[chess.Square, chess.Piece]:
    out = {}
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch != ".":
                out[square_of(r, c, home)] = chess.Piece.from_symbol(ch)
    return out


def rotate_rows(rows: list[str], quarter_turns: int) -> list[str]:
    """The photo-frame grid turned clockwise by 90 degrees ``quarter_turns`` times."""
    g = [list(r) for r in rows]
    for _ in range(quarter_turns % 4):
        g = [list(col) for col in zip(*g[::-1])]
    return ["".join(r) for r in g]


def _orientation_score(rows: list[str], home: str) -> float:
    """How much a home side looks like White's: White's men low, Black's high."""
    board = to_board_map(rows, home)
    score = 0.0
    for sq, p in board.items():
        rank = chess.square_rank(sq)
        if p.piece_type == chess.PAWN and rank in (0, 7):
            score -= 10.0
        score += (3.5 - rank) if p.color == chess.WHITE else (rank - 3.5)
    return score


def guess_home(rows: list[str]) -> str:
    return max(HOME_SIDES, key=lambda h: _orientation_score(rows, h))


def _castling(board: chess.Board) -> str:
    rights = ""
    for color, k_sq, rooks in ((chess.WHITE, chess.E1, ((chess.H1, "K"), (chess.A1, "Q"))),
                               (chess.BLACK, chess.E8, ((chess.H8, "k"), (chess.A8, "q")))):
        if board.piece_at(k_sq) != chess.Piece(chess.KING, color):
            continue
        for r_sq, letter in rooks:
            if board.piece_at(r_sq) == chess.Piece(chess.ROOK, color):
                rights += letter
    return rights or "-"


def build_position(rows: list[str], home: str, white_to_move: bool = True,
                   fullmove: int = 1) -> Position:
    """A FEN and its legality flags from a photo-frame read."""
    guessed = home not in HOME_SIDES
    if guessed:
        home = guess_home(rows)
    board = chess.Board(None)
    for sq, piece in to_board_map(rows, home).items():
        board.set_piece_at(sq, piece)
    board.turn = chess.WHITE if white_to_move else chess.BLACK
    board.set_castling_fen(_castling(board))
    board.fullmove_number = max(1, int(fullmove))
    status = board.status()
    flags = [name for bit, name in _STATUS.items() if status & bit]
    return Position(fen=board.fen(), home=home, home_guessed=guessed, flags=flags)


def fullmove_of(ply: int) -> int:
    """Fullmove number of the position after ``ply`` half-moves from the start."""
    return ply // 2 + 1
