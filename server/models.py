"""Pydantic models for the BoardCam wire protocol (docs/PROTOCOL.md)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Result = Literal["1-0", "0-1", "1/2-1/2", "*"]
Status = Literal["ready", "running", "paused", "finished"]
Role = Literal["clock", "camera"]

# Event types the clock may send. Anything else is ignored rather than rejected,
# so later phases can add events without breaking older phone pages.
CLOCK_EVENTS = {
    "clock.config",
    "clock.start",
    "clock.press",
    "clock.pause",
    "clock.resume",
    "clock.flag",
    "clock.stop",
    "clock.reset",
}

# Which clock events make the camera grab a burst, and the `reason` it is told.
CAPTURE_REASONS = {
    "clock.start": "start",
    "clock.press": "press",
    "clock.flag": "flag",
    "clock.stop": "stop",
}


class GameConfig(BaseModel):
    initial_ms: int = Field(default=600_000, ge=1_000, le=3 * 3600_000)
    increment_ms: int = Field(default=5_000, ge=0, le=600_000)
    white_name: str = Field(default="White", max_length=40)
    black_name: str = Field(default="Black", max_length=40)


class GameCreated(BaseModel):
    game_id: str
    room: str
    created_at: float
    initial_ms: int
    increment_ms: int


class GameSummary(BaseModel):
    game_id: str
    room: str
    created_at: float
    status: Status
    result: Result | None
    frames: int
    plies: int
    white_name: str
    black_name: str


class FrameAck(BaseModel):
    ok: bool
    seq: int
    k: int
    bytes: int
    path: str
