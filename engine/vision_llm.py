"""Vision-LLM fallback for flagged plies.

The tracker (Notes §C, ``engine/emission.py``) is a forward model over
silhouettes: it knows what *should* be a piece's height and base, never what a
piece looks like. When two or more candidates end up close (``low_margin``) or
nothing explains a frame at all (``unexplained``), no amount of re-tuning the
score fixes that — the fix is to show the actual photographs to something that
can see. This module is that fallback: one forced tool call per flagged ply,
capped and cached per game so a bad game cannot spend without limit.

``resolve_ply`` is the pure, single-call primitive (mockable in tests).
``LLMResolver`` adds the caching and call cap that make it safe to wire into a
beam search that revisits the same frame many times across corrections.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .rectify import square_box

DEFAULT_MODEL = "claude-sonnet-5"
ESCALATION_MODEL = "claude-opus-5"
MAX_CALLS_PER_GAME = 10
CONFIDENCE_ESCALATE_BELOW = 0.7
NONE_OF_THESE = "none_of_these"

# $ / 1M tokens (input, output) — logged, never used to gate a call.
_PRICE = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}


@dataclass
class Option:
    """One candidate the model may pick, as shown to the LLM.

    ``label`` is the exact string that appears in the tool's enum and in the
    answer, so it must be unique within one call. ``moves`` is 0, 1 or 2 UCI
    strings — empty for "no move happened".
    """

    label: str
    moves: tuple[str, ...] = ()


def _as_option(o) -> Option:
    return o if isinstance(o, Option) else Option(**o)


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def _load_bgr(source) -> np.ndarray:
    if isinstance(source, np.ndarray):
        return source
    img = cv2.imread(str(source), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read image: {source}")
    return img


def _b64_png(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("failed to encode image as PNG")
    return base64.standard_b64encode(buf.tobytes()).decode("ascii")


def _image_block(img: np.ndarray) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _b64_png(img)}}


def _raw_crop(source, calib, margin: float = 0.15) -> np.ndarray:
    """The raw photograph, cropped to the board's bounding box plus a margin.

    This is what puts hands, the far strip and the captured-piece pile in
    frame for the model without also handing it the whole table.
    """
    img = _load_bgr(source)
    corners = np.asarray(getattr(calib, "corners_image", None)
                         if getattr(calib, "corners_image", None) is not None
                         else calib.corners, dtype=np.float64).reshape(4, 2)
    x0, y0 = corners.min(axis=0)
    x1, y1 = corners.max(axis=0)
    w, h = x1 - x0, y1 - y0
    x0, x1 = x0 - w * margin, x1 + w * margin
    y0, y1 = y0 - h * margin, y1 + h * margin
    H, W = img.shape[:2]
    x0i, y0i = max(int(x0), 0), max(int(y0), 0)
    x1i, y1i = min(int(x1), W), min(int(y1), H)
    return img[y0i:y1i, x0i:x1i]


def _squares_touched(uci_moves: tuple[str, ...]) -> set[tuple[int, int]]:
    import chess
    out = set()
    for u in uci_moves:
        m = chess.Move.from_uci(u)
        out.add((chess.square_file(m.from_square), chess.square_rank(m.from_square)))
        out.add((chess.square_file(m.to_square), chess.square_rank(m.to_square)))
    return out


def _outline_squares(rect: np.ndarray, squares: set[tuple[int, int]]) -> np.ndarray:
    """Rectified board with the candidates' squares outlined in red."""
    out = rect.copy()
    size = out.shape[0]
    thickness = max(2, size // 170)
    for file, rank in squares:
        x0, y0, x1, y1 = square_box(size, file, rank)
        cv2.rectangle(out, (x0, y0), (x1, y1), (0, 0, 255), thickness)
    return out


# --------------------------------------------------------------------------
# The API call
# --------------------------------------------------------------------------

def _tool_schema(labels: list[str]) -> dict:
    return {
        "name": "choose_move",
        "description": ("Identify which candidate move, if any, was played on a "
                         "chess board between two photographs."),
        "input_schema": {
            "type": "object",
            "properties": {
                "choice": {"type": "string", "enum": labels + [NONE_OF_THESE]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["choice", "confidence"],
            "additionalProperties": False,
        },
    }


def _call(model: str, content: list[dict], tool: dict, client=None) -> dict:
    import anthropic
    client = client or anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=256,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}],
    )
    block = next(b for b in response.content if b.type == "tool_use")
    inp = block.input
    if isinstance(inp, str):                     # some transports hand back raw JSON
        inp = json.loads(inp)
    price_in, price_out = _PRICE.get(model, (0.0, 0.0))
    usage = response.usage
    cost = (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000
    confidence = max(0.0, min(1.0, float(inp["confidence"])))
    return {"san": inp["choice"], "confidence": confidence, "model": model, "cost": cost}


def resolve_ply(before_paths, after_paths, rect_before, rect_after, candidates,
                 side_to_move: str, calib, model: str = DEFAULT_MODEL, client=None) -> dict:
    """Ask Claude which candidate happened, from the actual photographs.

    ``before_paths``/``after_paths``: raw burst frames (paths or ndarrays) for
    the two captures either side of the ply; the first is used for the wide
    crop — burst selection already happened upstream.
    ``rect_before``/``rect_after``: the two captures already rectified to a
    top-down board (``engine.rectify.rectify``).
    ``candidates``: a list of ``Option`` (or equivalent dicts) — what the model
    is allowed to answer, besides "none of these".
    ``calib``: the game's ``Calibration``, used for the raw-photo crop.

    Returns ``{"san": <label or "none_of_these">, "confidence": float, "model":
    <model actually used>, "cost": float}``. Escalates once, from
    ``claude-sonnet-5`` to ``claude-opus-5``, when the first answer is
    "none_of_these" or its confidence is below 0.7.
    """
    opts = [_as_option(o) for o in candidates]
    if not opts:
        raise ValueError("resolve_ply needs at least one candidate")
    labels = [o.label for o in opts]
    if len(set(labels)) != len(labels):
        raise ValueError(f"candidate labels must be unique, got {labels}")

    squares = set()
    for o in opts:
        squares |= _squares_touched(o.moves)

    raw_before = _raw_crop(before_paths[0], calib)
    raw_after = _raw_crop(after_paths[0], calib)
    rect_before_o = _outline_squares(rect_before, squares)
    rect_after_o = _outline_squares(rect_after, squares)

    prompt = (
        f"Two photographs of the same chess board, taken moments apart. It is "
        f"{side_to_move}'s turn to move. Between the two photos, one of these "
        f"candidates happened, or none of them did:\n"
        + "\n".join(f"- {l}" for l in labels)
        + "\n\nImages, in order: the raw camera view before, the raw camera view "
          "after, then the same two moments rectified to a top-down view with "
          "every candidate's touched squares outlined in red. Call choose_move "
          "with your answer and how confident you are."
    )
    content = [
        {"type": "text", "text": "BEFORE (raw):"}, _image_block(raw_before),
        {"type": "text", "text": "AFTER (raw):"}, _image_block(raw_after),
        {"type": "text", "text": "BEFORE (rectified):"}, _image_block(rect_before_o),
        {"type": "text", "text": "AFTER (rectified):"}, _image_block(rect_after_o),
        {"type": "text", "text": prompt},
    ]
    tool = _tool_schema(labels)

    result = _call(model, content, tool, client=client)
    if model != ESCALATION_MODEL and (
        result["san"] == NONE_OF_THESE or result["confidence"] < CONFIDENCE_ESCALATE_BELOW
    ):
        escalated = _call(ESCALATION_MODEL, content, tool, client=client)
        escalated["cost"] += result["cost"]
        result = escalated
    return result


# --------------------------------------------------------------------------
# Caching + per-game call budget
# --------------------------------------------------------------------------

class LLMResolver:
    """Caches and budgets vision-LLM calls for one game.

    Persists to ``<game_dir>/llm_cache.json`` keyed by a caller-chosen string
    (the tracker uses the frame's capture ``seq``), so re-running a correction
    over frames already resolved costs nothing. A hard cap of ``max_calls``
    *new* calls per game means a bad game degrades to more flagged, unresolved
    plies rather than an unbounded bill.
    """

    def __init__(self, game_dir, max_calls: int = MAX_CALLS_PER_GAME):
        self.game_dir = Path(game_dir)
        self.path = self.game_dir / "llm_cache.json"
        self.max_calls = max_calls
        self.calls_made = 0
        self.total_cost = 0.0
        self._cache: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def resolve(self, key: str, **kwargs) -> dict | None:
        """Return a cached answer, make one new call, or ``None`` past budget."""
        key = str(key)
        if key in self._cache:
            return self._cache[key]
        if self.calls_made >= self.max_calls:
            return None
        result = resolve_ply(**kwargs)
        self.calls_made += 1
        self.total_cost += result.get("cost", 0.0)
        self._cache[key] = result
        self._save()
        return result

    def _save(self) -> None:
        try:
            self.game_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._cache, indent=1))
        except OSError:
            pass
