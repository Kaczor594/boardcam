"""Rooms and event fan-out (docs/PROTOCOL.md §1, §3).

One room per game, at most one socket per role. A second socket for a role
replaces the first — that is what makes phone-sleep reconnects safe.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

CLOSE_REPLACED = 4000


@dataclass
class Room:
    room: str
    game_id: str
    sockets: dict[str, WebSocket] = field(default_factory=dict)
    camera_status: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Hub:
    """All live rooms. Sends never raise: a dead socket is simply dropped."""

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def room_for(self, room: str, game_id: str) -> Room:
        r = self._rooms.get(room)
        if r is None:
            r = Room(room=room, game_id=game_id)
            self._rooms[room] = r
        else:
            r.game_id = game_id
        return r

    def peek(self, room: str) -> Room | None:
        return self._rooms.get(room)

    async def join(self, room: str, game_id: str, role: str, ws: WebSocket) -> Room:
        r = self.room_for(room, game_id)
        async with r.lock:
            stale = r.sockets.get(role)
            r.sockets[role] = ws
        if stale is not None and stale is not ws:
            try:
                await stale.close(code=CLOSE_REPLACED, reason="replaced")
            except (RuntimeError, OSError):
                pass
        return r

    async def leave(self, room: str, role: str, ws: WebSocket) -> None:
        r = self._rooms.get(room)
        if r is None:
            return
        async with r.lock:
            # Only clear the slot if it still holds *this* socket; a replacing
            # socket must not be evicted by the replaced one's cleanup.
            if r.sockets.get(role) is ws:
                r.sockets.pop(role, None)
                if role == "camera":
                    r.camera_status = {}
            empty = not r.sockets
        if empty:
            self._rooms.pop(room, None)

    def is_connected(self, room: str, role: str) -> bool:
        r = self._rooms.get(room)
        return bool(r and role in r.sockets)

    async def send(self, room: str, role: str, message: dict) -> bool:
        r = self._rooms.get(room)
        if r is None:
            return False
        ws = r.sockets.get(role)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except (RuntimeError, OSError):
            await self.leave(room, role, ws)
            return False

    async def broadcast(self, room: str, message: dict, exclude: str | None = None) -> None:
        r = self._rooms.get(room)
        if r is None:
            return
        for role in list(r.sockets):
            if role != exclude:
                await self.send(room, role, message)


def now_ms() -> float:
    return time.time() * 1000.0


hub = Hub()
