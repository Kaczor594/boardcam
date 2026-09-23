# Claude Code Handoff — BoardCam

> Last updated: 2026-09-23
> Repo: https://github.com/Kaczor594/boardcam.git (public)
> Branch: main

## Project Summary

Clock-triggered over-the-board chess recorder. Two phones and the Mac turn a
casual rapid game into a PGN with clock times, with no notation by hand. One
phone runs the clock; a second phone on a stand photographs the board on every
press. A tracking engine turns the photo sequence into a move list.

The engine never classifies pieces. It knows the start position and sees one
photograph per ply, so it tracks: each frame is compared with the last, the
pattern of changed squares is scored against every legal move, and a beam search
over the whole game picks the most likely sequence.

Python 3.13 + FastAPI + OpenCV on the Mac, vanilla ES modules on the phones, no
build step. Plan and history: `planning/specs/2026-09-12-boardcam.md`.

## Current State

Phases 1–5 are complete and committed. **Phase 6 (field test) is in progress.**
On 2026-09-23 a review pass over the three recorded games fixed most of what
they exposed (`3c625a7`, see the spec's last amendment). Nothing has been played
on the new build yet.

Working:

- Pairing, clock, capture, upload, WebSocket reconnect. Three real games recorded
  without losing a frame.
- ngrok tunnel on the reserved domain `absolutely-peelable-luisa.ngrok-free.dev`
  (`BOARDCAM_DOMAIN` in `~/.zprofile`).
- Calibration now finds the board on all three games' start frames (partial
  checkerboard detection of the empty middle ranks, pooled with the line
  detector). Game 3: 0.11 → 0.93.
- A game whose board was never found is refused analysis, and both phones say so
  before the first move.
- The tracker uses who pressed the clock (`press_side_penalty` = 10).
- Camera works portrait or landscape; captures at 1920 px.
- Games `20260912-212357-0e1f` and `20260912-213953-bc0f` are re-analysed on
  disk and waiting for Isaac's review. Game 3 now reads
  `1.e4 e5 2.Nc3 Nc6 3.Nf3 Nf6 4.Nxe5 Bb4 5.Qh5 Nxe5 6.Bc4 b5 7.Bd5 Bxc3 8.Qxe5+`.

Not working / unverified:

1. **No real game has been confirmed correct yet** — the reviews above are the
   next step.
2. The phone-page changes (banner, undismissable corner panel, landscape CSS,
   setup warnings, overlay fix, 1920×1080 request) were only syntax-checked,
   never run on an iPhone.
3. Synthetic accuracy is far from the gates: `shallow` 62.1 %, `hard` 86.9 %
   plies (40/30-game corpora, `--no-llm`). Five `tests/test_tracker.py` tests
   fail, the same five as before this session; everything else passes (79).

## Environment Setup

```bash
cd ~/claude-projects/boardcam
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv   # never system 3.14
.venv/bin/pip install -e '.[dev]'
```

`BOARDCAM_DOMAIN` is already exported from `~/.zprofile`. ngrok is installed and
authenticated. To run a game:

```bash
cd ~/claude-projects/boardcam && caffeinate -i scripts/serve.sh
```

Phones open `https://absolutely-peelable-luisa.ngrok-free.dev/camera` (first) and
`/clock` (second, or by scanning the QR). The free tier allows one online
endpoint, so nothing else may be tunnelling at the same time.

`ANTHROPIC_API_KEY` comes from the environment and is only used by
`engine/vision_llm.py`, which the live server never triggers. Only
`engine.evaluate` without `--no-llm` spends money.

## File Structure

| Path | What |
|---|---|
| `server/app.py` | FastAPI: pages, REST, WebSocket bridge |
| `server/ws.py`, `storage.py`, `models.py` | Rooms, game directories, wire models |
| `server/analysis.py` | Calibration and tracking beside a live game |
| `server/review.py` | Review payload, corrections, rectified overlays, labels |
| `server/lichess.py` | PGN import (needs `Accept: application/json`) |
| `static/*.html`, `static/js/*.js` | Phone pages and the games/review pages |
| `engine/calibrate.py` | Board detection, orientation, drift |
| `engine/features.py`, `emission.py`, `tracker.py` | Forward model and beam search |
| `engine/vision_llm.py` | Vision fallback for flagged plies |
| `engine/evaluate.py`, `pgn.py` | Corpus scoring, PGN with `%clk` |
| `synth/` | Renderer and corpus generator the engine is gated on |
| `scripts/serve.sh`, `scripts/tune.py` | Run a game; refit parameters |
| `docs/PROTOCOL.md` | **Authoritative** wire and storage contract |
| `docs/SETUP.md` | Phone placement and the pre-game routine |
| `engine/README.md` | Cross-phase engine contract and measured accuracy |

## Architecture

Clock phone owns the clock and the capture sequence number. Every press is a
WebSocket event; the server persists it and tells the camera to grab a burst of
three frames. The camera uploads them over HTTP. Frames and events land in
`data/games/<id>/`, the same layout the synthetic corpus uses, so
`engine.evaluate` runs unchanged on real and synthetic games.

`server/analysis.py` calibrates once on the start frame and feeds each capture to
a persistent `Tracker`. `clock.stop` writes `analysis.json` and `game.pgn` and
pushes `analysis.ready` to both phones.

A correction is not an edit: `POST /api/games/{id}/corrections` pins a ply and
re-tracks the whole game with every pin constrained, because a misread ply
usually poisons the plies after it. `labels.json` keeps the pins and the move
list they produced; `scripts/tune.py` promotes reviewed games into `data/real/`
with a `truth.pgn` before refitting.

