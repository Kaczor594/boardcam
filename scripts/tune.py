#!/usr/bin/env python
"""Refit the engine's scoring parameters against every corpus at once.

Coordinate search: one parameter at a time, each over a small ladder of values,
keeping a change only when it improves the objective and regresses no corpus.
That last clause is the point of tuning against all of them together — a value
that buys two plies on ``shallow`` by losing five on ``clean`` is not an
improvement, and a coordinate search on a single corpus will happily take it.

Real games count for more than synthetic ones, because the synthetic corpus is
an approximation of Isaac's board and the real one is his board. Corrections
made on the review page are what produce ``data/real/<id>/truth.pgn``, so every
wrong ply he fixes ends up here.

    python scripts/tune.py --dry-run          # report, write nothing
    python scripts/tune.py --out engine/params.json

Manual only. There is no launchd job and there should not be one: a refit that
nobody reads can quietly move the engine, and the numbers below are the whole
point of running it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.evaluate import evaluate, game_dirs        # noqa: E402
from engine.features import PARAMS_PATH, load_params   # noqa: E402

# Each parameter and the values worth trying for it. Deliberately short: a
# coordinate search re-evaluates every corpus per value, and the corpora take
# minutes.
LADDERS: dict[str, list[float]] = {
    "theta": [18, 25, 32],
    "q_hit": [0.35, 0.5, 0.65],
    "nu": [0.08, 0.17, 0.35],
    "q0_max": [0.15, 0.25, 0.4],
    "cap_area": [2500, 4000, 8000],
    "silhouette_slack": [0.02, 0.03, 0.05],
    "vis_min": [0.25, 0.35, 0.5],
    "prior_null": [0.001, 0.004, 0.02],
    "promo_queen": [0.7, 0.85, 0.95],
    "score_clip": [8.0, 20.0, 60.0],
    "tau": [5.0, 10.0, 20.0],
    "occ_weight": [0.0, 0.15],
}

# Parameters that change what `features.extract` writes rather than how a
# candidate is scored. Changing one costs a full re-extraction of every frame.
EXPENSIVE = {"theta", "cap_area"}

REAL_WEIGHT = 3.0

GAMES_ROOT = ROOT / "data/games"
REAL_ROOT = ROOT / "data/real"


# --------------------------------------------------------------------------
# Corrected games become training truth
# --------------------------------------------------------------------------

def export_labelled(games_root: Path = GAMES_ROOT,
                    real_root: Path = REAL_ROOT) -> list[str]:
    """Copy every reviewed game into ``data/real/<id>/`` with a ``truth.pgn``.

    A game qualifies once a human has been through it — either they corrected a
    ply or they pressed "confirm" on the review page. Both mean the move list has
    been read by someone, which is the only thing that makes it truth.

    The frames are symlinked rather than copied: a game is a few hundred
    megabytes of photographs and there is no reason to hold two of them.
    """
    from engine.pgn import pgn_from_sans

    exported: list[str] = []
    if not games_root.is_dir():
        return exported
    for gdir in sorted(games_root.iterdir()):
        labels_path = gdir / "labels.json"
        if not gdir.is_dir() or not labels_path.exists():
            continue
        try:
            labels = json.loads(labels_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        moves = labels.get("moves") or []
        if not moves or not (labels.get("verified") or labels.get("corrections")):
            continue

        meta = {}
        try:
            meta = json.loads((gdir / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        out = real_root / gdir.name
        out.mkdir(parents=True, exist_ok=True)
        try:
            pgn = pgn_from_sans(moves, {
                "White": meta.get("white_name", "White"),
                "Black": meta.get("black_name", "Black"),
                "Result": labels.get("result") or meta.get("result") or "*",
            })
        except ValueError as exc:                  # a label list that is not a game
            print(f"  ! {gdir.name}: {exc}")
            continue
        (out / "truth.pgn").write_text(pgn + "\n")
        for name in ("events.jsonl", "meta.json", "calibration.json"):
            if (gdir / name).exists():
                shutil.copy2(gdir / name, out / name)
        link = out / "frames"
        if not link.exists():
            link.symlink_to(gdir / "frames", target_is_directory=True)
        exported.append(gdir.name)
    return exported


def corpora(roots: list[Path]) -> list[Path]:
    out = []
    for root in roots:
        if not root.exists():
            continue
        if game_dirs(root):
            out.append(root)
        else:
            out.extend(d for d in sorted(root.iterdir())
                       if d.is_dir() and game_dirs(d))
    return out


def measure(params: dict, roots: list[Path], workers: int, limit: int | None) -> dict:
    """Plies-correct per corpus, under one parameter set."""
    import tempfile
    import os
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(params, fh)
    fh.close()
    try:
        return {str(root): evaluate(root, fh.name, False, workers, limit)[0]
                for root in roots}
    finally:
        os.unlink(fh.name)


def objective(scores: dict[str, dict]) -> float:
    """Weighted mean of plies-correct, with real games counting for more."""
    total = weight = 0.0
    for name, s in scores.items():
        w = REAL_WEIGHT if "real" in name else 1.0
        total += w * s["plies_correct"]
        weight += w
    return total / max(weight, 1e-9)


def regressed(before: dict, after: dict, tol: float) -> str | None:
    for name, s in after.items():
        if s["plies_correct"] < before[name]["plies_correct"] - tol:
            return name
    return None


def table(title: str, scores: dict[str, dict]) -> None:
    print(f"\n{title}")
    print(f"  {'corpus':34s} {'plies':>8s} {'final':>8s} {'recall':>8s} {'flag/game':>10s}")
    for name, s in scores.items():
        print(f"  {name:34s} {s['plies_correct']:7.2f}% {s['final_correct']:7.1f}% "
              f"{s['wrong_recall']:7.1f}% {s['flagged_per_game']:10.2f}")
    print(f"  objective {objective(scores):.3f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roots", nargs="*", type=Path,
                    default=[ROOT / "data/synth", ROOT / "data/real"])
    ap.add_argument("--out", type=Path, default=PARAMS_PATH)
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--only", nargs="*", default=None, help="tune just these params")
    ap.add_argument("--skip-expensive", action="store_true",
                    help="leave the parameters that force a re-extraction alone")
    ap.add_argument("--limit", type=int, default=None, help="games per corpus")
    ap.add_argument("--workers", type=int, default=9)
    ap.add_argument("--tol", type=float, default=0.05,
                    help="plies-correct a corpus may lose and still count as no regression")
    ap.add_argument("--no-export", action="store_true",
                    help="do not promote reviewed games into data/real first")
    a = ap.parse_args(argv)

    if not a.no_export:
        exported = export_labelled()
        print(f"exported {len(exported)} reviewed game(s) to {REAL_ROOT}"
              + (": " + ", ".join(exported) if exported else ""))

    roots = corpora(a.roots)
    if not roots:
        raise SystemExit(f"no corpora with truth.pgn under {a.roots}")

    params = load_params()
    base = measure(params, roots, a.workers, a.limit)
    table("before", base)
    best_scores, best_obj = base, objective(base)

    names = a.only or list(LADDERS)
    if a.skip_expensive:
        names = [n for n in names if n not in EXPENSIVE]

    changes: list[tuple[str, float, float, float]] = []
    for name in names:
        ladder = LADDERS.get(name)
        if not ladder:
            print(f"  (no ladder for {name}, skipped)")
            continue
        current = params.get(name)
        for value in ladder:
            if value == current:
                continue
            trial = dict(params)
            trial[name] = value
            scores = measure(trial, roots, a.workers, a.limit)
            obj = objective(scores)
            bad = regressed(best_scores, scores, a.tol)
            mark = "regresses " + bad if bad else f"{obj - best_obj:+.3f}"
            print(f"  {name} {current} -> {value}: objective {obj:.3f}  ({mark})")
            if bad is None and obj > best_obj + 1e-9:
                changes.append((name, current, value, obj - best_obj))
                params, best_scores, best_obj = trial, scores, obj
                current = value

    table("after", best_scores)
    if changes:
        print("\nchanged:")
        for name, old, new, gain in changes:
            print(f"  {name}: {old} -> {new}  (+{gain:.3f})")
    else:
        print("\nno parameter improved every corpus at once; nothing to change")

    if a.dry_run:
        print("\n--dry-run: params.json not written")
        return 0
    if not changes:
        return 0
    existing = json.loads(Path(a.out).read_text()) if Path(a.out).exists() else {}
    existing.update(params)
    Path(a.out).write_text(json.dumps(existing, indent=2) + "\n")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
