# BoardCam

Over-the-board chess recorder. One phone runs the clock and, a few times a
game, takes a hand-held photo of the board; a server on the Mac turns each
photo into a position and infers the moves in between, so a casual rapid game
ends as a PGN with clock times without anyone writing moves down.

- **Reading a photo.** Claude (Fable 5.2 by default) reads the photo into an
  8×8 board and White's home side: 99.6 % of squares on 21 hold-out photos.
  The player confirms the board with a few taps (`engine/reader.py`,
  `engine/position.py`).
- **Between photos.** A search finds legal move sequences of exactly the
  clocked number of plies from one confirmed position to the next and ranks
  them by how likely players of the configured rating are to play them (Maia
  through lc0, plus an opening prior from the players' own online games). Up
  to 8 plies the true line is in the top three 75–99 % of the time; at 16 it is
  a best guess (`engine/lines.py`, `engine/README.md`).
- **Review.** The review page steps through the game, shows the likely moves
  and Stockfish's as arrows, lets the player accept the move that was played
  (which re-solves the gap), draws an evaluation graph, and exports a PGN that
  marks every inferred move. A slow frame pulses around the clock screen when
  the moves since the last photo are becoming too many to reconstruct.

## Status

Phases 1–4 of `planning/specs/2026-11-16-photo-positions.md` are built: reader,
photo intake, line solver, review. Phases 5–6 (a locally trained reader that
needs no API key) are next. The September stand-camera design is abandoned.

## Quick start

```bash
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
brew install lc0 stockfish ngrok           # Maia weights go in data/maia/ (docs/SETUP.md)
export BOARDCAM_DOMAIN=your-name.ngrok-free.app ANTHROPIC_API_KEY=…
scripts/serve.sh
```

Open `/clock` on the phone. Full instructions: [`docs/SETUP.md`](docs/SETUP.md).

## Layout

| Path | What |
|---|---|
| `server/` | FastAPI app, clock socket, photo intake (`photos.py`), gap solving (`gaps.py`), review payload |
| `static/` | Clock page, review page, games list. Vanilla ES modules, no build step |
| `engine/` | Reader, positions, human-move model, line solver, Stockfish, PGN |
| `scripts/` | `serve.sh`, `replay_game.py` (solve a recorded or demo game), `replay_photos.py`, `label_page.py` |
| `docs/PROTOCOL.md` | Wire and storage contract. Authoritative |
| `data/` | Games, photos, labels, engine weights and caches. Never committed |

## Tests

```bash
.venv/bin/pytest -q          # everything, including the node clock tests; lc0/Stockfish stubbed
.venv/bin/python -m scripts.replay_game --demo some.pgn --photos 11,22,33   # real engines
```
