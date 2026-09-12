"""From a pair of photographs to "where on the board did anything change".

This is the only module that looks at pixels during tracking. It answers two
questions per frame, both close to binary:

``w`` / ``w_capped``
    a change map: how many pixels differ from the previous frame, per cell of a
    coarse grid over the image. ``w_capped`` scales each connected region down
    so that no single one of them — a forearm, a torso — can outvote the board.

``o``
    per square, how far its near strip is from the colour it is when empty, in
    Mahalanobis units. Only meaningful where the tracked position says nothing
    stands between the square and the camera.

It also builds the ``SilhouetteBank``: where on the image a piece of each type
standing on each square would be, projected through the metric camera. That is
what lets the emission model *predict* a change region for a candidate move
instead of trying to infer squares from the change region — the inference
direction §C took, which cannot work when the camera is low enough that pieces
occlude each other (see the spec's Amendments).

Everything outside the board and the strip of image a piece on the far rank can
reach is zeroed before any of this runs. Players, forearms resting at the edge,
the clock phone and the growing pile of captured pieces are therefore invisible,
which is the point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import chess
import cv2
import numpy as np

from . import camera as cammod
from .calibrate import Calibration, load_frames, recalibrate
from .rectify import rectify, square_patch

PARAMS_PATH = Path(__file__).with_name("params.json")


def load_params(path: str | Path | None = None) -> dict:
    with open(path or PARAMS_PATH) as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


# --------------------------------------------------------------------------
# Burst selection
# --------------------------------------------------------------------------

def skin_fraction(bgr: np.ndarray, mask: np.ndarray) -> float:
    """Fraction of the analysed area that looks like a hand."""
    if mask.sum() == 0:
        return 0.0
    ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    cr, cb = ycrcb[:, :, 1].astype(np.int16), ycrcb[:, :, 2].astype(np.int16)
    skin = (cr >= 135) & (cr <= 180) & (cb >= 85) & (cb <= 135) & (ycrcb[:, :, 0] > 60)
    return float((skin & (mask > 0)).sum()) / float((mask > 0).sum())


def choose_burst(paths, mask: np.ndarray, skin_max: float) -> tuple[np.ndarray, list[str]]:
    """Reduce a burst to one image, dropping candidates with a hand in them.

    The test is *relative*. A wooden board and light pieces sit inside the same
    chroma range as skin, so an absolute threshold flags every candidate of
    every frame and the filter stops filtering. What identifies a hand is the
    candidate having markedly more of that colour than its siblings, taken a
    few hundred milliseconds apart from the same camera.
    """
    imgs, skins = [], []
    for path in paths:
        im = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if im is None:
            continue
        imgs.append(im)
        skins.append(skin_fraction(im, mask))
    if not imgs:
        raise FileNotFoundError(f"no readable frames in {list(paths)!r}")

    warnings: list[str] = []
    floor = min(skins)
    keep = [i for i, sk in enumerate(skins) if sk <= floor + skin_max]
    if len(keep) < len(imgs):
        warnings.append("hand-dropped")
    shape = imgs[keep[0]].shape
    kept = [imgs[i] for i in keep if imgs[i].shape == shape]
    if len(kept) >= 3:
        out = np.median(np.stack(kept), axis=0).astype(np.uint8)
    else:
        out = kept[-1]          # the latest survivor: the most settled position
    return out, warnings


# --------------------------------------------------------------------------
# Photometry and alignment
# --------------------------------------------------------------------------

def _robust_gain_offset(cur: np.ndarray, ref: np.ndarray,
                        mask: np.ndarray) -> tuple[float, float]:
    sel = mask > 0
    a, b = cur[sel].astype(np.float32), ref[sel].astype(np.float32)
    if a.size < 64:
        return 1.0, 0.0
    ma, mb = float(np.median(a)), float(np.median(b))
    sa = float(np.median(np.abs(a - ma))) + 1e-3
    sb = float(np.median(np.abs(b - mb))) + 1e-3
    gain = float(np.clip(sb / sa, 0.5, 2.0))
    return gain, mb - gain * ma


def normalise_to(cur_gray: np.ndarray, ref_gray: np.ndarray,
                 mask: np.ndarray) -> np.ndarray:
    gain, off = _robust_gain_offset(cur_gray, ref_gray, mask)
    return np.clip(cur_gray.astype(np.float32) * gain + off, 0, 255)


def align_shift(cur_gray: np.ndarray, ref_gray: np.ndarray,
                box: tuple[int, int, int, int]) -> tuple[float, float, float]:
    """Translation that takes ``cur`` onto ``ref``, by phase correlation."""
    x0, y0, x1, y1 = box
    a = ref_gray[y0:y1, x0:x1].astype(np.float32)
    b = cur_gray[y0:y1, x0:x1].astype(np.float32)
    if a.shape != b.shape or a.size < 1024:
        return 0.0, 0.0, 0.0
    win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(b, a, win)
    return float(dx), float(dy), float(resp)


# --------------------------------------------------------------------------
# Per-frame output
# --------------------------------------------------------------------------

@dataclass
class FrameFeatures:
    """One frame's evidence, in the coarse grid the emission model works in."""

    seq: int
    w: np.ndarray                 # (H, W) changed full-res pixels per coarse cell
    w_capped: np.ndarray          # the same, each connected region capped
    total_mass: float             # w_capped.sum(), in pixels
    n_blobs: int
    o: np.ndarray                 # (8,8) Mahalanobis distance of each near strip
    rect: np.ndarray              # rectified board, for the review page and the LLM
    shift: tuple[float, float]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Silhouettes
