"""Rendering profiles: how hard a synthetic corpus is.

``clean``      steep, tidy, no people — sanity corpus, not a real target.
``shallow``    Isaac's actual setup: ~25-40 deg elevation, camera on a *file*
               side so the players sit left and right, plus the full scene mess.
               This is the primary gate for the tracker.
``hard``       steeper than shallow but with hands, jitter and tripod bumps.
``nightmare``  20-25 deg, dim, heavy jitter. Reported, never gated.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np

from .render import (
    PIECE_PALETTES, SHIRT_PALETTES, SQUARE_PALETTES, TABLE_PALETTES,
    Camera, Scene, _rgb2bgr, place_camera,
)


@dataclass(frozen=True)
class Profile:
    name: str
    elev: tuple[float, float]
    fill: tuple[float, float]
    azim_jitter: float = 35.0
    sides: tuple[int, ...] = (1, 3)         # board edge the camera faces
    players: bool = True
    hand_prob: float = 0.0                  # chance a burst candidate has a hand in it
    jitter_px: float = 0.0
    bump_prob: float = 0.0
    bump_px: float = 0.0
    bursts: int = 3
    exposure: tuple[float, float] = (0.9, 1.12)
    noise: tuple[float, float] = (1.5, 3.5)
    blur: tuple[float, float] = (0.3, 0.9)
    markings_prob: float = 0.35
    pile: bool = True
    bg_motion_prob: float = 0.0
    roll_deg: float = 3.0
    light_elev: tuple[float, float] = (40.0, 80.0)


PROFILES: dict[str, Profile] = {
    "clean": Profile(
        name="clean", elev=(45.0, 80.0), fill=(0.55, 0.85), players=False,
        pile=False, bursts=1, jitter_px=0.0, noise=(1.0, 2.0), blur=(0.2, 0.6),
        markings_prob=0.15, roll_deg=1.5,
    ),
    "shallow": Profile(
        name="shallow", elev=(25.0, 40.0), fill=(0.60, 0.86),
        azim_jitter=22.0, hand_prob=0.55, jitter_px=2.0, bump_prob=0.35,
        bump_px=15.0, bg_motion_prob=0.12, noise=(2.0, 4.5),
    ),
    "hard": Profile(
        name="hard", elev=(35.0, 70.0), fill=(0.45, 0.85), hand_prob=0.55,
        jitter_px=2.0, bump_prob=0.5, bump_px=15.0, bg_motion_prob=0.15,
        noise=(2.0, 5.0), blur=(0.3, 1.2), markings_prob=0.5,
    ),
    "nightmare": Profile(
        name="nightmare", elev=(20.0, 25.0), fill=(0.50, 0.86), hand_prob=0.7,
        jitter_px=5.0, bump_prob=0.6, bump_px=25.0, bg_motion_prob=0.25,
        exposure=(0.45, 0.75), noise=(4.0, 9.0), blur=(0.6, 1.8),
        markings_prob=0.5, light_elev=(25.0, 55.0),
    ),
}


def _texture(rng: random.Random, h: int, w: int, cells: int, amp: float) -> np.ndarray:
    g = np.random.default_rng(rng.getrandbits(32))
    small = g.normal(1.0, amp, (cells, cells)).astype(np.float32)
    import cv2
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def _gradient(rng: random.Random, h: int, w: int, strength: float) -> np.ndarray:
    ang = rng.uniform(0, 2 * math.pi)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u = (xx / w - 0.5) * math.cos(ang) + (yy / h - 0.5) * math.sin(ang)
    return (1.0 + strength * u).astype(np.float32)


def _vignette(rng: random.Random, h: int, w: int, strength: float) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx / w - 0.5) * 2) ** 2 + ((yy / h - 0.5) * 2) ** 2) / math.sqrt(2)
    return (1.0 - strength * r ** 2).astype(np.float32)


def make_scene(profile: Profile, rng: random.Random, *, w: int = 1280, h: int = 720) -> Scene:
    sq_light, sq_dark, border = SQUARE_PALETTES[rng.randrange(len(SQUARE_PALETTES))]
    pw, pb = PIECE_PALETTES[rng.randrange(len(PIECE_PALETTES))]
    table = TABLE_PALETTES[rng.randrange(len(TABLE_PALETTES))]
    shirt = SHIRT_PALETTES[rng.randrange(len(SHIRT_PALETTES))]

    side = rng.choice(profile.sides)
    # Edge 0 (rank1) is faced from azimuth 270; each further edge adds 90.
    azim = (270.0 + 90.0 * side + rng.uniform(-profile.azim_jitter, profile.azim_jitter)) % 360.0
    elev = rng.uniform(*profile.elev)
    fill = rng.uniform(*profile.fill)
    focal = w * rng.uniform(0.95, 1.20)
    roll = math.radians(rng.uniform(-profile.roll_deg, profile.roll_deg))
    aim = np.array([rng.uniform(-0.7, 0.7), rng.uniform(-0.7, 0.7)])

    cam = place_camera(elev_deg=elev, azim_deg=azim, fill=fill, w=w, h=h,
                       focal=focal, roll=roll, aim_offset=aim)

    l_elev = math.radians(rng.uniform(*profile.light_elev))
    l_azim = rng.uniform(0, 2 * math.pi)
    light = np.array([math.cos(l_elev) * math.cos(l_azim),
                      math.cos(l_elev) * math.sin(l_azim), math.sin(l_elev)])

    offsets = {}
    for sq in range(64):
        offsets[sq] = (rng.uniform(-0.25, 0.25), rng.uniform(-0.25, 0.25))

    tint = np.array([[1.0 + rng.uniform(-0.025, 0.025) for _ in range(8)] for _ in range(8)])

    ang = math.radians(azim)
    outward = np.array([math.cos(ang), math.sin(ang)])
    tang = np.array([-outward[1], outward[0]])
    pile = np.array([4.0, 4.0]) + outward * rng.uniform(5.3, 6.2) + \
        tang * rng.choice([-1.0, 1.0]) * rng.uniform(3.2, 4.6)
    phone = np.array([4.0, 4.0]) + outward * rng.uniform(5.2, 5.8) + \
        tang * rng.choice([-1.0, 1.0]) * rng.uniform(1.4, 2.6)

    return Scene(
        cam=cam,
        light=light,
        light_elev=l_elev,
        sq_light=_rgb2bgr(sq_light),
        sq_dark=_rgb2bgr(sq_dark),
        border_col=_rgb2bgr(border),
        table_col=_rgb2bgr(table),
        piece_white=_rgb2bgr(pw),
        piece_black=_rgb2bgr(pb),
        shirt=_rgb2bgr(shirt),
        border=rng.uniform(0.22, 0.45),
        piece_scale=rng.uniform(0.92, 1.08),
        offsets=offsets,
        square_tint=tint,
        table_tex=_texture(rng, h, w, 24, 0.05),
        markings=rng.random() < profile.markings_prob,
        exposure=rng.uniform(*profile.exposure),
        warmth=np.array([rng.uniform(0.88, 1.10), 1.0, rng.uniform(0.88, 1.10)]),
        grad=_gradient(rng, h, w, rng.uniform(0.05, 0.22)),
        vignette=_vignette(rng, h, w, rng.uniform(0.10, 0.30)),
        noise_sigma=rng.uniform(*profile.noise),
        blur=rng.uniform(*profile.blur),
        camera_side=side,
        azim_deg=azim,
        elev_deg=elev,
        players=profile.players,
        props=profile.pile,
        pile_side=pile,
        phone_xy=phone,
        seat_shift=rng.uniform(-0.4, 0.4),
    )
