"""Find the playing area of a board in a hand-held photo, and cut square crops.

    python -m engine.gridfind --eval data/labels/green-mat
    python -m engine.gridfind --overlay data/labels/green-mat [--out DIR]
    python -m engine.gridfind --crops data/labels/green-mat

``find_grid(image)`` returns the four outer corners of the 8x8 playing area in
upright full-resolution pixel coordinates (EXIF orientation applied, as
``engine.reader.load_image`` does), clockwise in the image starting from the
corner nearest the image's top-left, plus a ``confidence`` in [0, 1] that is
meant to be low when the corners are wrong. A board's orientation (which
corner is a1) is not the grid finder's job: the reader's home-side call, or a
label, supplies it.

Method (classical, no training):

1. Dark-square mask: the green mat's squares by colour (``green``); for other
   boards (``auto``), the darker of two colour clusters inside the region
   around the image's most chessboard-like area. An opening breaks the
   single-pixel contacts where diagonal squares touch and removes the mat's
   thin frame line and letters.
2. Square-shaped blobs (convex, roughly four-sided) are the candidates.
3. Lattice fit: every good blob seeds a homography from the unit square to
   its outline; blob centroids are mapped through it to lattice coordinates and
   counted as inliers when they land near the centre of a dark cell. The seed
   with most inliers is refitted on all inliers (centroids, then blob vertices
   matched to predicted cell corners, RANSAC) until it stops growing.
4. Extent: the inlier cells span the board. A span of 8 in both directions is
   the board. A shorter span (an edge file hidden under pieces, or off the
   photo) is extended to 8 on the side that is not visibly mat margin; a
   longer one keeps the 8-wide window with most inliers.
5. Lens bend: one radial distortion term about the photo centre, chosen as
   the one under which the inlier vertices fit a single homography best; the
   board corners are distorted back through it. Phone lenses and mats that do
   not lie flat bend the edges a plain homography extrapolates to (photos
   2777-2779, 3044 missed by 0.3-0.5 squares without it).
6. Confidence: inlier share of the 32 dark squares, residual in squares (after
   the lens bend), and how the extent was decided.

Corner truth lives in a label set's ``truth.json`` as ``corners``: the outer
corners of a8, h8, h1 and a1, in that order (board order, so a diagonal photo is
unambiguous). The evaluation compares at the best of the four cyclic
rotations, as ``engine.read_eval`` scores reading at the best orientation.

``square_crops(img, corners)`` cuts one crop per square from the unflattened
photo: sized by the square's width in the image, with the square's footprint
always in the same place (bottom centre) so the target is unambiguous even
when a tall neighbour leans into the crop.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

WORK_EDGE = 1600            # long edge the finder works at
CONF_OK = 0.5               # below this a grid should be checked by hand; not calibrated:
                            # the 2026-11-23 eval had no misses to set it from
BOARD_ORDER = ("a8", "h8", "h1", "a1")


@dataclass
class Grid:
    corners: list[list[float]] | None     # 4 x [x, y], full-res upright, clockwise
    confidence: float
    info: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Masks
# --------------------------------------------------------------------------

def _green_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (36, 45, 22), (95, 255, 230))


def _auto_mask(bgr: np.ndarray) -> np.ndarray:
    """Dark squares of a non-green board: darker of two clusters, locally thresholded."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    L = cv2.GaussianBlur(lab[..., 0], (5, 5), 0)
    k = max(31, (min(bgr.shape[:2]) // 12) | 1)
    return cv2.adaptiveThreshold(L, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, k, 6)


def dark_mask(bgr: np.ndarray, board: str = "green") -> np.ndarray:
    m = _green_mask(bgr) if board == "green" else _auto_mask(bgr)
    k = max(3, round(min(bgr.shape[:2]) / 260)) | 1
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    # Diagonal neighbours touch at a corner point; contours are 8-connected,
    # so one pixel off every edge keeps them apart (sharp edges, synthetic or
    # in focus, would otherwise merge the whole board into one blob).
    return cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3)))


# --------------------------------------------------------------------------
# Blobs
# --------------------------------------------------------------------------

@dataclass
class _Blob:
    c: np.ndarray            # centroid (2,)
    area: float
    quad: np.ndarray | None  # 4x2 clockwise, or None
    pts: np.ndarray          # contour points


