"""Scoring one frame transition against every move it could have been.

The tracker never asks "what piece is this". It asks "given that the position
was *this*, which legal move would produce the change I can see" — and because
the position is tracked, it knows the type of every piece on the board, and
therefore how tall each one is and where in the image it stands. So the question
is answered forwards: each candidate **predicts** a region of the image that
should have changed, and the score is how well that prediction matches.

Predicting is what makes a low camera survivable. A piece that moves out from
behind a king produces change only where the king was not; a piece that moves
*behind* a king produces almost none. Both facts fall out of projecting the two
silhouettes and letting the nearer one occlude the further one, and neither can
be recovered by reasoning backwards from the change region to a square, which is
what the spec's original footprint model tried to do (see Amendments).

"How well" is one likelihood ratio, per cell of a coarse grid. A cell changes
with probability ``q`` where the candidate predicts it and ``q0`` where it does
not, so a predicted cell that lit up is evidence for, a predicted cell that
stayed quiet is evidence against, and every cell the candidate says nothing
about drops out of the comparison entirely. That last property is what makes
the shallow setup workable: most of the change in a frame is forearms and the
pile of captured pieces, no candidate predicts any of it, and under a ratio it
cancels instead of being charged to everyone in units nobody can interpret.

``q0`` is measured per frame rather than fixed, because the ambient change rate
swings by an order of magnitude between a quiet frame and one with a hand across
the board. The sum is tempered by ``nu``: sixteen pixels that morphological
closing has already merged into one blob are not sixteen independent trials, and
without the discount the likelihood is wildly overconfident.

An occupancy term over squares the tracked position says are unobstructed is
also implemented, and currently weighted zero — it measured negative value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import chess
import numpy as np

from .features import PIECE_H, SilhouetteBank

NULL = "null"


@dataclass(frozen=True)
class Candidate:
    """One hypothesis for what happened between two frames."""

    moves: tuple[chess.Move, ...]              # 0, 1 or 2 moves
    changed: tuple[tuple[int, int], ...]       # (file, rank) squares that differ
    gone: tuple[tuple[int, int, int], ...]     # (file, rank, piece_type) removed
    came: tuple[tuple[int, int, int], ...]     # (file, rank, piece_type) added
    carried: tuple[tuple[int, int], ...]       # (index into gone, index into came)
    kind: str                                  # "null" | "move" | "pair"

    @property
    def n_plies(self) -> int:
        return len(self.moves)


def _fr(square: int) -> tuple[int, int]:
    return chess.square_file(square), chess.square_rank(square)


def _move_delta(board: chess.Board, move: chess.Move) -> list:
    """``(square, piece_before, piece_after)`` for every square one move touches.

    Worked out from the move rather than by pushing it and diffing two piece
    maps. The diff is correct and costs a board copy and two full map builds per
    candidate; with a wide beam that is most of the running time, and there are
    only four kinds of square a move can touch.
    """
    piece = board.piece_at(move.from_square)
    if piece is None:                     # should not happen for a legal move
        return []
    out = [(move.from_square, piece, None)]

    if board.is_en_passant(move):
        taken = move.to_square + (-8 if piece.color == chess.WHITE else 8)
        out.append((taken, board.piece_at(taken), None))

    arrives = (chess.Piece(move.promotion, piece.color) if move.promotion else piece)
    out.append((move.to_square, board.piece_at(move.to_square), arrives))

    if board.is_castling(move):
        rank = chess.square_rank(move.from_square)
        if board.is_kingside_castling(move):
            rook_from, rook_to = chess.square(7, rank), chess.square(5, rank)
        else:
            rook_from, rook_to = chess.square(0, rank), chess.square(3, rank)
        rook = board.piece_at(rook_from)
        out.append((rook_from, rook, None))
        out.append((rook_to, board.piece_at(rook_to), rook))
    return out


def transition(board: chess.Board, moves, after: chess.Board | None = None):
    """What leaves the board, what arrives, and which of those are the same piece.

    The last part is what stops a capture being mistaken for a non-event. Where
    a piece merely *travels*, the part of the image its old and new silhouettes
    share looks the same before and after, so no change is expected there. Where
    a piece is *replaced* — a capture, a promotion — the pixels change even
    though the square stays occupied. Matching each departure to an arrival of
    the same colour and type, nearest first, separates the two.

    ``after`` is the board with ``moves[0]`` already played, supplied by the
    caller when it has one to hand: every pair sharing a first move shares it.
    """
    state: dict[int, tuple] = {}
    board_now = board
    for i, move in enumerate(moves):
        if i:
            board_now = after if after is not None else _pushed(board, moves[:i])
        for square, before, arrives in _move_delta(board_now, move):
            if square in state:
                state[square] = (state[square][0], arrives)
            else:
                state[square] = (before, arrives)

    gone, came, changed = [], [], []
    for square in sorted(state):
        before, arrives = state[square]
        if before == arrives:
            continue
        f, r = _fr(square)
        changed.append((f, r))
        if before is not None:
            gone.append((f, r, before.piece_type, before.color))
        if arrives is not None:
            came.append((f, r, arrives.piece_type, arrives.color))

    carried, used = [], set()
    for gi, (gf, gr, gt, gc) in enumerate(gone):
        best, best_d = None, 1 << 30
        for ci, (cf, cr, ct, cc) in enumerate(came):
            if ci in used or ct != gt or cc != gc:
                continue
            d = (cf - gf) ** 2 + (cr - gr) ** 2
            if d < best_d:
                best, best_d = ci, d
        if best is not None:
            used.add(best)
            carried.append((gi, best))

    return (tuple((f, r, t) for f, r, t, _ in gone),
            tuple((f, r, t) for f, r, t, _ in came),
            tuple(changed), tuple(carried))


def _pushed(board: chess.Board, moves) -> chess.Board:
    b = board.copy(stack=False)
    for m in moves:
        b.push(m)
    return b


def make_candidate(board: chess.Board, moves, kind: str,
                   after: chess.Board | None = None) -> Candidate:
    gone, came, changed, carried = transition(board, moves, after)
    return Candidate(moves=tuple(moves), changed=changed, gone=gone, came=came,
                     carried=carried, kind=kind)


def candidates(board: chess.Board, include_pairs: bool = False,
               max_pairs: int = 600) -> list[Candidate]:
    """The hypothesis set for one frame transition."""
    out = [Candidate((), (), (), (), (), NULL)]
    for m in board.legal_moves:
        out.append(make_candidate(board, (m,), "move"))
    if include_pairs:
        for m1 in list(board.legal_moves):
            b = board.copy(stack=False)
            b.push(m1)
            for m2 in b.legal_moves:
                out.append(make_candidate(board, (m1, m2), "pair", after=b))
                if len(out) >= max_pairs:
                    return out
    return out


def occupancy_and_heights(board: chess.Board) -> tuple[np.ndarray, np.ndarray]:
    occ = np.zeros((8, 8), dtype=bool)
    hgt = np.zeros((8, 8), dtype=np.float64)
    for sq, pc in board.piece_map().items():
        f, r = _fr(sq)
        occ[f, r] = True
        hgt[f, r] = PIECE_H[pc.piece_type]
    return occ, hgt


def promotion_prior(cand: Candidate, params: dict) -> float:
    """How much of a move's prior mass each promotion piece deserves.

    The four promotion variants of one pawn push are near-indistinguishable to a
    silhouette: a rook and a queen differ by a quarter of a square in height and
    nothing else the camera can see. Left uniform, the beam picks between them
    on noise, and picking wrong is not a one-ply error — the piece is the wrong
    height for the rest of the game, so every later frame is scored against a
    board that is subtly wrong. Over the board, promotions are queens almost
    every time, so that is what the tie breaks to; the alternatives stay in the
    beam, the ply is flagged ``promotion_unknown``, and Phase 4 asks.
    """
    q = params.get("promo_queen", 0.85)
    adj = 0.0
    for m in cand.moves:
        if m.promotion is not None:
            share = q if m.promotion == chess.QUEEN else (1.0 - q) / 3.0
            adj += math.log(max(4.0 * share, 1e-12))
    return adj


def log_priors(n_legal: int, n_pairs: int, params: dict) -> dict[str, float]:
    p_move = params["prior_move"] / max(n_legal, 1)
    p_pair = (params["prior_pair"] / n_pairs) if n_pairs else 0.0
    return {NULL: math.log(max(params["prior_null"], 1e-12)),
            "move": math.log(max(p_move, 1e-12)),
            "pair": math.log(max(p_pair, 1e-12))}


# --------------------------------------------------------------------------
# What one tracked position implies about the image
# --------------------------------------------------------------------------

class BoardGeometry:
    """Which cells each piece of a position covers, and what hides what.

    Front-to-back painting gives, per image cell, the distance of the nearest
    piece covering it. A silhouette is visible where nothing nearer stands in
    front — except that pieces the candidate itself removes do not count as
    occluders, because by the time the photograph was taken they were gone.
    """

    def __init__(self, board: chess.Board, bank: SilhouetteBank):
        self.bank = bank
        self.occ_sq = np.full(bank.n_cells, -1, dtype=np.int32)
        self.occ_d = np.full(bank.n_cells, np.inf, dtype=np.float32)
        pieces = [(bank.dist[_fr(sq)], _fr(sq), pc.piece_type)
                  for sq, pc in board.piece_map().items()]
        for dist, (f, r), ptype in sorted(pieces):
            idx = bank.idx(f, r, ptype)
            free = self.occ_sq[idx] < 0
            self.occ_sq[idx[free]] = f * 8 + r
            self.occ_d[idx[free]] = dist
        self._cache: dict[tuple, tuple[np.ndarray, int]] = {}
        self._observable: np.ndarray | None = None
        self._board = board

    def observable(self) -> np.ndarray:
        """(8,8) bool: squares whose near strip nothing else stands in front of.

        This is what makes the occupancy cue usable at a shallow angle. A piece
        of height ``h`` hides the board from its own base out to where the ray
        grazing its top meets the plane, which is roughly ``h / tan(elevation)``
        squares away from the camera — three squares for a king at 30 degrees.
        """
        if self._observable is not None:
            return self._observable
        cam = self.bank.cam
        hidden = np.zeros((8, 8), dtype=bool)
        for sq, pc in self._board.piece_map().items():
            f0, r0 = _fr(sq)
            base = np.array([f0 + 0.5, r0 + 0.5])
            end = cam.shadow_end(base[None, :], PIECE_H[pc.piece_type])[0]
            seg = end - base
            length = float(np.linalg.norm(seg))
            if length < 0.35:
                continue
            for i in range(1, max(int(length / 0.25), 1) + 1):
                pt = base + seg * (i / max(int(length / 0.25), 1))
                f, r = int(np.floor(pt[0])), int(np.floor(pt[1]))
                if 0 <= f < 8 and 0 <= r < 8 and not (f == f0 and r == r0):
                    hidden[f, r] = True
        self._observable = ~hidden
        return self._observable

    def visible(self, f: int, r: int, ptype: int, transparent: tuple[int, ...],
                wide: bool = False) -> tuple[np.ndarray, int]:
        """Cells of this silhouette that nothing else stands in front of."""
        key = (f, r, ptype, transparent, wide)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        idx = self.bank.idx(f, r, ptype, wide)
        if idx.size == 0:
            out = (idx, 0)
            self._cache[key] = out
            return out
        d = self.bank.dist[f, r]
        blocked = self.occ_d[idx] < d - 0.05
        if transparent and blocked.any():
            blocked &= ~np.isin(self.occ_sq[idx], transparent)
        out = (idx[~blocked], idx.size)
        self._cache[key] = out
        return out


# --------------------------------------------------------------------------
# One frame
# --------------------------------------------------------------------------

class Emission:
    """Everything about one frame that does not depend on the candidate."""

    def __init__(self, feats, params: dict, cell_px: float, mask_w: np.ndarray):
        self.params = params
        self.w = feats.w.ravel()
        self.w_capped = feats.w_capped.ravel()
        self.total_mass = feats.total_mass
        self.cell_px = cell_px            # full-res pixels per coarse cell
        self.mask_w = mask_w              # analysed pixels per coarse cell

        a, b = params["occ_a"], params["occ_b"]
        p_occ = 1.0 / (1.0 + np.exp(-a * (feats.o - b)))
        clip, weight = params["occ_clip"], params["occ_weight"]
        self.log_occ = np.clip(np.log(np.clip(p_occ, 1e-6, 1.0)), -clip, 0.0) * weight
        self.log_emp = np.clip(np.log(np.clip(1.0 - p_occ, 1e-6, 1.0)), -clip, 0.0) * weight
        self.use_occ = weight > 0.0

        # Ambient change rate: what fraction of an average analysed pixel differs
        # from the previous frame for reasons that have nothing to do with the
        # move — forearms, the captured-piece pile, a body crossing behind the
        # board. Measured per frame rather than fixed, because it swings by an
        # order of magnitude between a quiet frame and one with a hand in it.
        total_area = float(self.mask_w.sum())
        ambient = (self.total_mass / total_area) if total_area > 0 else 0.0
        self.q0 = float(np.clip(ambient, params.get("q0_min", 0.005),
                                params.get("q0_max", 0.25)))
        self.q = float(np.clip(params.get("q_hit", 0.35), self.q0 + 0.02, 0.98))
        nu = float(params.get("nu", 0.17))
        # One coarse cell is 16 pixels that morphological closing has already
        # made into a single blob, and neighbouring cells are correlated too, so
        # a cell is nowhere near an independent trial. nu is the discount that
        # keeps the likelihood from being absurdly overconfident.
        self.lr_change = nu * math.log(self.q / self.q0)
        self.lr_quiet = nu * math.log((1.0 - self.q) / (1.0 - self.q0))
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.where(self.mask_w > 0, self.w / np.maximum(self.mask_w, 1e-6), 0.0)
        self.frac = np.clip(frac, 0.0, 1.0)
        self.cell_lr = self.frac * self.lr_change + (1.0 - self.frac) * self.lr_quiet
        self.cell_lr = np.where(self.mask_w > 0, self.cell_lr, 0.0)
        # Two generation-stamped scratch buffers. Marking cells and reading the
        # marks back is linear; np.unique and friends sort, and with a wide beam
        # the sorting was most of the running time.
        n = self.w.size
        self._mark_a = np.zeros(n, dtype=np.int32)
        self._mark_b = np.zeros(n, dtype=np.int32)
        self._gen = 0

    # -- per-board pieces, shared by every candidate from that board -------

    def board_terms(self, board: chess.Board, observable: np.ndarray):
        """(base occupancy score, per-square delta if that square flips).

        Averaged over the observable squares, not summed. Summing would mean a
        position that hides more of the board scores higher for free, since
        every term is a log-probability and so never positive — and the beam,
        given ninety frames to compound that, will happily invent a position
        with more occlusion in it.
        """
        occ, _ = occupancy_and_heights(board)
        chosen = np.where(occ, self.log_occ, self.log_emp)
        other = np.where(occ, self.log_emp, self.log_occ)
        n_obs = int(observable.sum())
        if n_obs == 0:
            return 0.0, np.zeros((8, 8))
        scale = float(self.params.get("occ_scale", 32.0)) / n_obs
        return (float(chosen[observable].sum()) * scale,
                np.where(observable, other - chosen, 0.0) * scale)

    # -- the candidate score ----------------------------------------------

    def log_lik(self, cand: Candidate, geom: BoardGeometry, base_occ: float,
                delta_occ: np.ndarray, log_prior: float) -> float:
        p = self.params
        total = log_prior + base_occ

        if self.use_occ and cand.changed:
            idx_f = np.fromiter((c[0] for c in cand.changed), int, len(cand.changed))
            idx_r = np.fromiter((c[1] for c in cand.changed), int, len(cand.changed))
            total += float(delta_occ[idx_f, idx_r].sum())

        if not cand.gone and not cand.came:
            # Nothing moved, so the candidate predicts no cell anywhere and the
            # whole frame is background. Under a likelihood *ratio* against that
            # same background, that is exactly zero — the null candidate lives or
            # dies on its prior, and any real move beats it by however much
            # change its silhouettes actually contain.
            return total

        transparent = tuple(sorted(f * 8 + r for f, r, _ in cand.gone))
        vis_gone = [geom.visible(f, r, t, transparent) for f, r, t in cand.gone]
        vis_came = [geom.visible(f, r, t, transparent) for f, r, t in cand.came]
        eff_gone = [v for v, _ in vis_gone]
        eff_came = [v for v, _ in vis_came]

        # Where a piece's own before- and after-silhouettes overlap, the image
        # is unchanged: same piece, same pixels. Nowhere else cancels.
        mark = self._mark_b
        for gi, ci in cand.carried:
            self._gen += 1
            gen = self._gen
            a, b = eff_gone[gi], eff_came[ci]
            if not a.size or not b.size:
                continue
            mark[b] = gen
            eff_gone[gi] = a[mark[a] != gen]
            mark[a] = gen + 1
            self._gen += 1
            eff_came[ci] = b[mark[b] != gen + 1]

        vis_min = p["vis_min"]
        self._gen += 1
        gen = self._gen
        seen = self._mark_a
        for eff, (_, full) in zip(eff_gone + eff_came, vis_gone + vis_came):
            if full == 0 or eff.size < vis_min * full:
                # Hidden either way. It contributes to neither side of the
                # ledger: the candidate had no way to predict pixels there, so
                # it is neither credited nor charged for them.
                continue
            fresh = eff[seen[eff] != gen]
            if not fresh.size:
                continue
            seen[fresh] = gen
            total += float(self.cell_lr[fresh].sum())
        return total
