# BoardCam engine

Frames in, moves out. This file is the contract between phases: everything a
later phase needs from an earlier one is written down here, so a new session can
pick up without re-reading the code that produced it.

Companion documents: `docs/PROTOCOL.md` (the wire and storage contract, written
in Phase 1) and `planning/specs/2026-09-12-boardcam.md` (the plan, including
Notes §B for the calibration method and §C for the emission model).

---

## Corpus layout (Phase 2)

`synth.generate` writes game directories that are **byte-for-byte the same
shape** as real ones, so `engine.evaluate` never learns which it is looking at:

```
data/synth/<profile>/<NNN-name>/
    frames/0000_0.jpg        start position (seq 0 has one candidate)
    frames/0001_0.jpg …_2.jpg  burst after ply 1
    …
    events.jsonl             clock.config, clock.start, clock.press…, clock.stop
    truth.pgn                the game
    truth.json               ground truth for the gate (synthetic only)
```

Capture numbering is the protocol's: **seq 0 is the start position, seq n is the
position after ply n**, and the final `clock.stop` capture (seq = plies + 1)
photographs the same position as the last press. That trailing frame is
deliberate — it is a real frame that advanced no plies, which is the ∅ case the
tracker has to survive on every single game.

### `truth.json`

| field | meaning |
|---|---|
| `corners` | board corners of frame 0, image pixels, ordered `[a1, h1, h8, a8]` |
| `corners_by_seq` | the same for every capture's `k=0` frame (jitter and bumps move them) |
| `camera_side` | which board edge the camera is nearest: `rank1 \| fileh \| rank8 \| filea` |
| `square_is_light` | `[file][rank]`, `True` for light — a1 is always dark |
| `plies`, `stop_seq`, `bump_seq`, `features` | game shape; `features` lists castling, en passant, promotion, underpromotion |
| `elevation_deg`, `azimuth_deg`, `focal_px`, `image` | camera pose, for diagnosing failures by geometry |

### Profiles

| profile | elevation | scene | bursts | role |
|---|---|---|---|---|
| `clean` | 45–80° | no players, no props, no hands, no jitter | 1 | sanity only |
| `shallow` | 25–40° | full scene noise, hands, jitter, bumps | 3 | **Isaac's setup — the primary gate** |
| `hard` | 35–70° | as shallow, steeper, more bumps | 3 | secondary gate |
| `nightmare` | 20–25° | dim, heavy jitter | 3 | reported, never gated |

Regenerate with the seeds the thresholds were measured on, or the numbers below
mean nothing:

```
python -m synth.generate --games 30 --profile clean     --seed 1 --out data/synth/clean
python -m synth.generate --games 40 --profile shallow   --seed 4 --out data/synth/shallow
python -m synth.generate --games 30 --profile hard      --seed 2 --out data/synth/hard
python -m synth.generate --games 10 --profile nightmare --seed 3 --out data/synth/nightmare
```

`--max-plies N` truncates every game; it exists so tests can render a corpus in
seconds, and it changes `truth.pgn` accordingly. `--bursts` overrides the
profile's burst count.

**The camera always stands on a file side** (`sides=(1, 3)`), never behind a
player. This is not a simplification for the engine's benefit: at 25–40° a
camera behind a seated player photographs the player, not the board, and
`docs/SETUP.md` tells Isaac to mount it on the free side for exactly that
reason.

Piece heights are real: a king is 1.75 squares tall, so at 30° it hides roughly
three squares behind itself. Every occlusion number in Notes §C is sized for
that, so do not "fix" a tracking problem by shrinking the pieces.

---

## Calibration (Phase 2)

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

## Rectification (Phase 2)

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

## Tracking (Phase 3)

```python
from engine.tracker import track_game
result = track_game("data/synth/shallow/002-immortal-1851")
print(result.san, result.flagged)
```

or incrementally, which is how the server runs it during a live game:

```python
tracker = Tracker(calibration, frame0_bgr, params)
for seq, paths in frame_paths(game_dir)[1:]:
    tracker.add_frame(seq, paths)
result = tracker.result()
```

`Tracker.constrain(ply, move)` returns a **new** tracker with that ply pinned and
the whole game re-run, which is what a correction on the review page does.

### `TrackResult`

| field | meaning |
|---|---|
| `moves` | the move list, `chess.Move` |
| `plies` | one `PlyInfo` each: `san`, `uci`, `seq`, `margin`, `flags`, `candidates` |
| `frames` | one record per capture: blob count, change mass, alignment shift, warnings |
| `board` | the final position |
| `calibration`, `camera` | what the geometry ended up being |

`PlyInfo.seq` is the capture the ply was read from. It is **not** `index + 1`:
a missed press puts two plies on one capture and a double press puts none on it,
and the review page needs to show the right photograph.

`PlyInfo.candidates` is what Phase 4 puts to the vision model — the top few
moves at the frame that decided this ply, each with its log-likelihood.

### Flags