def _order_cw(q: np.ndarray) -> np.ndarray:
    """Clockwise in image coordinates (y down), starting nearest the top-left."""
    c = q.mean(axis=0)
    ang = np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0])
    q = q[np.argsort(ang)]
    start = int(np.argmin(q[:, 0] + q[:, 1]))
    return np.roll(q, -start, axis=0)


def _blobs(mask: np.ndarray) -> list[_Blob]:
    h, w = mask.shape
    # CCOMP, not EXTERNAL: the mat's green frame band can be a closed ring,
    # and EXTERNAL would drop every square inside its hole.
    cs, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return []
    out = []
    for c, hh in zip(cs, hier[0]):
        if hh[3] != -1:            # a hole, not a component's outline
            continue
        a = cv2.contourArea(c)
        if a < 2.5e-4 * h * w or a > 0.03 * h * w:
            continue
        hull = cv2.convexHull(c)
        ha = cv2.contourArea(hull)
        if ha <= 0 or a / ha < 0.80:
            continue
        (rw, rh) = cv2.minAreaRect(c)[1]
        if min(rw, rh) <= 0 or max(rw, rh) / min(rw, rh) > 4.0:
            continue
        m = cv2.moments(c)
        cen = np.array([m["m10"] / m["m00"], m["m01"] / m["m00"]])
        approx = cv2.approxPolyDP(hull, 0.06 * cv2.arcLength(hull, True), True).reshape(-1, 2)
        quad = _order_cw(approx.astype(np.float64)) if len(approx) == 4 else None
        out.append(_Blob(cen, a, quad, c.reshape(-1, 2).astype(np.float64)))
    if not out:
        return out
    # Typical square size from the larger half: crumbs of half-hidden squares
    # outnumber whole squares when many pieces stand on the board.
    areas = sorted(b.area for b in out)
    med = float(np.median(areas[len(areas) // 2:]))
    return [b for b in out if 0.12 * med <= b.area <= 4.0 * med]


# --------------------------------------------------------------------------
# Lattice
# --------------------------------------------------------------------------

UNIT = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float64)


def _apply(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    p = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(p, H).reshape(-1, 2)


def _inliers(Hinv: np.ndarray, cents: np.ndarray, tol: float,
             radius: float = 12) -> tuple[np.ndarray, np.ndarray]:
    """Blobs whose centroid lands near a dark cell centre within ``radius`` cells
    of the seed; returns mask and cells."""
    uv = _apply(Hinv, cents) - 0.5
    cell = np.round(uv)
    d = np.abs(uv - cell).max(axis=1)
    dark = (cell.sum(axis=1) % 2) == 0
    ok = (d < tol) & dark & (np.abs(cell) <= radius).all(axis=1)
    return ok, cell.astype(int)


def _fit(blobs: list[_Blob], cells: np.ndarray, idx: np.ndarray,
         H0: np.ndarray, px_per_sq: float) -> np.ndarray:
    """Refit on centroids, then on blob vertices matched to predicted cell corners."""
    src = cells[idx] + 0.5
    dst = np.array([blobs[i].c for i in idx])
    H = H0
    if len(idx) >= 4:
        Hc, _ = cv2.findHomography(src, dst, 0)
        if Hc is not None:
            H = Hc
    S, D = [], []
    for i in idx:
        q = blobs[i].quad
        if q is None:
            continue
        cc = cells[i] + UNIT
        pred = _apply(H, cc)
        for p in q:
            j = int(np.argmin(np.linalg.norm(pred - p, axis=1)))
            if np.linalg.norm(pred[j] - p) < 0.25 * px_per_sq:
                S.append(cc[j]); D.append(p)
    if len(S) >= 8:
        Hv, _ = cv2.findHomography(np.array(S), np.array(D), cv2.RANSAC, 0.06 * px_per_sq)
        if Hv is not None:
            H = Hv
    return H


def _lattice(blobs: list[_Blob]) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    cents = np.array([b.c for b in blobs])
    best = None
    seeds = sorted([i for i, b in enumerate(blobs) if b.quad is not None],
                   key=lambda i: -blobs[i].area)[:40]
    for s in seeds:
        H = cv2.getPerspectiveTransform(UNIT.astype(np.float32),
                                        blobs[s].quad.astype(np.float32)).astype(np.float64)
        px = np.sqrt(blobs[s].area)
        prev = -1
        if abs(np.linalg.det(H)) < 1e-9 or cv2.contourArea(blobs[s].quad.astype(np.float32)) < 0.5 * blobs[s].area:
            continue
        # Grow ring by ring from the seed, refitting in between, so the
        # extrapolation never runs far ahead of the fitted area (strong
        # perspective bends a one-square fit within a few squares).
        for radius in (1, 2, 3, 4, 5, 7, 9, 9):
            try:
                Hinv = np.linalg.inv(H)
            except np.linalg.LinAlgError:
                break
            ok, cells = _inliers(Hinv, cents, 0.25, radius)
            n = int(ok.sum())
            if n < 2 or (radius >= 9 and n <= prev):
                break
            prev = n
            H = _fit(blobs, cells, np.flatnonzero(ok), H, px)
        try:
            ok, cells = _inliers(np.linalg.inv(H), cents, 0.25)
        except np.linalg.LinAlgError:
            continue
        # Same cell claimed twice: keep it once (a split blob).
        n = len({tuple(c) for c in cells[ok]})
        if best is None or n > best[0]:
            best = (n, H, ok, cells)
    if best is None or best[0] < 4:
        return None
    return best[1], best[2], best[3]


# --------------------------------------------------------------------------
# Extent
# --------------------------------------------------------------------------

def _green_frac(mask: np.ndarray, H: np.ndarray, i: int, j: int) -> float | None:
    """Share of dark-mask pixels in cell (i, j), or None when it is off the photo."""
    q = _apply(H, np.array([[i + 0.2, j + 0.2], [i + 0.8, j + 0.2],
                            [i + 0.8, j + 0.8], [i + 0.2, j + 0.8]]))
    h, w = mask.shape
    if (q[:, 0] < 0).any() or (q[:, 1] < 0).any() or (q[:, 0] >= w).any() or (q[:, 1] >= h).any():
        return None
    poly = np.zeros_like(mask)
    cv2.fillConvexPoly(poly, np.round(q).astype(np.int32), 255)
    n = int((poly > 0).sum())
    return float((mask[poly > 0] > 0).sum()) / n if n else None


def _extent(cells: list[tuple[int, int]], axis: int, mask: np.ndarray,
            H: np.ndarray) -> tuple[int, str]:
    """Start index of the 8-wide window along ``axis`` and how it was decided."""
    vals = [c[axis] for c in cells]
    lo, hi = min(vals), max(vals)
    span = hi - lo + 1
    if span == 8:
        return lo, "seen"
    if span > 8:
        counts = {s: sum(s <= v < s + 8 for v in vals) for s in range(lo, hi - 6)}
        return max(counts, key=counts.get), "trimmed"
    other = [c[1 - axis] for c in cells]
    olo, ohi = min(other), max(other)
    # Each candidate window adds columns on one side or the other. A board
    # column alternates: its dark cells show green around whatever piece
    # stands on them, its light cells show none. The mat margin shows none
    # anywhere, and the mat's green frame band shows green on both — so the
    # evidence is green on dark cells minus green on light cells. Cells off
    # the photo are neutral.
    best = None
    for s in range(hi - 7, lo + 1):
        added = [v for v in range(s, s + 8) if v < lo or v > hi]
        dark, light = [], []
        for v in added:
            for o in range(olo - 1, ohi + 2):
                ij = (v, o) if axis == 0 else (o, v)
                f = _green_frac(mask, H, *ij)
                if f is not None:
                    (light if (v + o) % 2 else dark).append(f)
        seen = len(dark)
        score = (np.mean(dark) - (np.mean(light) if light else 0.0)) if dark else 0.02
        if best is None or score > best[0]:
            best = (score, s, seen)
    _, s, seen = best
    return s, ("margin" if seen else "off-photo")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _vertex_pairs(blobs: list[_Blob], cells: np.ndarray, ok: np.ndarray,
                  H: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Inlier blob vertices matched to their lattice cell corners."""
    S, D = [], []
    for i in np.flatnonzero(ok):
        q = blobs[i].quad
        if q is None:
            continue
        cc = cells[i] + UNIT
        pred = _apply(H, cc)
        px = np.sqrt(blobs[i].area)
        for p in q:
            j = int(np.argmin(np.linalg.norm(pred - p, axis=1)))
            if np.linalg.norm(pred[j] - p) < 0.25 * px:
                S.append(cc[j]); D.append(p)
    return np.array(S, np.float64).reshape(-1, 2), np.array(D, np.float64).reshape(-1, 2)


def _undistort(P: np.ndarray, k: float, centre: np.ndarray, norm: float) -> np.ndarray:
    d = (P - centre) / norm
    return centre + (P - centre) * (1 + k * (d * d).sum(axis=1, keepdims=True))


def _distort(P: np.ndarray, k: float, centre: np.ndarray, norm: float) -> np.ndarray:
    """Inverse of ``_undistort`` by fixed-point iteration (k is small)."""
    Q = P.copy()
    for _ in range(20):
        d = (Q - centre) / norm
        Q = centre + (P - centre) / (1 + k * (d * d).sum(axis=1, keepdims=True))
    return Q


K_RANGE = np.linspace(-0.25, 0.25, 51)


def _radial_corners(blobs, cells, ok, H, full: np.ndarray,
                    shape: tuple[int, int]) -> tuple[np.ndarray, float, float]:
    """Board corners with one radial lens-distortion term about the photo centre.

    A phone lens bends the board's straight lines, most at the edges the
    homography extrapolates to. Undistorting the square vertices with the
    ``k`` that makes them fit one homography best, then distorting the board
    corners back, follows the bend. Returns corners, k and the vertex rms in
    squares at that k.
    """
    S, D = _vertex_pairs(blobs, cells, ok, H)
    h, w = shape
    centre = np.array([w / 2, h / 2])
    norm = float(np.hypot(w, h) / 2)
    px = float(np.median([np.sqrt(b.area) for b in blobs]))

    def fit(k):
        Du = _undistort(D, k, centre, norm)
        Hk, _ = cv2.findHomography(S, Du, 0)
        if Hk is None:
            return None, np.inf
        r = np.linalg.norm(_apply(Hk, S) - Du, axis=1) / px
        return Hk, float(np.sqrt(np.mean(r * r)))

    if len(S) < 24:
        return _apply(H, full), 0.0, np.inf
    best = min(((fit(k), k) for k in K_RANGE), key=lambda t: t[0][1])
    (Hk, rms), k = best
    return _distort(_apply(Hk, full), k, centre, norm), float(k), rms


def _work_image(image) -> tuple[np.ndarray, float]:
    from .reader import load_image
    img = load_image(image)
    rgb = np.asarray(img)
    scale = WORK_EDGE / max(rgb.shape[:2])
    if scale < 1:
        rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        scale = 1.0
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), scale


def find_grid(image, board: str = "green") -> Grid:
    """Corners of the playing area. ``board``: ``green`` (the mat) or ``auto``."""
    bgr, scale = _work_image(image)
    mask = dark_mask(bgr, board)
    blobs = _blobs(mask)
    if len(blobs) < 4:
        return Grid(None, 0.0, {"reason": "no squares", "blobs": len(blobs)})
    fit = _lattice(blobs)
    if fit is None:
        return Grid(None, 0.0, {"reason": "no lattice", "blobs": len(blobs)})
    H, ok, cells = fit
    seen = sorted({tuple(c) for c in cells[ok]})
    # For the extent, a mask opened at a fraction of a square: the mat's frame
    # line and letters are thin and vanish, while the green showing around a
    # piece on a dark square survives.
    sq = float(np.median([np.sqrt(b.area) for b in blobs]))
    k = max(3, int(0.15 * sq)) | 1
    thick = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    i0, how_i = _extent(seen, 0, thick, H)
    j0, how_j = _extent(seen, 1, thick, H)
    inside = [c for c in seen if i0 <= c[0] < i0 + 8 and j0 <= c[1] < j0 + 8]

    full = np.array([[i0, j0], [i0 + 8, j0], [i0 + 8, j0 + 8], [i0, j0 + 8]], np.float64)
    # rms: inlier vertices against the fitted board, in squares.
    rc, k, rms = _radial_corners(blobs, cells, ok, H, full, mask.shape)
    rms = rms if np.isfinite(rms) else 1.0
    corners = _order_cw(rc) / scale

    hows = (how_i, how_j)
    f_ext = 1.0
    for how in hows:
        f_ext *= {"seen": 1.0, "trimmed": 0.85, "margin": 0.8, "off-photo": 0.7, "guessed": 0.3}[how]
    f_n = min(1.0, len(inside) / 20)
    f_res = float(np.exp(-(rms / 0.06) ** 2))
    conf = round(f_n * f_res * f_ext, 3)
    return Grid(corners.round(1).tolist(), conf,
                {"squares": len(inside), "rms": round(rms, 4), "extent": list(hows),
                 "blobs": len(blobs), "k": round(k, 3)})


# --------------------------------------------------------------------------
# Crops
# --------------------------------------------------------------------------

CROP_W, CROP_H = 64, 112     # output size; the footprint sits bottom-centre
UP, DOWN, SIDE = 2.6, 0.55, 0.8   # crop extent around the base, in square widths


def board_homography(corners_board: np.ndarray) -> np.ndarray:
    """Board coordinates (file 0..8 left to right, rank 8..0 top to bottom as
    in a8-h8-h1-a1) to image pixels."""
    src = np.array([[0, 0], [8, 0], [8, 8], [0, 8]], np.float32)   # a8, h8, h1, a1
    return cv2.getPerspectiveTransform(src, np.asarray(corners_board, np.float32)).astype(np.float64)


def square_crops(img, corners_board, size=(CROP_W, CROP_H)) -> dict[str, np.ndarray]:
    """One RGB crop per square name ('a1'..'h8'). ``corners_board`` is a8, h8, h1, a1."""
    from .reader import load_image
    rgb = np.asarray(load_image(img)) if not isinstance(img, np.ndarray) else img
    H = board_homography(corners_board)
    out = {}
    cw, ch = size
    for f in range(8):
        for r in range(8):           # rank index 0 = rank 8 (top row in board coords)
            q = _apply(H, np.array([[f, r], [f + 1, r], [f + 1, r + 1], [f, r + 1]], np.float64))
            base = _apply(H, np.array([[f + 0.5, r + 0.5]]))[0]
            wsq = float(np.mean([np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[3]),
                                 np.linalg.norm(q[3] - q[0]), np.linalg.norm(q[2] - q[1])]))
            # Axis-aligned window in the photo: SIDE left/right, UP above, DOWN below the base.
            x0, x1 = base[0] - SIDE * wsq, base[0] + SIDE * wsq
            y0, y1 = base[1] - UP * wsq, base[1] + DOWN * wsq
            src = np.array([[x0, y0], [x1, y0], [x1, y1]], np.float32)
            dst = np.array([[0, 0], [cw, 0], [cw, ch]], np.float32)
            A = cv2.getAffineTransform(src, dst)
            crop = cv2.warpAffine(rgb, A, (cw, ch), flags=cv2.INTER_AREA,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
            out[f"{'abcdefgh'[f]}{8 - r}"] = crop
    return out


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def corner_errors(found: np.ndarray, truth: np.ndarray) -> np.ndarray:
    """Per-corner error in squares at the best cyclic rotation. ``truth`` is in
    board order (a8, h8, h1, a1); either order is a cycle of the same quad."""
    truth = np.asarray(truth, np.float64)
    found = np.asarray(found, np.float64)
    # Square size at each truth corner: the two adjacent board edges / 8.
    side = np.array([(np.linalg.norm(truth[k] - truth[k - 1]) +
                      np.linalg.norm(truth[k] - truth[(k + 1) % 4])) / 16 for k in range(4)])
    best = None
    for flip in (False, True):
        f = found[::-1] if flip else found
        for k in range(4):
            e = np.linalg.norm(np.roll(f, k, axis=0) - truth, axis=1) / side
            if best is None or e.max() < best.max():
                best = e
    return best


def evaluate(label_dir: Path, board: str) -> dict:
    truth = json.loads((label_dir / "truth.json").read_text())
    rows, errs = [], []
    for name, e in sorted(truth.items()):
        if not e.get("corners") or e.get("excluded"):
            continue
        g = find_grid(label_dir / "photos" / f"{name}.jpg", board)
        if g.corners is None:
            rows.append({"name": name, "max": float("inf"), "conf": g.confidence,
                         "info": g.info})
            errs.extend([float("inf")] * 4)
            continue
        e4 = corner_errors(np.array(g.corners), np.array(e["corners"]))
        errs.extend(e4.tolist())
        rows.append({"name": name, "max": float(e4.max()), "conf": g.confidence,
                     "info": g.info})
    n = len(rows)
    if not n:
        return {"photos": 0}
    within = sum(r["max"] <= 0.25 for r in rows)
    misses = [r for r in rows if r["max"] > 0.25]
    return {
        "photos": n,
        "median-corner-error": float(np.median(errs)),
        "within-0.25": within / n,
        "threshold": CONF_OK,
        "misses": len(misses),
        "misses-below-threshold": sum(r["conf"] < CONF_OK for r in misses),
        "hits-below-threshold": sum(r["conf"] < CONF_OK for r in rows if r["max"] <= 0.25),
        "per-photo": rows,
    }


def _board_kind(label_dir: Path) -> str:
    return "green" if "green" in label_dir.name else "auto"


def overlay(image, grid: Grid, truth_corners=None) -> np.ndarray:
    from .reader import load_image
    rgb = np.asarray(load_image(image)).copy()
    t = max(3, max(rgb.shape[:2]) // 400)
    if truth_corners is not None:
        cv2.polylines(rgb, [np.round(truth_corners).astype(np.int32)], True, (255, 200, 0), t)
    if grid.corners is not None:
        c = np.array(grid.corners)
        cv2.polylines(rgb, [np.round(c).astype(np.int32)], True, (230, 40, 40), t)
        # the inner grid lines, to judge squares as well as corners
        src = np.array([[0, 0], [8, 0], [8, 8], [0, 8]], np.float32)
        H = cv2.getPerspectiveTransform(src, c.astype(np.float32)).astype(np.float64)
        for k in range(1, 8):
            for a, b in (([k, 0], [k, 8]), ([0, k], [8, k])):
                p = _apply(H, np.array([a, b], np.float64))
                cv2.line(rgb, tuple(np.round(p[0]).astype(int)), tuple(np.round(p[1]).astype(int)),
                         (230, 40, 40), max(1, t // 2))
    cv2.putText(rgb, f"conf {grid.confidence:.2f}", (40, 40 + 30 * t), cv2.FONT_HERSHEY_SIMPLEX,
                t, (230, 40, 40), t)
    return rgb


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("label_dir", nargs="?", type=Path)
    ap.add_argument("--eval", dest="eval_dir", type=Path)
    ap.add_argument("--overlay", dest="overlay_dir", type=Path)
    ap.add_argument("--crops", dest="crops_dir", type=Path)
    ap.add_argument("--board", choices=("green", "auto"))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.eval_dir:
        res = evaluate(a.eval_dir, a.board or _board_kind(a.eval_dir))
        if a.json:
            print(json.dumps(res, indent=1))
            return 0
        if not res["photos"]:
            print(f"no photos with corner truth in {a.eval_dir}")
            return 2
        print(f"photos {res['photos']}")
        print(f"median corner error {res['median-corner-error']:.3f} squares")
        print(f">= 0.25 squares: {res['misses']} photos; within 0.25: {res['within-0.25']:.3f}")
        print(f"threshold {res['threshold']}: misses below it {res['misses-below-threshold']}"
              f"/{res['misses']}, hits below it {res['hits-below-threshold']}")
        for r in res["per-photo"]:
            mark = "MISS" if r["max"] > 0.25 else "    "
            print(f"  {mark} {r['name']:>28}  max {r['max']:.3f}  conf {r['conf']:.2f}  {r['info']}")
        return 0

    if a.overlay_dir:
        d = a.overlay_dir
        out = a.out or (d / "gridfind")
        out.mkdir(parents=True, exist_ok=True)
        truth = json.loads((d / "truth.json").read_text())
        kind = a.board or _board_kind(d)
        for p in sorted((d / "photos").glob("*.jpg")):
            g = find_grid(p, kind)
            tc = truth.get(p.stem, {}).get("corners")
            im = overlay(p, g, np.array(tc) if tc else None)
            im = cv2.resize(im, None, fx=1200 / max(im.shape[:2]), fy=1200 / max(im.shape[:2]))
            cv2.imwrite(str(out / f"{p.stem}.jpg"), cv2.cvtColor(im, cv2.COLOR_RGB2BGR))
            print(f"{p.stem:>28}  conf {g.confidence:.2f}  {g.info}")
        return 0

    if a.crops_dir:
        from .labels import write_crops
        n = write_crops(a.crops_dir)
        print(f"wrote crops for {n} photos to {a.crops_dir / 'crops'}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
