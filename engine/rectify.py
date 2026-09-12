"""Top-down board views and per-square patches.

The rectified board is a square image with **a8 at the top-left and h1 at the
bottom-right** — white at the bottom, the way a diagram is drawn — regardless of
where the camera actually stands. Square ``(file, rank)`` occupies

    x in [file * S/8, (file+1) * S/8]
    y in [(7-rank) * S/8, (8-rank) * S/8]

Rectification is not de-occlusion. The camera sits ~30 degrees above the board,
so a piece's body lands one to three squares *away from the camera* of where it
really stands; only its base is on its own square. That is why patches come in
strips: ``near`` is the 30 % of a square closest to the camera, which is where
its own piece's base sits, and ``far`` is the 30 % furthest away, which is the
part of the square least contaminated by the piece in front of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import cv2
import numpy as np

if TYPE_CHECKING:
    from .calibrate import Calibration

Part = Literal["full", "near", "far"]

# Unit vector, in rectified pixel coordinates, pointing from the board toward
# the camera, for each board edge the camera can be nearest.
TOWARD_CAMERA: dict[str, tuple[int, int]] = {
    "rank1": (0, 1),    # white's edge is the bottom of the rectified image
    "rank8": (0, -1),
    "filea": (-1, 0),
    "fileh": (1, 0),
}

STRIP = 0.30


def board_to_rect(size: int) -> np.ndarray:
    """Board corners [a1, h1, h8, a8] -> rectified pixel corners."""
    s = float(size)
    return np.array([[0.0, s], [s, s], [s, 0.0], [0.0, 0.0]], dtype=np.float32)


def homography(corners: np.ndarray, size: int) -> np.ndarray:
    """Image -> rectified, from board-ordered corners [a1, h1, h8, a8]."""
    src = np.asarray(corners, dtype=np.float32).reshape(4, 2)
    return cv2.getPerspectiveTransform(src, board_to_rect(size))


def rectify(img: np.ndarray, calib: "Calibration", size: int = 512) -> np.ndarray:
    """Warp a photograph to the top-down board view."""
    h = calib.H if size == calib.size else homography(calib.corners, size)
    return cv2.warpPerspective(img, h, (size, size), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT)


def square_box(size: int, file: int, rank: int) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of one square in a rectified image."""
    s = size / 8.0
    x0 = int(round(file * s))
    y0 = int(round((7 - rank) * s))
    return x0, y0, int(round(x0 + s)), int(round(y0 + s))


def square_patch(rect: np.ndarray, file: int, rank: int, part: Part = "full",
                 camera_side: str = "rank1", inset: float = 0.06) -> np.ndarray:
    """One square (or one strip of it) out of a rectified board.

    ``inset`` trims a fraction of the square from every side before the strip is
    taken, so grid lines and neighbouring squares stay out of the statistics.
    """
    size = rect.shape[0]
    x0, y0, x1, y1 = square_box(size, file, rank)
    w = x1 - x0
    pad = int(round(w * inset))
    x0, y0, x1, y1 = x0 + pad, y0 + pad, x1 - pad, y1 - pad
    if part != "full":
        dx, dy = TOWARD_CAMERA[camera_side]
        near = part == "near"
        keep = STRIP
        if dx:
            span = int(round((x1 - x0) * keep))
            if (dx > 0) == near:
                x0 = x1 - span
            else:
                x1 = x0 + span
        else:
            span = int(round((y1 - y0) * keep))
            if (dy > 0) == near:
                y0 = y1 - span
            else:
                y1 = y0 + span
    return rect[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)]


def square_medians(rect: np.ndarray, ranks: range, part: Part = "far",
                   camera_side: str = "rank1") -> np.ndarray:
    """(8, len(ranks)) median luminance per square, for a Lab or grey image."""
    lab = rect if rect.ndim == 2 else cv2.cvtColor(rect, cv2.COLOR_BGR2LAB)[:, :, 0]
    out = np.zeros((8, len(ranks)), dtype=np.float64)
    for f in range(8):
        for i, r in enumerate(ranks):
            p = square_patch(lab, f, r, part, camera_side)
            out[f, i] = float(np.median(p)) if p.size else 0.0
    return out


def checker_score(rect: np.ndarray, ranks: range = range(2, 6),
                  camera_side: str = "rank1") -> tuple[float, int]:
    """How checkered the middle of the board looks, and in which phase.

    Returns ``(score, phase)`` where ``phase`` is 0 if squares with an even
    file+rank sum are the light ones and 1 otherwise. Only the empty middle
    ranks are used, and only each square's far strip, because at a shallow
    camera angle the near strip of a middle square carries the top of whatever
    piece stands in front of it.
    """
    m = square_medians(rect, ranks, "far", camera_side)
    if m.size == 0 or not np.isfinite(m).all():
        return 0.0, 0
    pattern = np.array([[1.0 if (f + r) % 2 == 0 else -1.0 for r in ranks]
                        for f in range(8)])
    x = m - m.mean()
    denom = np.linalg.norm(x) * np.linalg.norm(pattern)
    if denom < 1e-9:
        return 0.0, 0
    corr = float((x * pattern).sum() / denom)
    return abs(corr), (0 if corr > 0 else 1)
