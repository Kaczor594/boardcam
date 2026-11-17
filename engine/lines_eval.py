"""Score the line solver on real games cut at known plies.

A *case* is a real game from the chess-insights database, a start ply ``s``
and a gap ``g``: A is the position after ``s`` plies, B the placement after
``s + g``, and the solver is asked for lines of ``g`` plies. The true moves are
known, so each case reports: a path found, the true line is the top line, the
true line is in the top three, and the share of plies where the true move is
the top marginal.

Splits (all chess.com games of 10 minutes or more involving the database
owner, so that ratings are on the chess.com scale the app uses):

* ``after-cutoff`` — games on or after the cut-off. Nothing is tuned on them.
  The owner's opening prior is built from games before the cut-off.
* ``before-cutoff`` — games in ``[prior_split, cutoff)``; the prior is built
  from games before ``prior_split``. ``--tune`` and ``--hint-threshold`` fit
  on this split only.

Results are cached per case under ``data/lines_eval/`` (re-running a table is
free); the cache key includes the solver settings.

    python -m engine.lines_eval --gaps 4,8 --games 200 --split after-cutoff
    python -m engine.lines_eval --check-leak
    python -m engine.lines_eval --tune
    python -m engine.lines_eval --hint-threshold
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import chess

from engine import humanlike as hl
from engine.lines import SOLVER_CONFIG, load_solver_config, solve

OUT = hl.ROOT / "data" / "lines_eval"
_W: dict = {}       # per-worker state


# ------------------------------------------------------------------- cases

def split_games(split: str, games: list[hl.Game] | None = None) -> tuple[list[hl.Game], dict]:
    cfg = hl.load_config()
    games = games if games is not None else hl.load_games()
    owner = hl._owner(games)
    cut, ps = cfg["cutoff"], cfg["prior_split"]
    if split == "after-cutoff":
        lo, hi, prior_before = cut, "9999", cut
    elif split == "before-cutoff":
        lo, hi, prior_before = ps, cut, ps
    else:
        raise ValueError(split)
    sel = [g for g in games if g.platform == "chesscom" and (g.base_s or 0) >= 600
           and lo <= g.date < hi and (owner & {g.white, g.black})
           and g.white_rating and g.black_rating]
    return sel, {"owner": sorted(owner), "prior_before": prior_before, "lo": lo, "hi": hi}


def make_cases(split: str, gaps: list[int], n_games: int, seed: int = 0) -> list[dict]:
    games, meta = split_games(split)
    rng = random.Random(seed)
    order = list(range(len(games)))
    rng.shuffle(order)
    cases = []
    for gap in gaps:
        taken = 0
        for i in order:
            g = games[i]
            if len(g.moves) < gap + 4:
                continue
            r = random.Random(f"{seed}-{g.id}-{gap}")
            s = r.randint(0, len(g.moves) - gap)
            cases.append({"game": g.id, "date": g.date, "gap": gap, "s": s,
                          "white": g.white, "black": g.black,
                          "white_rating": g.white_rating, "black_rating": g.black_rating,
                          "moves": g.moves[s:s + gap], "pre": g.moves[:s],
                          "prior_before": meta["prior_before"], "owner": meta["owner"]})
            taken += 1
            if taken >= n_games:
                break
    return cases


# ------------------------------------------------------------------ worker

def _init_worker(lc0_workers: int = 3):
    _W["maia"] = hl.Maia(workers=lc0_workers)
    _W["games"] = hl.load_games()
    _W["priors"] = {}


def _prior(owner: tuple, before: str) -> hl.OpeningPrior:
    key = (owner, before)
    if key not in _W["priors"]:
        _W["priors"][key] = hl.OpeningPrior.build(_W["games"], set(owner), before=before)
    return _W["priors"][key]


def run_case(case: dict, solver: dict) -> dict:
    prior = _prior(tuple(case["owner"]), case["prior_before"])
    owner = set(case["owner"])
    sides = []
    for color in ("white", "black"):
        sides.append(hl.Side(hl.band_for(case[f"{color}_rating"]),
                             prior if case[color] in owner else None))
    mover = hl.Mover(sides[0], sides[1], maia=_W["maia"])
    a = chess.Board()
    for u in case["pre"]:
        a.push_uci(u)
    b = a.copy()
    for u in case["moves"]:
        b.push_uci(u)
    t0 = time.time()
    res = solve(a.fen(), b.board_fen(), case["gap"], mover.policy, config=solver,
                prefetch=mover.prefetch)
    secs = time.time() - t0
    true = case["moves"]
    lines = [ln.moves for ln in res.lines]
    marg = 0.0
    if res.n_used == len(true) and res.marginals:
        marg = sum(ply[0][0] == t for ply, t in zip(res.marginals, true)) / len(true)
    return {"game": case["game"], "gap": case["gap"], "s": case["s"], "date": case["date"],
            "band_w": sides[0].band, "band_b": sides[1].band,
            "status": res.status, "complete": res.complete, "n_used": res.n_used,
            "found": bool(lines) and res.n_used == case["gap"], "any_found": bool(lines),
            "top1": bool(lines) and lines[0] == true,
            "top3": true in lines[:3], "rank": lines.index(true) + 1 if true in lines else None,
            "marg_top": marg, "secs": round(secs, 2), "expansions": res.expansions,
            "true_logp": _true_logp(a, true, mover.policy)}


def _true_logp(a: chess.Board, true: list[str], policy) -> float:
    b = a.copy(stack=False)
    lp = 0.0
    for u in true:
        lp += math.log(policy(b).get(u, hl.P_FLOOR))
        b.push_uci(u)
    return round(lp, 4)


def _run(args):
    case, solver = args
    return run_case(case, solver)


def evaluate(split: str, gaps: list[int], n_games: int, solver: dict, workers: int = 3,
             seed: int = 0, quiet: bool = False, lc0_workers: int = 3) -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    import hashlib
    keys = ("c", "lam", "top", "time_box", "width", "astar_budget", "main_share",
            "astar_max_gap", "beam_min_p", "beam_div", "beam_v")
    tag = hashlib.sha1(json.dumps({k: solver.get(k) for k in keys}, sort_keys=True)
                       .encode()).hexdigest()[:10]
    cache_file = OUT / f"{split}-{tag}.jsonl"
    done = {}
    if cache_file.exists():
        for line in cache_file.read_text().splitlines():
            r = json.loads(line)
            done[(r["game"], r["gap"], r["s"])] = r
    cases = make_cases(split, gaps, n_games, seed)
    todo = [c for c in cases if (c["game"], c["gap"], c["s"]) not in done]
    if todo:
        if not quiet:
            print(f"solving {len(todo)} cases ({len(cases) - len(todo)} cached), {workers} workers",
                  flush=True)
        with ProcessPoolExecutor(workers, initializer=_init_worker,
                                 initargs=(lc0_workers,)) as ex, \
                cache_file.open("a") as f:
            for r in ex.map(_run, [(c, solver) for c in todo]):
                done[(r["game"], r["gap"], r["s"])] = r
                f.write(json.dumps(r) + "\n")
                f.flush()
    return [done[(c["game"], c["gap"], c["s"])] for c in cases]


def table(results: list[dict]) -> list[dict]:
    rows = []
    for gap in sorted({r["gap"] for r in results}):
        rs = [r for r in results if r["gap"] == gap]
        secs = sorted(r["secs"] for r in rs)
        rows.append({
            "gap": gap, "cases": len(rs),
            "path-found": sum(r["found"] for r in rs) / len(rs),
            "true-line-top1": sum(r["top1"] for r in rs) / len(rs),
            "true-line-top3": sum(r["top3"] for r in rs) / len(rs),
            "marg-top": statistics.mean(r["marg_top"] for r in rs),
            "median-s": statistics.median(secs),
            "p90-s": secs[int(0.9 * (len(secs) - 1))],
            "timeouts": sum(r["status"] == "timeout" for r in rs),
            "exact-n": sum(r["n_used"] == gap for r in rs) / len(rs),
        })
    return rows


def print_table(rows: list[dict]) -> None:
    cols = ["gap", "cases", "path-found", "true-line-top1", "true-line-top3", "marg-top",
            "median-s", "p90-s", "timeouts", "exact-n"]
    print("  ".join(f"{c:>14s}" for c in cols))
    for r in rows:
        print("  ".join(f"{r[c]:>14.3f}" if isinstance(r[c], float) else f"{r[c]:>14}"
                        for c in cols))
    for r in rows:
        print(f"gap {r['gap']}: path-found {r['path-found']:.3f}  "
              f"true-line-top1 {r['true-line-top1']:.3f}  true-line-top3 {r['true-line-top3']:.3f}  "
              f"median {r['median-s']:.1f} s")


# ------------------------------------------------------------- leak, tune, hint

def check_leak() -> int:
    cfg = hl.load_config()
    games = hl.load_games()
    after, meta = split_games("after-cutoff", games)
    before, meta_b = split_games("before-cutoff", games)
    n_bad = sum(g.date < cfg["cutoff"] for g in after)
    prior_bad = meta["prior_before"] > cfg["cutoff"] or meta_b["prior_before"] > cfg["cutoff"]
    overlap = {g.id for g in after} & {g.id for g in before}
    print(f"cut-off {cfg['cutoff']}: {len(after)} evaluation games "
          f"({min(g.date for g in after)[:10]} … {max(g.date for g in after)[:10]}); "
          f"fit games {len(before)} ({min(g.date for g in before)[:10]} … "
          f"{max(g.date for g in before)[:10]})")
    print(f"opening prior for evaluation built from games before {meta['prior_before']}; "
          f"band offset and k fitted on games before {cfg['cutoff']}")
    print(f"{n_bad} evaluation games before cut-off")
    if prior_bad or overlap:
        print(f"LEAK: prior_after_cutoff={prior_bad} overlap={len(overlap)}")
        return 1
    return 1 if n_bad else 0


TUNE_GAPS = [8, 16, 24]


def tune(workers: int, n_games: int = 20) -> dict:
    """Pick the beam's ``lam`` and ``width`` on the before-cutoff split.

    Coordinate search: ``lam`` in {1, 2, 3} at width 3000, then width in
    {1500, 3000, 5000} at the best ``lam``. Score: mean path-found over the
    gaps, then mean top-3 (path-found is what the gate asks; top-3 breaks ties).
    """
    base = {**load_solver_config(), "main_share": 0.85}
    log = []

    def score(s):
        rows = table(evaluate("before-cutoff", TUNE_GAPS, n_games, s, workers, seed=1,
                              quiet=True))
        found = statistics.mean(r["path-found"] for r in rows)
        top3 = statistics.mean(r["true-line-top3"] for r in rows)
        line = {"lam": s["lam"], "width": s["width"], "found": round(found, 3),
                "top3": round(top3, 3),
                "by_gap": {r["gap"]: {"found": round(r["path-found"], 3),
                                      "top3": round(r["true-line-top3"], 3),
                                      "median_s": round(r["median-s"], 1)} for r in rows}}
        log.append(line)
        print(json.dumps(line), flush=True)
        return (found, top3)

    best_lam = max((1.0, 2.0, 3.0), key=lambda l: score({**base, "lam": l, "width": 3000}))
    best_w = max((1500, 3000, 5000),
                 key=lambda w: score({**base, "lam": best_lam, "width": w}))
    cfg = {**base, "lam": best_lam, "width": best_w,
           "tune": {"split": "before-cutoff", "gaps": TUNE_GAPS, "cases_per_gap": n_games,
                    "grid": log}}
    SOLVER_CONFIG.write_text(json.dumps(cfg, indent=1) + "\n")
    print(f"chose lam={best_lam} width={best_w}")
    return cfg


def _logistic(xs: list[list[float]], ys: list[int], iters: int = 50) -> list[float]:
    import numpy as np
    X = np.array([[1.0, *x] for x in xs])
    y = np.array(ys, dtype=float)
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (y - p) - 1e-3 * w
        H = -(X.T * (p * (1 - p))) @ X - 1e-3 * np.eye(len(w))
        w = w - np.linalg.solve(H, g)
    return w.tolist()


HINT_GAPS = [4, 8, 12, 16, 24]


def hint_threshold(workers: int, n_games: int = 60) -> dict:
    """Plies since the last photo beyond which the true line is outside the top
    three more often than not. Fitted on before-cutoff, reported on after-cutoff.

    A case counts as dependable when the true line is in the top three; a timeout
    is not dependable. Logistic in gap with the game's band (mean of the two
    players' ratings) as a covariate; threshold = gap where P = 0.5, per band,
    clamped to [6, 60]."""
    solver = load_solver_config()
    fit_res = evaluate("before-cutoff", HINT_GAPS, n_games, solver, workers, seed=2, quiet=True)
    xs, ys = [], []
    for r in fit_res:
        band = (r["band_w"] + r["band_b"]) / 2
        xs.append([r["gap"], (band - 1500) / 100])
        ys.append(int(r["top3"]))
    w0, wg, wb = _logistic(xs, ys)
    out = {}
    for band in hl.BANDS:
        z = (band - 1500) / 100
        g = -(w0 + wb * z) / wg if wg < 0 else 60
        out[str(band)] = int(max(6, min(60, round(g))))
    seen = sorted({(r["band_w"] + r["band_b"]) // 2 // 100 * 100 for r in fit_res})
    cfg = {**solver, "hint_plies": out,
           "hint_fit": {"coef": [round(v, 4) for v in (w0, wg, wb)], "cases": len(fit_res),
                        "gaps": HINT_GAPS, "bands_seen": seen,
                        "rule": "band of the mean of the two chess.com ratings"}}
    SOLVER_CONFIG.write_text(json.dumps(cfg, indent=1) + "\n")
    rows = table(fit_res)
    print("fit (before-cutoff):")
    for r in rows:
        print(f"  gap {r['gap']:>2}: top3 {r['true-line-top3']:.2f}  found {r['path-found']:.2f}"
              f"  timeouts {r['timeouts']}")
    # the report reuses the gate's after-cutoff cases (seed 0, 200 games)
    rep = evaluate("after-cutoff", HINT_GAPS, 200, solver, workers, seed=0, quiet=True)
    print("report (after-cutoff):")
    for r in table(rep):
        print(f"  gap {r['gap']:>2}: top3 {r['true-line-top3']:.2f}  found {r['path-found']:.2f}"
              f"  timeouts {r['timeouts']}")
    print(f"bands seen in the fit: {seen}; coefficients {cfg['hint_fit']['coef']}")
    for band, n in out.items():
        print(f"band {band}: hint after {n} plies")
    return cfg


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gaps", default="4,8")
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--split", default="after-cutoff", choices=["after-cutoff", "before-cutoff"])
    ap.add_argument("--workers", type=int, default=3, help="cases solved at once")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--check-leak", action="store_true")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--hint-threshold", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.check_leak:
        raise SystemExit(check_leak())
    if a.tune:
        tune(a.workers)
        return
    if a.hint_threshold:
        hint_threshold(a.workers)
        return
    gaps = [int(x) for x in a.gaps.split(",")]
    res = evaluate(a.split, gaps, a.games, load_solver_config(), a.workers, a.seed)
    rows = table(res)
    if a.json:
        print(json.dumps(rows))
    print_table(rows)


if __name__ == "__main__":
    main()
