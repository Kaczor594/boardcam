"""Lichess PGN import. Phase 5 wires this to a button; Phase 1 just ships it."""

from __future__ import annotations

import httpx

IMPORT_URL = "https://lichess.org/api/import"


async def import_pgn(pgn: str, timeout: float = 15.0) -> str | None:
    """POST a PGN to lichess, return the game URL (or None on failure)."""
    if not pgn.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(IMPORT_URL, data={"pgn": pgn})
            resp.raise_for_status()
            return resp.json().get("url")
    except (httpx.HTTPError, ValueError):
        return None