# --------------------------------------------------------------------------

# Piece heights and base radii in squares. The renderer uses the same numbers,
# and they are real: a 55 mm square and a 95 mm king make the king 1.75 squares
# tall, which is what makes it hide three squares at 30 degrees.
PIECE_H = {chess.PAWN: 0.85, chess.BISHOP: 1.02, chess.KNIGHT: 1.15,
           chess.ROOK: 1.25, chess.QUEEN: 1.50, chess.KING: 1.75}
PIECE_R = {chess.PAWN: 0.30, chess.BISHOP: 0.32, chess.KNIGHT: 0.33,
           chess.ROOK: 0.35, chess.QUEEN: 0.37, chess.KING: 0.39}

N_ANG = 12


class SilhouetteBank:
    """Where each piece would be in the image, if it stood on each square.

    Built once per game. A silhouette is the convex hull of a projected vertical
    prism — good enough, because what matters is which *cells* a piece covers,
    not its profile. It is grown by ``slack`` squares to absorb calibration
    error and the fact that nobody centres a piece exactly.
    """

    def __init__(self, cam, shape: tuple[int, int], ds: int, slack: float = 0.03,
                 slack_hit: float | None = None):   # slack_hit: unused, kept for callers
        self.cam = cam
        self.ds = ds
        self.h = shape[0] // ds
        self.w = shape[1] // ds
        self.n_cells = self.h * self.w
        self.slack = slack
        self.slack_hit = slack if slack_hit is None else slack_hit
        self._idx: dict[tuple[int, int, int, bool], np.ndarray] = {}
        self.dist = np.zeros((8, 8), dtype=np.float64)
        for f in range(8):
            for r in range(8):
                self.dist[f, r] = float(np.linalg.norm(
                    np.array([f + 0.5, r + 0.5]) - cam.centre[:2]))

    def idx(self, file: int, rank: int, ptype: int, wide: bool = False) -> np.ndarray:
        key = (file, rank, ptype, wide)
        got = self._idx.get(key)
        if got is None:
            got = self._render(file, rank, ptype, wide)
            self._idx[key] = got
        return got

    def _render(self, file: int, rank: int, ptype: int, wide: bool) -> np.ndarray:
        cx, cy = file + 0.5, rank + 0.5
        slack = self.slack_hit if wide else self.slack
        rad = PIECE_R[ptype] + slack
        top_rad = 0.5 * PIECE_R[ptype] + slack
        hgt = PIECE_H[ptype]
        ang = np.linspace(0, 2 * np.pi, N_ANG, endpoint=False)
        base = np.stack([cx + rad * np.cos(ang), cy + rad * np.sin(ang),
                         np.zeros(N_ANG)], axis=1)
        top = np.stack([cx + top_rad * np.cos(ang), cy + top_rad * np.sin(ang),
                        np.full(N_ANG, hgt)], axis=1)
        pts = self.cam.project(np.vstack([base, top])) / self.ds
        if not np.isfinite(pts).all():
            return np.zeros(0, dtype=np.int32)
        hull = cv2.convexHull(np.round(pts).astype(np.int32))
        buf = np.zeros((self.h, self.w), dtype=np.uint8)
        cv2.fillConvexPoly(buf, hull, 1)
        return np.flatnonzero(buf.ravel()).astype(np.int32)


