"""Read a chess position from one hand-held photo.

``read_position`` returns the 8x8 board **in the photo's own frame** — row 0 is
the rank of squares farthest from the camera, column 0 the leftmost — plus the
reader's call on which side of the photo White's home rank is. Turning that into
a FEN is ``engine.position``'s job, so the reader never has to know who sits
where.

Backends are swappable. ``claude`` (the default) sends the photo to a Claude
model with structured output; ``local`` arrives in Phase 6 of
planning/specs/2026-11-16-photo-positions.md behind the same signature.

A refusal, a truncated reply or a malformed board is raised as ``ReadError``,
never returned as an empty board: an empty board is a legal-looking answer and
would be stored as one. No server-side model fallback is configured, on purpose
— a read silently served by a different model would carry that model's
accuracy under this one's name.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Literal

from PIL import Image, ImageOps
from pydantic import BaseModel

DEFAULT_MODEL = "claude-fable-5-2"
MAX_EDGE = 2576                 # long-edge pixels the current Claude models accept
PIECES = set("KQRBNPkqrbnp.")
HOME_SIDES = ("top", "bottom", "left", "right", "unknown")

# $ per million tokens, input / output (claude-api skill, cached 2026-11-03).
PRICES = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

SET_DESCRIPTION = """The mat is green and white. The printed letters and numbers on the mat \
are unreliable; ignore them. Pieces standing off the playing squares are captured and must be \
ignored. A piece belongs to the square its base stands on; tall pieces lean into the square \
behind them in the photo.

The set is a modernist one. White pieces are light tan wood, black pieces are black.
- Pawn: short plain cone.
- Bishop: tall slender cone, clearly taller than a pawn.
- Rook: waisted cylinder with a flat, cupped (concave) top.
- Queen: like the rook but taller, with a rounded dome top.
- King: the tallest piece, with a small finial or cross on top.
- Knight: carved block with a notch (stylised horse head).
A piece may be lying on its side."""

ANSWER = """Report the position as 8 strings, R1 first, each exactly 8 characters for C1..C8. \
Use K Q R B N P for white, k q r b n p for black and . for an empty square. Also say on which \
side of the photo White's home rank is (where White's pieces started), or unknown."""

PROMPT_PHOTO = f"""This is a photo of a chess game in progress, taken by hand from beside \
the board. Number the 8 rows of squares R1 (the row farthest from the camera, at the top of \
the photo) to R8 (the row nearest the camera), and the 8 columns C1 (left) to C8 (right), as \
they appear in the photo. {SET_DESCRIPTION}

{ANSWER}"""

# The pilot's prompt (2026-11-16): a crop of the mat plus a copy with a drawn grid.
PROMPT_CROP_GRID = f"""These are two copies of the same photo of a chess game in progress on \
a green-and-white mat. The second copy has a red 8x8 grid drawn over the playing squares, with \
rows labelled R1 (far side) to R8 (near side) and columns C1 (left) to C8 (right). \
{SET_DESCRIPTION}

{ANSWER}"""


class ReadError(RuntimeError):
    """The reader produced no usable board. ``reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class BoardRead:
    rows: list[str]                     # 8 strings of 8, photo frame, row 0 = far side
    home: str                           # White's home side in the photo, or "unknown"
    backend: str
    model: str
    cost: float = 0.0                   # dollars
    secs: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


class _Board(BaseModel):
    rows: List[str]
    white_home_side: Literal["top", "bottom", "left", "right", "unknown"]


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def load_image(image) -> Image.Image:
    """A path, bytes or PIL image, upright (EXIF orientation applied), RGB."""
    if isinstance(image, Image.Image):
        img = image
    elif isinstance(image, (bytes, bytearray)):
        img = Image.open(io.BytesIO(image))
    else:
        img = Image.open(Path(image))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def jpeg_b64(img: Image.Image, max_edge: int = MAX_EDGE, quality: int = 90) -> str:
    img = img.copy()
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode()


def check_rows(rows: list[str]) -> list[str]:
    if len(rows) != 8 or any(len(r) != 8 for r in rows):
        raise ReadError("bad_shape", f"{len(rows)} rows of lengths {[len(r) for r in rows]}")
    bad = {ch for r in rows for ch in r} - PIECES
    if bad:
        raise ReadError("bad_symbol", "".join(sorted(bad)))
    return list(rows)


# --------------------------------------------------------------------------
# Claude backend
# --------------------------------------------------------------------------

_client = None


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _read_claude(images: list[Image.Image], prompt: str, model: str,
                 max_edge: int, effort: str | None, client) -> BoardRead:
    import anthropic

    client = client or _anthropic()
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": jpeg_b64(img, max_edge)}} for img in images]
    content.append({"type": "text", "text": prompt})
    kwargs = {}
    if effort:
        kwargs["output_config"] = {"effort": effort}
    t0 = time.time()
    try:
        r = client.messages.parse(model=model, max_tokens=16000,
                                  messages=[{"role": "user", "content": content}],
                                  output_format=_Board, **kwargs)
    except anthropic.RateLimitError as exc:
        raise ReadError("rate_limited", str(exc)[:300]) from exc
    except anthropic.APIStatusError as exc:
        raise ReadError(f"api_{exc.status_code}", str(exc.message)[:300]) from exc
    except anthropic.APIConnectionError as exc:
        raise ReadError("connection", str(exc)[:300]) from exc
    secs = time.time() - t0

    if r.stop_reason == "refusal":
        cat = getattr(getattr(r, "stop_details", None), "category", None)
        raise ReadError("refusal", str(cat or ""))
    if r.stop_reason == "max_tokens":
        raise ReadError("max_tokens", f"after {r.usage.output_tokens} output tokens")
    if r.stop_reason != "end_turn" or r.parsed_output is None:
        raise ReadError("no_board", f"stop_reason={r.stop_reason}")

    pin, pout = PRICES.get(model, (0.0, 0.0))
    u = r.usage
    return BoardRead(rows=check_rows(r.parsed_output.rows),
                     home=r.parsed_output.white_home_side,
                     backend="claude", model=model,
                     cost=round((u.input_tokens * pin + u.output_tokens * pout) / 1e6, 5),
                     secs=round(secs, 1), tokens_in=u.input_tokens,
                     tokens_out=u.output_tokens)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def read_position(image, backend: str = "claude", model: str = DEFAULT_MODEL, *,
                  grid_image=None, max_edge: int = MAX_EDGE,
                  effort: str | None = None, client=None) -> BoardRead:
    """Read one photo. ``image`` is a path, bytes or PIL image.

    ``grid_image`` selects the pilot's two-image variant (a crop of the mat plus
    the same crop with a labelled grid drawn on it); without it the whole photo
    is read on its own, which needs no grid finder.
    """
    if backend != "claude":
        raise ValueError(f"unknown reader backend {backend!r}")
    images = [load_image(image)]
    prompt = PROMPT_PHOTO
    if grid_image is not None:
        images.append(load_image(grid_image))
        prompt = PROMPT_CROP_GRID
    return _read_claude(images, prompt, model, max_edge, effort, client)
