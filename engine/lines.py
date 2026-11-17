"""The likely moves between two confirmed positions.

Given position A, a board B, and the number of plies ``n`` the clock counted
between them, find move sequences of exactly ``n`` legal plies from A that end
on B's placement (with the side to move that ``n`` implies), and rank them by
how likely the two players are to play them: the product of per-move
probabilities from :class:`engine.humanlike.Mover`.

Method (see ``engine/README.md`` → *Lines*):

* **Search.** Best-first over partial paths with priority
  ``f = g + c · plies_left + λ · distance``, where ``g = −Σ log p`` is the
  path's cost so far, ``distance`` the lower bound on moves both sides still
  need to reach B (below), and ``c``, ``λ`` constants fitted on real games
  before the cut-off. ``c = λ = 0`` is uniform-cost search (exact order, far
  too slow past ~8 plies); ``c`` pushes deeper paths first, ``λ`` prefers
  prefixes that are closer to B. A position reached by different move orders is
  expanded once (one network call) but may be popped up to ``top`` times, so
  the ``top`` best distinct lines through it survive (k-shortest paths).
* **Pruning.** A child is dropped when a lower bound on the moves each side
  still needs to turn it into B exceeds the moves that side has left. The bound
  is admissible (never too high): for each piece type, a least-cost matching of
  B's squares to distinct current pieces (or promotions) at their empty-board
  move distance (knight BFS, bishop 1/2 and never across colours, rook and
  queen 1/2, king steps, castling as one king move that carries the rook); a
  pawn matching where pawns only advance and change file only by capturing;
  captures needed to remove the material B lacks; and a side already exactly
  on B cannot spend a single move.
* **Answer.** Found lines are normalised among themselves; per-ply marginals
  are the probability-weighted share of lines playing each move at that ply.
  If ``n`` yields nothing, ``n − 1`` and ``n + 1`` are tried (a photo taken
  after a move but before the press). Everything is time-boxed; the result says
  whether the search finished or gave up.

The castling rights and en-passant square of each line's final FEN come from
the path, not from the photo.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import chess

from engine.humanlike import fast_key

SOLVER_CONFIG = Path(__file__).with_name("solver.json")
PIECES = (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
Policy = Callable[[chess.Board], dict[str, float]]


def load_solver_config() -> dict:
    if SOLVER_CONFIG.exists():
        return json.loads(SOLVER_CONFIG.read_text())
    return {"c": 1.0, "lam": 1.0, "top": 5, "time_box": 30.0, "width": 1000,
            "astar_budget": 2000, "astar_max_gap": 10, "beam_min_p": 0.0, "main_share": 0.7}


# ------------------------------------------------------------------- bound

INF = 1 << 20


def _knight_table() -> list[list[int]]:
    table = []
    for a in chess.SQUARES:
        d = [INF] * 64
        d[a] = 0
        frontier = [a]
        while frontier:
            nxt = []
            for sq in frontier:
                for to in chess.scan_forward(chess.BB_KNIGHT_ATTACKS[sq]):
                    if d[to] == INF:
                        d[to] = d[sq] + 1
                        nxt.append(to)
            frontier = nxt
        table.append(d)
    return table


_KNIGHT = _knight_table()


def _piece_distance(pt: int, a: int, b: int) -> int:
    """Moves a lone piece needs from ``a`` to ``b`` on an empty board."""
    if a == b:
        return 0
    if pt == chess.KNIGHT:
        return _KNIGHT[a][b]
    if pt == chess.KING:
        return chess.square_distance(a, b)
    fa, ra, fb, rb = chess.square_file(a), chess.square_rank(a), chess.square_file(b), chess.square_rank(b)
    straight = fa == fb or ra == rb
    diagonal = abs(fa - fb) == abs(ra - rb)
    if pt == chess.ROOK:
        return 1 if straight else 2
    if pt == chess.QUEEN:
        return 1 if straight or diagonal else 2
    if pt == chess.BISHOP:
        if (fa + ra) % 2 != (fb + rb) % 2:
            return INF
        return 1 if diagonal else 2
    raise ValueError(pt)


class Bound:
    """Lower bounds on the moves each side needs to reach a target placement."""

    def __init__(self, target: chess.Board):
        self.target = target
        self.placement = target.board_fen()
        self._masks = self._mask_key(target)
        self.t = {(c, pt): target.pieces_mask(pt, c) for c in chess.COLORS
                  for pt in chess.PIECE_TYPES}
        self._n_tgt = {c: {pt: self.t[(c, pt)].bit_count() for pt in chess.PIECE_TYPES}
                       for c in chess.COLORS}
        self.t_king = {c: target.king(c) for c in chess.COLORS}
        self._pawn_cache: dict[tuple[bool, int], tuple[int, int] | None] = {}
        self._piece_cache: dict[tuple, int | None] = {}
        self._side_cache: dict[tuple, tuple | None] = {}
        self._n_tgt_list = {c: [self._n_tgt[c][pt] for pt in chess.PIECE_TYPES]
                            for c in chess.COLORS}

    @staticmethod
    def _mask_key(b: chess.Board) -> tuple:
        return (b.pawns, b.knights, b.bishops, b.rooks, b.queens, b.kings,
                b.occupied_co[chess.WHITE], b.occupied_co[chess.BLACK])

    def matches(self, board: chess.Board) -> bool:
        return self._mask_key(board) == self._masks

    def _pawns(self, color: bool, cur: int) -> tuple[int, int] | None:
        """(min pawn moves, min file shifts) over matchings of target pawns to
        current pawns, or None when no matching exists."""
        key = (color, cur)
        if key in self._pawn_cache:
            return self._pawn_cache[key]
        cs = list(chess.scan_forward(cur))
        ts = list(chess.scan_forward(self.t[(color, chess.PAWN)]))
        start_rank = 1 if color == chess.WHITE else 6
        cost = []
        for t in ts:
            row = []
            tr, tf = chess.square_rank(t), chess.square_file(t)
            for c in cs:
                cr, cf = chess.square_rank(c), chess.square_file(c)
                dr = (tr - cr) if color == chess.WHITE else (cr - tr)
                df = abs(tf - cf)
                if dr < 0 or df > dr:
                    row.append(None)
                    continue
                moves = dr
                if cr == start_rank and dr - df >= 2:
                    moves = dr - 1           # one double step
                row.append((moves, df))
            cost.append(row)
        res = []
        for idx in (0, 1):
            dp = {0: 0}
            for row in cost:
                nxt: dict[int, int] = {}
                for mask, v in dp.items():
                    for j, cell in enumerate(row):
                        if cell is None or mask >> j & 1:
                            continue
                        m2, v2 = mask | 1 << j, v + cell[idx]
                        if v2 < nxt.get(m2, 1 << 30):
                            nxt[m2] = v2
                dp = nxt
                if not dp:
                    break
            if not dp:
                self._pawn_cache[key] = None
                return None
            res.append(min(dp.values()))
        out = (res[0], res[1])
        self._pawn_cache[key] = out
        return out

    def _piece_cost(self, color: bool, pt: int, cur: int, spare: int,
                    rights: int) -> int | None:
        """Least moves for this side's pieces of type ``pt`` to occupy every target
        square of that type, each target by a distinct piece (or by a promotion,
        at most ``spare`` of them), ignoring blockers. None when impossible."""
        key = (color, pt, cur, spare, rights)
        if key in self._piece_cache:
            return self._piece_cache[key]
        targets = list(chess.scan_forward(self.t[(color, pt)]))
        sources = list(chess.scan_forward(cur))
        home = 0 if color == chess.WHITE else 7
        castles = []
        if pt in (chess.KING, chess.ROOK):
            for r_file, k_to, r_to in ((7, 6, 5), (0, 2, 3)):
                rook_sq = chess.square(r_file, home)
                if rights & chess.BB_SQUARES[rook_sq]:
                    castles.append((rook_sq, chess.square(k_to, home), chess.square(r_to, home)))

        def dist(a: int, b: int) -> int:
            d = _piece_distance(pt, a, b)
            for rook_sq, k_to, r_to in castles:      # castling: the king's move carries the rook
                if pt == chess.KING and a == chess.square(4, home):
                    d = min(d, 1 + _piece_distance(pt, k_to, b))
                if pt == chess.ROOK and a == rook_sq:
                    d = min(d, _piece_distance(pt, r_to, b))
            return d

        promo_rank = 7 if color == chess.WHITE else 0
        cost = []
        for t in targets:
            row = [dist(src, t) for src in sources]
            promo = INF
            if spare and pt != chess.KING:
                promo = 1 + min(_piece_distance(pt, chess.square(f, promo_rank), t)
                                for f in range(8))
            cost.append((row, promo))
        dp = {0: 0}           # mask of real sources used → cost; promos used = i − popcount
        for i, (row, promo) in enumerate(cost):
            nxt: dict[int, int] = {}
            for mask, v in dp.items():
                for j, d in enumerate(row):
                    if d < INF and not mask >> j & 1:
                        m2 = mask | 1 << j
                        if v + d < nxt.get(m2, INF):
                            nxt[m2] = v + d
                if promo < INF and i - mask.bit_count() < spare:
                    if v + promo < nxt.get(mask, INF):
                        nxt[mask] = v + promo
            dp = nxt
            if not dp:
                break
        out = min(dp.values()) if dp else None
        self._piece_cache[key] = out
        return out

    def _side(self, color: bool, cur: tuple, rights: int):
        """(moves needed, pieces lost, pawn file shifts) for one side, memoised on
        that side's own bitboards: a move leaves the other side's unchanged."""
        key = (color, cur, rights)
        hit = self._side_cache.get(key, False)
        if hit is not False:
            return hit
        out = None
        n_cur = [m.bit_count() for m in cur]          # by PIECE_TYPES order
        n_tgt = self._n_tgt_list[color]
        spare = n_cur[0] - n_tgt[0]
        if spare >= 0:
            promos = sum(max(0, n_tgt[i] - n_cur[i]) for i in (1, 2, 3, 4))
            lost = sum(n_cur) - sum(n_tgt)
            if promos <= spare and lost >= 0:
                total = 0
                for i, pt in ((5, chess.KING), (1, chess.KNIGHT), (2, chess.BISHOP),
                              (3, chess.ROOK), (4, chess.QUEEN)):
                    pc = self._piece_cost(color, pt, cur[i], spare, rights)
                    if pc is None:
                        total = None
                        break
                    total += pc
                if total is not None:
                    pw = self._pawns(color, cur[0])
                    if pw is not None:
                        out = (pw[0] + total, lost, pw[1])
        self._side_cache[key] = out
        return out

    def need(self, board: chess.Board) -> tuple[int, int] | None:
        """(moves White needs, moves Black needs), or None if B is unreachable."""
        res = []
        for color, rank in ((chess.WHITE, chess.BB_RANK_1), (chess.BLACK, chess.BB_RANK_8)):
            occ = board.occupied_co[color]
            cur = (board.pawns & occ, board.knights & occ, board.bishops & occ,
                   board.rooks & occ, board.queens & occ, board.kings & occ)
            r = self._side(color, cur, board.castling_rights & rank)
            if r is None:
                return None
            res.append(r)
        (lw, cw, sw), (lb_, cb, sb) = res
        if sw > cb or sb > cw:          # a pawn changes file only by capturing
            return None
        return max(lw, cb), max(lb_, cw)

    def _in_place(self, board: chess.Board, color: bool) -> bool:
        return all(board.pieces_mask(pt, color) == self.t[(color, pt)]
                   for pt in chess.PIECE_TYPES)

    def distance(self, board: chess.Board, plies_left: int) -> int | None:
        """Moves both sides still need (a lower bound), or None if they cannot
        make them in ``plies_left`` plies with ``board.turn`` to move."""
        need = self.need(board)
        if need is None:
            return None
        mover = board.turn
        have = {mover: (plies_left + 1) // 2, not mover: plies_left // 2}
        for color, k in ((chess.WHITE, need[0]), (chess.BLACK, need[1])):
            if k > have[color]:
                return None
            # a side already exactly on B cannot spend a single move: any move
            # changes its own placement, and getting back takes another
            if have[color] == 1 and self._in_place(board, color):
                return None
        return need[0] + need[1]

    def feasible(self, board: chess.Board, plies_left: int) -> bool:
        return self.distance(board, plies_left) is not None


# ------------------------------------------------------------------ search

@dataclass
class Line:
    moves: list[str]
    logp: float
    fen_end: str
    prob: float = 0.0

    def to_json(self, start: chess.Board) -> dict:
        b = start.copy(stack=False)
        san = []
        for u in self.moves:
            mv = chess.Move.from_uci(u)
            san.append(b.san(mv))
            b.push(mv)
        return {"moves": self.moves, "san": san, "logp": round(self.logp, 4),
                "prob": round(self.prob, 6), "fen_end": self.fen_end}


@dataclass
class Result:
    fen_a: str
    placement_b: str
    n_requested: int
    n_used: int | None = None
    status: str = "none"            # found | none | timeout
    complete: bool = False          # search finished (top lines found or space exhausted)
    lines: list[Line] = field(default_factory=list)
    marginals: list[list[tuple[str, float]]] = field(default_factory=list)
    expansions: int = 0
    secs: float = 0.0
    tried: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        start = chess.Board(self.fen_a)
        marg = []
        b = start.copy(stack=False)
        top = self.lines[0].moves if self.lines else []
        for i, ply in enumerate(self.marginals):
            row = []
            for u, p in ply:
                try:
                    s = b.san(chess.Move.from_uci(u))
                except Exception:       # the move belongs to another line's board
                    s = None
                row.append({"uci": u, "san": s, "p": round(p, 6)})
            marg.append(row)
            if i < len(top):
                b.push(chess.Move.from_uci(top[i]))
        return {"fen_a": self.fen_a, "placement_b": self.placement_b,
                "n_requested": self.n_requested, "n_used": self.n_used,
                "status": self.status, "complete": self.complete,
                "lines": [ln.to_json(start) for ln in self.lines],
                "marginals": marg, "expansions": self.expansions,
                "secs": round(self.secs, 3), "tried": self.tried}


def _search(start: chess.Board, bound: Bound, n: int, policy: Policy, *, top: int,
            c: float, lam: float, deadline: float, max_expansions: int | None):
    """Up to ``top`` lines of exactly ``n`` plies; (lines, complete, expansions)."""
    if n == 0:
        ok = bound.matches(start)
        return ([Line([], 0.0, start.fen())] if ok else []), True, 0
    d0 = bound.distance(start, n)
    if d0 is None:
        return [], True, 0
    tie = itertools.count()
    heap = [(c * n + lam * d0, next(tie), 0.0, 0, start, ())]
    pops: dict[tuple[str, int], int] = {}
    found: list[Line] = []
    expansions = 0
    while heap:
        if time.monotonic() > deadline or (max_expansions and expansions >= max_expansions):
            return found, False, expansions
        f, _, g, depth, board, moves = heapq.heappop(heap)
        if depth == n:
            found.append(Line(list(moves), -g, board.fen()))
            if len(found) >= top:
                return found, True, expansions
            continue
        key = (fast_key(board), depth)
        k = pops.get(key, 0)
        if k >= top:
            continue
        pops[key] = k + 1
        pol = policy(board)
        expansions += 1
        left = n - depth - 1
        for uci, p in pol.items():
            child = board.copy(stack=False)
            child.push(chess.Move.from_uci(uci))
            if left == 0:
                if not bound.matches(child):
                    continue
                dist = 0
            else:
                dist = bound.distance(child, left)
                if dist is None:
                    continue
            g2 = g - math.log(p)
            heapq.heappush(heap, (g2 + c * left + lam * dist, next(tie), g2, depth + 1,
                                  child, moves + (uci,)))
    return found, True, expansions


def _beam(start: chess.Board, bound: Bound, n: int, policy: Policy, *, top: int,
          width: int, lam: float, deadline: float, prefetch=None, min_p: float = 0.0,
          div: float = 0.0, trace: list | None = None, safety: float = 0.6,
          grace: float = 3.0, finish_width: int = 30):
    """Depth-synchronous beam: every ply keeps the ``width`` positions with the
    lowest ``g + λ·distance`` (best path's ``g``), each with its ``top`` best
    paths; a share ``div`` of the width goes instead to the positions closest
    to B (fewest moves still needed, then ``g``), so a reachable B is not lost
    to likelier positions that drift away from it. Width shrinks if the
    remaining time would not reach ply ``n`` (planned on ``safety`` of the
    measured rate). Past the deadline the last plies run on the
    ``finish_width`` positions nearest B, up to ``grace`` seconds more.
    Complete only if nothing was cut."""
    hard = deadline + grace
    layer = {fast_key(start): (start, [(0.0, ())])}
    evals = 0
    pruned = False
    cand: dict[str, list] = {}
    for depth in range(n):
        left = n - depth - 1
        t_layer = time.monotonic()
        cand = {}
        late = time.monotonic() > deadline
        if late:
            # past the deadline: finish the remaining plies on the few positions
            # closest to B rather than return nothing
            pruned = True
            keep = sorted(layer.items(), key=lambda kv: (bound.distance(kv[1][0], left + 1) or 0,
                                                         kv[1][1][0][0]))[:finish_width]
            layer = dict(keep)
        if prefetch is not None:
            boards = [b for b, _ in layer.values()]
            for i in range(0, len(boards), 256):
                if time.monotonic() > hard:
                    return [], False, evals
                prefetch(boards[i:i + 256])
        for board, paths in layer.values():
            now = time.monotonic()
            if now > hard:
                return [], False, evals
            if not late and now > deadline and cand:
                pruned = True
                break          # keep the children gathered so far
            pol = policy(board)
            evals += 1
            for uci, p in pol.items():
                if p < min_p:
                    continue
                child = board.copy(stack=False)
                child.push(chess.Move.from_uci(uci))
                if left == 0:
                    if not bound.matches(child):
                        continue
                    dist = 0
                else:
                    dist = bound.distance(child, left)
                    if dist is None:
                        continue
                ck = fast_key(child)
                e = cand.get(ck)
                if e is None:
                    e = cand[ck] = [child, [], dist]
                lp = -math.log(p)
                e[1].extend((g + lp, mv + (uci,)) for g, mv in paths)
        for e in cand.values():
            e[1].sort()
            del e[1][top:]
        if left == 0 or not cand:
            break
        per_state = (time.monotonic() - t_layer) / max(1, len(layer))
        affordable = safety * (deadline - time.monotonic()) / left / max(per_state, 1e-4)
        floor = finish_width if time.monotonic() > deadline else 1
        w = max(floor, min(width, int(affordable)))
        if len(cand) > w:
            pruned = True
            n_div = int(w * div)
            ranked = sorted(cand.items(), key=lambda kv: kv[1][1][0][0] + lam * kv[1][2])
            layer = {k: (e[0], e[1]) for k, e in ranked[:w - n_div]}
            if n_div:
                near = sorted((kv for kv in ranked[w - n_div:]),
                              key=lambda kv: (kv[1][2], kv[1][1][0][0]))[:n_div]
                layer.update((k, (e[0], e[1])) for k, e in near)
        else:
            layer = {k: (e[0], e[1]) for k, e in cand.items()}
        if trace is not None:
            trace.append({"depth": depth + 1, "cand": len(cand), "kept": len(layer),
                          "keys": set(layer)})
    lines = []
    for board, paths, _ in cand.values():
        if bound.matches(board):
            for g, mv in paths:
                b = start.copy(stack=False)
                for u in mv:
                    b.push(chess.Move.from_uci(u))
                lines.append(Line(list(mv), -g, b.fen()))
    lines.sort(key=lambda ln: -ln.logp)
    return lines[:top], not pruned, evals


def _find(start: chess.Board, bound: Bound, n: int, policy: Policy, *, top: int, c: float,
          lam: float, width: int, astar_budget: int, deadline: float,
          max_expansions: int | None, prefetch=None, astar_max_gap: int = 10,
          min_p: float = 0.0, div: float = 0.0):
    """Best-first with a capped budget (exact and fast on short gaps); if it does
    not finish, or the gap is longer than ``astar_max_gap``, a beam search with
    the time that is left."""
    if n > astar_max_gap:
        lines, complete, exp = [], False, 0
        return _beam_merge(start, bound, n, policy, top, width, lam, deadline, prefetch,
                           min_p, lines, exp, div)
    budget = min(astar_budget, max_expansions) if max_expansions else astar_budget
    lines, complete, exp = _search(start, bound, n, policy, top=top, c=c, lam=lam,
                                   deadline=deadline, max_expansions=budget)
    if complete or (max_expansions and exp >= max_expansions):
        return lines, complete, exp
    return _beam_merge(start, bound, n, policy, top, width, lam, deadline, prefetch, min_p,
                       lines, exp, div)


def _beam_merge(start, bound, n, policy, top, width, lam, deadline, prefetch, min_p,
                lines, exp, div=0.0):
    blines, bcomplete, bexp = _beam(start, bound, n, policy, top=top, width=width, lam=lam,
                                    deadline=deadline, prefetch=prefetch, min_p=min_p,
                                    div=div)
    seen, merged = set(), []
    for ln in sorted(lines + blines, key=lambda ln: -ln.logp):
        if tuple(ln.moves) not in seen:
            seen.add(tuple(ln.moves))
            merged.append(ln)
    return merged[:top], bcomplete, exp + bexp


def solve(fen_a: str, b: str | chess.Board, n: int, policy: Policy, *, top: int | None = None,
          c: float | None = None, lam: float | None = None, time_box: float | None = None,
          width: int | None = None, max_expansions: int | None = None,
          neighbours: bool = True, prefetch=None, config: dict | None = None) -> Result:
    """Likely lines of ``n`` plies from ``fen_a`` to board ``b`` (FEN or placement).

    ``policy(board)`` gives move probabilities; ``prefetch(boards)``, if given,
    evaluates a batch in parallel first (``Mover.prefetch``)."""
    cfg = {**load_solver_config(), **(config or {})}
    top = top or cfg["top"]
    c = cfg["c"] if c is None else c
    lam = cfg.get("lam", 0.0) if lam is None else lam
    time_box = cfg["time_box"] if time_box is None else time_box
    width = width or cfg.get("width", 1000)
    astar_budget = cfg.get("astar_budget", 2000)
    astar_max_gap = cfg.get("astar_max_gap", 10)
    min_p = cfg.get("beam_min_p", 0.0)
    start = chess.Board(fen_a)
    if isinstance(b, chess.Board):
        target = b.copy(stack=False)
    else:
        target = chess.Board(None)
        target.set_board_fen(b.split()[0])
    bound = Bound(target)
    t0 = time.monotonic()
    deadline = t0 + time_box
    res = Result(fen_a=start.fen(), placement_b=bound.placement, n_requested=n)
    plan = [n] + ([m for m in (n - 1, n + 1) if m >= 0] if neighbours else [])
    for i, m in enumerate(plan):
        if i > 0 and res.lines:
            break
        if time.monotonic() > deadline:
            break
        # the clock's count gets most of the box; the ±1 retries share the rest
        dl = t0 + time_box * cfg.get("main_share", 0.7) if (i == 0 and neighbours) else deadline
        lines, complete, exp = _find(start, bound, m, policy, top=top, c=c, lam=lam,
                                     width=width, astar_budget=astar_budget, deadline=dl,
                                     max_expansions=max_expansions, prefetch=prefetch,
                                     astar_max_gap=astar_max_gap, min_p=min_p,
                                     div=cfg.get("beam_div", 0.0))
        res.expansions += exp
        res.tried.append({"n": m, "lines": len(lines), "complete": complete, "expansions": exp})
        if lines:
            res.lines, res.n_used, res.complete = lines, m, complete
        elif i == 0:
            res.complete = complete
    res.secs = time.monotonic() - t0
    if res.lines:
        res.status = "found"
        res.lines.sort(key=lambda ln: -ln.logp)
        best = res.lines[0].logp
        w = [math.exp(ln.logp - best) for ln in res.lines]
        z = sum(w)
        for ln, wi in zip(res.lines, w):
            ln.prob = wi / z
        res.marginals = []
        for j in range(res.n_used):
            acc: dict[str, float] = {}
            for ln in res.lines:
                acc[ln.moves[j]] = acc.get(ln.moves[j], 0.0) + ln.prob
            res.marginals.append(sorted(acc.items(), key=lambda kv: -kv[1]))
    else:
        res.status = "none" if all(t["complete"] for t in res.tried) and res.tried else "timeout"
    return res


def main(argv=None) -> None:
    from engine.humanlike import Maia, for_game
    ap = argparse.ArgumentParser(description="Likely lines between two positions")
    ap.add_argument("fen_a")
    ap.add_argument("fen_b", help="FEN or placement of the later position")
    ap.add_argument("n", type=int, help="plies between them")
    ap.add_argument("--white", type=float, default=1350, help="chess.com rating")
    ap.add_argument("--black", type=float, default=1350)
    ap.add_argument("--white-name", default="")
    ap.add_argument("--black-name", default="")
    ap.add_argument("--top", type=int)
    ap.add_argument("--time-box", type=float)
    a = ap.parse_args(argv)
    mover = for_game(a.white, a.black, white_name=a.white_name, black_name=a.black_name,
                     maia=Maia(workers=4))
    res = solve(a.fen_a, a.fen_b, a.n, mover.policy, top=a.top, time_box=a.time_box,
                prefetch=mover.prefetch)
    print(json.dumps(res.to_json(), indent=1))


if __name__ == "__main__":
    main()
