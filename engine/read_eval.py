"""Score a reader against labelled photos.

    python -m engine.read_eval data/labels/green-mat --split pilot --model claude-fable-5-2
    python -m engine.read_eval data/labels/green-mat --split holdout --variant photo

A label set is ``<dir>/photos/<name>.jpg`` plus ``<dir>/truth.json``:
``{name: {"fen": placement | null, "rows": [8 x 8 photo frame] | null, "home":
side | null, "split": ..., "session": date, "done": bool, "excluded": reason |
null}}``. Truth is either a FEN placement (board frame, hold-out labels) or
photo-frame rows plus White's home side (the pilot). Photos that are not done
or are excluded are skipped and counted.

Scoring is in board coordinates. ``per-square``, ``occupied-square`` and
``perfect-board`` take the read at whichever of the four orientations fits the
truth best — reading accuracy, independent of the orientation call.
``home-side`` is the share where the reader's own call gives that best
orientation, and ``fen-correct`` the share where the FEN it implies is exactly
right.

Every paid read is cached under ``<dir>/reads/<model>/<variant>/<name>.json``,
so re-scoring costs nothing; ``--fresh`` re-reads. A read that failed counts as
a board with every square wrong — a reader that errors is not more accurate for
it — and is listed.

Variants: ``photo`` sends the whole photo (no grid finder needed);
``crop-grid`` is the pilot's crop plus drawn grid, read from
``<dir>/pilot/crop/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chess

from .position import HOME_SIDES, guess_home, to_board_map
from .reader import DEFAULT_MODEL, ReadError, read_position

VARIANTS = ("photo", "crop-grid")
# Failures that are the model's own answer and so belong in the cache and the score.
MODEL_ERRORS = {"refusal", "max_tokens", "no_board", "bad_shape", "bad_symbol"}


def load_truth(label_dir: Path, split: str | None) -> tuple[dict, dict]:
    truth = json.loads((label_dir / "truth.json").read_text())
    use, skipped = {}, Counter()
    for name, e in sorted(truth.items()):
        if split and split != "all" and e.get("split") != split:
            continue
        if e.get("excluded"):
            skipped["excluded"] += 1
        elif not e.get("done") or not (e.get("fen") or (e.get("rows") and e.get("home"))):
            skipped["unlabelled"] += 1
        else:
            use[name] = e
    return use, dict(skipped)


def _inputs(label_dir: Path, name: str, variant: str):
    if variant == "photo":
        return label_dir / "photos" / f"{name}.jpg", None
    crop = label_dir / "pilot" / "crop"
    return crop / f"{name}.jpg", crop / f"{name}_grid.jpg"


def read_one(label_dir: Path, name: str, model: str, variant: str, fresh: bool,
             effort: str | None) -> dict:
    tag = variant if not effort else f"{variant}-{effort}"
    cache = label_dir / "reads" / model / tag / f"{name}.json"
    if cache.exists() and not fresh:
        return json.loads(cache.read_text())
    image, grid = _inputs(label_dir, name, variant)
    try:
        out = read_position(image, model=model, grid_image=grid, effort=effort).to_json()
    except ReadError as exc:
        out = {"error": exc.reason, "detail": exc.detail}
        if exc.reason not in MODEL_ERRORS:
            return out              # auth, network, rate limit: not the reader's answer
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=1))
    return out


def truth_placement(e: dict) -> dict[int, str]:
    """Truth in board coordinates: a FEN placement, or photo-frame rows plus home side."""
    if e.get("fen"):
        board = chess.Board(None)
        board.set_board_fen(e["fen"].split()[0])
        return {sq: p.symbol() for sq, p in board.piece_map().items()}
    return {sq: p.symbol() for sq, p in to_board_map(e["rows"], e["home"]).items()}


def _agree(read: dict[int, str], want: dict[int, str]) -> int:
    return sum(read.get(sq, ".") == want.get(sq, ".") for sq in chess.SQUARES)


def score(truth: dict, reads: dict) -> dict:
    """Reading accuracy is scored at the orientation that fits the truth best, so a
    diagonal photo with no well-defined "far row" is scored fairly; whether the
    reader's own home-side call picks that orientation is scored separately."""
    sq_ok = sq_n = occ_ok = occ_n = perfect = home_ok = fen_ok = 0
    confusions: Counter = Counter()
    errors, cost, secs = {}, [], []
    for name, e in truth.items():
        want = truth_placement(e)
        got = reads.get(name) or {"error": "missing"}
        sq_n += 64
        occ_n += len(want)
        if "error" in got:
            errors[name] = got["error"]
            continue
        cost.append(got.get("cost", 0.0))
        secs.append(got.get("secs", 0.0))
        maps = {h: {sq: p.symbol() for sq, p in to_board_map(got["rows"], h).items()}
                for h in HOME_SIDES}
        fit = {h: _agree(m, want) for h, m in maps.items()}
        best = max(HOME_SIDES, key=lambda h: fit[h])
        called = got.get("home") if got.get("home") in HOME_SIDES else guess_home(got["rows"])
        read = maps[best]
        sq_ok += fit[best]
        occ_ok += sum(read.get(sq) == sym for sq, sym in want.items())
        perfect += fit[best] == 64
        home_ok += fit[called] == fit[best]
        fen_ok += fit[called] == 64
        for sq in chess.SQUARES:
            t, g = want.get(sq, "."), read.get(sq, ".")
            if t != g:
                confusions[f"{t}->{g}"] += 1
    n = len(truth)
    return {
        "photos": n,
        "per-square": sq_ok / sq_n if sq_n else 0.0,
        "occupied-square": occ_ok / occ_n if occ_n else 0.0,
        "perfect-board": perfect / n if n else 0.0,
        "perfect-boards": perfect,
        "home-side": home_ok / n if n else 0.0,
        "fen-correct": fen_ok / n if n else 0.0,
        "cost-per-photo": sum(cost) / len(cost) if cost else 0.0,
        "secs-per-photo": sum(secs) / len(secs) if secs else 0.0,
        "errors": errors,
        "confusions": dict(confusions.most_common(12)),
    }


