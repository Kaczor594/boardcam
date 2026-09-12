# Claude Code Handoff — BoardCam

> Last updated: 2026-09-12
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

Phases 1–5 are complete and committed. **Phase 6 (field test) is in progress and
is where the open work is.**

Working:

- Pairing, clock, capture, upload, WebSocket reconnect. Three real games recorded
  without losing a frame.
- ngrok tunnel on the reserved domain `absolutely-peelable-luisa.ngrok-free.dev`
  (`BOARDCAM_DOMAIN` in `~/.zprofile`). Health, WebSocket and the phone pages all
  verified through it.
- Calibration, rectification, the tracking engine, the vision-LLM fallback, the
  review and correction loop, lichess import, and `scripts/tune.py`.
- Review page at `/review?game=<id>`: move list with confidence, before/after raw
  and rectified images, candidates, SAN correction that re-runs the tracker,
  result override, verify, delete.

Not working, in priority order:

1. **No real game has produced a correct PGN yet.** Three attempts, three
   different causes, all now understood (see Known Issues).
2. The Phase 3 accuracy gate was never met and Isaac chose to proceed
   (74.6 % plies on `shallow` against a 98 % gate). Five `tests/test_tracker.py`
   tests fail because of it. They are the only failures in the suite.
3. Automatic calibration is defeated by wood grain (see Known Issues).

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

### 2026-09-12 (this session)

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

**1. Three real games, three failure causes.** All diagnosed, with the evidence
in the spec's Phase 6 amendments.

- Game 1 (15:33, `20260912-152655-89a0`): the start photo already contained
  White's first move, because the camera refreshed frame 0 until the clock
  started, and the clock starts on White's first press. Fixed by the "Board is
  set" button.
- Game 2 (21:23): recorded into game 1's directory. The camera page rejoined the
  finished game from its own URL and the clock carried its press numbering on, so
  three sessions ended up interleaved in one directory. Fixed by the
  finished-game guards. The session was rebuilt into `20260912-212357-0e1f` and
  tracks to 11 plies with 4 flagged, uncorrected.
- Game 3 (21:41, `20260912-213953-bc0f`): recorded cleanly, own directory,
  correct sequence numbers — but **calibration failed (score 0.11, `ok: False`)
  and the tracker ran anyway**, emitting a confident nonsense PGN of rook
  shuffles. See issue 3.

**2. Automatic calibration loses to wood grain.** The detector looks for the most
convincing progression of nine evenly spaced parallel lines. On the first game it
found 1418 segments, the longest 748 px against a 40 px square edge, and every
candidate quad landed on the hardwood floor. It succeeded unaided (score 0.923)
only on the game where the board filled a good part of the frame. Root cause is a
corpus gap: `synth/` renders a textured tabletop with no long straight lines.
`cv2.findChessboardCornersSB` was tried and rejected — it needs the full 7×7
inner grid unoccluded, which a board with pieces never is.

**3. A failed calibration does not stop the analysis.** `server/analysis.py`
tracks the game with `ok: False` and writes a PGN that looks as confident as any
other. Nobody is told the geometry was never found. This is the single cheapest
fix available and it is not done.

**4. Phase 3 accuracy gate unmet.** 74.6 % plies correct on `shallow` against a
98 % gate; five `tests/test_tracker.py` failures follow from it. Phase 3's own
analysis says the search, not the model, is the bottleneck: the true move is
ranked first on 88 % of frames and in the top three on 98 %, and the true game
scores 60–180 nats better in total yet is still evicted from a beam of 30.
Whoever tunes this next should fit against the beam's output, not per-frame
top-1, and re-derive anything measured in nats when the likelihood is rescaled.

**5. Lighting is not modelled.** The evening frames are far darker than the start
photograph with a strong shadow gradient. No synthetic profile reproduces a
global brightness shift of that size between `seq 0` and `seq 1`.

## Next Steps

The spec `planning/specs/2026-09-12-boardcam.md` is the source of truth; Phase 6
is the only open phase. In the order that unblocks the next game:

- [ ] **Refuse to analyse a game whose calibration never passed.** Surface it on
      both phones before the first move rather than after the last, and make the
      camera page insist on manual corners instead of letting the panel be
      dismissed. This is what wasted game 3.
- [ ] Have Isaac review and correct `20260912-212357-0e1f` and
      `20260912-213953-bc0f` on `/review`, then press confirm. That produces the
      first labelled real games, which is what `scripts/tune.py` needs.
- [ ] Add a synthetic profile with a competing floor grid, so the calibrator is
      gated against the failure that actually happens.
- [ ] Consider restricting the line search to the region whose local colours
      alternate between two tones, rather than the whole frame.
- [ ] Run `.venv/bin/python -m engine.evaluate data/real` once there are labelled
      games, then `scripts/tune.py --dry-run`.
- [ ] Phase 6 final validation: ≥ 99 % plies correct on `data/real` with 100 %
      wrong-ply recall, and one real game importing into lichess without hand
      edits.

Open question for Isaac: whether the camera phone should be held landscape. Both
failures had the board in the top third of a portrait frame with the lower half
unlit and unused.