# --------------------------------------------------------------------------
# Extractor
# --------------------------------------------------------------------------

class FeatureExtractor:
    """Holds everything that persists across a game's frames."""

    def __init__(self, calib: Calibration, frame0: np.ndarray, params: dict,
                 cam: cammod.CameraModel | None = None):
        self.calib = calib
        self.params = params
        self.shape = frame0.shape
        self.ds = int(params.get("ds", 4))
        self.cam = cam if cam is not None else cammod.estimate(calib, frame0.shape)
        self.mask = self._build_mask()
        self.board_mask = self._board_mask()
        self.box = self._bbox(self.board_mask)
        self.bank = SilhouetteBank(
            self.cam, frame0.shape[:2], self.ds,
            float(params.get("silhouette_slack", 0.03)),
            float(params.get("silhouette_slack_hit",
                             params.get("silhouette_slack", 0.03))))
        self.mask_w = self._coarse(self.mask > 0)
        self.ref_gray = cv2.cvtColor(frame0, cv2.COLOR_BGR2GRAY)
        self.prev_gray = self.ref_gray.astype(np.float32)
        self.prev_shift = (0.0, 0.0)
        self._last_lab = None
        self._track = calib
        self._init_colour_models(frame0)

    # -- masks ------------------------------------------------------------

    def _poly(self, pts: np.ndarray) -> np.ndarray:
        m = np.zeros(self.shape[:2], dtype=np.uint8)
        cv2.fillConvexPoly(m, np.round(pts).astype(np.int32), 255)
        return m

    def _board_mask(self) -> np.ndarray:
        return self._poly(np.asarray(self.calib.corners, dtype=np.float64))

    def _build_mask(self) -> np.ndarray:
        """Exactly the image a piece standing on the board can occupy.

        Not the convex hull of the board and its raised corners: that hull
        sweeps wide wedges past the two side edges, and at a shallow angle those
        wedges are precisely where the players' forearms rest. Taking the union
        of the 64 per-square prisms instead hugs the board and rises only by a
        piece's height, which keeps the arms out while losing nothing.
        """
        m = np.zeros(self.shape[:2], dtype=np.uint8)
        h = float(self.params.get("max_piece_h", 1.75))
        if self.cam.ok:
            for f in range(8):
                for r in range(8):
                    base = np.array([[f, r], [f + 1, r], [f + 1, r + 1], [f, r + 1]],
                                    dtype=np.float64)
                    pts = self.cam.project(np.vstack([
                        np.hstack([base, np.zeros((4, 1))]),
                        np.hstack([base, np.full((4, 1), h)])]))
                    if not np.isfinite(pts).all():
                        continue
                    cv2.fillConvexPoly(m, cv2.convexHull(np.round(pts).astype(np.int32)), 255)
            return m
        corners = np.asarray(self.calib.corners, dtype=np.float64)
        edge = float(np.linalg.norm(corners[0] - corners[1]))
        strip = self.params.get("strip_squares", 2.2) * edge / 8.0
        pts = np.vstack([corners, corners - np.array([0.0, strip])])
        cv2.fillConvexPoly(m, cv2.convexHull(np.round(pts).astype(np.int32)), 255)
        return m

    def _coarse(self, full: np.ndarray) -> np.ndarray:
        """Full-resolution pixels per coarse cell, as a flat array."""
        ds, hh, ww = self.ds, self.bank.h, self.bank.w
        crop = full[:hh * ds, :ww * ds].astype(np.float32)
        return crop.reshape(hh, ds, ww, ds).sum(axis=(1, 3)).ravel()

    @staticmethod
    def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
        ys, xs = np.nonzero(mask)
        return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1

    # -- geometry helpers -------------------------------------------------

    # -- colour models ----------------------------------------------------

    def _init_colour_models(self, frame0: np.ndarray) -> None:
        """What each square looks like with nothing standing on it.

        The middle ranks are empty at frame 0, so they describe themselves. The
        two occupied bands get their colour class's average instead, and the
        exponential update takes over as soon as they empty out.
        """
        rect = rectify(frame0, self.calib)
        lab = cv2.cvtColor(rect, cv2.COLOR_BGR2LAB).astype(np.float32)
        med = np.zeros((8, 8, 3), dtype=np.float32)
        for f in range(8):
            for r in range(8):
                patch = square_patch(lab, f, r, "near", self.calib.camera_side)
                if patch.size:
                    med[f, r] = np.median(patch.reshape(-1, 3), axis=0)

        light = np.asarray(self.calib.square_is_light, dtype=bool)
        empty = np.zeros((8, 8), dtype=bool)
        empty[:, 2:6] = True

        self.mu = np.zeros((8, 8, 3), dtype=np.float32)
        self.sigma_inv = {}
        for is_light in (False, True):
            rows = med[empty & (light == is_light)]
            if len(rows) > 3:
                cov = np.atleast_2d(np.cov(rows.T.astype(np.float64)))
                mean = rows.mean(axis=0)
            else:
                cov, mean = np.eye(3) * 25.0, med.reshape(-1, 3).mean(axis=0)
            cov = cov + np.eye(3) * 4.0
            self.sigma_inv[is_light] = np.linalg.inv(cov).astype(np.float32)
            self.mu[light == is_light] = mean
        self.mu[empty] = med[empty]

    def _mahalanobis(self, lab_rect: np.ndarray) -> np.ndarray:
        out = np.zeros((8, 8), dtype=np.float32)
        for f in range(8):
            for r in range(8):
                p = square_patch(lab_rect, f, r, "near", self.calib.camera_side)
                if p.size == 0:
                    continue
                d = np.median(p.reshape(-1, 3), axis=0) - self.mu[f, r]
                si = self.sigma_inv[bool(self.calib.square_is_light[f][r])]
                out[f, r] = float(np.sqrt(max(d @ si @ d, 0.0)))
        return out

    def update_colour_models(self, lab_rect: np.ndarray, empty: np.ndarray,
                             observable: np.ndarray) -> None:
        a = float(self.params.get("ema_alpha", 0.15))
        for f in range(8):
            for r in range(8):
                if not (empty[f, r] and observable[f, r]):
                    continue
                p = square_patch(lab_rect, f, r, "near", self.calib.camera_side)
                if p.size == 0:
                    continue
                self.mu[f, r] = (1 - a) * self.mu[f, r] + a * np.median(
                    p.reshape(-1, 3), axis=0)

    # -- the main entry point ---------------------------------------------

    def extract(self, seq: int, paths) -> FrameFeatures:
        p = self.params
        bgr, warnings = choose_burst(paths, self.mask, p["skin_frac_max"])
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        dx, dy, resp = align_shift(gray, self.ref_gray, self.box)
        if resp < 0.05 or abs(dx) > 80 or abs(dy) > 80:
            dx, dy = self.prev_shift
            warnings.append("align-weak")
        bump = abs(dx) > p.get("bump_px", 4.0) or abs(dy) > p.get("bump_px", 4.0)
        if bump:
            warnings.append("bump")
            bgr, gray, ok = self._unbump(bgr, gray)
            if ok:
                dx = dy = 0.0
                warnings.append("re-snapped")
        if abs(dx) > 0.3 or abs(dy) > 0.3:
            mat = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
            bgr = cv2.warpAffine(bgr, mat, (bgr.shape[1], bgr.shape[0]),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self.prev_shift = (dx, dy)

        cur = normalise_to(gray, self.prev_gray, self.mask)
        diff = np.abs(cur - self.prev_gray)
        change = ((diff > p["theta"]) & (self.mask > 0)).astype(np.uint8)
        k = np.ones((3, 3), np.uint8)
        change = cv2.morphologyEx(change, cv2.MORPH_OPEN, k)
        change = cv2.morphologyEx(change, cv2.MORPH_CLOSE, k, iterations=2)

        w, w_capped, n_blobs = self._change_map(change)

        rect = rectify(bgr, self.calib)
        if p.get("occ_weight", 0.0) > 0:
            lab = cv2.cvtColor(rect, cv2.COLOR_BGR2LAB).astype(np.float32)
            o = self._mahalanobis(lab)
        else:
            lab, o = None, np.zeros((8, 8), dtype=np.float32)

        self.prev_gray = cur
        self._last_lab = lab
        return FrameFeatures(seq=seq, w=w, w_capped=w_capped,
                             total_mass=float(w_capped.sum()), n_blobs=n_blobs,
                             o=o, rect=rect, shift=(dx, dy), warnings=warnings)

    def _unbump(self, bgr: np.ndarray, gray: np.ndarray):
        """Put a knocked camera back where the calibration thinks it is.

        A knock turns the tripod as well as moving it, so undoing it with a
        translation leaves several pixels of error at the board edges — enough
        to light up every edge as change and bury the one piece that moved. What
        did *not* move is the board relative to itself, so re-finding its
        corners and warping them back onto the start frame's corners undoes the
        knock exactly, and leaves the calibration, the mask and the silhouettes
        untouched.
        """
        cal = recalibrate(bgr, self._track)
        if cal.method == "failed" or "drift" in cal.warnings:
            return bgr, gray, False
        self._track = cal
        h = cv2.getPerspectiveTransform(
            np.asarray(cal.corners, dtype=np.float32),
            np.asarray(self.calib.corners, dtype=np.float32))
        out = cv2.warpPerspective(bgr, h, (bgr.shape[1], bgr.shape[0]),
                                  flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
        return out, cv2.cvtColor(out, cv2.COLOR_BGR2GRAY), True

    # -- the change map ---------------------------------------------------

    def _change_map(self, change: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
        """Coarse change map, and the same map with each region's weight capped.

        Connected regions that never touch the board interior are dropped: a
        body passing behind the far edge lives entirely in the strip above it.
        Everything that survives is capped at ``cap_area`` pixels, so a forearm
        crossing the corner of the board costs a bounded amount rather than
        deciding the frame.
        """
        p = self.params
        ds, hh, ww = self.ds, self.bank.h, self.bank.w
        n, labels, stats, _ = cv2.connectedComponentsWithStats(change, 8)
        keep = np.zeros(n, dtype=bool)
        scale = np.ones(n, dtype=np.float32)
        board = self.board_mask > 0
        cap = float(p["cap_area"])
        n_blobs = 0
        for i in range(1, n):
            area = float(stats[i, cv2.CC_STAT_AREA])
            if area < p["min_area"]:
                continue
            comp = labels == i
            if not (comp & board).any():
                continue
            keep[i] = True
            scale[i] = min(1.0, cap / area)
            n_blobs += 1

        kept = keep[labels]
        crop = (kept[:hh * ds, :ww * ds]).astype(np.float32)
        w = crop.reshape(hh, ds, ww, ds).sum(axis=(1, 3))
        scaled = (kept * scale[labels])[:hh * ds, :ww * ds].astype(np.float32)
        w_capped = scaled.reshape(hh, ds, ww, ds).sum(axis=(1, 3))
        return w, w_capped, n_blobs
