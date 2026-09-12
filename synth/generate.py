"""Generate a synthetic corpus.

    python -m synth.generate --games 40 --profile shallow --seed 4 --out data/synth/shallow

Each game lands in its own directory using **exactly** the real-game layout from
`docs/PROTOCOL.md`, so `engine.evaluate` runs unchanged on synthetic and real
games:

    <out>/<NNN-name>/
        frames/0000_0.jpg ... 00NN_2.jpg
        events.jsonl
        truth.pgn
        truth.json

Capture numbering follows the protocol: seq 0 is the start position, seq n is
the position after ply n, and a final `clock.stop` capture photographs the same
position as the last press — a frame that advanced no plies, which the tracker
has to survive.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import chess
import cv2
import numpy as np

from .games import build_corpus
from .profiles import PROFILES, Profile, make_scene
from .render import EDGE_NAMES, FrameOpts, Renderer

JPEG_Q = 85
INITIAL_MS = 600_000
INCREMENT_MS = 5_000


def _clock_times(rng: random.Random, n_plies: int) -> list[tuple[int, int]]:
    """(white_ms, black_ms) remaining after each of the n plies."""
    w = b = INITIAL_MS
    out = []
    for i in range(n_plies):
        spent = int(rng.triangular(1200, 40_000, 7_000))
        if i % 2 == 0:
            w = max(0, w - spent + INCREMENT_MS)
        else:
            b = max(0, b - spent + INCREMENT_MS)
        out.append((w, b))
    return out


def _bump_affine(rng: random.Random, px: float) -> np.ndarray:
    ang = rng.uniform(-0.012, 0.012)
    c, s = np.cos(ang), np.sin(ang)
    tx, ty = rng.uniform(-px, px), rng.uniform(-px, px)
    return np.array([[c, -s, tx], [s, c, ty]])


def render_game(args) -> dict:
    idx, game, profile_name, seed, out_dir, bursts, w, h = args
    profile: Profile = PROFILES[profile_name]
    rng = random.Random((seed * 1_000_003) ^ (idx * 7919) ^ 0x5EED)

    scene = make_scene(profile, rng, w=w, h=h)
    renderer = Renderer(scene)
    gdir = Path(out_dir) / f"{idx:03d}-{game.name}"
    (gdir / "frames").mkdir(parents=True, exist_ok=True)

    n = len(game.moves)
    bump_seq = None
    bump = None
    if profile.bump_prob and rng.random() < profile.bump_prob:
        bump_seq = rng.randint(3, max(4, n - 1))

    times = _clock_times(rng, n)
    t0 = int(time.time() * 1000)
    events = [{"type": "clock.config", "initial_ms": INITIAL_MS,
               "increment_ms": INCREMENT_MS, "white_name": "White",
               "black_name": "Black", "t": t0, "server_t": t0}]

    board = chess.Board()
    captured = 0
    corners_by_seq: dict[str, list] = {}
    pose = rng.uniform(0, 6.0)
    elapsed = 0

    def write_burst(seq: int, brd: chess.Board, k_count: int, hand_sq: int | None,
                    white_moved: bool | None) -> None:
        nonlocal corners_by_seq
        hand_k = -1
        if hand_sq is not None and profile.hand_prob and rng.random() < profile.hand_prob:
            hand_k = rng.randrange(k_count)
        for k in range(k_count):
            opt = FrameOpts(
                jitter=(rng.uniform(-profile.jitter_px, profile.jitter_px),
                        rng.uniform(-profile.jitter_px, profile.jitter_px)),
                bump=bump,
                pose=pose + 0.35 * k,
                captured=captured,
                pile_nudge=(rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2)),
                bg_motion=(rng.random() if rng.random() < profile.bg_motion_prob else None),
                hand=((hand_sq, 0.25 + 0.35 * k, bool(white_moved)) if k == hand_k else None),
                noise_seed=rng.getrandbits(31),
            )
            img, corners = renderer.render(brd, opt)
            cv2.imwrite(str(gdir / "frames" / f"{seq:04d}_{k}.jpg"), img,
                        [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q])
            if k == 0:
                corners_by_seq[str(seq)] = np.round(corners, 3).tolist()

    # seq 0: the start position, uploaded before the clock starts.
    write_burst(0, board, 1, None, None)

    for ply, mv in enumerate(game.moves, start=1):
        white_moved = board.turn == chess.WHITE
        if board.is_capture(mv):
            captured += 1
        board.push(mv)
        pose += rng.uniform(0.25, 0.9)
        elapsed += rng.randint(1500, 30_000)
        if bump_seq is not None and ply == bump_seq:
            bump = _bump_affine(rng, profile.bump_px)
        wm, bm = times[ply - 1]
        ev = {"type": "clock.start" if ply == 1 else "clock.press",
              "seq": ply, "side": "white" if white_moved else "black",
              "white_ms": wm, "black_ms": bm, "t": t0 + elapsed,
              "server_t": t0 + elapsed + rng.randint(20, 90)}
        events.append(ev)
        write_burst(ply, board, bursts, mv.to_square, white_moved)

    stop_seq = n + 1
    elapsed += rng.randint(1000, 8000)
    events.append({"type": "clock.stop", "seq": stop_seq, "result": game.result(),
                   "t": t0 + elapsed, "server_t": t0 + elapsed + 40})
    write_burst(stop_seq, board, bursts, None, None)

    (gdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    (gdir / "truth.pgn").write_text(game.pgn({
        "Event": "BoardCam synthetic", "Site": profile_name,
        "TimeControl": f"{INITIAL_MS // 1000}+{INCREMENT_MS // 1000}"}) + "\n")

    truth = {
        "game": game.name,
        "source": game.source,
        "profile": profile_name,
        "seed": seed,
        "index": idx,
        "plies": n,
        "stop_seq": stop_seq,
        "bump_seq": bump_seq,
        "features": sorted(game.features),
        "image": [w, h],
        "corners": corners_by_seq["0"],
        "corners_by_seq": corners_by_seq,
        "camera_side_idx": scene.camera_side,
        "camera_side": EDGE_NAMES[scene.camera_side],
        "elevation_deg": round(scene.elev_deg, 3),
        "azimuth_deg": round(scene.azim_deg, 3),
        "focal_px": round(scene.cam.f, 3),
        "square_is_light": [[(f + r) % 2 == 1 for r in range(8)] for f in range(8)],
    }
    (gdir / "truth.json").write_text(json.dumps(truth, indent=1))
    return {"dir": gdir.name, "plies": n, "frames": (n + 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--games", type=int, required=True)
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES))
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bursts", type=int, default=None,
                    help="burst candidates per capture (default: the profile's)")
    ap.add_argument("--max-plies", type=int, default=None,
                    help="truncate every game to this many plies (tests and dev only)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    a = ap.parse_args(argv)

    profile = PROFILES[a.profile]
    bursts = a.bursts if a.bursts is not None else profile.bursts
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    corpus = build_corpus(a.games, random.Random(a.seed))
    if a.max_plies:
        for g in corpus:
            del g.moves[a.max_plies:]
    jobs = [(i, g, a.profile, a.seed, str(out), bursts, a.width, a.height)
            for i, g in enumerate(corpus)]

    t0 = time.time()
    done = 0
    if a.workers <= 1:
        results = [render_game(j) for j in jobs]
        done = len(results)
    else:
        results = []
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for r in ex.map(render_game, jobs):
                results.append(r)
                done += 1
                print(f"  [{done:3d}/{len(jobs)}] {r['dir']}  "
                      f"{r['plies']} plies", flush=True)

    dt = time.time() - t0
    frames = sum(r["frames"] for r in results)
    print(f"{len(results)} game dirs in {out}  "
          f"({frames} captures, {dt:.1f}s, {dt / max(frames, 1):.2f}s per capture)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
