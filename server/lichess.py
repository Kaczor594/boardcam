"""Lichess PGN import: the one button that leaves the house.

The endpoint answers a plain form post with a 303 to the new game, and only
returns the JSON body if it is asked for one — so the ``Accept`` header here is
load-bearing, not decoration. The redirect is still honoured as a fallback,
because a URL in a ``Location`` header is just as good as one in a body.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

IMPORT_URL = "https://lichess.org/api/import"


async def import_pgn(pgn: str, timeout: float = 15.0) -> str | None:
    """POST a PGN to lichess, return the game URL (or None on failure)."""
    if not pgn.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(IMPORT_URL, data={"pgn": pgn},
                                     headers={"Accept": "application/json"})
            resp.raise_for_status()
            if resp.is_redirect:
                return resp.headers.get("location")
            return resp.json().get("url")
    except httpx.HTTPStatusError as exc:
        # 429 is the one that will actually happen: lichess throttles imports,
        # and a silent None looks like a bug rather than a wait.
        log.warning("lichess import refused: %s", exc.response.status_code)
        return None
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("lichess import failed: %s", exc)
        return None
