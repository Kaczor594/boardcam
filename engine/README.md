# BoardCam engine

Photos in, positions and likely moves out. This file is the contract between
phases: what a later phase needs from an earlier one is written here, so a new
session can pick up without re-reading the code that produced it.

Companion documents: `docs/PROTOCOL.md` (wire and storage contract; §9 is the
photo reader and `engine.position`) and
`planning/specs/2026-11-16-photo-positions.md` (the plan).

| module | does |
|---|---|
| `reader.py`, `position.py`, `read_eval.py` | photo → 8×8 read → FEN; reader accuracy (PROTOCOL §9) |
| `humanlike.py` | move probabilities for a player of a given strength (Maia via lc0, opening prior) |
| `lines.py` | likely lines of exactly `n` plies between two positions |
| `lines_eval.py` | scores the solver on real games cut at known plies; tunes it; fits the photo hint |
| `evalgraph.py` | Stockfish evaluation per ply and top moves (review page) |
| `pgn.py` | PGN export |
| `calibrate.py`, `rectify.py` | September grid finder, kept until Phase 5 decides what to reuse |

---

## Lines (photo-positions Phase 3)

### The question

Two confirmed photo positions A and B, and the clock's ply count `n` between
them. Which `n`-ply move sequences take A to B, and how likely is each? Every
legal path of exactly `n` plies from A that ends on B's placement is a
candidate; its weight is the product of the per-move probabilities that the
two players (at their ratings) would play those moves. Normalising over the
candidates is Bayes' rule with the endpoint as the evidence:
`P(line | A, B, n) ∝ Π p(move_i | position_i)`.

### Move probabilities (`engine.humanlike`)

