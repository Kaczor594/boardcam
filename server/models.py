"""Pydantic models for the BoardCam wire protocol (docs/PROTOCOL.md)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Result = Literal["1-0", "0-1", "1/2-1/2", "*"]
Status = Literal["ready", "running", "paused", "finished"]

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

# Reader models a game may choose (PROTOCOL.md §4). `local` arrives in Phase 6.
READER_MODELS = ("claude-fable-5-2", "claude-sonnet-5-5", "claude-opus-5-5")
ReaderModel = Literal["claude-fable-5-2", "claude-sonnet-5-5", "claude-opus-5-5"]

# Game settings a player can change after the game was created.
SETTING_KEYS = ("white_name", "black_name", "white_rating", "black_rating", "reader_model")


class GameConfig(BaseModel):
    initial_ms: int = Field(default=600_000, ge=1_000, le=3 * 3600_000)
    increment_ms: int = Field(default=5_000, ge=0, le=600_000)
    white_name: str = Field(default="White", max_length=40)
    black_name: str = Field(default="Black", max_length=40)
    # chess.com scale; Phase 3 converts to a Maia band.
    white_rating: int = Field(default=1350, ge=100, le=3500)
    black_rating: int = Field(default=1350, ge=100, le=3500)
    reader_model: ReaderModel = "claude-fable-5-2"


class GameSettings(BaseModel):
    """A partial update of the settings a finished or running game may change."""
    white_name: str | None = Field(default=None, max_length=40)
    black_name: str | None = Field(default=None, max_length=40)
    white_rating: int | None = Field(default=None, ge=100, le=3500)
    black_rating: int | None = Field(default=None, ge=100, le=3500)
    reader_model: ReaderModel | None = None


class Confirm(BaseModel):
    """A player's corrected board for one photo (PROTOCOL.md §5)."""
    rows: list[str]                 # photo frame, 8 strings of 8
    home: Literal["top", "bottom", "left", "right"]
    ply: int = Field(ge=0)
