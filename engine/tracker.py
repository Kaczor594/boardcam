"""Beam search over a whole game.

Each frame is one transition and contributes one emission term, so paths that
disagree about how many plies have been played are still directly comparable —
which is what lets a missed press (one frame, two plies) and a double press (one
frame, none) sit in the same beam as the ordinary case.

The beam is what makes late evidence fix early mistakes: a ply that two
candidates explain equally well at the time is usually decided three frames
later, when only one of the two leaves a position the following frames agree
with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn
import cv2
import numpy as np

from . import camera as cammod
from . import emission as em
from . import rectify as rectmod
from .calibrate import Calibration
from .features import FeatureExtractor, load_params

INF = float("inf")
_ZERO = np.zeros((8, 8))


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class PlyInfo:
    index: int
    san: str
    uci: str
    seq: int                       # the capture this ply was read from
    margin: float                  # nats; small means "ask someone else"
    flags: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)


@dataclass
class TrackResult:
    moves: list[chess.Move]
    plies: list[PlyInfo]
    frames: list[dict]
    board: chess.Board
    calibration: Calibration
    camera: cammod.CameraModel
    warnings: list[str] = field(default_factory=list)

    @property
    def san(self) -> list[str]:
        return [p.san for p in self.plies]

    @property
    def flagged(self) -> list[int]:
        return [p.index for p in self.plies if p.flags]

    def to_json(self) -> dict:
        return {
            "moves": [m.uci() for m in self.moves],
            "plies": [{"index": p.index, "san": p.san, "uci": p.uci, "seq": p.seq,
                       "margin": (None if p.margin == INF else round(p.margin, 3)),
                       "flags": p.flags, "candidates": p.candidates}
                      for p in self.plies],
            "frames": self.frames,
            "final_fen": self.board.fen(),
            "calibration": self.calibration.to_json(),
            "camera": self.camera.to_json(),
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
# Beam paths
# --------------------------------------------------------------------------

@dataclass
class _Path:
    board: chess.Board
    moves: tuple[chess.Move, ...]
    score: float
    decisions: tuple                # one entry per frame consumed

    def key(self) -> tuple:
        return self.moves


def _lse(xs: list[float]) -> float:
    if not xs:
        return -INF
    m = max(xs)
    if m == -INF:
        return -INF
    return m + math.log(sum(math.exp(x - m) for x in xs))


# --------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------

class Tracker:
    """Frames in, moves out. Feed it ``add_frame`` in capture order."""

    def __init__(self, calib: Calibration, frame0: np.ndarray,
                 params: dict | None = None,
                 constraints: dict[int, chess.Move] | None = None,
                 orient_check: bool = True,
                 start_board: chess.Board | None = None):
        self.params = params or load_params()
        self.frame0 = frame0
        self.calib = calib
        self.constraints = dict(constraints or {})
        self.orient_check = orient_check
        self.camera = cammod.estimate(calib, frame0.shape)
        self.extractor = FeatureExtractor(calib, frame0, self.params, self.camera)
        self.warnings: list[str] = list(calib.warnings)
        if not self.camera.ok:
            self.warnings.append("camera-estimate-fallback")
        board = start_board.copy() if start_board else chess.Board()
        self.beam: list[_Path] = [_Path(board, (), 0.0, ())]
        self.frames: list[dict] = []
        self._inputs: list[tuple[int, list]] = []
        self._first = True
        self._llm = None
        self._llm_tried = False

    # -- public API -------------------------------------------------------

    def add_frame(self, seq: int, paths) -> None:
        paths = [str(p) for p in paths]
        self._inputs.append((seq, paths))
        if self._first and self.orient_check:
            self._resolve_orientation(seq, paths)
            self._first = False
            return
        self._first = False
        feats = self.extractor.extract(seq, paths)
        self._step(feats)

    def result(self) -> TrackResult:
        best = max(self.beam, key=lambda p: p.score)
        plies = self._ply_infos(best)
        board = chess.Board()
        for m in best.moves:
            board.push(m)
        return TrackResult(moves=list(best.moves), plies=plies, frames=self.frames,
                           board=board, calibration=self.calib, camera=self.camera,
                           warnings=self.warnings)

    def constrain(self, ply: int, move: chess.Move) -> "Tracker":
        """Re-run the whole game with one ply pinned to a known move."""
        cons = dict(self.constraints)
        cons[ply] = move
        t = Tracker(self.calib, self.frame0, self.params, constraints=cons,
                    orient_check=False)
        for seq, paths in self._inputs:
            t.add_frame(seq, paths)
        return t

    # -- orientation ------------------------------------------------------

    def _resolve_orientation(self, seq: int, paths) -> None:
        """Frame 1 must change squares on white's side of the board.

        The Phase 2 calibration decides white from piece luminance, which is a
        close call on a board whose two sets are similar. The first move is a
        second, independent vote: score it under both orientations and keep the
        one that explains the picture.
        """
        feats = self.extractor.extract(seq, paths)
        best_a = self._best_single(feats, self.extractor, self.beam[0].board)

        alt_calib = self.calib.flipped()
        alt_cam = cammod.estimate(alt_calib, self.frame0.shape)
        alt_ex = FeatureExtractor(alt_calib, self.frame0, self.params, alt_cam)
        alt_feats = alt_ex.extract(seq, paths)
        best_b = self._best_single(alt_feats, alt_ex, self.beam[0].board)

        if best_b > best_a + 1e-9:
            self.calib, self.camera, self.extractor = alt_calib, alt_cam, alt_ex
            self.warnings.append("orientation-flipped-by-frame1")
            feats = alt_feats
        self._step(feats)

    def _best_single(self, feats, extractor, board: chess.Board) -> float:
        e = self._emission(feats, extractor)
        geom = em.BoardGeometry(board, extractor.bank)
        base, delta = ((0.0, _ZERO) if not e.use_occ
                       else e.board_terms(board, geom.observable()))
        cands = em.candidates(board, include_pairs=False)
        lp = em.log_priors(len(cands) - 1, 0, self.params)
        return max(e.log_lik(c, geom, base, delta,
                             lp[c.kind] + em.promotion_prior(c, self.params))
                   for c in cands if c.kind == "move")

    # -- one frame --------------------------------------------------------

    def _emission(self, feats, extractor=None) -> em.Emission:
        ex = extractor or self.extractor
        return em.Emission(feats, self.params, float(ex.ds * ex.ds), ex.mask_w)

    def _score_board(self, e: em.Emission, path: _Path):
        board = path.board
        geom = em.BoardGeometry(board, self.extractor.bank)
        if e.use_occ:
            obs = geom.observable()
            base, delta = e.board_terms(board, obs)
        else:
            obs, base, delta = None, 0.0, _ZERO
        cands = em.candidates(board, include_pairs=False)
        n_legal = len(cands) - 1
        lp = em.log_priors(n_legal, 0, self.params)
        scored = [(c, e.log_lik(c, geom, base, delta,
                                lp[c.kind] + em.promotion_prior(c, self.params)))
                  for c in cands]
        scored.sort(key=lambda x: -x[1])

        top = scored[0][1]
        runner = scored[1][1] if len(scored) > 1 else -INF
        if top - runner < self.params["pair_trigger"]:
            pairs = self._pair_candidates(board, scored)
            if pairs:
                lp2 = em.log_priors(n_legal, len(pairs), self.params)
                scored += [(c, e.log_lik(c, geom, base, delta,
                                         lp2["pair"] + em.promotion_prior(c, self.params)))
                           for c in pairs]
                scored.sort(key=lambda x: -x[1])
        return scored, obs

    def _pair_candidates(self, board: chess.Board, scored) -> list[em.Candidate]:
        top1 = int(self.params.get("pair_top1", 12))
        heads = [c.moves[0] for c, _ in scored if c.kind == "move"][:top1]
        out: list[em.Candidate] = []
        cap = int(self.params.get("max_pairs", 600))
        for m1 in heads:
            b = board.copy(stack=False)
            b.push(m1)
            for m2 in b.legal_moves:
                out.append(em.make_candidate(board, (m1, m2), "pair"))
                if len(out) >= cap:
                    return out
        return out

    def _allowed(self, path: _Path, cand: em.Candidate) -> bool:
        if not self.constraints:
            return True
        n = len(path.moves)
        for i, m in enumerate(cand.moves):
            want = self.constraints.get(n + i)
            if want is not None and want != m:
                return False
        return True

    # -- vision-LLM fallback (Phase 4) -------------------------------------

    def _llm_resolver(self):
        """The game's ``LLMResolver``, built lazily once a frame path is known."""
        if not self.params.get("use_llm"):
            return None
        if self._llm is None and not self._llm_tried and self._inputs:
            from . import vision_llm
            game_dir = Path(self._inputs[-1][1][0]).parent.parent
            self._llm = vision_llm.LLMResolver(game_dir)
            self._llm_tried = True
        return self._llm

    def _consult_llm(self, seq: int, scored, board: chess.Board, unexplained: bool):
        """Ask the vision model about the frame that is currently deciding a ply.

        Builds the candidate list from ``scored`` (already includes pairs when
        the frame triggered them), sends the two captures either side of it, and
        returns the move signature to reward — or ``None`` if the budget is
        spent, the call failed, or nothing in ``scored`` survives to a label.
        """
        from . import vision_llm

        top_n = scored[: (12 if unexplained else 6)]
        seen: set[str] = set()
        opts: list[dict] = []
        label_to_moves: dict[str, tuple[str, ...]] = {}
        for cand, _ in top_n:
            if cand.kind == "null":
                label = "no move happened"
            else:
                try:
                    sans = []
                    b = board.copy(stack=False)
                    for m in cand.moves:
                        sans.append(b.san(m))
                        b.push(m)
                    label = " then ".join(sans)
                except (AssertionError, ValueError):
                    continue
            if label in seen:
                continue
            seen.add(label)
            uci = tuple(m.uci() for m in cand.moves)
            label_to_moves[label] = uci
            opts.append({"label": label, "moves": uci})
        if len(opts) < 2:
            return None

        if len(self._inputs) < 2:
            before_paths: list = [self.frame0]
        else:
            before_paths = self._inputs[-2][1]
        after_paths = self._inputs[-1][1]

        try:
            rect_before = rectmod.rectify(self._frame_bgr(before_paths[0]), self.calib)
            rect_after = rectmod.rectify(self._frame_bgr(after_paths[0]), self.calib)
        except Exception:
            return None

        resolver = self._llm
        result = resolver.resolve(
            str(seq), before_paths=before_paths, after_paths=after_paths,
            rect_before=rect_before, rect_after=rect_after, candidates=opts,
            side_to_move=("white" if board.turn == chess.WHITE else "black"),
            calib=self.calib,
        )
        if result is None:
            return None
        return {"san": result["san"], "confidence": result["confidence"],
                "cost": result.get("cost", 0.0), "model": result.get("model"),
                "chosen_moves": label_to_moves.get(result["san"])}

    @staticmethod
    def _frame_bgr(source) -> np.ndarray:
        if isinstance(source, np.ndarray):
            return source
        img = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"cannot read image: {source}")
        return img

    def _step(self, feats) -> None:
        e = self._emission(feats)
        width = int(self.params["beam_width"])
        expanded: dict[tuple, _Path] = {}
        frame_rec = {"seq": feats.seq, "blobs": feats.n_blobs,
                     "mass": round(feats.total_mass, 1),
                     "shift": [round(v, 2) for v in feats.shift],
                     "warnings": list(feats.warnings)}
        best_overall = -INF
        best_obs = None

        clip = float(self.params.get("score_clip", 0.0))
        # Children per parent, deliberately far below the beam width. Letting one
        # parent contribute `width` children lets the current leader's variants
        # fill every slot, and every rival reading of an earlier ply is evicted
        # by siblings rather than by evidence.
        children = int(self.params.get("children_per_path", 5))

        # A flagged frame gets one look from a vision model — at the current
        # leader's board, not every path in the beam, since that is what
        # decides the ply everyone downstream sees. The answer is a bonus on
        # whichever candidate shares its exact move signature, wherever that
        # candidate turns up across the beam.
        llm_bonus: dict[tuple[str, ...], float] = {}
        if self._llm_resolver() is not None:
            lead_scored, _ = self._score_board(e, self.beam[0])
            lead_top = lead_scored[0][1]
            lead_runner = lead_scored[1][1] if len(lead_scored) > 1 else -INF
            unexplained_lead = lead_top < self.params["divergence_floor"]
            if (lead_top - lead_runner) < self.params["tau"] or unexplained_lead:
                llm_result = self._consult_llm(feats.seq, lead_scored, self.beam[0].board,
                                               unexplained_lead)
                if llm_result is not None:
                    frame_rec["llm_cost"] = llm_result["cost"]
                    frame_rec["llm_model"] = llm_result["model"]
                    frame_rec["llm_confidence"] = llm_result["confidence"]
                    frame_rec["llm_choice"] = llm_result["san"]
                    if llm_result["chosen_moves"] is not None:
                        llm_bonus[llm_result["chosen_moves"]] = math.log(
                            max(llm_result["confidence"], 1e-3))

        for path in self.beam:
            scored, obs = self._score_board(e, path)
            if best_obs is None:
                best_obs = obs
            local_top = scored[0][1]
            best_overall = max(best_overall, local_top)
            # A pinned ply is a human saying what happened, so it has to be
            # reachable even when the photograph scores it badly — which is the
            # normal case, since a ply is corrected precisely when the engine
            # ranked the truth below the first few candidates.
            pool = ([cs for cs in scored if self._allowed(path, cs[0])][:children]
                    if self.constraints else scored[:children])
            for cand, sc in pool:
                # One frame may only punish a path so far. A hand over the board
                # makes every candidate score badly and their differences are
                # noise; unclipped, that noise is enough to evict the right game
                # from the beam, and nothing later can put it back.
                if clip > 0.0:
                    sc = max(sc, local_top - clip)
                if llm_bonus:
                    sc += llm_bonus.get(tuple(m.uci() for m in cand.moves), 0.0)
                if not self._allowed(path, cand):
                    continue
                b = path.board.copy(stack=False)
                for m in cand.moves:
                    b.push(m)
                moves = path.moves + cand.moves
                dec = {"seq": feats.seq, "kind": cand.kind,
                       "n": cand.n_plies, "score": sc, "local_best": local_top,
                       "alts": [(c.moves, s) for c, s in scored[:6]]}
                np_ = _Path(b, moves, path.score + sc, path.decisions + (dec,))
                prev = expanded.get(moves)
                if prev is None or np_.score > prev.score:
                    expanded[moves] = np_

        if not expanded:                      # every candidate was constrained away
            self.beam = [
                _Path(p.board, p.moves, p.score,
                      p.decisions + ({"seq": feats.seq, "kind": "null", "n": 0,
                                      "score": 0.0, "local_best": 0.0,
                                      "alts": []},))
                for p in self.beam]
            self.frames.append(frame_rec)
            return

        ranked = sorted(expanded.values(), key=lambda p: -p.score)
        margin = float(self.params.get("beam_margin", 0.0))
        if margin > 0.0:
            # Keep anything still in contention, not just the best few: the true
            # game routinely spends a handful of frames a few nats down before
            # later frames vindicate it.
            cut = ranked[0].score - margin
            keep = max(width, sum(1 for p in ranked if p.score >= cut))
            self.beam = ranked[:min(keep, int(self.params.get("beam_max", 200)))]
        else:
            self.beam = ranked[:width]
        frame_rec["best"] = round(best_overall, 2)
        frame_rec["unexplained"] = best_overall < self.params["divergence_floor"]
        self.frames.append(frame_rec)

        best = self.beam[0]
        if e.use_occ and self.extractor._last_lab is not None:
            occ, _ = em.occupancy_and_heights(best.board)
            geom = em.BoardGeometry(best.board, self.extractor.bank)
            self.extractor.update_colour_models(
                self.extractor._last_lab, ~occ, geom.observable())

    # -- margins and flags ------------------------------------------------

    def _ply_infos(self, best: _Path) -> list[PlyInfo]:
        tau = self.params["tau"]
        floor = self.params["divergence_floor"]

        # Where each ply came from, and the local margin of the frame that
        # decided it.
        ply_seq: list[int] = []
        local: list[float] = []
        cand_lists: list[list[dict]] = []
        frame_unexpl: list[bool] = []
        board = chess.Board()
        for dec in best.decisions:
            n = dec["n"]
            if n == 0:
                continue
            chosen = tuple(best.moves[len(ply_seq):len(ply_seq) + n])
            alt = [s for mv, s in dec["alts"] if mv[:n] != chosen[:n]]
            margin = dec["score"] - max(alt) if alt else INF
            cands = []
            b = board.copy(stack=False)
            for mv, s in dec["alts"][:6]:
                try:
                    cands.append({"san": (b.san(mv[0]) if mv else "--"),
                                  "uci": (mv[0].uci() if mv else None),
                                  "n": len(mv), "score": round(s, 2)})
                except (AssertionError, ValueError):
                    continue
            for _ in range(n):
                ply_seq.append(dec["seq"])
                local.append(margin)
                cand_lists.append(cands)
                frame_unexpl.append(dec["local_best"] < floor)
            for m in chosen:
                board.push(m)

        # Global margin: how much of the beam's mass agrees with this ply.
        glob: list[float] = []
        for i in range(len(best.moves)):
            agree = [p.score for p in self.beam
                     if len(p.moves) > i and p.moves[i] == best.moves[i]]
            dis = [p.score for p in self.beam
                   if not (len(p.moves) > i and p.moves[i] == best.moves[i])]
            glob.append(INF if not dis else _lse(agree) - _lse(dis))

        out: list[PlyInfo] = []
        board = chess.Board()
        for i, m in enumerate(best.moves):
            san = board.san(m)
            margin = min(local[i] if i < len(local) else INF,
                         glob[i] if i < len(glob) else INF)
            flags: list[str] = []
            if margin < tau:
                flags.append("low_margin")
            if i < len(frame_unexpl) and frame_unexpl[i]:
                flags.append("unexplained")
            if self._promotion_flag(m):
                flags.append("promotion_unknown")
            out.append(PlyInfo(index=i, san=san, uci=m.uci(),
                               seq=ply_seq[i] if i < len(ply_seq) else -1,
                               margin=margin, flags=flags,
                               candidates=cand_lists[i] if i < len(cand_lists) else []))
            board.push(m)
        return out

    @staticmethod
    def _promotion_flag(move: chess.Move) -> bool:
        """Every promotion is flagged, without exception.

        The engine has no way to name the piece: the four variants differ by a
        quarter-square of height and nothing else, and the prior that broke the
        tie toward a queen is a statement about how people play, not about what
        was photographed. Asking is cheap and promotions are rare.
        """
        return move.promotion is not None


# --------------------------------------------------------------------------
# Whole-game convenience
# --------------------------------------------------------------------------

def frame_paths(game_dir: Path) -> list[tuple[int, list[Path]]]:
    """Every capture in a game directory, in order, with its burst candidates."""
    frames = sorted((game_dir / "frames").glob("*.jpg"))
    by_seq: dict[int, list[Path]] = {}
    for f in frames:
        seq = int(f.stem.split("_")[0])
        by_seq.setdefault(seq, []).append(f)
    return [(s, sorted(by_seq[s])) for s in sorted(by_seq)]


def track_game(game_dir: str | Path, params: dict | None = None,
               constraints: dict[int, chess.Move] | None = None) -> TrackResult:
    from .calibrate import calibrate
    game_dir = Path(game_dir)
    seqs = frame_paths(game_dir)
    if not seqs or seqs[0][0] != 0:
        raise ValueError(f"{game_dir} has no start frame")
    calib = calibrate(seqs[0][1])
    frame0 = cv2.imread(str(seqs[0][1][0]), cv2.IMREAD_COLOR)
    t = Tracker(calib, frame0, params, constraints=constraints)
    for seq, paths in seqs[1:]:
        t.add_frame(seq, paths)
    return t.result()