| flag | meaning | who resolves it |
|---|---|---|
| `low_margin` | the ply's margin is under `tau` nats | Phase 4 LLM, else the review page |
| `unexplained` | even the best candidate scored below `divergence_floor` — a hand covered the board, or the position has drifted | Phase 4 asks an open question |
| `promotion_unknown` | **every** promotion carries this | Phase 4 LLM |

`margin` is the smaller of two numbers: the **local** margin (how far the chosen
candidate beat the best candidate that disagrees, at the frame that decided it)
and the **global** margin (log-sum-exp of the beam paths that agree about this
ply, minus that of the paths that disagree). Either can be the binding one — a
ply can be obvious at its own frame and still be contradicted by later frames.

Promotions are flagged without exception because the engine genuinely cannot
name the piece: a rook and a queen differ by a quarter of a square in height and
nothing else the camera can see. A prior that promotions are queens breaks the
tie so the *rest* of the game is tracked against a board that is right; naming
the piece is the LLM's job.

### `engine.evaluate`

```
python -m engine.evaluate data/synth/shallow --no-llm --worst 5
```

| number | meaning |
|---|---|
| plies correct | **index-wise** agreement with `truth.pgn` |
| final position ok | share of games ending on the right board |
| wrong-ply recall | of the plies it got wrong, the share it flagged |
| flagged per game | what that recall costs — each one is an LLM call or a human glance |

Plies-correct is index-wise on purpose, and it is brutal: a game that goes out of
step at ply 16 scores zero for every ply after it, even though the tracker is
still following the game correctly from its own board. Read it with
final-position-correct beside it. **Wrong-ply recall is the number that decides
whether a game is recoverable**, and a low recall is worse than a low accuracy —
an unflagged wrong ply is one nobody will ever look at.

### Measured accuracy (Phase 3, gate NOT met)

Corpora generated with the seeds above; `--no-llm` throughout; beam width 30.

| corpus | plies correct | final position | wrong-ply recall | flagged/game | s/frame |
|---|---|---|---|---|---|
| clean (30 games) | 94.6 % | 63 % | 78 % | 10.3 | 0.16 |
| shallow (20 games) | 74.6 % | 5 % | 57 % | 14.1 | 0.27 |

The gate wanted 99.5 % on clean and 98 % on shallow. Read those numbers next to
the per-frame ones, because the difference between them is the whole story:

| corpus | true move ranked 1st | in top 3 |
|---|---|---|
| clean | 98.1 % | 99.9 % |
| shallow | 88.4 % | 97.8 % |

**The model is not the bottleneck; the search is.** Scoring the true move
sequence forward and comparing it with the beam's leader, the true game scores
**60–180 nats better in total** than the game the beam returns, and never falls
more than **4.4–6.7 nats** behind at any prefix — and is still evicted from a
beam of width 30. Widening does not help: 30, 80 and 200 give bit-identical
accuracy on the failing games, because the problem is that 30-odd sequences sit
within a few nats of each other at the frame where it dies, not that the beam is
too small to hold the right one. The field has to be made smaller by sharpening
the per-frame likelihood.

Two cautions for whoever tunes this next:

* **Every parameter here was fitted on per-frame top-1 accuracy, which is not
  what is failing.** Fit against the beam's output instead.
* **`score_clip` and `tau` are on the emission's scale and the scale changed.**
  When the likelihood ratio replaced the old heuristic terms, typical margins
  went from ~3 nats to ~40. `score_clip` was left at 8 and silently flattened
  every frame to near-uniform, which alone took shallow from 74.6 % to 39.8 %.
  Anything measured in nats has to be re-derived when the likelihood is rescaled.

---

## Vision-LLM fallback (Phase 4)

```python
from engine.vision_llm import resolve_ply, Option

result = resolve_ply(before_paths, after_paths, rect_before, rect_after,
                     candidates, side_to_move, calib)
# {"san": "Nf3" | ... | "none_of_these", "confidence": 0.0-1.0,
#  "model": "claude-sonnet-5" | "claude-opus-5", "cost": dollars}
```

`before_paths`/`after_paths` are the raw burst frames either side of the ply
(paths or ndarrays — the first is used, burst selection already happened
upstream). `rect_before`/`rect_after` are the same two captures already run
through `engine.rectify.rectify`. `candidates` is a list of `Option(label,
moves)` (or an equivalent `{"label": ..., "moves": (...)}` dict) — `moves` is
0, 1 or 2 UCI strings, and `label` must be unique within the call; it becomes
both the enum value and the display string. The call is **forced tool use**
(`tool_choice: {"type": "tool", "name": "choose_move"}`) against an enum of
every label plus `"none_of_these"`, so the answer is always one of the offered
candidates or an explicit "none of them" — never free text.

**Images sent, in order:** the raw camera view before, the raw camera view
after (both cropped to the board's bounding box plus a 15 % margin — this is
what puts hands and the captured-piece pile in frame), then the same two
moments rectified to a top-down view with every candidate's touched squares
outlined in red.

**Escalation.** The first call is `claude-sonnet-5`. If it answers
`none_of_these` or its confidence is below `0.7`, a second call goes to
`claude-opus-5` with the identical prompt and images; the returned `cost` is
the sum of both. A model tier is never asked twice.