def report(res: dict, skipped: dict, model: str, variant: str, split: str) -> str:
    lines = [f"model {model}  variant {variant}  split {split}  photos {res['photos']}"
             + (f"  skipped {skipped}" if skipped else ""),
             f"per-square       {res['per-square']:.3f}",
             f"occupied-square  {res['occupied-square']:.3f}",
             f"perfect-board    {res['perfect-board']:.3f}  ({res['perfect-boards']}/{res['photos']})",
             f"home-side        {res['home-side']:.3f}",
             f"fen-correct      {res['fen-correct']:.3f}",
             f"cost/photo       ${res['cost-per-photo']:.4f}",
             f"secs/photo       {res['secs-per-photo']:.1f}"]
    if res["errors"]:
        lines.append(f"errors           {res['errors']}")
    if res["confusions"]:
        lines.append("confusions       " + ", ".join(f"{k} x{v}" for k, v in res["confusions"].items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("label_dir", type=Path)
    ap.add_argument("--split", default="all")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--variant", choices=VARIANTS, default="photo")
    ap.add_argument("--effort", default=None, help="output_config.effort; default: the model's")
    ap.add_argument("--fresh", action="store_true", help="ignore cached reads")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    truth, skipped = load_truth(a.label_dir, a.split)
    if not truth:
        print(f"no labelled photos in split {a.split!r} (skipped {skipped})", file=sys.stderr)
        return 2
    with ThreadPoolExecutor(a.workers) as ex:
        reads = dict(zip(truth, ex.map(
            lambda n: read_one(a.label_dir, n, a.model, a.variant, a.fresh, a.effort), truth)))
    res = score(truth, reads)
    print(json.dumps(res, indent=1) if a.json else report(res, skipped, a.model, a.variant, a.split))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
