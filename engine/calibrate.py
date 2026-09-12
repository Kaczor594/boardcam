"""Find the board in a photograph, and work out which way round it is.

Camera position, lighting, board, pieces and which side white is on all change
between games, so every game is calibrated from its own start-position frame.

The detector is a grid finder, not a quad finder. It clusters Hough segments
into the board's two edge directions, finds each direction's vanishing point,
and then exploits one fact: if ``s`` is where a line of one family crosses a
fixed transversal and ``s_inf`` is where the *other* family's vanishing point
crosses it, then ``w = 1/(s - s_inf)`` is an affine function of the board
coordinate. So the nine grid lines of a family, which are anything but evenly
spaced in the image, become an arithmetic progression in ``w``. Fitting that
progression pools all nine lines into the two outer ones, which is why the
corners come out sharper than any single edge detection would give — and it is
also what lets the outer edges be *reconstructed* when they are too low in
contrast to be detected at all.

Orientation then comes from the pixels: which pair of edge bands is occupied
(all eight cells full, not four), which of those two bands holds the brighter
pieces, and which colour phase the empty middle ranks are in. White's band plus
the phase fix a1 uniquely, because a1 is dark.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .rectify import board_to_rect, checker_score, homography

WORK_EDGE = 1024
RECT_SIZE = 512
CHECKER_MIN = 0.45
MIN_FAMILY_LINES = 7
MIN_RUN = 6          # consecutive grid lines needed to call a family a grid


@dataclass
class Calibration:
    """Where the board is and which way round it is.

    ``corners`` is the answer to both: the four board corners in image pixels,
    ordered ``[a1, h1, h8, a8]``. Everything else is derived from it.
    """

    corners: np.ndarray                 # (4,2) float64, board order [a1,h1,h8,a8]
    corners_image: np.ndarray           # (4,2) the raw detected quad, image order
    H: np.ndarray                       # (3,3) image -> rectified board of `size`
    size: int
    square_is_light: np.ndarray         # (8,8) bool, indexed [file][rank]
    camera_side: str                    # "rank1" | "fileh" | "rank8" | "filea"
    camera_side_idx: int
    score: float                        # checker score, 0..1; >= CHECKER_MIN is good
    white_margin: float                 # luminance gap between the two piece bands
    ok: bool
    warnings: list[str] = field(default_factory=list)
    method: str = "grid"

    def flipped(self) -> "Calibration":
        """The same board with white and black swapped.

        Phase 3's second orientation check: frame 1 must change squares on
        white's side of the board. If it changed black's instead, the piece
        luminances lied and the whole calibration is 180 degrees out, so the
        tracker restarts from this.
        """
        rot = np.array([self.corners[2], self.corners[3],
                        self.corners[0], self.corners[1]])
        c = Calibration(
            corners=rot, corners_image=self.corners_image,
            H=homography(rot, self.size), size=self.size,
            square_is_light=self.square_is_light.copy(),
            camera_side=_edge_name_after_flip(self.camera_side),
            camera_side_idx=(self.camera_side_idx + 2) % 4,
            score=self.score, white_margin=-self.white_margin, ok=self.ok,
            warnings=list(self.warnings) + ["flipped"], method=self.method,
        )
        return c

    def to_json(self) -> dict:
        return {
            "corners": np.asarray(self.corners).round(3).tolist(),
            "corners_image": np.asarray(self.corners_image).round(3).tolist(),
            "size": self.size,
            "camera_side": self.camera_side,
            "camera_side_idx": self.camera_side_idx,
            "score": round(float(self.score), 4),
            "white_margin": round(float(self.white_margin), 3),
            "ok": bool(self.ok),
            "warnings": list(self.warnings),
            "method": self.method,
            "square_is_light": np.asarray(self.square_is_light).tolist(),
        }


_FLIP = {"rank1": "rank8", "rank8": "rank1", "filea": "fileh", "fileh": "filea"}


def _edge_name_after_flip(name: str) -> str:
    return _FLIP[name]


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def load_frames(paths) -> np.ndarray:
    """Read one or more burst candidates and reduce them to a single image."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    imgs = []
    for p in paths:
        im = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if im is not None:
            imgs.append(im)
    if not imgs:
        raise FileNotFoundError(f"no readable frames in {list(paths)!r}")
    if len(imgs) == 1:
        return imgs[0]
    shape = imgs[0].shape
    imgs = [i for i in imgs if i.shape == shape]
    return np.median(np.stack(imgs), axis=0).astype(np.uint8)


# --------------------------------------------------------------------------
# Lines, families, vanishing points
# --------------------------------------------------------------------------

# (canny_lo, canny_hi, hough_threshold, min_len_frac, max_gap_frac). Level 0 is
# the normal pass; level 1 is the fallback for boards whose border is the same
# colour as the dark squares, where the outer grid lines barely register.
_HOUGH_LEVELS = [
    (35, 110, 40, 0.035, 0.020),
    (18, 65, 24, 0.022, 0.035),
]


