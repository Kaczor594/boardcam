"""Board calibration: where the board is, and which way round it is.

The thresholds here are the Phase 2 gate. They are measured on the corpora in
``data/synth/`` when those exist (regenerate them with the seeds in the spec);
otherwise a small corpus is rendered into a temp directory, because calibration
only ever looks at the start-position frame and so needs almost no game.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from engine.calibrate import calibrate, calibrate_image, load_frames, recalibrate
from engine.rectify import TOWARD_CAMERA, rectify, square_patch
from synth import generate

REPO = Path(__file__).resolve().parents[1]
GATE_SEEDS = {"clean": 1, "shallow": 4, "hard": 2, "nightmare": 3}
FALLBACK_GAMES = 6
RMS_PX = 3.0
PASS_RATE = 0.95


def _corpus(profile: str, tmp_root: Path) -> list[Path]:
    real = REPO / "data" / "synth" / profile
    if real.is_dir():
        dirs = sorted(p for p in real.iterdir() if (p / "truth.json").exists())
        if dirs:
            return dirs
    out = tmp_root / profile
    if not out.exists():
        generate.main(["--games", str(FALLBACK_GAMES), "--profile", profile,
                       "--seed", str(GATE_SEEDS[profile]), "--out", str(out),
                       "--bursts", "1", "--max-plies", "2", "--workers", "3"])
    return sorted(p for p in out.iterdir() if (p / "truth.json").exists())


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("synth")


def _calibrate_dir(d: Path):
    truth = json.loads((d / "truth.json").read_text())
    cal = calibrate(sorted((d / "frames").glob("0000_*.jpg")))
    rms = float(np.sqrt(((cal.corners - np.array(truth["corners"])) ** 2).sum(axis=1).mean()))
    return cal, truth, rms


@pytest.fixture(scope="session")
def gate_results(corpus_root):
    out = {}
    for profile in ("clean", "shallow", "hard"):
        out[profile] = [_calibrate_dir(d) for d in _corpus(profile, corpus_root)]
    return out


def test_corner_accuracy(gate_results):
    """At 30 degrees the far edge is short and overhung by pieces, so the
    detector has to lean on the near edge and the two sides."""
    rows = [r for rs in gate_results.values() for r in rs]
    rms = np.array([r[2] for r in rows])
    passed = rms <= RMS_PX
    report = "\n".join(
        f"  {t['profile']:9s} {t['game'][:26]:26s} elev={t['elevation_deg']:5.1f} "
        f"rms={v:8.2f} score={c.score:.3f} {','.join(c.warnings)}"
        for (c, t, v) in rows if v > RMS_PX)
    assert passed.mean() >= PASS_RATE, (
        f"{passed.sum()}/{len(passed)} frame-0s within {RMS_PX}px "
        f"(median {np.median(rms):.2f}px)\n{report}")


def test_orientation_where_corners_passed(gate_results):
    bad = []
    for profile, rows in gate_results.items():
        for cal, truth, rms in rows:
            if rms > RMS_PX:
                continue
            if cal.camera_side != truth["camera_side"]:
                bad.append(f"{profile}/{truth['game']}: camera side "
                           f"{cal.camera_side} != {truth['camera_side']}")
            if not np.array_equal(cal.square_is_light, np.array(truth["square_is_light"])):
                bad.append(f"{profile}/{truth['game']}: square colours")
    assert not bad, "\n".join(bad)


def test_light_squares_read_lighter_in_the_rectified_view(gate_results):
    """End-to-end check of the orientation chain, straight off the pixels.

    Only the empty middle ranks are looked at. A corner square like a1 is no use
    here: at a shallow angle its far strip shows the body of the rook standing
    on it, not the square, which is the whole reason `square_patch` has strips.
    """
    checked = 0
    for rows in gate_results.values():
        for cal, truth, rms in rows:
            if rms > RMS_PX:
                continue
            d = REPO / "data" / "synth" / truth["profile"]
            gdir = d / f"{truth['index']:03d}-{truth['game']}"
            frames = sorted((gdir / "frames").glob("0000_*.jpg")) if gdir.is_dir() else []
            if not frames:
                continue
            rect = rectify(load_frames(frames), cal)
            lab = cv2.cvtColor(rect, cv2.COLOR_BGR2LAB)[:, :, 0]
            light, dark = [], []
            for f in range(8):
                for r in range(2, 6):
                    v = float(np.median(square_patch(lab, f, r, "far", cal.camera_side)))
                    (light if cal.square_is_light[f][r] else dark).append(v)
            assert np.median(light) > np.median(dark) + 8, (
                f"{truth['profile']}/{truth['game']}: light {np.median(light):.0f} "
                f"vs dark {np.median(dark):.0f}")
            checked += 1
    if checked == 0:
        pytest.skip("no generated corpus on disk")
    assert checked >= 10


def test_flip_is_an_involution(gate_results):
    cal = gate_results["shallow"][0][0]
    back = cal.flipped().flipped()
    assert np.allclose(back.corners, cal.corners)
    assert cal.flipped().camera_side != cal.camera_side
    assert cal.flipped().camera_side in TOWARD_CAMERA
    assert back.camera_side == cal.camera_side


def _first_frame(corpus_root, profile="shallow"):
    d = _corpus(profile, corpus_root)[0]
    return load_frames(sorted((d / "frames").glob("0000_*.jpg")))


def test_recalibrate_follows_a_bump(corpus_root):
    img = _first_frame(corpus_root)
    prior = calibrate_image(img)
    assert prior.ok
    shift = np.array([[1.0, 0.0, 11.0], [0.0, 1.0, -7.0]])
    moved = cv2.warpAffine(img, shift, (img.shape[1], img.shape[0]),
                           borderMode=cv2.BORDER_REPLICATE)
    cal = recalibrate(moved, prior)
    assert "drift" not in cal.warnings
    delta = cal.corners - prior.corners
    assert np.allclose(delta, np.array([11.0, -7.0]), atol=2.0), delta


def test_recalibrate_keeps_the_prior_when_the_board_is_gone(corpus_root):
    img = _first_frame(corpus_root)
    prior = calibrate_image(img)
    blank = np.full_like(img, 128)
    cal = recalibrate(blank, prior)
    assert "drift" in cal.warnings
    assert np.allclose(cal.corners, prior.corners)


@pytest.mark.parametrize("side,axis,sign", [
    ("rank1", 1, +1), ("rank8", 1, -1), ("filea", 0, -1), ("fileh", 0, +1)])
def test_square_patch_strips_face_the_camera(side, axis, sign):
    """`near` must be the strip on the camera's side of the square."""
    size = 512
    rect = np.zeros((size, size), dtype=np.uint8)
    rect[:] = np.arange(size)[None, :] if axis == 0 else np.arange(size)[:, None]
    near = float(square_patch(rect, 3, 3, "near", side).mean())
    far = float(square_patch(rect, 3, 3, "far", side).mean())
    assert (near > far) == (sign > 0)