* **Maia-1** (CSSLab v1.0 weights, nine bands 1100–1900 on the Lichess scale)
  through `lc0` at one node. The policy is read from `VerboseMoveStats` at
  `--policy-softmax-temp=1.0` (lc0's default 1.359 flattens it) on the CPU
  BLAS backend (0.6 ms per position vs 2.0 ms on Metal; same policy to
  1e-4, same top move on 200/200 test positions). lc0 prints P to 0.01 %; a
  printed 0.00 % becomes 5e-5 and the policy is renormalised over the legal
  moves. Positions go to lc0 as a FEN without history (`fen_only`), so the
  cache key is the position.
* **Rating → band.** `band = nearest band to (chess.com rating + offset)`.
  The offset is fitted on 9 257 moves sampled from 783 chess.com games of
  10+ minutes before the cut-off, maximising the log-likelihood of the moves
  actually played (`python -m engine.humanlike fit`). **Offset = +250**, so the
  default 1350 plays as Maia-1600. The surface is flat (−1.850 nats/move at
  +250, −1.869 at 0): band choice matters little next to the prior. The
  per-game rating setting is the input; the two players can have different
  bands.
* **Opening prior.** For a player with known online accounts
  (`data/players.json`, untracked: `{"Isaac": ["kaczor594", "simsimlamachine"]}`),
  the moves they actually played from a position (first 40 plies, all their
  games) are blended with Maia as a Dirichlet prior:
  `p(m) = (count(m) + k·maia(m)) / (N + k)`. **k = 5**, fitted on the owner's
  moves in `[2024-07-01, 2025-07-01)` with the prior built from games before
  2024-07-01: −0.988 nats/move with the prior vs −1.577 with Maia alone, on
  3 124 moves from positions the owner had reached before. Unknown players get
  Maia only.
* Fitted values live in `engine/humanlike.json` (offset, k, cut-off, the fit's
  per-offset and per-k log-likelihoods, best band per 100-point bucket).

### Search (`engine.lines`)

```python
from engine.humanlike import Maia, for_game
from engine.lines import solve
mover = for_game(1350, 1350, white_name="Isaac", black_name="Opponent",
                 maia=Maia(workers=4))
res = solve(fen_a, fen_or_placement_b, n, mover.policy, prefetch=mover.prefetch)
res.to_json()        # the lines.json "result" object below
```

1. **Pruning bound** (`Bound.need`, admissible — checked on 600 random games
   in `tests/test_lines.py`). For each side, a lower bound on the moves it still
   needs: per piece type, a least-cost matching of B's squares to distinct
   current pieces (or promotions) at empty-board distance — knight BFS, bishop
   1/2 and never across colours, rook and queen 1/2, king steps, castling as one
   king move that carries the rook; plus a pawn matching (pawns only advance,
   change file only by capturing, one double step from the start rank); plus
   one move per enemy piece that must be captured. A state is dropped when a
   side needs more moves than it has left, when the pawn file changes exceed
   the captures available, when material would have to grow, or when a side
   already exactly on B would have to spend a single move.
2. **Short gaps (n ≤ 10): best-first.** Priority `g + c·plies_left +
   λ·distance` with `g = −Σ log p` and `distance` the bound summed over both
   sides. A position reached by different move orders is evaluated once but
   may be popped up to `top` times (k-shortest paths). Capped at 2 000
   expansions; if it does not finish, the beam takes over with the time left.
3. **Long gaps: depth-synchronous beam.** Every ply keeps the `width`
   positions with the lowest `g + λ·distance` (best path's `g`), each with its
   `top` best paths; transpositions merge. A layer's positions are sent to
   lc0 in parallel (`prefetch`, `Maia(workers=…)`). Half the width
   (`beam_div` 0.5) goes not by score but to the positions that need the
   fewest moves to reach B, so a reachable B is not crowded out by likelier
   positions drifting away from it. Width shrinks if the time left would not
   reach ply `n` (planned on 60 % of the measured rate). Past the deadline the
   remaining plies run on the 30 positions nearest B for up to 3 s more,
   rather than returning nothing. A beam that cut anything reports
   `complete: false`, so a miss is `timeout`, never `none`.
4. **Answer.** The `top` lines found, normalised among themselves; per-ply
   marginals are the probability-weighted share of those lines playing each
   move. If `n` yields nothing, `n − 1` then `n + 1` are tried (a photo taken
   after a move but before the press); `n` gets `main_share` of the time box.
   Castling rights and the en-passant square in `fen_end` come from the path.

Solver constants live in `engine/solver.json` (written by
`python -m engine.lines_eval --tune` and `--hint-threshold`).

### `lines.json` (written per game by Phase 4)

`data/games/<id>/lines.json` holds one entry per gap between consecutive
confirmed positions (the first gap starts at the initial position, ply 0):

```json
{"game_id": "20261117-201500-a1b2",
 "gaps": [
  {"from": {"photo": null, "ply": 0, "fen": "rnbqkbnr/… w KQkq - 0 1"},
   "to":   {"photo": 1, "ply": 14, "fen": "<confirmed FEN of photo 1>"},
   "bands": {"white": 1600, "black": 1600},
   "prior": {"white": true, "black": false},
   "solver": {"lam": 2.0, "width": 1500, "time_box": 30.0},
   "result": {                                   // engine.lines.Result.to_json()
     "fen_a": "…", "placement_b": "…",
     "n_requested": 14,                          // the clock's count
     "n_used": 14,                               // n, n−1 or n+1; null if nothing found
     "status": "found",                          // found | none | timeout
     "complete": true,                           // false: the time box cut the search
     "lines": [                                  // most likely first, at most `top` (5)
       {"moves": ["e2e4", "…"], "san": ["e4", "…"],
        "logp": -17.42,                          // Σ log p under the movers
        "prob": 0.61,                            // normalised among the lines returned
        "fen_end": "… b Kkq - 2 8"}],            // castling / e.p. from this path
     "marginals": [                              // one list per ply, n_used long
       [{"uci": "e2e4", "san": "e4", "p": 0.83}, {"uci": "d2d4", "san": "d4", "p": 0.17}]],
     "expansions": 4210, "secs": 3.1,
     "tried": [{"n": 14, "lines": 5, "complete": true, "expansions": 4210}]}}]}
```

- `san` in `marginals` is relative to the top line's board at that ply; a move
  that only appears in other lines can have `san: null`.
- The next gap starts from `to.fen` of this one. When the top line's
  `fen_end` has different castling or en-passant fields from the photo's
  confirmed FEN, the line's value is the one of record (the photo cannot see
  castling rights).
- `status: "none"` with `complete: true` means no legal path of `n − 1…n + 1`
  plies exists: one of the two boards, or the ply stamp, is wrong. `timeout`
  means the search gave up; the review page shows the gap as unsolved.

### Evaluation (`engine.lines_eval`)

A *case* is a real game from the chess-insights database cut at a start ply
`s` (uniform over the game, seeded) with gap `g`: A is the true position after
`s` plies (true castling and e.p.), B the placement after `s + g`, and the
solver is asked for `g` plies with the time box and settings above. Games are
chess.com, 10+ minutes, involving the database owner, so ratings are on the
app's scale.

| split | games | used for |
|---|---|---|
| fit: `before-cutoff` | 142, 2024-12-24 … 2025-06-23 (`[2024-07-01, 2025-07-01)`) | `--tune` (λ, width), `--hint-threshold` fit |
| evaluation: `after-cutoff` | 768, 2025-07-18 … 2026-11-16 | the gate tables; nothing is tuned on it |

The rating offset and `k` were fitted on games before the cut-off
(2025-07-01); the opening prior used for evaluation is built from the owner's
games before the cut-off; for the fit split, from games before 2024-07-01.
`--check-leak` checks all of this.

Metrics: **path-found** — a line of exactly `g` plies was returned (a ±1 line
does not count); **top-1 / top-3** — the true line is first / among the first
three; **marg-top** — share of plies where the true move is the top marginal;
times are wall-clock per solve with three solves running at once, each with
three lc0 processes (a single solve on the phone's server is faster).

### Measured accuracy (2026-11-17)

**Evaluation split (`after-cutoff`), 200 cases per gap**, `engine/solver.json`
as committed (λ 2, width 1500, `beam_div` 0.5, time box 30 s):

| gap (plies) | path-found | true line top-1 | true line top-3 | true move = top marginal | median s | p90 s | timeouts |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 1.000 | 0.915 | 0.985 | 0.943 | 0.0 | 0.1 | 0 |
| 8 | 1.000 | 0.590 | 0.745 | 0.779 | 0.1 | 0.8 | 0 |
| 12 | 1.000 | 0.350 | 0.505 | 0.685 | 10.2 | 13.9 | 0 |
| 16 | 0.970 | 0.210 | 0.295 | 0.550 | 17.1 | 18.4 | 5 |
| 24 | **0.870** | 0.020 | 0.030 | 0.210 | 20.7 | 29.7 | 22 |

Gaps 4 and 8 never reach the beam (best-first finishes), so they were measured
once, before `beam_div`; 12–24 are with it. Before `beam_div` and the deadline
fix, gap 24 was 0.735 found (47 of its 53 misses labelled `none` though the beam
had only pruned), 16 was 0.950.

**Fit split (`before-cutoff`)**, the numbers the settings were chosen on:

| setting | gap 16 found / top-3 / median | gap 24 found / top-3 / median |
|---|---|---|
| λ 2, width 1500 (tune, 20 cases) | 0.95 / 0.30 / 17.6 s | 0.95 / 0.00 / 23.8 s |
| + deadline fix, `beam_div` 0 (40 cases) | 0.95 / 0.20 / 14.7 s | 0.825 / 0.00 / 19.6 s |
| + deadline fix, `beam_div` 0.5 (40 cases) | 0.95 / 0.20 / 17.1 s | 0.925 / 0.00 / 20.4 s |
| final, hint-fit cases (60) | 0.98 / 0.27 | 0.85 / 0.03 |

Reading: up to 8 plies the true line is almost always among the top three; at
12 it is a coin flip; from 16 the solver gives *a* plausible line and the
page must present it as a guess. Ply-level marginals stay informative longer
than whole lines (0.55 of moves at 16).

### Photo hint threshold

`python -m engine.lines_eval --hint-threshold` fits a logistic model of
"true line in top 3" on gap and band (band of the mean of the two ratings,
`(band − 1500)/100`) on before-cutoff cases (60 games × gaps 4, 8, 12, 16, 24,
seed 2), and takes per band the gap where it crosses 0.5, clamped to
[6, 60]. Coefficients: intercept 3.684, gap −0.305, band −0.465. Written to
`engine/solver.json` as `hint_plies`:

| band | 1100 | 1200 | 1300 | 1400 | 1500 | 1600 | 1700 | 1800 | 1900 |
|---|---|---|---|---|---|---|---|---|---|
| hint after (plies) | 18 | 17 | 15 | 14 | 12 | 11 | 9 | 8 | 6 |

Only bands 1300–1600 occur in the fit data; the others are extrapolated along
the band slope. Stronger players are *less* predictable to Maia (more distinct
reasonable moves), hence the earlier hint. The default setting (1350 →
Maia-1600) gives 11 plies, about every 5–6 moves each.

### Tried and dropped

- **Pure best-first (A*-style) at all gaps**, priority `g + c·plies_left`:
  exact and fast to 8 plies, but at 16 it timed out on 11 of 12 cases. Adding
  `λ·distance` helped, not enough; best-first is now used only to 10 plies.
- **Wider beams** (3000, 5000): no gain in path-found or top-3 within 30 s on
  the fit split; the time goes into fewer plies.
- **Metal backend for lc0**: 2.0 ms vs 0.6 ms per one-node eval on BLAS; same
  policy.
- **`beam_min_p`** (drop children below a probability): no measurable effect.
- **Per-player fitted band** instead of the game's rating setting: the
  likelihood surface over offsets is flat; the setting is the input.
- **FEN-string keys**: replaced by `_transposition_key()` and bitboard masks;
  with per-side memoisation of the bound, `need()` went 8 s → 1.8 s per
  gap-16 solve.
- **`beam_div` 0.25**: same as 0 on the fit split.

---

# Kept until Phase 5

September grid finder (`calibrate.py`, `rectify.py`); nothing in the photo flow depends on it.

### Calibration (Phase 2)

```python
from engine.calibrate import calibrate
cal = calibrate(sorted(gdir.glob("frames/0000_*.jpg")))
```

`calibrate` takes the start-position frame (or several burst candidates, which
it reduces by median) and returns a `Calibration`.

### `Calibration`

| field | meaning |
|---|---|
| `corners` | `(4,2)` float, **board order `[a1, h1, h8, a8]`** — this single field encodes the orientation |
| `corners_image` | the raw detected quad |
| `H`, `size` | homography from image to a `size`×`size` rectified board |
| `square_is_light` | `(8,8)` bool, `[file][rank]` |
| `camera_side`, `camera_side_idx` | the board edge nearest the camera |
| `score` | checker score in 0..1 over the empty middle ranks; `>= 0.45` (`CHECKER_MIN`) is good |
| `white_margin` | luminance gap between the two piece bands; small means the white/black call was nearly a coin toss |
| `ok` | `score >= CHECKER_MIN` |
| `warnings` | `no-grid`, `low-checker-score`, `weak-white-margin`, `no-saddle-refine`, `fallback-hough`, `drift`, `recovered`, `flipped` |
| `method` | `grid` (saddle-refined), `grid-lines` (line fit only), `tracked` (drift re-snap), `failed` |

`cal.flipped()` returns the same board with white and black swapped. That is the
**second orientation check** Phase 3 owes: frame 1 must change squares on
white's side of the board; if it changed black's, the piece luminances lied, and
the tracker restarts from `cal.flipped()`.

### How it finds the board

Not by looking for a quadrilateral — by looking for a *grid*.

1. Hough segments, clustered into two directions, then **re-assigned by
   vanishing point**. A forearm lying across the board points roughly along a
   grid direction but does not pass through that direction's vanishing point,
   which is what sheds it.
2. Each family's lines are mapped to a coordinate `w = 1/(s - s_inf)`, where `s`
   is where the line crosses a fixed transversal and `s_inf` is where the *other*
   family's vanishing point crosses it. In `w`, the nine grid lines are an
   arithmetic progression — perspective is undone exactly, with no rectification
   and no horizon to go singular.
3. The progression is fitted over **(anchor, spacing) pairs drawn from observed
   lines**, scored by the longest *consecutive* run (a spacing three times too
   small also "explains" every line — as indices 0, 3, 6, 9 with holes), with at
   most one line per index (the board's border edge runs parallel to the
   outermost grid line and would otherwise drag the fit outward at exactly the
   two indices that become corners).
4. Nine consecutive indices are chosen; a short run leaves several placements,
   and each is scored and the best kept.
5. Corners are **reconstructed from the fit**, not from single detections, so
   the outer edges survive being low-contrast or invisible where the border is
   the same colour as the dark squares.
6. Three `cornerSubPix` passes with a shrinking window snap the quad onto the
   7×7 interior saddles. The first window has to be wide (the extrapolated outer
   lines start tens of pixels out) and the last narrow (so it cannot wander onto
   the neighbouring saddle half a square away).
7. Finally the grid **hill-climbs by whole squares**: shift one square in each
   direction, re-refine, re-score, keep what improves. This is not a nicety. At
   30° the far edge runs about 38 px per square against 80 px on the near edge,
   the line fit lands one square out in that direction often enough to matter,
   and sub-pixel refinement then locks happily onto the shifted grid — a board
   shifted by one file is still a checkerboard. What gives it away is the eighth
   file falling on the border instead of on a square, which costs real checker
   score. Without this step the shallow profile calibrates 85 % of games; with
   it, 97.5 %.

### How it works out which way round

* **Which axis the players are on**: pieces fill *every* cell of a back-rank
  band but only half the cells of a file band.
* **Colour phase**: correlation against the checker pattern over the empty
  middle ranks, on each square's **far strip** (see below).
* **Which band is white**: the pixels furthest from the empty-square colour
  models are the piece pixels; the band whose piece pixels are brighter is
  white. The gap is `white_margin`.
* **a1**: white's band gives the rank-1 edge, and a1 is the dark one of its two
  corners. White's band plus the phase fix it uniquely.

Candidates are scored by `checker + 0.25·phase + 0.20·min(white_margin/25, 1)`.
The margin term earns its place: a quad whose two bands are indistinguishable
has been oriented by a coin toss and must not win on checker score alone.

### Known failure modes

* A border the same colour as the dark squares hides the outer grid lines. Step
  5 is the answer, and it is why the outer lines are always reconstructed.
* Very shallow angles foreshorten one family badly; runs of 6 (not 9) are
  accepted, which leaves more window placements for the checker score to judge.
* `ok=False` or a `weak-white-margin` warning is the signal to fall back —
  Phase 4 wires the vision-LLM corner hint, and the camera page already has a
  manual 4-corner drag UI waiting behind `calibration{ok:false}`.

### Measured accuracy

Corner RMS against `truth.json`, over every game's start frame, on the corpora
generated with the seeds above. This is the Phase 2 gate and the baseline any
later change must not regress.

| profile | games | corners ≤ 3 px | median error | orientation | mean score | per game |
|---|---|---|---|---|---|---|
| clean | 30 | 100.0 % | 0.43 px | 30/30 | 0.998 | 0.40 s |
| shallow | 40 | 97.5 % | 0.67 px | 39/39 | 0.992 | 0.46 s |
| hard | 30 | 93.3 % | 0.65 px | 28/28 | 0.949 | 0.44 s |
| nightmare | 10 | 80.0 % | 1.63 px | 8/8 | 0.984 | 0.48 s |

Pooled over clean + shallow + hard: **97 / 100 within 3 px, and orientation
correct on 100 % of those**. Orientation is only ever counted where the corners
passed — a wrong quad has no orientation to be right about.

The errors are bimodal, which is worth knowing before tuning anything: a game
either calibrates to well under a pixel or misses by tens of pixels. There is no
middle ground, because the failure mode is picking the wrong nine lines, not
locating the right ones imprecisely. Chasing the median is pointless; the only
thing that moves the pass rate is generating a better candidate.

### Drift

```python
cal2 = recalibrate(frame_bgr, prior=cal)
```
The board has not moved relative to itself, only in the frame, so this does not
re-detect a grid: it **re-snaps the previous corners onto the new frame's
interior saddles**, which preserves the orientation the start frame established.
Only if that fails does it fall back to a full detection restricted to a band
around the previous grid (`recovered` warning).

If neither holds up — the checker score drops below `CHECKER_MIN` or below 75 %
of the prior's, or the corners jump further than a bump plausibly could (60 px)
— the previous calibration is kept and a `drift` warning is raised. A stale
board is much cheaper than a wrong one.

---

### Rectification (Phase 2)

The rectified board always has **a8 top-left and h1 bottom-right** — white at
the bottom, like a diagram — whatever the camera is doing. Square `(file, rank)`
occupies `x ∈ [file·S/8, (file+1)·S/8]`, `y ∈ [(7-rank)·S/8, (8-rank)·S/8]`.

```python
rect = rectify(bgr, cal, size=512)
patch = square_patch(rect, file, rank, part, cal.camera_side)
```

`part` is `full`, `near` or `far`, and the distinction is the whole reason this
API exists. **Rectification is not de-occlusion**: at 30° a piece's body lands
one to three squares away from the camera of where it actually stands, and only
its base is on its own square.

* `near` — the 30 % of the square closest to the camera. This is where a piece
  standing on that square puts its base, so it is the strip that answers *is
  this square occupied*.
* `far` — the 30 % furthest from the camera, the part least contaminated by the
  piece in front of it. This is the strip that answers *what colour is this
  square*, and it is what `checker_score` uses.

`TOWARD_CAMERA[camera_side]` gives the unit vector, in rectified pixels,
pointing from the board toward the camera.

---

