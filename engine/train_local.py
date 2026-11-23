"""Local reader: training data inventory now; the classifier arrives in Phase 6.

    python -m engine.train_local --inventory [data/labels/green-mat ...]

The inventory counts, per label set, the photos that can train a per-square
classifier: labelled (``done``, not excluded), with corner truth (needed to cut
square crops), by session (the learning curve trains on early sessions and
tests on later ones) and by class (square contents, empty included).
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import chess

from . import labels as L

CLASSES = ".KQRBNPkqrbnp"


def inventory(d: Path) -> dict:
    truth = L.load(d)
    labelled = {n: e for n, e in truth.items()
                if e.get("done") and not e.get("excluded") and L.placement(e)}
    classes: Counter = Counter()
    for e in labelled.values():
        board = chess.Board(None)
        board.set_board_fen(L.placement(e))
        pm = board.piece_map()
        classes["."] += 64 - len(pm)
        classes.update(p.symbol() for p in pm.values())
    return {
        "set": d.name,
        "photos": len(truth),
        "labelled": len(labelled),
        "with-corners": sum(bool(e.get("corners")) for e in labelled.values()),
        "excluded": sum(bool(e.get("excluded")) for e in truth.values()),
        "unchecked-game": sum(e.get("source", "").startswith("game:") and not e.get("checked")
                              for e in labelled.values()),
        "sessions": dict(sorted(Counter(e.get("session") for e in labelled.values()).items())),
        "splits": dict(sorted(Counter(e.get("split") for e in labelled.values()).items())),
        "classes": {c: classes.get(c, 0) for c in CLASSES},
    }


def report(inv: dict) -> str:
    lines = [f"set {inv['set']}: photos {inv['photos']}, labelled {inv['labelled']}, "
             f"with corners {inv['with-corners']}, excluded {inv['excluded']}, "
             f"unchecked game photos {inv['unchecked-game']}",
             f"  sessions {len(inv['sessions'])}: " +
             ", ".join(f"{s} {n}" for s, n in inv["sessions"].items()),
             "  splits: " + ", ".join(f"{s} {n}" for s, n in inv["splits"].items()),
             "  classes: " + "  ".join(f"{c} {n}" for c, n in inv["classes"].items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("sets", nargs="*", type=Path)
    a = ap.parse_args(argv)
    if not a.inventory:
        ap.print_help()
        return 2
    dirs = a.sets or [L.set_dir(s) for s in L.list_sets()]
    for d in dirs:
        print(report(inventory(d)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
