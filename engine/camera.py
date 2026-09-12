"""A metric camera from the board homography.

The emission model in Notes §C is sized in *squares of occlusion*: a piece of
height ``h`` hides everything within ``h / tan(elevation)`` squares behind it.
Nothing in Phase 2 measured elevation — ``Calibration`` knows where the board is
and which way round, not where the camera stands. This module recovers the rest.

A homography from a known square (the board) to the image is two constraints on
the image of the absolute conic. With the principal point assumed at the image
centre and square pixels, that is enough to solve for the focal length, and from
there for the full pose. The output is the camera centre in *board coordinates*,
where one unit is one square, the board occupies ``[0,8]^2`` at ``z = 0``, and
``+z`` is up out of the board.

Board coordinates: ``(bx, by)`` with ``bx`` running a->h and ``by`` running
1->8, so the centre of square ``(file, rank)`` is ``(file + 0.5, rank + 0.5)``
and a1's outer corner is the origin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Direction each camera side points, in board units, used only when the metric
# estimate fails and we have to fall back on the Phase 2 answer.
_SIDE_DIR = {"rank1": (0.0, -1.0), "rank8": (0.0, 1.0),
             "filea": (-1.0, 0.0), "fileh": (1.0, 0.0)}

FALLBACK_ELEVATION_DEG = 32.0
FALLBACK_DISTANCE = 14.0          # squares from the board centre


@dataclass
class CameraModel:
    """Where the camera is, in units of squares."""

    K: np.ndarray                  # (3,3) intrinsics
    R: np.ndarray                  # (3,3) board -> camera rotation
    t: np.ndarray                  # (3,) board -> camera translation
    centre: np.ndarray             # (3,) camera position in board coords
    elevation_deg: float
    azimuth_deg: float             # 0 = +x (toward the h file), CCW
    focal_px: float
    ok: bool                       # False => centre is a plausible guess, not a measurement
    warnings: list[str]

    # -- geometry ---------------------------------------------------------

    @property
    def tan_elev(self) -> float:
        return max(math.tan(math.radians(self.elevation_deg)), 1e-3)

    def project(self, pts: np.ndarray) -> np.ndarray:
        """Board points ``(N,3)`` (or ``(N,2)``, z=0) -> image pixels ``(N,2)``."""
        p = np.asarray(pts, dtype=np.float64).reshape(-1, np.shape(pts)[-1])
        if p.shape[1] == 2:
            p = np.hstack([p, np.zeros((len(p), 1))])
        cam = p @ self.R.T + self.t
        cam[:, 2] = np.where(np.abs(cam[:, 2]) < 1e-6, 1e-6, cam[:, 2])
        img = cam @ self.K.T
        return img[:, :2] / img[:, 2:3]

    def toward_camera(self, pts_xy: np.ndarray) -> np.ndarray:
        """Unit vectors, in board units, from each point toward the camera."""
        p = np.asarray(pts_xy, dtype=np.float64).reshape(-1, 2)
        d = self.centre[:2][None, :] - p
        n = np.linalg.norm(d, axis=1, keepdims=True)
        return d / np.maximum(n, 1e-9)

    def shadow_end(self, base_xy: np.ndarray, height: float) -> np.ndarray:
        """Where the top of a piece of ``height`` standing at ``base_xy`` lands.

        The ray from the camera through the piece's top meets the board plane
        beyond the piece, away from the camera: that landing point is the far
        end of the strip of board the piece hides. Everything between the base
        and it is out of view.
        """
        p = np.asarray(base_xy, dtype=np.float64).reshape(-1, 2)
        cz = self.centre[2]
        if cz <= height + 1e-6:                       # camera below the piece top
            s = 40.0
        else:
            s = height / (cz - height)
        return p + s * (p - self.centre[:2][None, :])

    def to_json(self) -> dict:
        return {"elevation_deg": round(self.elevation_deg, 3),
                "azimuth_deg": round(self.azimuth_deg, 3),
                "focal_px": round(self.focal_px, 2),
                "centre": np.round(self.centre, 4).tolist(),
                "ok": bool(self.ok), "warnings": list(self.warnings)}


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------

def _board_to_image(calib) -> np.ndarray:
    """Homography from board coords ``(bx, by, 1)`` to image pixels."""
    k = calib.size / 8.0
    # board (bx, by) -> rectified (bx*k, (8-by)*k)
    s = np.array([[k, 0.0, 0.0],
                  [0.0, -k, 8.0 * k],
                  [0.0, 0.0, 1.0]])
    return np.linalg.inv(calib.H) @ s


def _focal_from_homography(m: np.ndarray, w_img: float) -> float | None:
    """Least squares over the two IAC constraints, by scanning the focal length.

    Solving either constraint in closed form is tempting and wrong: each one
    goes singular on its own set of camera poses, and a board photographed
    near-square to one grid direction makes the first one's denominator vanish.
    The residual below is what both constraints are trying to drive to zero —
    that the two rotation columns be orthogonal and of equal length — and
    scanning it never divides by anything.
    """
    lo, hi = 0.35 * w_img, 3.5 * w_img
    grid = np.exp(np.linspace(np.log(lo), np.log(hi), 400))

    def residual(f: float) -> float:
        k_inv = np.array([[1.0 / f, 0.0, 0.0], [0.0, 1.0 / f, 0.0], [0.0, 0.0, 1.0]])
        a = k_inv @ m
        n1 = np.linalg.norm(a[:, 0])
        if n1 < 1e-12:
            return float("inf")
        r1, r2 = a[:, 0] / n1, a[:, 1] / n1
        return float((r1 @ r2) ** 2 + (np.linalg.norm(r1) - 1.0) ** 2
                     + (np.linalg.norm(r2) - 1.0) ** 2)

    vals = np.array([residual(f) for f in grid])
    i = int(np.argmin(vals))
    if not np.isfinite(vals[i]):
        return None
    a, b = grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)]
    for _ in range(40):                      # golden-section on the bracket
        m1 = a + 0.382 * (b - a)
        m2 = a + 0.618 * (b - a)
        if residual(m1) < residual(m2):
            b = m2
        else:
            a = m1
    return float(0.5 * (a + b))


def estimate(calib, image_shape: tuple[int, ...],
             focal_hint: float | None = None) -> CameraModel:
    """Recover the camera from a ``Calibration`` and the frame size."""
    h_img, w_img = image_shape[0], image_shape[1]
    cx, cy = w_img / 2.0, h_img / 2.0
    warnings: list[str] = []

    m = _board_to_image(calib)
    t_pp = np.array([[1.0, 0.0, cx], [0.0, 1.0, cy], [0.0, 0.0, 1.0]])
    mp = np.linalg.inv(t_pp) @ m
    mp = mp / (np.linalg.norm(mp[:, 0]) + 1e-12)

    f = _focal_from_homography(mp, float(w_img))
    if f is None or not (0.25 * w_img < f < 8.0 * w_img):
        if f is not None:
            warnings.append("focal-out-of-range")
        else:
            warnings.append("focal-unsolvable")
        f = float(focal_hint) if focal_hint else 1.2 * w_img

    k_mat = np.array([[f, 0.0, cx], [0.0, f, cy], [0.0, 0.0, 1.0]])
    k_inv = np.linalg.inv(k_mat)
    a = k_inv @ m
    lam = 1.0 / max(np.linalg.norm(a[:, 0]), 1e-12)
    if a[2, 2] < 0:                       # board must be in front of the camera
        lam = -lam
    r1, r2, tv = lam * a[:, 0], lam * a[:, 1], lam * a[:, 2]
    r3 = np.cross(r1, r2)
    r = np.stack([r1, r2, r3], axis=1)
    u, _, vt = np.linalg.svd(r)           # nearest rotation
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, 2] *= -1
        r = u @ vt

    centre = -r.T @ tv
    horiz = float(math.hypot(centre[0] - 4.0, centre[1] - 4.0))
    elev = math.degrees(math.atan2(float(centre[2]), max(horiz, 1e-6)))
    azim = math.degrees(math.atan2(centre[1] - 4.0, centre[0] - 4.0))

    ok = True
    if not (3.0 <= elev <= 88.0) or not (3.0 <= horiz <= 60.0) or centre[2] <= 0:
        warnings.append("pose-implausible")
        ok = False
    if not ok:
        return _fallback(calib, k_mat, f, warnings)

    return CameraModel(K=k_mat, R=r, t=tv, centre=centre, elevation_deg=elev,
                       azimuth_deg=azim, focal_px=f, ok=True, warnings=warnings)


def _fallback(calib, k_mat: np.ndarray, f: float, warnings: list[str]) -> CameraModel:
    """A plausible camera consistent with the Phase 2 camera side."""
    dx, dy = _SIDE_DIR.get(calib.camera_side, (0.0, -1.0))
    horiz = FALLBACK_DISTANCE
    cz = horiz * math.tan(math.radians(FALLBACK_ELEVATION_DEG))
    centre = np.array([4.0 + dx * horiz, 4.0 + dy * horiz, cz])
    # A rotation that looks from `centre` at the board centre, board z up.
    fwd = np.array([4.0, 4.0, 0.0]) - centre
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, np.array([0.0, 0.0, 1.0]))
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    r = np.stack([right, down, fwd], axis=0)
    tv = -r @ centre
    return CameraModel(K=k_mat, R=r, t=tv, centre=centre,
                       elevation_deg=FALLBACK_ELEVATION_DEG,
                       azimuth_deg=math.degrees(math.atan2(dy, dx)),
                       focal_px=f, ok=False, warnings=warnings)
