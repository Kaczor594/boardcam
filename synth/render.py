"""Pinhole renderer for synthetic board photographs.

The engine never classifies pieces from pixels, so this renderer does not try to
be a chess-set catalogue. What it *does* have to get right is everything the
tracker actually keys on:

* correct projective geometry of the 8x8 grid, so calibration has a real target;
* piece silhouettes that occlude the squares behind them by the right amount at
  the camera's elevation (the whole reason the emission model is footprint
  based);
* the mess around the board — players, forearms, captured pieces, a phone,
  someone walking past — which must be there for the gate to mean anything.

Board coordinates are squares: file ``f`` in 0..7 and rank ``r`` in 0..7 occupy
``x in [f, f+1]``, ``y in [r, r+1]``, ``z = 0``. So a1 is the corner ``(0, 0)``
and h8 is ``(8, 8)``. White plays the low ranks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import chess
import cv2
import numpy as np

SHIFT = 4
_S = 1 << SHIFT

# Edge i of the board runs between board corners i and i+1 of
# [a1, h1, h8, a8]; these are its names.
EDGE_NAMES = ("rank1", "fileh", "rank8", "filea")
BOARD_CORNERS = np.array([[0.0, 0.0], [8.0, 0.0], [8.0, 8.0], [0.0, 8.0]])


# --------------------------------------------------------------------------
# Palettes
# --------------------------------------------------------------------------

# (light square, dark square, border) as RGB.
SQUARE_PALETTES = [
    ((238, 216, 178), (168, 118, 74), (92, 58, 34)),      # classic wood
    ((232, 226, 206), (108, 140, 96), (58, 72, 52)),      # green vinyl
    ((240, 240, 236), (90, 116, 156), (44, 58, 84)),      # blue vinyl
    ((222, 198, 160), (126, 84, 56), (70, 44, 28)),       # dark wood
    ((246, 238, 224), (150, 148, 146), (74, 74, 76)),     # grey stone
    ((236, 222, 192), (132, 100, 132), (66, 50, 68)),     # purple club set
]

PIECE_PALETTES = [
    ((236, 228, 210), (46, 42, 40)),     # cream / near-black
    ((226, 210, 176), (74, 52, 38)),     # boxwood / rosewood
    ((242, 240, 236), (62, 62, 68)),     # white plastic / charcoal
    ((214, 198, 170), (56, 46, 44)),     # aged boxwood / ebony
]

TABLE_PALETTES = [
    (150, 126, 102), (96, 92, 90), (176, 168, 152), (72, 78, 84), (122, 104, 88),
]

SHIRT_PALETTES = [
    (60, 72, 104), (132, 60, 58), (58, 58, 62), (112, 120, 128), (86, 104, 76),
]

SKIN_RGB = (226, 184, 152)


def _rgb2bgr(c) -> tuple[float, float, float]:
    return (float(c[2]), float(c[1]), float(c[0]))


# --------------------------------------------------------------------------
# Piece shapes
# --------------------------------------------------------------------------

# Heights in square-widths. Ordering P < B < N < R < Q < K, with the bishop
# only ~1.2x the pawn, which is what Isaac's set looks like.
# Real proportions: a tournament square is ~55 mm and a king ~95 mm, so the king
# is ~1.75 squares tall and occludes ~3 squares at 30 deg elevation. That number
# is what the whole footprint-based emission model is sized for, so it matters.
PIECE_H = {
    chess.PAWN: 0.85,
    chess.BISHOP: 1.02,
    chess.KNIGHT: 1.15,
    chess.ROOK: 1.25,
    chess.QUEEN: 1.50,
    chess.KING: 1.75,
}

PIECE_BASE_R = {
    chess.PAWN: 0.30,
    chess.BISHOP: 0.32,
    chess.KNIGHT: 0.33,
    chess.ROOK: 0.35,
    chess.QUEEN: 0.37,
    chess.KING: 0.39,
}

# (z fraction, radius as a fraction of the base radius, lateral lean)
_PROFILES: dict[int, list[tuple[float, float, float]]] = {
    chess.PAWN: [(0.00, 1.00, 0), (0.07, 0.96, 0), (0.13, 0.62, 0), (0.30, 0.42, 0),
                 (0.58, 0.36, 0), (0.68, 0.44, 0), (0.74, 0.38, 0), (0.84, 0.52, 0),
                 (0.95, 0.44, 0), (1.00, 0.16, 0)],
    chess.BISHOP: [(0.00, 1.00, 0), (0.07, 0.95, 0), (0.14, 0.58, 0), (0.34, 0.40, 0),
                   (0.60, 0.34, 0), (0.70, 0.46, 0), (0.78, 0.38, 0), (0.88, 0.46, 0),
                   (0.96, 0.34, 0), (1.00, 0.14, 0)],
    chess.KNIGHT: [(0.00, 1.00, 0), (0.07, 0.95, 0), (0.14, 0.60, 0), (0.34, 0.44, 0),
                   (0.55, 0.42, 0.06), (0.70, 0.46, 0.16), (0.82, 0.50, 0.26),
                   (0.92, 0.44, 0.30), (0.98, 0.30, 0.24), (1.00, 0.18, 0.18)],
    chess.ROOK: [(0.00, 1.00, 0), (0.07, 0.95, 0), (0.14, 0.64, 0), (0.34, 0.56, 0),
                 (0.66, 0.54, 0), (0.80, 0.58, 0), (0.86, 0.74, 0), (0.94, 0.78, 0),
                 (1.00, 0.74, 0)],
    chess.QUEEN: [(0.00, 1.00, 0), (0.07, 0.95, 0), (0.14, 0.60, 0), (0.34, 0.42, 0),
                  (0.56, 0.34, 0), (0.68, 0.40, 0), (0.76, 0.32, 0), (0.86, 0.62, 0),
                  (0.94, 0.70, 0), (1.00, 0.60, 0)],
    chess.KING: [(0.00, 1.00, 0), (0.07, 0.95, 0), (0.14, 0.60, 0), (0.34, 0.40, 0),
                 (0.54, 0.32, 0), (0.66, 0.38, 0), (0.74, 0.30, 0), (0.84, 0.56, 0),
                 (0.90, 0.62, 0), (0.94, 0.30, 0), (0.97, 0.20, 0), (1.00, 0.30, 0)],
}

N_ANG = 12


def _piece_rings(ptype: int, scale: float) -> list[tuple[float, float, float]]:
    """Absolute (z, radius, lean) rings for one piece, in square units."""
    h = PIECE_H[ptype] * scale
    br = PIECE_BASE_R[ptype] * scale
    return [(zf * h, rf * br, lean * br) for zf, rf, lean in _PROFILES[ptype]]


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------

@dataclass
class Camera:
    pos: np.ndarray
    target: np.ndarray
    roll: float
    f: float
    w: int
    h: int

    def basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        fwd = self.target - self.pos
        fwd = fwd / np.linalg.norm(fwd)
        world_up = np.array([0.0, 0.0, 1.0])
        right = np.cross(fwd, world_up)
        n = np.linalg.norm(right)
        if n < 1e-8:  # looking straight down
            right = np.array([1.0, 0.0, 0.0])
        else:
            right = right / n
        up = np.cross(right, fwd)
        if self.roll:
            c, s = math.cos(self.roll), math.sin(self.roll)
            right, up = c * right + s * up, -s * right + c * up
        return right, up, fwd

    def project(self, pts: np.ndarray) -> np.ndarray:
        """World (N,3) -> image (N,2). Points behind the camera come back as NaN."""
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        right, up, fwd = self.basis()
        rel = pts - self.pos
        z = rel @ fwd
        x = rel @ right
        y = rel @ up
        z = np.where(np.abs(z) < 1e-6, np.nan, z)
        u = self.f * x / z + self.w / 2.0
        v = -self.f * y / z + self.h / 2.0
        out = np.stack([u, v], axis=1)
        out[z <= 0] = np.nan
        return out

    def depth(self, pts: np.ndarray) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        return np.linalg.norm(pts - self.pos, axis=1)


def place_camera(*, elev_deg: float, azim_deg: float, fill: float, w: int, h: int,
                 focal: float, roll: float, aim_offset: np.ndarray) -> Camera:
    """Camera at the given pose, pushed back until the board fills ``fill`` of the frame."""
    centre = np.array([4.0, 4.0, 0.0])
    e, a = math.radians(elev_deg), math.radians(azim_deg)
    direction = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
    target = centre + np.array([aim_offset[0], aim_offset[1], 0.0])
    # Fit the board *and* the space a back-rank piece projects into: the engine's
    # analysis mask is the board quad plus a strip above its far edge, and that
    # strip has to actually be inside the photograph.
    fit_pts = np.vstack([
        np.column_stack([BOARD_CORNERS, np.zeros(4)]),
        np.column_stack([BOARD_CORNERS, np.full(4, 1.9)]),
    ])
    dist = 14.0
    cam = None
    for _ in range(6):
        cam = Camera(pos=centre + dist * direction, target=target, roll=roll,
                     f=focal, w=w, h=h)
        p = cam.project(fit_pts)
        if np.isnan(p).any():
            dist *= 1.5
            continue
        bw = (p[:, 0].max() - p[:, 0].min()) / w
        bh = (p[:, 1].max() - p[:, 1].min()) / h
        cur = max(bw, bh)
        if abs(cur - fill) < 0.005:
            break
        dist *= cur / fill
        dist = float(np.clip(dist, 4.0, 80.0))
    assert cam is not None
    return cam


# --------------------------------------------------------------------------
# Scene
# --------------------------------------------------------------------------

@dataclass
class Scene:
    cam: Camera
    light: np.ndarray               # unit vector toward the light
    light_elev: float               # radians
    sq_light: tuple                 # BGR
    sq_dark: tuple
    border_col: tuple
    table_col: tuple
    piece_white: tuple
    piece_black: tuple
    shirt: tuple
    border: float                   # border width in squares
    piece_scale: float
    offsets: dict[int, tuple[float, float]]   # square -> (dx, dy) placement error
    square_tint: np.ndarray         # (8,8) multiplicative tint
    table_tex: np.ndarray           # (h,w) float texture
    markings: bool
    exposure: float
    warmth: np.ndarray              # (3,) BGR channel gain
    grad: np.ndarray                # (h,w) float luminance ramp
    vignette: np.ndarray            # (h,w) float
    noise_sigma: float
    blur: float
    camera_side: int                # edge index nearest the camera
    azim_deg: float
    elev_deg: float
    players: bool
    props: bool                     # the clock phone and the captured-piece pile
    pile_side: np.ndarray           # (2,) world xy of the captured-piece pile
    phone_xy: np.ndarray
    seat_shift: float = 0.0


@dataclass
class FrameOpts:
    """Everything that varies between two photographs of the same scene."""
    jitter: tuple[float, float] = (0.0, 0.0)
    bump: np.ndarray | None = None          # persistent 2x3 affine
    pose: float = 0.0                       # player pose phase
    captured: int = 0                       # pieces in the pile
    pile_nudge: tuple[float, float] = (0.0, 0.0)
    bg_motion: float | None = None          # position of a passer-by, 0..1
    hand: tuple[int, float, bool] | None = None  # (target square, reach 0..1, white to move)
    noise_seed: int = 0


# --------------------------------------------------------------------------
# Low-level drawing
# --------------------------------------------------------------------------

def _fill(img, pts, colour):
    p = np.asarray(pts, dtype=float)
    if not np.isfinite(p).all() or len(p) < 3:
        return
    p = np.round(p * _S).astype(np.int32)
    cv2.fillConvexPoly(img, p, colour, lineType=cv2.LINE_AA, shift=SHIFT)


def _fill_poly(img, pts, colour):
    p = np.asarray(pts, dtype=float)
    if not np.isfinite(p).all() or len(p) < 3:
        return
    p = np.round(p * _S).astype(np.int32)
    cv2.fillPoly(img, [p], colour, lineType=cv2.LINE_AA, shift=SHIFT)


def _expand(poly: np.ndarray, factor: float) -> np.ndarray:
    c = poly.mean(axis=0)
    return c + (poly - c) * factor


def _ring_points(cx: float, cy: float, z: float, r: float, lean: float,
                 lean_dir: np.ndarray, n: int = N_ANG) -> np.ndarray:
    a = np.linspace(0, 2 * math.pi, n, endpoint=False)
    x = cx + r * np.cos(a) + lean * lean_dir[0]
    y = cy + r * np.sin(a) + lean * lean_dir[1]
    return np.column_stack([x, y, np.full(n, z)])


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

class Renderer:
    def __init__(self, scene: Scene):
        self.s = scene

    # -- board -------------------------------------------------------------
    def _draw_table(self, img):
        s = self.s
        img[:] = np.array(s.table_col, dtype=np.float32)
        img *= s.table_tex[:, :, None]

    def _draw_board(self, img):
        s = self.s
        b = s.border
        outer = np.array([[-b, -b], [8 + b, -b], [8 + b, 8 + b], [-b, 8 + b]])
        _fill(img, s.cam.project(np.column_stack([outer, np.zeros(4)])), s.border_col)
        inner = s.cam.project(np.column_stack([BOARD_CORNERS, np.zeros(4)]))
        _fill(img, inner, s.sq_light)
        for f in range(8):
            for r in range(8):
                if (f + r) % 2 == 1:      # light square, already painted
                    continue
                quad = np.array([[f, r], [f + 1, r], [f + 1, r + 1], [f, r + 1]], dtype=float)
                p = s.cam.project(np.column_stack([quad, np.zeros(4)]))
                col = tuple(float(c * s.square_tint[f, r]) for c in s.sq_dark)
                _fill(img, _expand(p, 1.006), col)
        if s.markings:
            self._draw_markings(img)

    def _draw_markings(self, img):
        s = self.s
        b = s.border
        col = tuple(min(255.0, c * 1.9 + 40) for c in s.border_col)
        for i in range(8):
            for (cx, cy) in ((i + 0.5, -b / 2), (i + 0.5, 8 + b / 2)):
                q = np.array([[cx - 0.09, cy - 0.09], [cx + 0.09, cy - 0.09],
                              [cx + 0.09, cy + 0.09], [cx - 0.09, cy + 0.09]])
                _fill(img, s.cam.project(np.column_stack([q, np.zeros(4)])), col)
            for (cx, cy) in ((-b / 2, i + 0.5), (8 + b / 2, i + 0.5)):
                q = np.array([[cx - 0.09, cy - 0.09], [cx + 0.09, cy - 0.09],
                              [cx + 0.09, cy + 0.09], [cx - 0.09, cy + 0.09]])
                _fill(img, s.cam.project(np.column_stack([q, np.zeros(4)])), col)

    # -- pieces ------------------------------------------------------------
    def _piece_at(self, square: int, ptype: int) -> tuple[float, float]:
        f, r = chess.square_file(square), chess.square_rank(square)
        dx, dy = self.s.offsets.get(square, (0.0, 0.0))
        return f + 0.5 + dx, r + 0.5 + dy

    def _shadow_poly(self, cx, cy, rings) -> np.ndarray | None:
        s = self.s
        t = math.tan(max(s.light_elev, math.radians(12)))
        lh = s.light[:2]
        n = np.linalg.norm(lh)
        lh = lh / n if n > 1e-6 else np.array([1.0, 0.0])
        pts = []
        for z, r, lean in rings:
            off = -lh * (z / t)
            a = np.linspace(0, 2 * math.pi, 10, endpoint=False)
            pts.append(np.column_stack([cx + off[0] + r * np.cos(a),
                                        cy + off[1] + r * np.sin(a)]))
        world = np.vstack(pts)
        hull = cv2.convexHull(world.astype(np.float32)).reshape(-1, 2)
        return s.cam.project(np.column_stack([hull, np.zeros(len(hull))]))

    def _draw_piece(self, img, cx, cy, ptype, base_col, lean_dir):
        s = self.s
        rings = _piece_rings(ptype, s.piece_scale)
        pts3 = [_ring_points(cx, cy, z, r, lean, lean_dir) for z, r, lean in rings]
        proj = [s.cam.project(p) for p in pts3]
        base = np.array(base_col, dtype=float)

        quads = []
        for i in range(len(rings) - 1):
            lo3, hi3 = pts3[i], pts3[i + 1]
            lo2, hi2 = proj[i], proj[i + 1]
            for j in range(N_ANG):
                k = (j + 1) % N_ANG
                poly = np.array([lo2[j], lo2[k], hi2[k], hi2[j]])
                if not np.isfinite(poly).all():
                    continue
                p0, p1, p2 = lo3[j], lo3[k], hi3[j]
                nrm = np.cross(p1 - p0, p2 - p0)
                ln = np.linalg.norm(nrm)
                if ln < 1e-9:
                    continue
                nrm = nrm / ln
                radial = np.array([p0[0] - cx, p0[1] - cy, 0.0])
                if nrm @ radial < 0:
                    nrm = -nrm
                mid = (lo3[j] + lo3[k] + hi3[j] + hi3[k]) / 4.0
                view = mid - s.cam.pos
                if nrm @ view > 0:          # back face
                    continue
                lam = max(0.0, float(nrm @ s.light))
                shade = 0.34 + 0.76 * lam + 0.22 * lam ** 6
                quads.append((float(np.linalg.norm(view)), poly, shade))

        quads.sort(key=lambda q: -q[0])
        for _, poly, shade in quads:
            _fill(img, poly, tuple(np.clip(base * shade, 0, 255)))

        top = proj[-1]
        if np.isfinite(top).all():
            lam = max(0.0, float(s.light[2]))
            shade = 0.34 + 0.76 * lam + 0.22 * lam ** 6
            _fill(img, top, tuple(np.clip(base * shade, 0, 255)))

    def _draw_pieces(self, img, board: chess.Board):
        s = self.s
        lean_dir = np.array([math.cos(math.radians(s.azim_deg + 180)),
                             math.sin(math.radians(s.azim_deg + 180))])
        items = []
        for sq, piece in board.piece_map().items():
            cx, cy = self._piece_at(sq, piece.piece_type)
            d = float(np.linalg.norm(np.array([cx, cy, 0.3]) - s.cam.pos))
            items.append((d, cx, cy, piece))
        items.sort(key=lambda t: -t[0])

        shadow = np.zeros(img.shape[:2], dtype=np.uint8)
        for _, cx, cy, piece in items:
            poly = self._shadow_poly(cx, cy, _piece_rings(piece.piece_type, s.piece_scale))
            if poly is not None and np.isfinite(poly).all():
                cv2.fillConvexPoly(shadow, np.round(poly * _S).astype(np.int32), 255,
                                   lineType=cv2.LINE_AA, shift=SHIFT)
        shadow = cv2.GaussianBlur(shadow, (0, 0), 2.0).astype(np.float32) / 255.0
        img *= (1.0 - 0.34 * shadow)[:, :, None]

        for _, cx, cy, piece in items:
            col = s.piece_white if piece.color == chess.WHITE else s.piece_black
            self._draw_piece(img, cx, cy, piece.piece_type, col, lean_dir)

    # -- scene noise -------------------------------------------------------
    def _box(self, img, x0, x1, y0, y1, z0, z1, colour):
        s = self.s
        faces = [
            [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],   # top
            [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
            [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
            [(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)],
            [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
        ]
        centre = np.array([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2])
        order = []
        for k, fc in enumerate(faces):
            m = np.mean(fc, axis=0)
            order.append((float(np.linalg.norm(m - s.cam.pos)), k, fc, m))
        order.sort(key=lambda t: -t[0])
        for _, k, fc, m in order:
            nrm = m - centre
            ln = np.linalg.norm(nrm)
            nrm = nrm / ln if ln > 1e-9 else nrm
            if nrm @ (m - s.cam.pos) > 0:
                continue
            shade = 0.55 + 0.55 * max(0.0, float(nrm @ s.light))
            _fill(img, s.cam.project(np.array(fc)),
                  tuple(np.clip(np.array(colour, dtype=float) * shade, 0, 255)))

    def _capsule(self, img, p0, p1, r, colour, n=9):
        """A fat segment in world space, drawn as the hull of two projected spheres."""
        s = self.s
        pts = []
        for t in np.linspace(0, 1, n):
            c = np.array(p0) * (1 - t) + np.array(p1) * t
            rr = r * (1.0 - 0.25 * t)
            a = np.linspace(0, 2 * math.pi, 10, endpoint=False)
            ring = np.column_stack([c[0] + rr * np.cos(a), c[1] + rr * np.sin(a),
                                    np.full(10, c[2])])
            ring2 = np.column_stack([c[0] + rr * np.cos(a), np.full(10, c[1]),
                                     c[2] + rr * np.sin(a)])
            pts.append(s.cam.project(ring))
            pts.append(s.cam.project(ring2))
        allp = np.vstack(pts)
        allp = allp[np.isfinite(allp).all(axis=1)]
        if len(allp) < 3:
            return
        hull = cv2.convexHull(allp.astype(np.float32)).reshape(-1, 2)
        _fill(img, hull, colour)

    def _draw_players(self, img, opt: FrameOpts):
        """Two seated figures at the rank ends, reaching in to the board edge.

        Sizes are real: a seated torso stands ~45 cm above the table, which is
        eight squares, and the near edge of a player is ~20 cm back from the
        board. They are large, they move between frames, and the engine has to
        be blind to all of it.
        """
        s = self.s
        if not s.players:
            return
        skin = _rgb2bgr(SKIN_RGB)
        for sign, phase in ((-1.0, 0.0), (1.0, 1.7)):
            sway = math.sin(opt.pose * 2.0 + phase) * 0.5 + s.seat_shift
            x0, x1 = 0.6 + sway, 7.4 + sway
            near = 4.0 + sign * 7.6      # chest, ~20 cm back from the board edge
            far = 4.0 + sign * 11.0
            lo, hi = (far, near) if sign < 0 else (near, far)
            self._box(img, x0, x1, lo, hi, 0.0, 6.0, s.shirt)
            head_c = np.array([(x0 + x1) / 2, 4.0 + sign * 8.6, 7.0])
            self._capsule(img, head_c, head_c + np.array([0, 0, 0.9]), 1.15, skin, n=4)
            for ax in (x0 + 1.4, x1 - 1.4):
                elbow = np.array([ax, 4.0 + sign * 7.2, 1.1])
                hx = ax + sign * 0.4 * math.sin(opt.pose + phase)
                hy = 4.0 + sign * (4.9 + 0.3 * math.cos(opt.pose + phase))
                wrist = np.array([hx, hy, 0.45])
                self._capsule(img, elbow, wrist, 0.62, s.shirt, n=6)
                self._capsule(img, wrist, np.array([hx, hy - sign * 1.1, 0.30]),
                              0.45, skin, n=5)

    def _draw_pile(self, img, opt: FrameOpts):
        s = self.s
        if not s.props or opt.captured <= 0:
            return
        rng = np.random.default_rng(20260912)
        base = s.pile_side + np.array(opt.pile_nudge)
        types = [chess.PAWN, chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK,
                 chess.PAWN, chess.QUEEN, chess.PAWN, chess.BISHOP, chess.PAWN,
                 chess.KNIGHT, chess.ROOK, chess.PAWN, chess.PAWN, chess.PAWN, chess.PAWN]
        lean = np.array([1.0, 0.0])
        items = []
        for i in range(min(opt.captured, 16)):
            off = rng.normal(0, 0.75, 2)
            cx, cy = base[0] + off[0], base[1] + off[1]
            col = s.piece_white if i % 2 else s.piece_black
            items.append((float(np.linalg.norm(np.array([cx, cy, 0.2]) - s.cam.pos)),
                          cx, cy, types[i % len(types)], col))
        items.sort(key=lambda t: -t[0])
        for _, cx, cy, pt, col in items:
            self._draw_piece(img, cx, cy, pt, col, lean)

    def _draw_phone(self, img):
        s = self.s
        if not s.props:
            return
        x, y = s.phone_xy
        self._box(img, x - 0.35, x + 0.35, y - 0.7, y + 0.7, 0.0, 0.06, (34, 34, 38))

    def _draw_bg_motion(self, img, opt: FrameOpts):
        s = self.s
        if opt.bg_motion is None:
            return
        t = opt.bg_motion
        ang = math.radians(s.azim_deg + 180)
        outward = np.array([math.cos(ang), math.sin(ang)])
        tang = np.array([-outward[1], outward[0]])
        centre = np.array([4.0, 4.0]) + outward * 9.0 + tang * (t * 18.0 - 9.0)
        self._box(img, centre[0] - 1.1, centre[0] + 1.1, centre[1] - 0.7, centre[1] + 0.7,
                  0.0, 3.4, tuple(c * 0.8 for c in s.shirt))

    def _draw_hand(self, img, opt: FrameOpts):
        """An arm reaching in to move a piece — the burst candidate that must be dropped."""
        s = self.s
        if opt.hand is None:
            return
        sq, reach, white_to_move = opt.hand
        tx = chess.square_file(sq) + 0.5
        ty = chess.square_rank(sq) + 0.5
        sign = -1.0 if white_to_move else 1.0
        entry = np.array([4.0 + (tx - 4.0) * 0.4, 4.0 + sign * 7.0])
        skin = _rgb2bgr(SKIN_RGB)
        wrist = np.array([tx, ty + sign * 0.5, 1.0 + 0.5 * (1 - reach)])
        elbow = np.array([entry[0], entry[1], 1.6])
        self._capsule(img, elbow, wrist, 0.55, skin, n=8)
        self._capsule(img, wrist, np.array([tx, ty, 0.55 + 0.6 * (1 - reach)]),
                      0.38, skin, n=5)

    # -- photometry --------------------------------------------------------
    def _finish(self, img, opt: FrameOpts) -> np.ndarray:
        s = self.s
        img *= s.exposure
        img *= s.warmth[None, None, :]
        img *= s.grad[:, :, None]
        img *= s.vignette[:, :, None]
        if s.blur > 0.05:
            img = cv2.GaussianBlur(img, (0, 0), s.blur)
        rng = np.random.default_rng(opt.noise_seed)
        img += rng.normal(0, s.noise_sigma, img.shape)
        return np.clip(img, 0, 255).astype(np.uint8)

    # -- public ------------------------------------------------------------
    def render(self, board: chess.Board, opt: FrameOpts) -> tuple[np.ndarray, np.ndarray]:
        """Render one photograph. Returns (BGR image, board corners in image space)."""
        s = self.s
        img = np.zeros((s.cam.h, s.cam.w, 3), dtype=np.float32)
        self._draw_table(img)
        self._draw_bg_motion(img, opt)
        self._draw_board(img)
        self._draw_pieces(img, board)
        self._draw_pile(img, opt)
        self._draw_phone(img)
        self._draw_players(img, opt)
        self._draw_hand(img, opt)
        out = self._finish(img, opt)

        corners = s.cam.project(np.column_stack([BOARD_CORNERS, np.zeros(4)]))
        m = np.array([[1.0, 0.0, opt.jitter[0]], [0.0, 1.0, opt.jitter[1]]])
        if opt.bump is not None:
            a = np.vstack([opt.bump, [0, 0, 1]]) @ np.vstack([m, [0, 0, 1]])
            m = a[:2]
        if not np.allclose(m, [[1, 0, 0], [0, 1, 0]]):
            out = cv2.warpAffine(out, m, (s.cam.w, s.cam.h),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            corners = (np.column_stack([corners, np.ones(4)]) @ m.T)
        return out, corners