def _segments(gray: np.ndarray, mask: np.ndarray | None = None,
              level: int = 0) -> np.ndarray:
    lo, hi, thr, minlen, gap = _HOUGH_LEVELS[level]
    g = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    g = cv2.GaussianBlur(g, (0, 0), 1.0)
    edges = cv2.Canny(g, lo, hi, apertureSize=3)
    if mask is not None:
        edges = cv2.bitwise_and(edges, mask)
    n = max(gray.shape)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1440, threshold=thr,
                            minLineLength=int(minlen * n), maxLineGap=int(gap * n))
    if lines is None:
        return np.zeros((0, 4), dtype=np.float64)
    return lines.reshape(-1, 4).astype(np.float64)


def _line_eq(seg: np.ndarray) -> np.ndarray:
    """Normalised homogeneous line through a segment (a^2 + b^2 = 1)."""
    p0 = np.array([seg[0], seg[1], 1.0])
    p1 = np.array([seg[2], seg[3], 1.0])
    l = np.cross(p0, p1)
    n = math.hypot(l[0], l[1])
    return l / n if n > 1e-9 else l


def _cluster_families(segs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split segments into the board's two edge directions (length-weighted)."""
    d = segs[:, 2:] - segs[:, :2]
    length = np.hypot(d[:, 0], d[:, 1])
    theta = np.arctan2(d[:, 1], d[:, 0])
    feat = np.column_stack([np.cos(2 * theta), np.sin(2 * theta)])

    best = None
    for start in (0.0, math.pi / 4, math.pi / 8, 3 * math.pi / 8):
        c = np.array([[math.cos(2 * start), math.sin(2 * start)],
                      [math.cos(2 * (start + math.pi / 2)),
                       math.sin(2 * (start + math.pi / 2))]])
        lab = np.zeros(len(feat), dtype=int)
        for _ in range(30):
            dist = ((feat[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
            lab = dist.argmin(axis=1)
            moved = 0.0
            for k in (0, 1):
                m = lab == k
                if not m.any():
                    continue
                v = (feat[m] * length[m, None]).sum(axis=0)
                nv = np.linalg.norm(v)
                if nv > 1e-9:
                    v = v / nv
                    moved += float(np.linalg.norm(v - c[k]))
                    c[k] = v
            if moved < 1e-6:
                break
        inertia = float((length * ((feat - c[lab]) ** 2).sum(axis=1)).sum())
        if best is None or inertia < best[0]:
            best = (inertia, lab.copy())
    lab = best[1]
    return np.flatnonzero(lab == 0), np.flatnonzero(lab == 1)


def _vp_residual(segs: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Angular residual, in radians, of each segment against a vanishing point."""
    mids = np.column_stack([(segs[:, 0] + segs[:, 2]) / 2, (segs[:, 1] + segs[:, 3]) / 2])
    dirs = segs[:, 2:] - segs[:, :2]
    n = np.linalg.norm(dirs, axis=1, keepdims=True)
    dirs = dirs / np.where(n < 1e-9, 1.0, n)
    to_v = v[:2][None, :] - v[2] * mids
    nn = np.linalg.norm(to_v, axis=1, keepdims=True)
    to_v = to_v / np.where(nn < 1e-9, 1.0, nn)
    cosang = np.abs((to_v * dirs).sum(axis=1))
    return np.arccos(np.clip(cosang, -1, 1))


def _vanishing_point(segs: np.ndarray, rng: np.random.Generator,
                     iters: int = 300, tol_deg: float = 1.6) -> tuple[np.ndarray, np.ndarray]:
    """RANSAC vanishing point for a family. Returns (v homogeneous, inlier mask)."""
    if len(segs) < 2:
        return np.array([0.0, 0.0, 1.0]), np.zeros(len(segs), bool)
    lines = np.array([_line_eq(x) for x in segs])
    length = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    tol = math.radians(tol_deg)

    best_m, best_w = None, -1.0
    idx = np.arange(len(segs))
    for _ in range(iters):
        i, j = rng.choice(idx, 2, replace=False)
        v = np.cross(lines[i], lines[j])
        nv = np.linalg.norm(v)
        if nv < 1e-12:
            continue
        v = v / nv
        m = _vp_residual(segs, v) < tol
        wsum = float(length[m].sum())
        if wsum > best_w:
            best_w, best_m = wsum, m
    if best_m is None or best_m.sum() < 2:
        return np.array([0.0, 0.0, 1.0]), np.zeros(len(segs), bool)

    m = best_m
    v = np.array([0.0, 0.0, 1.0])
    for _ in range(4):
        L = lines[m] * length[m, None]
        _, _, vt = np.linalg.svd(L)
        v = vt[-1] / np.linalg.norm(vt[-1])
        m2 = _vp_residual(segs, v) < tol
        if m2.sum() < 2:
            break
        if (m2 == m).all():
            m = m2
            break
        m = m2
    return v, m


def _two_families(segs: np.ndarray, rng: np.random.Generator, tol_deg: float = 1.6):
    """Two vanishing points and the segments each one explains.

    The angular clustering only seeds this. What decides the split is the
    vanishing points themselves: a forearm lying across the board points roughly
    along a grid direction but does not pass through that direction's vanishing
    point, so re-assigning by VP residual sheds it.
    """
    ia, ib = _cluster_families(segs)
    if len(ia) < MIN_FAMILY_LINES or len(ib) < MIN_FAMILY_LINES:
        return None
    va, _ = _vanishing_point(segs[ia], rng)
    vb, _ = _vanishing_point(segs[ib], rng)
    tol = math.radians(tol_deg)
    ma = mb = None
    for _ in range(3):
        ra = _vp_residual(segs, va)
        rb = _vp_residual(segs, vb)
        ma = (ra < tol) & (ra <= rb)
        mb = (rb < tol) & (rb < ra)
        if ma.sum() < MIN_FAMILY_LINES or mb.sum() < MIN_FAMILY_LINES:
            return None
        va2, _ = _vanishing_point(segs[ma], rng, iters=120)
        vb2, _ = _vanishing_point(segs[mb], rng, iters=120)
        if np.allclose(va2, va) and np.allclose(vb2, vb):
            break
        va, vb = va2, vb2
    return va, vb, np.flatnonzero(ma), np.flatnonzero(mb)


# --------------------------------------------------------------------------
# The arithmetic progression that is the grid
# --------------------------------------------------------------------------

@dataclass
class Family:
    v: np.ndarray            # vanishing point (homogeneous)
    origin: np.ndarray       # (2,) a point on the transversal
    udir: np.ndarray         # (2,) unit direction of the transversal
    s_inf: float | None      # where the *other* family's VP sits on it
    a: float                 # w = a + b * k
    b: float
    kmin: int
    kmax: int
    n_inliers: int

    def w_of(self, k: int | float) -> float:
        return self.a + self.b * k

    def line(self, k: int | float) -> np.ndarray:
        """Homogeneous image line of this family at board index ``k``."""
        w = self.w_of(k)
        if self.s_inf is None:
            s = w
        else:
            if abs(w) < 1e-12:
                return np.array([np.nan, np.nan, np.nan])
            s = self.s_inf + 1.0 / w
        p = self.origin + s * self.udir
        return np.cross(np.array([p[0], p[1], 1.0]), self.v)


def _coords_on_transversal(lines: np.ndarray, weights: np.ndarray,
                           v_other: np.ndarray, centre: np.ndarray,
                           dedup_px: float = 6.0):
    """Map each line of a family to ``w``, in which the board grid is evenly spaced.

    Several Hough segments usually land on the same grid line. They are merged
    here, on the transversal where they are still plain pixels — before the
    reciprocal, which would turn a cluster of near-duplicates into a fake
    progression with a tiny spacing and swallow the real grid.
    """
    c_h = np.array([centre[0], centre[1], 1.0])
    ref = np.cross(c_h, v_other)          # a line of the *other* family, through the centre
    n = math.hypot(ref[0], ref[1])
    if n < 1e-9:
        return None
    ref = ref / n
    udir = np.array([-ref[1], ref[0]])     # along the transversal

    if abs(v_other[2]) < 1e-12 * max(1.0, float(np.linalg.norm(v_other[:2]))):
        s_inf = None
    else:
        vo = v_other[:2] / v_other[2]
        s_inf = float((vo - centre) @ udir)

    ss, ww = [], []
    for l, wt in zip(lines, weights):
        p = np.cross(l, ref)
        if abs(p[2]) < 1e-12:
            continue
        ss.append(float((p[:2] / p[2] - centre) @ udir))
        ww.append(float(wt))
    if len(ss) < MIN_FAMILY_LINES:
        return None

    order = np.argsort(ss)
    sv = np.array(ss)[order]
    wv = np.array(ww)[order]
    gs, gw = [], []
    cur_s, cur_w = [sv[0]], [wv[0]]
    for i in range(1, len(sv)):
        if sv[i] - cur_s[-1] <= dedup_px:
            cur_s.append(sv[i]); cur_w.append(wv[i])
        else:
            gs.append(float(np.average(cur_s, weights=cur_w))); gw.append(float(sum(cur_w)))
            cur_s, cur_w = [sv[i]], [wv[i]]
    gs.append(float(np.average(cur_s, weights=cur_w))); gw.append(float(sum(cur_w)))
    s_arr, w_arr = np.array(gs), np.array(gw)
    if len(s_arr) < MIN_FAMILY_LINES:
        return None

    if s_inf is None:
        coord = s_arr
    else:
        ds = s_arr - s_inf
        keep = np.abs(ds) > 1e-9
        s_arr, w_arr, ds = s_arr[keep], w_arr[keep], ds[keep]
        coord = 1.0 / ds
    ok = np.isfinite(coord)
    if ok.sum() < MIN_FAMILY_LINES:
        return None
    return coord[ok], w_arr[ok], centre, udir, s_inf


def _longest_run(idx: np.ndarray) -> tuple[int, int, int]:
    """Longest consecutive run in a set of integer indices: (length, start, end)."""
    u = np.unique(idx.astype(int))
    if len(u) == 0:
        return 0, 0, 0
    best = (1, int(u[0]), int(u[0]))
    run_start = prev = u[0]
    for k in u[1:]:
        if k != prev + 1:
            if prev - run_start + 1 > best[0]:
                best = (int(prev - run_start + 1), int(run_start), int(prev))
            run_start = k
        prev = k
    if prev - run_start + 1 > best[0]:
        best = (int(prev - run_start + 1), int(run_start), int(prev))
    return best


TOL = 0.18          # inlier half-width, as a fraction of the grid spacing


def _pick_one_per_index(k: np.ndarray, res: np.ndarray, inl: np.ndarray) -> np.ndarray:
    """Keep at most one line per grid index — the one that fits best.

    A board has a border, and a border has its own outer edge running parallel
    to the outermost grid line a fraction of a square away. Left in, it drags
    the least-squares fit outward at exactly the two indices that become the
    board's corners.
    """
    out = np.zeros_like(inl)
    for idx in np.unique(k[inl].astype(int)):
        m = inl & (k.astype(int) == idx)
        if not m.any():
            continue
        j = np.flatnonzero(m)[np.argmin(res[m])]
        out[j] = True
    return out


def _fit_progression(w: np.ndarray, weight: np.ndarray):
    """Find ``w_k = a + b k`` fitting the longest evenly spaced *run* of lines.

    Scoring on the longest consecutive run, not on the raw inlier count, is what
    keeps the fit honest: a spacing three times too small also "explains" every
    line, but it explains them as indices 0, 3, 6, 9 with holes in between.
    """
    order = np.argsort(w)
    ws, wt = w[order], weight[order]
    m = len(ws)
    # Each candidate is an (anchor, spacing) pair taken from two observed lines
    # assumed to be k indices apart. Anchoring on an observed line matters: a
    # correct spacing hung off a spurious line lines up with nothing.
    cands = []
    for i in range(m):
        for j in range(i + 1, m):
            d = ws[j] - ws[i]
            if d <= 0:
                continue
            for k in range(1, 9):
                cands.append((ws[i], d / k))
    if not cands:
        return None

    def evaluate(a, b):
        k = np.round((ws - a) / b)
        res = np.abs(ws - (a + b * k))
        inl = res < TOL * abs(b)
        if inl.sum() < MIN_FAMILY_LINES:
            return None
        inl = _pick_one_per_index(k, res, inl)
        run_len, lo, hi = _longest_run(k[inl])
        if run_len < MIN_RUN:
            return None
        sel = inl & (k >= lo) & (k <= hi)
        cost = float((res[sel] * wt[sel]).sum() / (wt[sel].sum() * abs(b)))
        return (-min(run_len, 11), cost), k, sel, lo, hi

    best = None
    for a0, d in cands:
        got = evaluate(a0, d)
        if got is None:
            continue
        key = got[0]
        if best is None or key < best[0][0]:
            best = (got, a0, d)
    if best is None:
        return None

    (key, k, sel, lo, hi), a, b = best
    for _ in range(6):
        kk, yy, ww = k[sel], ws[sel], wt[sel]
        A = np.column_stack([np.ones(len(kk)), kk]) * np.sqrt(ww)[:, None]
        coef, *_ = np.linalg.lstsq(A, yy * np.sqrt(ww), rcond=None)
        a, b = float(coef[0]), float(coef[1])
        if abs(b) < 1e-15:
            return None
        got = evaluate(a, b)
        if got is None:
            break
        _key, k_new, sel_new, lo, hi = got
        if sel_new.shape == sel.shape and bool((sel_new == sel).all()):
            k, sel = k_new, sel_new
            break
        k, sel = k_new, sel_new
    return a, b, int(lo), int(hi), int(sel.sum())


def _family(segs_all: np.ndarray, sel: np.ndarray, v_self: np.ndarray,
            v_other: np.ndarray, centre: np.ndarray) -> Family | None:
    if len(sel) < MIN_FAMILY_LINES:
        return None
    subset = segs_all[sel]
    lines = np.array([_line_eq(x) for x in subset])
    length = np.hypot(subset[:, 2] - subset[:, 0], subset[:, 3] - subset[:, 1])
    got = _coords_on_transversal(lines, length, v_other, centre)
    if got is None:
        return None
    coord, _wt, origin, udir, s_inf = got
    fit = _fit_progression(coord, _wt)
    if fit is None:
        return None
    a, b, kmin, kmax, n = fit
    return Family(v=v_self, origin=origin, udir=udir, s_inf=s_inf, a=a, b=b,
                  kmin=kmin, kmax=kmax, n_inliers=n)


def _windows(fam: Family, slack: int = 1) -> list[int]:
    """Candidate start indices for the board's nine lines.

    The run of detected lines has to sit *inside* the nine, so the start index
    runs from ``kmax - 8`` up to ``kmin``. A full run of nine pins it exactly; a
    short run, which is what a shallow camera gives in the direction where the
    grid lines are foreshortened and hidden behind pieces, leaves a handful of
    placements for the checker score to choose between.
    """
    lo = fam.kmax - 8 - slack
    hi = fam.kmin + slack
    if hi < lo:
        lo = hi = (fam.kmin + fam.kmax - 8) // 2
    centre = (fam.kmin + fam.kmax - 8) / 2.0
    out = list(range(lo, hi + 1))
    out.sort(key=lambda x: abs(x - centre))
    return out[:6]


def _intersect(l1: np.ndarray, l2: np.ndarray) -> np.ndarray | None:
    p = np.cross(l1, l2)
    if not np.isfinite(p).all() or abs(p[2]) < 1e-12:
        return None
    return p[:2] / p[2]


def _quad(fa: Family, sa: int, fb: Family, sb: int) -> np.ndarray | None:
    a0, a8 = fa.line(sa), fa.line(sa + 8)
    b0, b8 = fb.line(sb), fb.line(sb + 8)
    pts = [_intersect(a0, b0), _intersect(a8, b0), _intersect(a8, b8), _intersect(a0, b8)]
    if any(p is None for p in pts):
        return None
    q = np.array(pts)
    if not np.isfinite(q).all():
        return None
    area = 0.5 * abs(np.dot(q[:, 0], np.roll(q[:, 1], -1)) - np.dot(q[:, 1], np.roll(q[:, 0], -1)))
    if area < 1000:
        return None
    return q


# --------------------------------------------------------------------------
# Sub-pixel refinement against the interior saddle points
# --------------------------------------------------------------------------

def _refine_pass(gray: np.ndarray, quad: np.ndarray, win: int, keep_px: float,
                 max_shift: float) -> tuple[np.ndarray, int]:
    """One snap of the quad onto the 7x7 interior grid crossings."""
    S = RECT_SIZE
    Hinv = np.linalg.inv(homography(quad, S))
    step = S / 8.0
    rect_pts, img_pts = [], []
    for i in range(1, 8):
        for j in range(1, 8):
            rp = np.array([i * step, j * step, 1.0])
            ip = Hinv @ rp
            if abs(ip[2]) < 1e-12:
                continue
            rect_pts.append(rp[:2])
            img_pts.append(ip[:2] / ip[2])
    if len(img_pts) < 12:
        return quad, 0
    src = np.array(img_pts, dtype=np.float32).reshape(-1, 1, 2)
    h, w = gray.shape[:2]
    pad = win + 2
    inside = ((src[:, 0, 0] > pad) & (src[:, 0, 0] < w - pad) &
              (src[:, 0, 1] > pad) & (src[:, 0, 1] < h - pad))
    if inside.sum() < 12:
        return quad, 0
    src = src[inside]
    rect_pts = np.array(rect_pts, dtype=np.float32)[inside]
    moved = src.copy()
    cv2.cornerSubPix(gray, moved, (win, win), (-1, -1),
                     (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.01))
    delta = np.linalg.norm(moved - src, axis=2).ravel()
    keep = np.isfinite(delta) & (delta < keep_px)
    if keep.sum() < 12:
        return quad, 0
    Href, inliers = cv2.findHomography(rect_pts[keep], moved[keep].reshape(-1, 2),
                                       cv2.RANSAC, 2.0)
    if Href is None or inliers is None or int(inliers.sum()) < 12:
        return quad, 0
    br = board_to_rect(S)
    p = Href @ np.column_stack([br, np.ones(4)]).T.astype(np.float64)
    if np.abs(p[2]).min() < 1e-12:
        return quad, 0
    new = (p[:2] / p[2]).T
    if not np.isfinite(new).all():
        return quad, 0
    if np.linalg.norm(new - quad, axis=1).max() > max_shift:
        return quad, 0
    return new, int(inliers.sum())


def _refine(gray: np.ndarray, quad: np.ndarray, first_win: int = 11) -> tuple[np.ndarray, int]:
    """Snap to the 7x7 interior grid crossings, which are true checker saddles.

    Three passes with a shrinking search window. The first one has to be wide,
    because the outer grid lines come out of an extrapolated fit and can start
    tens of pixels out; the last one is narrow so it cannot wander onto the
    neighbouring saddle half a square away.
    """
    total = 0
    cur = quad
    for win, keep_px, max_shift in ((first_win, 16.0, 90.0), (7, 6.0, 30.0), (5, 3.0, 12.0)):
        nxt, n = _refine_pass(gray, cur, win, keep_px, max_shift)
        if n:
            cur, total = nxt, n
    return cur, total


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------

def _cell_strip(rect: np.ndarray, u: int, v: int, away: tuple[int, int],
                frac: float = 0.30, inset: float = 0.08) -> np.ndarray:
    """A strip of provisional cell (u, v), taken on the side ``away`` from the camera."""
    S = rect.shape[0]
    s = S / 8.0
    x0, y0 = int(round(u * s)), int(round(v * s))
    x1, y1 = int(round(x0 + s)), int(round(y0 + s))
    pad = int(round(s * inset))
    x0, y0, x1, y1 = x0 + pad, y0 + pad, x1 - pad, y1 - pad
    dx, dy = away
    if dx:
        span = int(round((x1 - x0) * frac))
        x0, x1 = (x1 - span, x1) if dx > 0 else (x0, x0 + span)
    elif dy:
        span = int(round((y1 - y0) * frac))
        y0, y1 = (y1 - span, y1) if dy > 0 else (y0, y0 + span)
    return rect[y0:y1, x0:x1]


def _cell_full(rect: np.ndarray, u: int, v: int, inset: float = 0.08) -> np.ndarray:
    S = rect.shape[0]
    s = S / 8.0
    pad = int(round(s * inset))
    x0, y0 = int(round(u * s)) + pad, int(round(v * s)) + pad
    x1, y1 = int(round((u + 1) * s)) - pad, int(round((v + 1) * s)) - pad
    return rect[y0:y1, x0:x1]


# Rect edge i (of corners [(0,0),(S,0),(S,S),(0,S)]) -> unit vector away from that edge.
_AWAY_FROM_EDGE = {0: (0, -1), 1: (1, 0), 2: (0, 1), 3: (-1, 0)}


def _orient(bgr: np.ndarray, quad: np.ndarray) -> dict | None:
    """Work out which quad corner is a1, from the pixels alone."""
    S = RECT_SIZE
    Hprov = cv2.getPerspectiveTransform(
        np.asarray(quad, dtype=np.float32),
        np.array([[0, 0], [S, 0], [S, S], [0, S]], dtype=np.float32))
    rect = cv2.warpPerspective(bgr, Hprov, (S, S))
    lab = cv2.cvtColor(rect, cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0].astype(np.float64)

    edge_len = [float(np.linalg.norm(quad[(i + 1) % 4] - quad[i])) for i in range(4)]
    cam_edge = int(np.argmax(edge_len))
    away = _AWAY_FROM_EDGE[cam_edge]

    # Occupancy: how far each cell strays from a flat, empty square.
    occ = np.zeros((8, 8))
    for u in range(8):
        for v in range(8):
            p = _cell_full(L, u, v)
            occ[u, v] = float(p.std()) if p.size else 0.0

    # Pieces fill *every* cell of the two back-rank bands, but only half the
    # cells of a file band. That asymmetry says which axis the players are on.
    def axis_score(ax: int) -> float:
        band = occ[:, [0, 1, 6, 7]] if ax == 1 else occ[[0, 1, 6, 7], :]
        mid = occ[:, 2:6] if ax == 1 else occ[2:6, :]
        outer_min = float(np.min(band.min(axis=ax)))
        return float(band.mean() - mid.mean()) + 0.5 * outer_min
    ax = 1 if axis_score(1) >= axis_score(0) else 0     # 1: bands along v, 0: along u

    # Colour phase from the empty middle, on the strip away from the camera.
    mids = [(u, v) for u in range(8) for v in range(8)
            if (2 <= v <= 5 if ax == 1 else 2 <= u <= 5)]
    vals, parity = [], []
    for u, v in mids:
        p = _cell_strip(L, u, v, away)
        if p.size:
            vals.append(float(np.median(p)))
            parity.append(1.0 if (u + v) % 2 == 0 else -1.0)
    if len(vals) < 16:
        return None
    vals = np.array(vals) - np.mean(vals)
    parity = np.array(parity)
    den = np.linalg.norm(vals) * np.linalg.norm(parity)
    if den < 1e-9:
        return None
    corr = float((vals * parity).sum() / den)
    even_is_light = corr > 0
    phase_score = abs(corr)

    # Empty-square reference colours, for telling piece pixels from board pixels.
    ref = []
    for u, v in mids:
        p = _cell_strip(lab, u, v, away)
        if p.size:
            ref.append(np.median(p.reshape(-1, 3), axis=0))
    ref = np.array(ref)
    kmeans_lo = ref[ref[:, 0] <= np.median(ref[:, 0])].mean(axis=0)
    kmeans_hi = ref[ref[:, 0] > np.median(ref[:, 0])].mean(axis=0)

    def band_piece_luma(cells) -> float:
        px = []
        for u, v in cells:
            p = _cell_full(lab, u, v)
            if p.size:
                px.append(p.reshape(-1, 3))
        if not px:
            return 0.0
        px = np.vstack(px).astype(np.float64)
        d = np.minimum(np.linalg.norm(px - kmeans_lo, axis=1),
                       np.linalg.norm(px - kmeans_hi, axis=1))
        cut = np.quantile(d, 0.5)
        sel = px[d >= cut]
        return float(np.median(sel[:, 0])) if len(sel) else 0.0

    if ax == 1:
        band_a = [(u, v) for u in range(8) for v in (0, 1)]
        band_b = [(u, v) for u in range(8) for v in (6, 7)]
        edge_a, edge_b = 0, 2          # rect edges y=0 and y=S
    else:
        band_a = [(u, v) for v in range(8) for u in (0, 1)]
        band_b = [(u, v) for v in range(8) for u in (6, 7)]
        edge_a, edge_b = 3, 1          # rect edges x=0 and x=S

    la, lb = band_piece_luma(band_a), band_piece_luma(band_b)
    white_is_a = la >= lb
    white_edge = edge_a if white_is_a else edge_b
    margin = abs(la - lb)

    # Rect edge k runs from quad corner k to corner k+1.
    i, j = white_edge, (white_edge + 1) % 4
    cell_i = [(0, 0), (7, 0), (7, 7), (0, 7)][i]
    even_light_i = (cell_i[0] + cell_i[1]) % 2 == 0
    corner_i_is_light = even_light_i if even_is_light else not even_light_i
    a1_idx, h1_idx = (j, i) if corner_i_is_light else (i, j)

    step = 1 if h1_idx == (a1_idx + 1) % 4 else -1
    order = [(a1_idx + step * t) % 4 for t in range(4)]
    corners = np.array([quad[o] for o in order], dtype=np.float64)
    return {"corners": corners, "phase_score": phase_score, "white_margin": margin,
            "axis": ax, "cam_edge": cam_edge}


EDGE_NAMES = ("rank1", "fileh", "rank8", "filea")


def _camera_side(corners: np.ndarray) -> tuple[int, str]:
    lens = [float(np.linalg.norm(corners[(i + 1) % 4] - corners[i])) for i in range(4)]
    k = int(np.argmax(lens))
    return k, EDGE_NAMES[k]


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def _detect(bgr: np.ndarray, mask: np.ndarray | None = None,
            seed: int = 0, level: int = 0) -> list[np.ndarray]:
    """Candidate board quads in full-image coordinates, best first."""
    h, w = bgr.shape[:2]
    scale = WORK_EDGE / max(h, w)
    scale = min(scale, 1.0)
    small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
        if scale < 1.0 else bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    m = None
    if mask is not None:
        m = cv2.resize(mask, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
    segs = _segments(gray, m, level)
    if len(segs) < 2 * MIN_FAMILY_LINES:
        return []
    rng = np.random.default_rng(seed)
    fam = _two_families(segs, rng)
    if fam is None:
        return []
    va, vb, ia, ib = fam
    centre = np.array([small.shape[1] / 2.0, small.shape[0] / 2.0])
    fa = _family(segs, ia, va, vb, centre)
    fb = _family(segs, ib, vb, va, centre)
    if fa is None or fb is None:
        return []
    out = []
    for sa in _windows(fa):
        for sb in _windows(fb):
            q = _quad(fa, sa, fb, sb)
            if q is not None:
                out.append(q / scale)
    return out


def _shift_grid(quad: np.ndarray, du: int, dv: int, size: int = RECT_SIZE) -> np.ndarray | None:
    """The same grid, moved by whole squares: which nine lines are the board."""
    try:
        Hinv = np.linalg.inv(homography(quad, size))
    except np.linalg.LinAlgError:
        return None
    step = size / 8.0
    dst = board_to_rect(size).astype(np.float64) + np.array([du * step, dv * step])
    p = Hinv @ np.column_stack([dst, np.ones(4)]).T
    if np.abs(p[2]).min() < 1e-12:
        return None
    out = (p[:2] / p[2]).T
    return out if np.isfinite(out).all() else None


def _evaluate(bgr: np.ndarray, gray: np.ndarray, quad: np.ndarray, size: int,
              refine: bool) -> dict | None:
    """Refine a candidate quad, orient it, and score how board-like it is."""
    qq, n_saddle = (_refine(gray, quad) if refine else (quad, 0))
    info = _orient(bgr, qq)
    if info is None:
        return None
    corners = info["corners"]
    _, cam_name = _camera_side(corners)
    rect = cv2.warpPerspective(bgr, homography(corners, size), (size, size))
    sc, _phase = checker_score(rect, range(2, 6), cam_name)
    # A candidate whose two piece bands are indistinguishable in brightness has
    # almost certainly been oriented by a coin toss, so the white margin votes
    # on the quad as well as reporting on it.
    total = (sc + 0.25 * info["phase_score"]
             + 0.20 * min(info["white_margin"] / 25.0, 1.0))
    return {"total": total, "score": sc, "info": info, "corners": corners,
            "quad": qq, "n_saddle": n_saddle, "camera_side": cam_name}


_NEIGHBOURS = [(-1, 0), (1, 0), (0, -1), (0, 1),
               (-1, -1), (-1, 1), (1, -1), (1, 1)]


def _climb(bgr: np.ndarray, gray: np.ndarray, start: dict, size: int,
           refine: bool, rounds: int = 3) -> dict:
    """Walk the grid a square at a time while the board looks more like a board.

    The line fit can land one square out in whichever direction is foreshortened
    — at 30 degrees the far edge runs about 38 px per square against 80 px on
    the near edge — and sub-pixel refinement then locks happily onto that
    shifted grid, because a board shifted by one file is still a checkerboard.
    What gives it away is the eighth file landing on the border instead of on a
    square, which costs real checker score. So: shift, re-refine, re-score, keep
    what improves.
    """
    best = start
    for _ in range(rounds):
        improved = False
        for du, dv in _NEIGHBOURS:
            shifted = _shift_grid(best["quad"], du, dv, size)
            if shifted is None:
                continue
            cand = _evaluate(bgr, gray, shifted, size, refine)
            if cand is not None and cand["total"] > best["total"] + 1e-6:
                best, improved = cand, True
        if not improved:
            break
    return best


def calibrate(frame0_paths, *, size: int = RECT_SIZE, refine: bool = True,
              seed: int = 0) -> Calibration:
    """Calibrate from the start-position frame(s) of one game."""
    bgr = frame0_paths if isinstance(frame0_paths, np.ndarray) else load_frames(frame0_paths)
    return calibrate_image(bgr, size=size, refine=refine, seed=seed)


def calibrate_image(bgr: np.ndarray, *, size: int = RECT_SIZE, refine: bool = True,
                    seed: int = 0, mask: np.ndarray | None = None,
                    candidates: list[np.ndarray] | None = None,
                    climb: bool = True) -> Calibration:
    gray_full = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    warnings: list[str] = []

    def run(quads: list[np.ndarray]) -> list[dict]:
        out = [_evaluate(bgr, gray_full, q, size, refine) for q in quads]
        return sorted((r for r in out if r is not None),
                      key=lambda r: -r["total"])

    if candidates is not None:
        results = run(list(candidates))
    else:
        results = run(_detect(bgr, mask, seed, level=0))
        if not results or results[0]["score"] < CHECKER_MIN:
            extra = run(_detect(bgr, mask, seed, level=1))
            if extra:
                warnings.append("fallback-hough")
            results = sorted(results + extra, key=lambda r: -r["total"])

    if climb and results:
        climbed = [_climb(bgr, gray_full, r, size, refine) for r in results[:3]]
        results = sorted(climbed + results, key=lambda r: -r["total"])

    if not results:
        warnings.append("no-grid")
        h, w = bgr.shape[:2]
        fallback = np.array([[w * 0.15, h * 0.75], [w * 0.85, h * 0.75],
                             [w * 0.85, h * 0.25], [w * 0.15, h * 0.25]])
        return Calibration(
            corners=fallback, corners_image=fallback,
            H=homography(fallback, size), size=size,
            square_is_light=np.array([[(f + r) % 2 == 1 for r in range(8)]
                                      for f in range(8)]),
            camera_side="rank1", camera_side_idx=0, score=0.0, white_margin=0.0,
            ok=False, warnings=warnings, method="failed")

    best = results[0]
    corners = best["corners"]
    idx, name = _camera_side(corners)
    if best["n_saddle"] == 0:
        warnings.append("no-saddle-refine")
    if best["score"] < CHECKER_MIN:
        warnings.append("low-checker-score")
    if best["info"]["white_margin"] < 4.0:
        warnings.append("weak-white-margin")

    return Calibration(
        corners=corners, corners_image=best["quad"],
        H=homography(corners, size), size=size,
        square_is_light=np.array([[(f + r) % 2 == 1 for r in range(8)] for f in range(8)]),
        camera_side=name, camera_side_idx=idx,
        score=float(best["score"]), white_margin=float(best["info"]["white_margin"]),
        ok=best["score"] >= CHECKER_MIN, warnings=warnings,
        method="grid" if best["n_saddle"] else "grid-lines",
    )


def recalibrate(bgr: np.ndarray, prior: Calibration, *, band_px: float = 20.0,
                max_shift: float = 60.0) -> Calibration:
    """Track slow drift (or a tripod bump) without re-solving the whole board.

    The board has not moved relative to itself, only in the frame, so this does
    not re-detect a grid: it re-snaps the previous corners onto the interior
    saddle points of the new frame, which keeps the orientation the start frame
    established. Only if that fails does it fall back to a full detection
    restricted to a band around the previous grid.

    If neither holds up — the checker score drops, or the corners jump further
    than a bump plausibly could — the previous calibration is kept and a
    ``drift`` warning is raised, because a stale board is much cheaper than a
    wrong one.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    def accept(corners: np.ndarray) -> Calibration | None:
        if np.linalg.norm(corners - prior.corners, axis=1).max() > max_shift:
            return None
        idx, name = _camera_side(corners)
        rect = cv2.warpPerspective(bgr, homography(corners, prior.size),
                                   (prior.size, prior.size))
        sc, _ = checker_score(rect, range(2, 6), name)
        if sc < CHECKER_MIN or sc < 0.75 * prior.score:
            return None
        return Calibration(
            corners=corners, corners_image=corners,
            H=homography(corners, prior.size), size=prior.size,
            square_is_light=prior.square_is_light.copy(),
            camera_side=name, camera_side_idx=idx, score=float(sc),
            white_margin=prior.white_margin, ok=True, warnings=[],
            method="tracked")

    snapped, n = _refine(gray, prior.corners, first_win=15)
    if n:
        got = accept(snapped)
        if got is not None:
            return got

    mask = np.zeros(bgr.shape[:2], np.uint8)
    S = prior.size
    Hinv = np.linalg.inv(prior.H)
    step = S / 8.0
    for i in range(9):
        for p0r, p1r in (((i * step, 0.0), (i * step, S)), ((0.0, i * step), (S, i * step))):
            pts = []
            for rp in (p0r, p1r):
                v = Hinv @ np.array([rp[0], rp[1], 1.0])
                if abs(v[2]) < 1e-12:
                    pts = []
                    break
                pts.append(v[:2] / v[2])
            if len(pts) == 2 and np.isfinite(pts).all():
                cv2.line(mask, tuple(np.round(pts[0]).astype(int)),
                         tuple(np.round(pts[1]).astype(int)), 255,
                         thickness=int(max(3, band_px)))

    cal = calibrate_image(bgr, size=prior.size, mask=mask, climb=False)
    if cal.ok:
        got = accept(cal.corners)
        if got is not None:
            got.warnings.append("recovered")
            return got

    out = Calibration(**{**prior.__dict__})
    out.corners = prior.corners.copy()
    out.warnings = list(prior.warnings) + ["drift"]
    return out