## Git Workflow

Single repo, no submodules, commit straight to `main` and push at every phase
boundary (the repo is public).

Never stage: `data/` (real game photographs and the 1.5 GB synthetic corpus),
`.env`, `.venv/`. All are gitignored; check `git status --short` before staging
rather than using `git add -A` blindly.

Each spec phase gets one commit, and its short hash is appended to the spec's
`commits` frontmatter in a follow-up commit.

## Recent Changes

### 2026-09-23 — review pass over the recorded games (`3c625a7`, `66f783b`)

- `engine/calibrate.py`: `_checker_quads` (cv2.findChessboardCornersSB on 7×n
  patterns, three scales, homography-residual filter) pooled into
  `calibrate_image`; `cv2.setRNGSeed(seed)` for reproducibility; new
  `frame_checks` → `lens-blocked` / `board-small` setup warnings.
- `server/analysis.py`: `calibration_usable` gate in `track_frame`/`finish`
  (`BOARD_NOT_FOUND`); payload gains `corners_norm`, `frame_size`,
  `setup_warnings`; `match_start_orientation` rotates uploads to frame 0's
  orientation; `_build_tracker` takes camera side from the saved corners.
- `server/app.py`: rotation on upload; `analysis.error` carries
  `board_not_found`; saving corners on a finished game re-runs the analysis.
- `engine/tracker.py`: `add_frame(seq, paths, side)`, `press_sides(game_dir)`,
  side-to-move penalty in `_step`; `engine/params.json` gains
  `press_side_penalty: 10.0` (6 was too weak — the true path is evicted first).
- `engine/pgn.py`: Date was 1970 (seconds ÷ 1000); TimeControl/names fall back
  to `meta.json`.
- Camera page: `LONG_EDGE` 1280 → 1920, stream asks 1920×1080, overlay uses
  `corners_norm` (it never showed before), corner panel undismissable until the
  board is found ("Retake start photo" before the first press), setup warning
  strip, landscape CSS. Clock page: board-not-found banner.
- `tests/test_field_fixes.py` (8 tests); PROTOCOL.md and SETUP.md updated.

### 2026-09-12

- **Phase 5 landed** (`60f5c73`): review page, correction loop, result override,
  verify, label export in `scripts/tune.py`, lichess import.
  - Fixed two latent bugs: the end-of-game push awaited a coroutine through
    `asyncio.to_thread`, and the lichess client did not ask for JSON, so
    lichess's 303 redirect read as a failure. Both verified against the real
    endpoint; the test game imported as `https://lichess.org/FByFfT8g`.
  - A pinned move is now forced into the beam's candidate pool. A ply is
    corrected precisely when the truth was outside the top few candidates, so
    filtering those few by the constraint left nothing and the corrected game
    came back empty.
  - `tests/conftest.py` now skips `live`-marked tests unless `-m live` is passed.
    They were billing the Anthropic API on every plain `pytest` run.
- **Phase 6, first field test** (`09a06b0`): four fixes after the first real game.
  "Board is set" button freezes the start frame; manual corners survive the
  pre-start re-detection; the corner handles are draggable; the instructions say
  corner rather than square.
- **Phase 6, second field test** (`8c429d4`): a finished game can no longer
  accept a new session from the camera page, the clock, or a frame upload.

## Known Issues

**1. Beam eviction (Phase 3 gate unmet).** The search, not the model, is the
bottleneck: the true move ranks first on 88 % of frames and top-three on 98 %,
yet the true game is evicted from the beam. The press-side fix only worked at a
penalty high enough to beat eviction. Whoever tunes this next should fit against
the beam's output, not per-frame top-1, and re-derive anything measured in nats
when the likelihood is rescaled. The null prior costs only ~2 nats more than a
move, which is how a real move gets read as "no move".

**2. Hands in the burst.** Test games were played fast, and the +900 ms burst
frame often already has the *next* player's hand over the board (game 3 seq 5,
12). `choose_burst` drops it relatively, which worked here.

**3. Lens obstruction on the stand.** In games 2 and 3 the lower ~45 % of every
frame is black. The camera page now warns about it; the physical cause is
unknown.

**4. Game 1's directory is unusable.** It holds three sessions, and its frame 0
was overwritten by the evening's. Game 2's start frame is borrowed from it
(byte-identical), so game 2 is also less trustworthy than game 3.

**5. `planning/specs/2026-09-12-boardcam.html` is stale** (last rendered at
Phase 3); the `.md` is authoritative.

The former "lighting not modelled" issue was retracted: it was an artefact of
rebuilding game 2.

## Next Steps

The spec `planning/specs/2026-09-12-boardcam.md` is the source of truth; Phase 6
is the only open phase.

- [ ] Isaac reviews and confirms `20260912-213953-bc0f` (the clean one) and
      `20260912-212357-0e1f` on `/review`. That gives the first labelled real games.
- [ ] Play one game on the new build, ideally landscape. Check that the green
      overlay lands on the board, the setup warnings, the `camera.status` w/h
      (does iOS give 1920×1080?), and what is blocking the bottom of the lens.
- [ ] Copy the reviewed games to `data/real/`, then run
      `.venv/bin/python -m engine.evaluate data/real` and `scripts/tune.py --dry-run`.
- [ ] Attack beam eviction (Known issue 1). It is now the main accuracy limit.
- [ ] Add a synthetic profile with a competing floor grid so the calibrator is
      gated against it.
- [ ] Phase 6 final validation: ≥ 99 % plies correct on `data/real` with 100 %
      wrong-ply recall, and one real game importing into lichess without hand edits.

Settled 2026-09-23: the camera phone should work held either way (it now does).
