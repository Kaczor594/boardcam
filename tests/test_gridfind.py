"""Grid finder and square crops on synthetic mats (photo-positions spec, Phase 5).

The synthetic mat copies what tripped the finder on real photos: a green
frame band of the same colour as the squares, a white margin, perspective,
and pieces hiding every dark square of one edge rank. Real-photo accuracy is
the ``engine.gridfind --eval`` gate on hand-tapped corners.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from PIL import Image

from engine import gridfind as G
from engine.labels import board_corner_order
from engine.position import square_of

GREEN = (38, 104, 52)
WHITE = (238, 236, 228)
WOOD = (176, 134, 88)
TAN = (214, 176, 120)
BLACK = (22, 20, 18)

# a8, h8, h1, a1 in a 1600 x 1200 image: seen from White's side, tilted.
CORNERS = np.array([[470, 260], [1150, 250], [1390, 940], [210, 960]], np.float64)


def render(corners=CORNERS, hide_rank=None, size=(1600, 1200), pieces=()):
    w, h = size
    H = G.board_homography(corners)
    Hinv = np.linalg.inv(H)
    ys, xs = np.mgrid[0:h, 0:w]
    pts = np.stack([xs.ravel(), ys.ravel()], 1).astype(np.float64)
    uv = G._apply(Hinv, pts)
    u, v = uv[:, 0], uv[:, 1]
    img = np.empty((h * w, 3), np.uint8)
    img[:] = WOOD
    mat = (u > -0.9) & (u < 8.9) & (v > -0.9) & (v < 8.9)
    img[mat] = WHITE
    band = mat & ~((u > -0.62) & (u < 8.62) & (v > -0.62) & (v < 8.62)) & \
        (u > -0.82) & (u < 8.82) & (v > -0.82) & (v < 8.82)
    img[band] = GREEN
    inside = (u >= 0) & (u < 8) & (v >= 0) & (v < 8)
    dark = inside & ((np.floor(u) + np.floor(v)) % 2 == 1)   # a8 (0,0) is light
    img[dark] = GREEN
    img = img.reshape(h, w, 3)
    # Pieces: a disc at the base plus a body rising up the photo.
    todo = list(pieces)
    if hide_rank is not None:
        r = 8 - hide_rank
        todo += [(f, r, TAN if hide_rank <= 2 else BLACK) for f in range(8)]
    for f, r, col in todo:
        base = G._apply(H, np.array([[f + 0.5, r + 0.5]]))[0]
        q = G._apply(H, np.array([[f, r], [f + 1, r], [f + 1, r + 1], [f, r + 1]], np.float64))
        wsq = np.linalg.norm(q[1] - q[0])
        cv2.ellipse(img, (int(base[0]), int(base[1])), (int(0.47 * wsq), int(0.4 * wsq)), 0, 0, 360, col, -1)
        cv2.rectangle(img, (int(base[0] - 0.3 * wsq), int(base[1] - 1.3 * wsq)),
                      (int(base[0] + 0.3 * wsq), int(base[1])), col, -1)
    return Image.fromarray(img)


def max_err(grid, truth=CORNERS):
    assert grid.corners is not None, grid.info
    return G.corner_errors(np.array(grid.corners), truth).max()


def test_finds_a_clear_mat():
    g = G.find_grid(render())
    assert max_err(g) < 0.1
    assert g.confidence >= G.CONF_OK


def test_hidden_edge_rank_is_not_extended_onto_the_frame_band():
    # Every square of rank 1 covered: the lattice sees 7 ranks and must add
    # the 8th on the board side, not on the green frame band beyond the margin.
    g = G.find_grid(render(hide_rank=1))
    assert max_err(g) < 0.1, g.info
    g = G.find_grid(render(hide_rank=8))
    assert max_err(g) < 0.1, g.info


def test_mat_running_off_the_photo():
    shifted = CORNERS - [330, 0]                   # a-file side out of the frame
    g = G.find_grid(render(shifted))
    assert max_err(g, shifted) < 0.15, g.info


def test_no_board_gives_no_corners_and_zero_confidence():
    g = G.find_grid(Image.new("RGB", (800, 600), WOOD))
    assert g.corners is None and g.confidence == 0.0


def test_corner_errors_ignore_starting_corner_and_direction():
    t = CORNERS
    for k in range(4):
        assert G.corner_errors(np.roll(t, k, axis=0), t).max() == pytest.approx(0)
        assert G.corner_errors(np.roll(t[::-1], k, axis=0), t).max() == pytest.approx(0)
    # One corner off by half a square at that corner.
    side = (np.linalg.norm(t[2] - t[1]) + np.linalg.norm(t[2] - t[3])) / 16
    off = t.copy()
    off[2] += [0.5 * side, 0]
    e = G.corner_errors(off, t)
    assert e.max() == pytest.approx(0.5) and sorted(e)[0] == pytest.approx(0)


def test_crops_put_the_target_square_bottom_centre():
    img = render(pieces=[(4, 6, BLACK)])            # e2 (file 4, row 6 from the a8 side)
    crops = G.square_crops(np.asarray(img), CORNERS)
    assert len(crops) == 64 and crops["e2"].shape == (G.CROP_H, G.CROP_W, 3)
    # The base of the piece on e2 sits at the bottom centre of its crop.
    by = int(G.CROP_H * G.UP / (G.UP + G.DOWN))
    patch = crops["e2"][by - 4:by + 4, G.CROP_W // 2 - 4:G.CROP_W // 2 + 4]
    assert patch.mean() < 60
    assert crops["e4"][by - 4:by + 4, G.CROP_W // 2 - 4:G.CROP_W // 2 + 4].mean() > 60


@pytest.mark.parametrize("home", ["bottom", "top", "left", "right"])
def test_board_corner_order_matches_square_of(home):
    # Photo corner k (TL, TR, BR, BL) is the outer corner of cell (r, c); the
    # square there must be the named corner square.
    cell = [(0, 0), (0, 7), (7, 7), (7, 0)]
    names = ["a8", "h8", "h1", "a1"]
    import chess
    for name, k in zip(names, board_corner_order(home)):
        assert chess.square_name(square_of(*cell[k], home)) == name


def test_lens_distortion_is_followed_to_the_corners():
    # Bend the clear mat with one radial term about the photo centre, as a
    # phone lens does; the corners must follow the bend, not a straight fit.
    img = np.asarray(render())
    h, w = img.shape[:2]
    centre, norm, k = np.array([w / 2, h / 2]), float(np.hypot(w, h) / 2), -0.08
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    # Output pixel p shows the undistorted point _undistort(p).
    src = G._undistort(np.stack([xs.ravel(), ys.ravel()], 1).astype(np.float64), k, centre, norm)
    mx = src[:, 0].reshape(h, w).astype(np.float32)
    my = src[:, 1].reshape(h, w).astype(np.float32)
    bent = cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderValue=WOOD)
    truth = G._distort(CORNERS, k, centre, norm)
    g = G.find_grid(Image.fromarray(bent))
    assert max_err(g, truth) < 0.1, g.info
    assert g.info["k"] == pytest.approx(k, abs=0.02)
