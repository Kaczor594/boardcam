# Claude Code Handoff — BoardCam

> Last updated: 2026-11-18
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

Phases 1–4 of the spec are built; **Phase 4's manual gate is open**: one real
game with at least three photos, played and reviewed on the phone, PGN
imported into lichess without edits. Nothing has been played on this build.

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
- Confirmed labels for Phase 5 accumulate in each game's `positions.json`
  (`read` beside `confirmed`).

## Next Steps

- [ ] Phase 4 manual gate: one real game, ≥ 3 photos, reviewed on the phone;
      lichess import without edits. Note: was the glow noticed, did it
      distract; how far apart were the photos (gap-24 cases); any UI issues.
      Record in the spec's Notes, then `/spec build` resumes at Phase 5.
- [ ] Then `scripts.replay_game data/games/<that game>` is the gate command.
