# Claude Code Handoff — BoardCam

> Last updated: 2026-11-25
> Repo: https://github.com/Kaczor594/boardcam.git (public)
> Branch: main

## Project Summary

Over-the-board chess recorder. One phone runs the clock and takes a few
hand-held photos per game; the FastAPI server on the Mac (behind ngrok) reads
each photo into a position with Claude, and a solver infers the moves between
photos (Maia through lc0 for human-like move probabilities, Stockfish for the
evaluation). The review page shows the game, likely-move and engine arrows,
an evaluation graph, and exports a PGN that marks inferred moves.

Plan: `planning/specs/2026-11-16-photo-positions.md` (read it first). Contracts:
`docs/PROTOCOL.md` (wire, storage, review), `engine/README.md` (lines method
and measured accuracy). The September stand-camera spec is abandoned.

## Current State

Phase 5 (labelled data + grid finder) is built. Its grid-finder gate passed
2026-11-23 on Isaac's corner taps: 45/45 green-mat photos within 0.25 squares,
median 0.045, after adding a radial lens-distortion term; crops are cut. The
gate is still open on data: 45 labelled green-mat photos of the 60 needed,
plus the staged and diagonal photos (see Next Steps). Phases 1–4 of the spec are built and gated. First field test 2026-11-21:
three real games (`data/games/20261121-*`), lichess import fine, photo gaps
6–16 plies plus one of 25. The only failure was a read error (bishop read as
rook on e3 in `20261121-115510-45fc` photos 4–5, confirmed unchanged), which
made one gap unreachable; correcting it in a scratch copy solved every gap.
The three phone issues it found (hint too subtle, review page jumping on
accept, clock needing White at the bottom) were fixed 2026-11-23 and are not
yet tested on the phone. Details in the spec's Notes.

- Reader: Fable 5.2 99.6 % per square on the 21-photo hold-out (Phase 1).
- Intake and confirmation tested on the iPhone for the clock half (Phase 2);
  a real read of a real board on the phone is still untested.
- Solver: true line in the top 3 at gap 4/8/12/16 = 0.99/0.75/0.51/0.30
  (Phase 3). Photo hint threshold 11 plies at the default 1350 ratings.
- Review (Phase 4): built and checked with a demo game (`--demo` from a
  chess.com game, photos at plies 11/22/33/44): 4 gaps in 17 s with real
  engines, 29/44 plies equal to the true game, PGN parses. Headless Chrome at
  1200 px and in a 390 px frame: no overflow, arrows and graph render.

## Environment

```bash
cd ~/claude-projects/boardcam
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv   # never system 3.14
.venv/bin/pip install -e '.[dev]'
caffeinate -i scripts/serve.sh      # needs ANTHROPIC_API_KEY and BOARDCAM_DOMAIN
```

Tunnel: `absolutely-peelable-luisa.ngrok-free.dev` (`BOARDCAM_DOMAIN` in
`~/.zprofile`; one ngrok endpoint at a time). Engines: `lc0`, `stockfish` from
Homebrew; Maia weights in `data/maia/`; opening prior from the chess-insights
DB plus untracked `data/players.json`.

## Layout

| Path | What |
|---|---|
| `server/app.py` | Pages, REST, clock socket, background solve jobs (`schedule_solve`) |
| `server/photos.py` | Photo upload, read, confirm (PROTOCOL §5) |
| `server/gaps.py` | Anchors → gaps → `engine.lines.solve`; pins; timeline; Stockfish cache; PGN |
| `server/review.py` | Review payload, pins, result |
| `engine/reader.py`, `position.py`, `read_eval.py` | Photo → board → FEN; reader scoring |
| `engine/humanlike.py`, `lines.py`, `lines_eval.py` | Move model, line solver, solver scoring |
| `engine/evalgraph.py`, `pgn.py` | Stockfish wrapper; PGN from the timeline |
| `static/clock.html`, `js/clock.js`, `js/clocksm.js` | Clock + camera page; `photoHint` rule |
| `static/review.html`, `js/review.js`, `js/photos.js`, `js/board.js` | Review page; board + arrows |
| `engine/labels.py`, `server/labelsapi.py`, `static/label.html`, `js/label.js` | Label store, game-photo intake on confirm, label page (PROTOCOL §10) |
| `engine/gridfind.py` | Playing-area corners from a hand-held photo; square crops; `--eval`/`--overlay` |
| `engine/train_local.py` | `--inventory` now; the local classifier is Phase 6 |
| `scripts/replay_game.py` | Solve a recorded game (or build a `--demo` one) with real engines |

## Gotchas

- `server.gaps.shutdown()` must run before a script exits: python-chess keeps
  Stockfish on a non-daemon thread and the interpreter hangs otherwise
  (`scripts/replay_game.py` and the app's shutdown hook do it).
- Tests stub lc0 and Stockfish for every test (`tests/conftest.py`
  `_no_engines`): confirming a photo starts a background solve.
- A pin's `ply` is the move's own 1-based ply (= clock press count); the solver
  forces it where `board.ply() == ply − 1`. Photo FENs carry
  `fullmove = ply // 2 + 1`, so `board.ply()` is the game ply.
- Headless Chrome's window is at least 500 px wide; check phone layouts inside
  a 390 px iframe.
- Confirmed game photos are copied into `data/labels/<meta.board_set or
  green-mat>/` on confirm (`g<game_id>-<nnn>`, `checked: false` until looked
  at on `/label`). There is no settings control for `board_set` yet: add one
  before the first wooden-board game.
- Corner truth is in board order (a8, h8, h1, a1), never photo frame.
- The mat's green frame band is the grid finder's main trap (see spec Notes).
- Phone photos bend: the grid finder fits one radial lens term per photo
  (`info["k"]`, from the squares' own vertices). Without it 7/45 green-mat
  grids missed by 0.25–0.5 squares at the edges. Don't tune grid-finder
  constants on the 45 tapped photos: they are the gate set.
- Everything is pushed (4a2332b, 2026-11-23); `data/` is never committed.

## Next Steps

- [ ] Next game: check on the phone the full-screen hint frame, the
      either-way-round clock, Flip view vs "White's side in the photo", and
      Accept stepping forward without the page jumping.
- [ ] Ground truth: `20261121-110553-673e` has all 64 plies accepted (its
      `pins.json` is the true game); guesses matched 45/63 plies (spec Notes).
      Review Accept now steps to the next move; static files are no-cache.
- [ ] Phase 5, Isaac: upload ≥ 16 staged rare-piece photos (queens, rooks,
      bishops; target 30) and ~5 diagonal photos on `/label`, tap their
      corners, and label the wooden boards if that set matters. Then
      `/spec build` re-runs `labels --crops` and the inventory (needs ≥ 60).
      `CONF_OK` stays 0.5, uncalibrated: the eval had no misses.
- [ ] Diagonal-prompt experiment (Phase 5 task) once the diagonal photos exist.
- [ ] Consider a material-count check that flags impossible reads before
      confirmation (field test: a misread was confirmed without looking).
