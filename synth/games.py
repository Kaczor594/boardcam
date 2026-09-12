"""Game corpus for the synthetic renderer.

Two sources:

* ``CLASSIC_PGNS`` — hand-entered move lists of well-known games. They are
  validated at import time and silently truncated at the first move that does
  not parse, so a mistyped move costs a few plies rather than breaking the
  corpus. ``classic_games()`` reports what survived.
* ``playout()`` — weighted random legal games, biased so that castling, en
  passant and promotion actually occur; ``targeted_playout()`` keeps drawing
  until a requested feature set appears.

Every game leaves this module as a ``SynthGame``: a name plus a list of
``chess.Move`` objects that are legal from the standard start position.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import chess
import chess.pgn

MIN_PLIES = 30
MAX_PLIES = 120


@dataclass
class SynthGame:
    """A legal game, ready to be rendered frame by frame."""

    name: str
    moves: list[chess.Move]
    source: str  # "classic" | "playout"
    truncated: bool = False
    features: frozenset[str] = field(default_factory=frozenset)

    def __len__(self) -> int:
        return len(self.moves)

    def board_states(self):
        """Yield (ply, board) for ply 0..n — board *after* that ply."""
        board = chess.Board()
        yield 0, board.copy()
        for i, mv in enumerate(self.moves, start=1):
            board.push(mv)
            yield i, board.copy()

    def pgn(self, headers: dict[str, str] | None = None) -> str:
        game = chess.pgn.Game()
        game.headers["Event"] = "BoardCam synthetic"
        game.headers["Site"] = "synthetic"
        game.headers["White"] = "White"
        game.headers["Black"] = "Black"
        game.headers["Result"] = self.result()
        for k, v in (headers or {}).items():
            game.headers[k] = v
        node = game
        for mv in self.moves:
            node = node.add_variation(mv)
        return str(game)

    def result(self) -> str:
        board = chess.Board()
        for mv in self.moves:
            board.push(mv)
        if board.is_checkmate():
            return "0-1" if board.turn == chess.WHITE else "1-0"
        if board.is_stalemate() or board.is_insufficient_material():
            return "1/2-1/2"
        return "*"


# --------------------------------------------------------------------------
# Classic games
# --------------------------------------------------------------------------

CLASSIC_PGNS: dict[str, str] = {
    "immortal-1851": """
        e4 e5 f4 exf4 Bc4 Qh4+ Kf1 b5 Bxb5 Nf6 Nf3 Qh6 d3 Nh5 Nh4 Qg5 Nf5 c6
        g4 Nf6 Rg1 cxb5 h4 Qg6 h5 Qg5 Qf3 Ng8 Bxf4 Qf6 Nc3 Bc5 Nd5 Qxb2 Bd6
        Bxg1 e5 Qxa1+ Ke2 Na6 Nxg7+ Kd8 Qf6+ Nxf6 Be7#
    """,
    "evergreen-1852": """
        e4 e5 Nf3 Nc6 Bc4 Bc5 b4 Bxb4 c3 Ba5 d4 exd4 O-O d3 Qb3 Qf6 e5 Qg6 Re1
        Nge7 Ba3 b5 Qxb5 Rb8 Qa4 Bb6 Nbd2 Bb7 Ne4 Qf5 Bxd3 Qh5 Nf6+ gxf6 exf6
        Rg8 Rad1 Qxf3 Rxe7+ Nxe7 Qxd7+ Kxd7 Bf5+ Ke8 Bd7+ Kf8 Bxe7#
    """,
    "opera-1858": """
        e4 e5 Nf3 d6 d4 Bg4 dxe5 Bxf3 Qxf3 dxe5 Bc4 Nf6 Qb3 Qe7 Nc3 c6 Bg5 b5
        Nxb5 cxb5 Bxb5+ Nbd7 O-O-O Rd8 Rxd7 Rxd7 Rd1 Qe6 Bxd7+ Nxd7 Qb8+ Nxb8
        Rd8#
    """,
    "steinitz-bardeleben-1895": """
        e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d4 exd4 cxd4 Bb4+ Nc3 d5 exd5 Nxd5 O-O
        Be6 Bg5 Be7 Bxd5 Bxd5 Nxd5 Qxd5 Bxe7 Nxe7 Re1 f6 Qe2 Qd7 Rac1 c6 d5
        cxd5 Nd4 Kf7 Ne6 Rhc8 Qg4 g6 Ng5+ Ke8 Rxe7+ Kf8 Rf7+ Kg8 Rg7+ Kh8
        Rxh7+
    """,
    "rotlewi-rubinstein-1907": """
        d4 d5 Nf3 e6 e3 c5 c4 Nc6 Nc3 Nf6 dxc5 Bxc5 a3 a6 b4 Bd6 Bb2 O-O Qd2
        Qe7 Bd3 dxc4 Bxc4 b5 Bd3 Rd8 Qe2 Bb7 O-O Ne5 Nxe5 Bxe5 f4 Bc7 e4 Rac8
        e5 Bb6+ Kh1 Ng4 Be4 Qh4 g3 Rxc3 gxh4 Rd2 Qxd2 Bxe4+ Qg2 Rh3
    """,
    "adams-torre-1920": """
        e4 e5 Nf3 d6 d4 exd4 Qxd4 Nc6 Bb5 Bd7 Bxc6 Bxc6 Nc3 Nf6 O-O Be7 Nd5
        Bxd5 exd5 O-O Bg5 c6 c4 cxd5 cxd5 Re8 Rfe1 a5 Re2 Rc8 Rae1 Qd7 Bxf6
        Bxf6 Qg4 Qb5 Qc4 Qd7 Qc7 Qb5 a4 Qxa4 Re4 Qb5 Qxb7
    """,
    "byrne-fischer-1956": """
        Nf3 Nf6 c4 g6 Nc3 Bg7 d4 O-O Bf4 d5 Qb3 dxc4 Qxc4 c6 e4 Nbd7 Rd1 Nb6
        Qc5 Bg4 Bg5 Na4 Qa3 Nxc3 bxc3 Nxe4 Bxe7 Qb6 Bc4 Nxc3 Bc5 Rfe8+ Kf1
        Be6 Bxb6 Bxc4+ Kg1 Ne2+ Kf1 Nxd4+ Kg1 Ne2+ Kf1 Nc3+ Kg1 axb6 Qb4 Ra4
        Qxb6 Nxd1 h3 Rxa2 Kh2 Nxf2 Re1 Rxe1 Qd8+ Bf8 Nxe1 Bd5 Nf3 Ne4 Qb8 b5
        h4 h5 Ne5 Kg7 Kg1 Bc5+ Kf1 Ng3+ Ke1 Bb4+ Kd1 Bb3+ Kc1 Ne2+ Kb1 Nc3+
        Kc1 Rc2#
    """,
    "spassky-bronstein-1960": """
        e4 e5 f4 exf4 Nf3 d5 exd5 Bd6 Nc3 Ne7 d4 O-O Bd3 Nd7 O-O h6 Ne4 Nxd5
        c4 Ne3 Bxe3 fxe3 c5 Be7 Bc2 Re8 Qd3 e2 Nd6 Nf8 Nxf7 exf1=Q+ Rxf1 Bf5
        Qxf5 Qd7 Qf4 Bf6 N3e5 Qe7 Bb3 Bxe5 Nxe5+ Kh7 Qe4+
    """,
    "fischer-spassky-1972-g6": """
        c4 e6 Nf3 d5 d4 Nf6 Nc3 Be7 Bg5 O-O e3 h6 Bh4 b6 cxd5 Nxd5 Bxe7 Qxe7
        Nxd5 exd5 Rc1 Be6 Qa4 c5 Qa3 Rc8 Bb5 a6 dxc5 bxc5 O-O Ra7 Be2 Nd7 Nd4
        Qf8 Nxe6 fxe6 e4 d4 f4 Qe7 e5 Rb8 Bc4 Kh8 Qh3 Nf8 b3 a5 f5 exf5 Rxf5
        Nh7 Rcf1 Qd8 Qg3 Re7 h4 Rbb7 e6 Rbc7 Qe5 Qe8 a4 Qd8 R1f2 Qe8 R2f3 Qd8
        Bd3 Qe8 Qe4 Nf6 Rxf6 gxf6 Rxf6 Kg8 Bc4 Kh8 Qf4
    """,
    "karpov-kasparov-1985-g16": """
        e4 c5 Nf3 e6 d4 cxd4 Nxd4 Nc6 Nb5 d6 c4 Nf6 Nc3 a6 Na3 d5 cxd5 exd5
        exd5 Nb4 Be2 Bc5 O-O O-O Bf3 Bf5 Bg5 Re8 Qd2 b5 Rad1 Nd3 Nab1 h6 Bh4
        b4 Na4 Bd6 Bg3 Rc8 b3 g5 Bxd6 Qxd6 g3 Nd7 Bg2 Qf6 a3 a5 axb4 axb4 Qa2
        Bg6 d6 g4 Qd2 Kg7 f3 Qxd6 fxg4 Qd4+ Kh1 Nf6 Rf4 Ne4 Qxd3 Nf2+ Rxf2
        Bxd3 Rfd2 Qe3 Rxd3 Rc1 Nb2 Qf2 Nd2 Rxd1+ Nxd1 Re1+
    """,
    "kasparov-topalov-1999": """
        e4 d6 d4 Nf6 Nc3 g6 Be3 Bg7 Qd2 c6 f3 b5 Nge2 Nbd7 Bh6 Bxh6 Qxh6 Bb7
        a3 e5 O-O-O Qe7 Kb1 a6 Nc1 O-O-O Nb3 exd4 Rxd4 c5 Rd1 Nb6 g3 Kb8 Na5
        Ba8 Bh3 d5 Qf4+ Ka7 Rhe1 d4 Nd5 Nbxd5 exd5 Qd6 Rxd4 cxd4 Re7+ Kb6
        Qxd4+ Kxa5 b4+ Ka4 Qc3 Qxd5 Ra7 Bb7 Rxb7 Qc4 Qxf6 Kxa3 Qxa6+ Kxb4 c3+
        Kxc3 Qa1+ Kd2 Qb2+ Kd1 Bf1 Rd2 Rd7 Rxd7 Bxc4 bxc4 Qxh8 Rd3 Qa8 c3 Qa4+
        Ke1 f4 f5 Kc1 Rd2 Qa7
    """,
    "morphy-paulsen-1857": """
        e4 e5 Nf3 Nc6 Nc3 Nf6 Bb5 Bc5 O-O O-O Nxe5 Re8 Nxc6 dxc6 Bc4 b5 Be2
        Nxe4 Nxe4 Rxe4 Bf3 Re6 c3 Qd3 b4 Bb6 a4 bxa4 Qxa4 Bd7 Ra2 Rae8 Qa6
        Qxf3 gxf3 Rg6+ Kh1 Bh3 Rd1 Bg2+ Kg1 Bxf3+ Kf1 Bg2+ Kg1 Bh3+ Kh1 Bxf2
        Qf1 Bxf1 Rxf1 Re2 Ra1 Rh6 d4 Be3
    """,
    "pillsbury-lasker-1896": """
        d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7 e3 O-O Rc1 b6 cxd5 exd5 Bd3 Bb7 Nf3 Nbd7
        O-O c5 Re1 c4 Bb1 a6 Ne5 b5 f4 Re8 Qf3 Nf8 Be3 Ne4 Qh3 Nxc3 bxc3 Qd6
        f5 b4 Rf1 bxc3 Nf3 Qd7 f6 g6 Ne5 Qb5
    """,
    "capablanca-marshall-1918": """
        e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 O-O c3 d5 exd5 Nxd5
        Nxe5 Nxe5 Rxe5 Nf6 Re1 Bd6 h3 Ng4 Qf3 Qh4 d4 Nxf2 Re2 Bg4 hxg4 Bh2+
        Kf1 Bg3 Rxf2 Qh1+ Ke2 Bxf2 Bd2 Bh4 Qh3 Rae8+ Kd3 Qf1+ Kc2 Bf2 Qf3 Qg1
        Bd5 c5 dxc5 Bxc5 b4 Bd6 a4 a5 axb5 axb4 Ra6 bxc3 Nxc3 Bb4 b6 Bxc3
        Bxc3 h6 b7 Re3 Bxf7+ Rxf7 b8=Q+ Kh7 Rxh6+ gxh6 Qxf7+
    """,
    "reti-alekhine-1925": """
        g3 e5 Nf3 e4 Nd4 d5 d3 exd3 Qxd3 Nf6 Bg2 Bb4+ Bd2 Bxd2+ Nxd2 O-O c4
        Na6 cxd5 Nb4 Qc4 Nbxd5 N2b3 c6 O-O Re8 Rfd1 Bg4 Rd2 Qc8 Nc5 Bh3 Bf3
        Bg4 Bg2 Bh3 Bf3 Bg4 Bg2 Bh3
    """,
    "tal-botvinnik-1960-g6": """
        e4 e6 d4 d5 Nc3 Bb4 e5 c5 a3 Bxc3+ bxc3 Qc7 Qg4 f5 Qg3 Ne7 Qxg7 Rg8
        Qxh7 cxd4 Ne2 Nbc6 f4 Bd7 Qd3 dxc3 Nxc3 a6 Rb1 O-O-O Be3 Nf5 Bf2 Rh8
        Qe2 Rdg8 g3 Na5 Bd3 Nc4 Kd1 Rh3 Bxf5 exf5 Ne2 Qa5 Bd4 Rh5
    """,
    "geller-euwe-1953": """
        d4 Nf6 c4 e6 Nc3 Bb4 e3 c5 a3 Bxc3+ bxc3 b6 Bd3 Bb7 f3 Nc6 Ne2 O-O
        e4 Ne8 Be3 d6 O-O Na5 Ng3 cxd4 cxd4 Rc8 f4 Nxc4 f5 f6 Rf4 b5 Rh4 Qb6
        e5 Nxe5 fxe6 Nd3 Qxd3 Rxc1+ Rxc1 Qxh4
    """,
    "bernstein-capablanca-1914": """
        d4 d5 c4 e6 Nc3 Nf6 Nf3 Be7 Bg5 O-O e3 b6 cxd5 exd5 Bd3 Bb7 O-O Nbd7
        Rc1 c5 Qe2 Ne4 Bxe7 Qxe7 Bxe4 dxe4 Qxe4 Qb4 Qxb4 cxb4 Ne2 Rfc8 Rfd1
        Rxc1 Rxc1 Rc8 Rxc8+ Bxc8 Nd2 Nf6 f3 Bf5 e4 Be6 Nc4 Bxc4 Rc1 Bd5
    """,
    "anderssen-zukertort-1865": """
        e4 e5 Nf3 Nc6 Bc4 Bc5 b4 Bxb4 c3 Ba5 d4 exd4 O-O Bb6 cxd4 d6 d5 Na5
        Bb2 Ne7 Bd3 O-O Nc3 Ng6 Ne2 c5 Qd2 f6 Kh1 Bc7 Rac1 Rb8 Ng3 b5 Nf5 b4
        Nh4 Nxh4 Nxh4 Qe8 Ng6 Rf7 Rg1 Kh8 Nf4 Qd8
    """,
    "smyslov-euwe-1948": """
        d4 Nf6 c4 e6 Nc3 Bb4 Qc2 d5 cxd5 exd5 Bg5 h6 Bh4 c5 dxc5 Nc6 e3 g5
        Bg3 Ne4 Nge2 Qa5 Rc1 Bf5 Qb3 Nxg3 hxg3 d4 exd4 Nxd4 Nxd4 Qxc5 Nxf5
        Qxf5 Bd3 Qg4 Rh5 Bxc3+ bxc3 O-O-O Qc2 Rd5 Ke2 Rhd8
    """,
    "nimzowitsch-capablanca-1927": """
        e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 Nf6 Nc3 d6 Be2 e6 O-O a6 Be3 Qc7 f4 Na5
        f5 Nc4 Bxc4 Qxc4 fxe6 fxe6 Rxf6 gxf6 Qh5+ Kd8 Qf7 Qe4 Nxe6+ Bxe6
        Qxe6 Rc8 Rf1 Qxe4 Bd4 Qd5 Rxf6 Qxe6 Rxe6 Bg7
    """,
    "polugaevsky-nezhmetdinov-1958": """
        d4 Nf6 c4 d6 Nc3 e5 e4 exd4 Qxd4 Nc6 Qd2 g6 b3 Bg7 Bb2 O-O Bd3 Ng4
        Nge2 Qh4 Ng3 Nge5 O-O f5 f3 Bh6 Qd1 f4 Nge2 g5 Nd5 g4 g3 fxg3 hxg3
        Qh3 f4 Be6 Bc2 Rf7 Kf2 Qh2+ Ke3 Bxd5 cxd5 Nb4
    """,
    "fischer-myagmarsuren-1967": """
        e4 e6 d3 d5 Nd2 Nf6 g3 c5 Bg2 Nc6 Ngf3 Be7 O-O O-O e5 Nd7 Re1 b5 Nf1
        b4 h4 a5 Bf4 a4 a3 bxa3 bxa3 Na5 Ne3 Ba6 Bh3 d4 Nf1 Nb6 Ng5 Nd5 Bd2
        Bxg5 Bxg5 Qd7 Qf3 Rfc8 Rd1 Nc3 bxc3 dxc3 Be1 c4 d4 cxd4 Bd2 Nc4 Qf4
        Nxd2 Rxd2 Rab8 Rc1 Qb5 Kh2 Qb1 Rcd1 Rb2 Rxb2 Qxb2 Bf1 Bxf1 Rxf1 Qc3
        Ng3 Rc7 Nf5 exf5 Qxf5 Qf3 Qxf3 Rb7 Qxf7+ Kh8 Qf8#
    """,
    "lasker-capablanca-1914": """
        e4 e5 Nf3 Nc6 Bb5 a6 Bxc6 dxc6 d4 exd4 Qxd4 Qxd4 Nxd4 Bd6 Nc3 Ne7 O-O
        O-O f4 Re8 Nb3 f6 f5 b6 Bf4 Bb7 Bxd6 cxd6 Rad1 Rad8 Rd2 Nc8 Rfd1 Rd7
        Ne2 Kf8 Nf4 g6 g4 Ne7 Kf2 h6 Kf3 Rd8 a4 Kg7 Nd4 Nc8 h4 Rd7 Rg1 Nb6 b3
        g5 hxg5 hxg5 Ne2 f6 Ne6+ Kf7 Nd8+
    """,
    "short-timman-1991": """
        Nf3 d5 d4 Nf6 c4 e6 Nc3 Be7 Bg5 h6 Bxf6 Bxf6 e3 O-O Rc1 c6 Bd3 Nd7
        O-O dxc4 Bxc4 e5 h3 exd4 exd4 Nb6 Bb3 Bf5 Re1 a5 a3 a4 Ba2 Bg6 d5
        Bxc3 Rxc3 cxd5 Nd4 Qf6 Rf3 Qd6 Nxf5 Qxf5 Rd1 Rfd8 Rxd5 Rxd5 Bxd5 Rd8
        Bb7 Nc4 Qe4
    """,
    "karpov-korchnoi-1974-g2": """
        e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6 Be3 Bg7 f3 Nc6 Bc4 O-O Qd2 Bd7
        O-O-O Rc8 Bb3 Ne5 h4 h5 Bg5 Rc5 Kb1 Re8 g4 hxg4 h5 Nxh5 Rxh5 gxh5
    """,
    "alekhine-bogoljubov-1922": """
        d4 f5 c4 Nf6 g3 e6 Bg2 Bb4+ Bd2 Bxd2+ Nxd2 Nc6 Ngf3 O-O O-O d6 Qb3
        Kh8 Qc3 e5 e3 a5 b3 Qe8 a3 Qh5 h4 Ng4 Ng5 Bd7 f3 Nf6 f4 e4 Rfd1 h6
        Nh3 d5 Nf1 Ne7 a4 Nc6 Rd2 Nb4 Bh1 Qe8 Rg2 dxc4 bxc4 Bxa4 Nf2 Bd7 Nd2
        b5 Nd1 Nd3 Rxa5 b4 Rxa8 bxc3 Rxe8 c2 Rxf8+ Kh7 Nf2 c1=Q+ Nf1 Ne1 Rh2
        Qxc4 Rb8 Bb5 Rxb5 Qxb5 g4 Nf3+ Bxf3 exf3 gxf5 Qe2 d5 Kg8 h5 Kh7 e4
        Nxe4 Nxe4 Qxe4 d6 cxd6 f6 gxf6 Rd2 Qe2 Rxe2 fxe2 Kf2 exf1=Q+ Kxf1
        Kg7 Kf2 Kf7 Ke3 Ke6 Ke4 d5+
    """,
    "botvinnik-capablanca-1938": """
        d4 Nf6 c4 e6 Nc3 Bb4 e3 d5 a3 Bxc3+ bxc3 c5 cxd5 exd5 Bd3 O-O Ne2 b6
        O-O Ba6 Bxa6 Nxa6 Bb2 Qd7 a4 Rfe8 Qd3 c4 Qc2 Nb8 Rae1 Nc6 Ng3 Na5 f3
        Nb3 e4 Qxa4 e5 Nd7 Qf2 g6 f4 f5 exf6 Nxf6 f5 Rxe1 Rxe1 Re8 Re6 Rxe6
        fxe6 Kg7 Qf4 Qe8 Qe5 Qe7 Ba3 Qxa3 Nh5+ gxh5 Qg5+ Kf8 Qxf6+ Kg8 e7
        Qc1+ Kf2 Qc2+ Kg3 Qd3+ Kh4 Qe4+ Kxh5 Qe2+ Kh4 Qe4+ g4 Qe1+ Kh5 Qe2
    """,
    "petrosian-spassky-1966-g10": """
        d4 Nf6 c4 g6 g3 Bg7 Bg2 O-O Nc3 d6 Nf3 Nbd7 O-O e5 e4 c6 h3 Qb6 Re1
        exd4 Nxd4 Re8 Nc2 a5 Rb1 a4 Be3 Qa6 b3 axb3 axb3 Nc5 b4 Ncd7 Qd2 b5
    """,
}


def _parse_classic(name: str, text: str) -> SynthGame:
    board = chess.Board()
    moves: list[chess.Move] = []
    truncated = False
    for token in text.split():
        if token in {"1-0", "0-1", "1/2-1/2", "*"}:
            break
        try:
            mv = board.parse_san(token)
        except (ValueError, AssertionError):
            truncated = True
            break
        board.push(mv)
        moves.append(mv)
        if len(moves) >= MAX_PLIES:
            break
    return SynthGame(name=name, moves=moves, source="classic", truncated=truncated,
                     features=game_features(moves))


_CLASSICS: list[SynthGame] | None = None


def classic_games() -> list[SynthGame]:
    """Validated classic games, longest-usable first. Cached."""
    global _CLASSICS
    if _CLASSICS is None:
        games = [_parse_classic(n, t) for n, t in CLASSIC_PGNS.items()]
        _CLASSICS = [g for g in games if len(g) >= MIN_PLIES]
    return _CLASSICS


# --------------------------------------------------------------------------
# Feature detection
# --------------------------------------------------------------------------

def game_features(moves: list[chess.Move]) -> frozenset[str]:
    """Which interesting rules a move list exercises."""
    board = chess.Board()
    found: set[str] = set()
    for mv in moves:
        if board.is_castling(mv):
            found.add("castling")
            found.add("kingside" if chess.square_file(mv.to_square) > 4 else "queenside")
        if board.is_en_passant(mv):
            found.add("en_passant")
        if mv.promotion:
            found.add("promotion")
            if mv.promotion != chess.QUEEN:
                found.add("underpromotion")
        if board.is_capture(mv):
            found.add("capture")
        board.push(mv)
    if board.is_checkmate():
        found.add("checkmate")
    return frozenset(found)


# --------------------------------------------------------------------------
# Weighted random playouts
# --------------------------------------------------------------------------

_PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def _move_weight(board: chess.Board, mv: chess.Move, bias: dict[str, float]) -> float:
    w = 1.0
    if board.is_castling(mv):
        w *= bias.get("castle", 6.0)
    if mv.promotion:
        w *= bias.get("promote", 30.0)
        if mv.promotion != chess.QUEEN:
            w *= bias.get("underpromote", 1.0)
    if board.is_en_passant(mv):
        w *= bias.get("en_passant", 40.0)
    piece = board.piece_at(mv.from_square)
    if piece is not None and piece.piece_type == chess.PAWN:
        w *= bias.get("pawn_push", 1.8)
        # Reward pawns that are close to promoting, so playouts actually get there.
        rank = chess.square_rank(mv.to_square)
        adv = rank if piece.color == chess.WHITE else 7 - rank
        if adv >= 4:
            w *= 1.0 + bias.get("advance", 1.2) * (adv - 3)
    if board.is_capture(mv):
        victim = board.piece_at(mv.to_square)
        val = _PIECE_VALUE[victim.piece_type] if victim else 1
        w *= bias.get("capture", 1.5) + 0.25 * val
    if piece is not None and piece.piece_type == chess.KING and not board.is_castling(mv):
        w *= bias.get("king_move", 0.25)
    return w


def playout(rng: random.Random, *, max_plies: int = MAX_PLIES,
            min_plies: int = MIN_PLIES, bias: dict[str, float] | None = None,
            name: str = "playout") -> SynthGame:
    """One weighted-random legal game."""
    bias = bias or {}
    board = chess.Board()
    moves: list[chess.Move] = []
    target = rng.randint(min_plies, max_plies)
    while len(moves) < target and not board.is_game_over(claim_draw=False):
        legal = list(board.legal_moves)
        weights = [_move_weight(board, m, bias) for m in legal]
        mv = rng.choices(legal, weights=weights, k=1)[0]
        board.push(mv)
        moves.append(mv)
    return SynthGame(name=name, moves=moves, source="playout",
                     features=game_features(moves))


def targeted_playout(rng: random.Random, wanted: set[str], *, tries: int = 400,
                     name: str = "playout", **kw) -> SynthGame:
    """Draw playouts until every feature in ``wanted`` appears (best effort)."""
    bias = dict(kw.pop("bias", None) or {})
    if "underpromotion" in wanted:
        bias.setdefault("underpromote", 3.0)
    best: SynthGame | None = None
    for i in range(tries):
        g = playout(rng, bias=bias, name=name, **kw)
        if wanted <= g.features and len(g) >= MIN_PLIES:
            return g
        score = len(wanted & g.features)
        if best is None or score > len(wanted & best.features):
            best = g
    assert best is not None
    return best


# --------------------------------------------------------------------------
# Corpus assembly
# --------------------------------------------------------------------------

def build_corpus(n: int, rng: random.Random) -> list[SynthGame]:
    """``n`` games: the guaranteed-feature ones first, then classics, then playouts.

    Index 0 always contains castling *and* promotion, so any corpus of any size
    satisfies the Phase 2 gate on its own.
    """
    games: list[SynthGame] = []

    seed_game = targeted_playout(
        rng, {"castling", "promotion", "en_passant", "capture"},
        name="feature-castle-promote-ep")
    games.append(seed_game)
    if n > 1:
        games.append(targeted_playout(
            rng, {"castling", "underpromotion"}, name="feature-underpromotion"))

    for g in classic_games():
        if len(games) >= n:
            break
        games.append(g)

    i = 0
    while len(games) < n:
        games.append(playout(rng, name=f"playout-{i:03d}"))
        i += 1

    return games[:n]