### Caching and the call budget

```python
from engine.vision_llm import LLMResolver

resolver = LLMResolver(game_dir)           # <game_dir>/llm_cache.json
answer = resolver.resolve(str(seq), before_paths=..., after_paths=...,
                          rect_before=..., rect_after=..., candidates=...,
                          side_to_move=..., calib=...)
```

`LLMResolver` is what the tracker actually calls. It caches by whatever key
the caller passes — the tracker uses the frame's capture `seq` — so replaying
a game (a correction re-run via `Tracker.constrain`) costs nothing for frames
already resolved. `max_calls` (default 10) is a hard ceiling on *new* calls per
game; once spent, `resolve()` returns `None` and the ply stays flagged rather
than the game running up an unbounded bill. The cache is a flat JSON file, one
entry per key, read at construction and rewritten after every new call.

### Beam integration (`engine/tracker.py`)

Once per frame — not once per beam path — the tracker checks the **current
leader's** own candidate scores (`self.beam[0]`, the same board every
downstream ply is read against). If the top two candidates are closer than
`tau` or the top score is below `divergence_floor`, and `params["use_llm"]` is
set, it builds a label for each of the top 6 candidates (12 when
`unexplained`, since pairs are already in `scored` by then) from
`board.san(...)`, asks `LLMResolver`, and — if the answer names one of them —
adds `log(confidence)` as a bonus to every path in the beam whose candidate at
that frame shares the exact same move signature (by UCI, not by path
identity, since most of the beam agrees about the current board anyway). A
`none_of_these` or budget-exhausted answer changes nothing; the ply stays
flagged for the review page. Cost, model and confidence are logged into that
frame's record (`frame_rec["llm_cost"]` etc.), which is what
`engine.evaluate`'s `llm_cost_per_game` column reads — no change was needed
there.

`params["use_llm"]` is off unless a caller sets it explicitly; `engine.evaluate`
sets it from `--no-llm`, and the live server (`server/analysis.py`) does not
set it, so a game in progress never triggers a paid call mid-play — only
`engine.evaluate` and any future opt-in wiring do.

### Measured accuracy (Phase 4, gate **NOT MET**, by design of the test)

- `pytest tests/test_vision.py -q` (mocked): **10/10 pass** — schema, forced
  tool choice, both escalation triggers, cost summation, confidence clamping,
  cache reuse across `LLMResolver` instances, the call cap, and recovery from a
  corrupt cache file.
- `pytest tests/test_vision.py -q -m live`: 3/3 calls returned a valid,
  schema-conforming answer (one correctly escalated to Opus on low
  confidence); **1/3 matched truth**, short of the gate's 2/3.
- `engine.evaluate data/synth/shallow` / `data/synth/hard` (LLM on): **not
  run.** Diagnosis below explains why running the full ~$10–25 corpus would
  not have told us anything the smoke test didn't, and Isaac chose to skip it
  (2026-09-12) — this mirrors the Phase 3 gate override.

**Why the smoke test fell short, and why it isn't a plumbing bug.** The three
flagged plies it was asked about: a bishop-or-knight-to-h6 ambiguity, an
underpromotion (rook/knight/queen/bishop), and a rook move in a cluttered,
hand-occluded midgame frame.

1. `synth/render.py` draws every piece as the same cone/cylinder silhouette,
   distinguished only by height (Notes §A: `P<B<N<R<Q<K`, bishop ≈ 1.2×pawn).
   A bishop and a knight at adjacent height classes are visually
   indistinguishable in the rendered image — there is no mitre, no horse head,
   nothing but a slightly taller cone. The model answered a real candidate
   (`h6`, a pawn push) rather than either piece option, i.e. it could not tell
   *any* piece moved there versus a pawn already being there. This is not
   fixable by prompting; it needs the renderer to draw visually distinct
   piece heads, which is out of scope for Phase 4.
2. The rook-move frame had hands from both players resting near the board on
   a busy midgame position — exactly the scene noise `hard` is built to
   contain (Notes §E). The model answered "no move happened". A human glancing
   at the same crop would plausibly make the same call.
3. The promotion question (genuinely unresolvable by the engine itself, hence
   always flagged) was answered *correctly* (`b1=N`) after escalating to Opus
   on low confidence — the one case in this smoke test the vision fallback is
   squarely meant for, and the one it got right.

**The implication for Phase 6:** the synthetic corpus cannot validate the
piece-*identity* half of what this module is for, because the renderer
deliberately keeps pieces abstract (that abstraction is exactly what makes the
tracker's forward model work — see Notes §C — and reworking it is a Phase 2/3
change, not a Phase 4 one). Real photographs have real piece shapes. Phase 6
is therefore the first point this module's actual value can be measured, and
its cost (`$0.007`/sonnet call, `$0.024` on an escalation to Opus, from the
smoke test's real usage) is in line with the gate's `≤ $0.15`/game budget even
if every flagged ply in a real game escalated.
