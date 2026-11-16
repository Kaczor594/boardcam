---
created: 2026-11-16                 # NEVER changes
modified: [2026-11-16]              # append-only
sessions: [2026-11-16]              # append-only; one entry per session that touches this spec
commits: []                         # append-only; short hashes, appended as phases land
back_refs: [planning/specs/2026-09-12-boardcam.md]
forward_refs: []
status: draft                       # draft | in-progress | done | abandoned
---

# BoardCam — hand-taken photos → positions → likely lines

## Problem
The September design needs a phone on a stand photographing the board on every
clock press. Where the games are actually played a stand is not available, so
that design cannot be field-tested and its tracker (the unmet Phase 3 and 6
gates) is solving a problem that no longer exists. What is available: one phone on the table running the clock, picked up a few
times per game to take a hand-held photo, from any side, at any angle. The app has to turn each photo into a
position on its own, and fill in the moves between photos as well as it can.

## Solution
One phone is both clock and camera: a camera button on the clock page pauses the
clock, opens the camera, and uploads a full-resolution photo stamped with the
clock's ply count. When the moves since the last photo are becoming too many to
reconstruct, the button glows faintly as a hint. A reader turns each
photo into a FEN. The reader is a swappable backend: Claude Fable 5.2 by default
(99.5 % of squares, 8 of 10 boards perfect in the 2026-11-16 pilot), selectable
per game in a settings menu, with a locally trained classifier as the long-term
backend that needs no API key. Between two confirmed positions a solver searches
for legal paths of exactly the clocked length and ranks them by how likely
players of the configured rating are to play them (Maia through lc0, plus an
opening prior from the players' own online games). The review page shows the
FENs, the most likely line, alternative moves as arrows, a Stockfish evaluation
graph, and exports a PGN that marks which moves are inferred.

## Files
**Existing (touched):**
- `static/clock.html`, `static/js/clock.js` — game creation, per-game settings, camera button and photo hint; clock state survives a page reload; drop capture/calibration and pairing wiring
- `static/js/clocksm.js` — unchanged; its press count is the ply count
- `static/index.html`, `static/games.html` — entry points and game list for the new flow
- `static/review.html`, `static/js/review.js` — rewritten around positions and lines
- `server/app.py`, `server/storage.py`, `server/models.py`, `server/ws.py` — photo upload, positions, settings; remove burst/tracker hooks
- `server/review.py` — pins become "confirmed square" and "accepted move"; re-solve replaces re-track
- `server/analysis.py` — replaced by position/line orchestration
- `engine/pgn.py` — mark inferred moves; keep `%clk` on every ply
- `server/lichess.py` — unchanged
- `docs/PROTOCOL.md`, `docs/SETUP.md`, `README.md`, `CLAUDE.md`, `CLAUDE_HANDOFF.md`, `pyproject.toml`
- `planning/specs/2026-09-12-boardcam.md` — set `abandoned` with an Amendments entry

**Removed:**
- `static/camera.html`, `static/js/camera.js`, `engine/tracker.py`, `engine/emission.py`,
  `engine/features.py`, `engine/evaluate.py`, `engine/vision_llm.py`, `engine/camera.py`,
  `engine/params.json`, `synth/`, `scripts/tune.py`, and their tests
- `engine/calibrate.py`, `engine/rectify.py` — kept until Phase 5 decides what the new grid finder reuses

**New:**
- `engine/reader.py` — `read_position(image, backend, model)` → 8×8 read, White's home side, cost
- `engine/position.py` — image-frame read → FEN (orientation, side to move), legality flags
- `engine/read_eval.py` — scores a reader against labelled photos
- `engine/humanlike.py` — Maia policy through lc0, cached per FEN; rating → band
- `engine/lines.py` — path search between two positions, ranked lines, per-move marginals
- `engine/lines_eval.py` — scores the solver on real games cut at known plies
- `engine/evalgraph.py` — Stockfish evaluation per ply
- `engine/gridfind.py`, `engine/local_reader.py`, `engine/train_local.py` — Phases 5–6
- `static/js/board.js` — board view, tap-to-fix editor, arrows
- `static/label.html`, `static/js/label.js` — labelling tool (from the pilot page)
- `tests/test_reader.py`, `test_position.py`, `test_lines.py`, `test_intake.py`, `test_gridfind.py`, `test_local_reader.py`

## Execution plan
| Phase | Model | Agents | Clear after | Why |
|---|---|---|---|---|
| 1 | Opus | none | yes | Removal plus one module; artifact on disk |
| 2 | Opus | none | yes | Backend and clock page; next is math |
| 3 | Fable 5 | none | yes | New search and probability design |
| 4 | Opus | none | yes | Frontend on a defined interface |
| 5 | Opus | none | no | Phase 6 uses its crops directly |
| 6 | Fable 5 | none | no | Model design; final phase |

Six sessions, one per phase, except that 5 and 6 may share one if the tier
switch is taken at the gate. Phases 1–4 deliver the working app on the Claude
reader; 5–6 deliver the no-API reader.

## Phase 1 — Clear the ground and build the reader
Remove the stand-camera code, port the pilot's reader into the engine, and make
its accuracy a repeatable measurement.
**Execution:** `Opus` — deletion plus one well-understood module. Agents: none — sequential, small surface.
**Context:** **CLEAR after this phase** — the old code's details die here; Phase 2 needs only the reader's interface, which is written into `docs/PROTOCOL.md`.
- [ ] Set the September spec to `abandoned` with an Amendments entry pointing here
- [ ] Delete the files listed under Removed and their tests; `pytest` green on what remains
- [ ] Move pilot data from `data/label-pilot/` to `data/labels/green-mat/` (photos + `truth.json`; stays gitignored)
- [ ] `engine/reader.py`: Claude backend from the pilot prompt; structured output; model id is a parameter, default `claude-fable-5-2`; refusal and `max_tokens` handled as errors, not empty boards. Load the `claude-api` skill first.
- [ ] Measure photo-only against photo-plus-grid on the 10 pilot photos and keep photo-only if it is within 0.5 points (it removes the grid dependency)
- [ ] `engine/position.py`: rotate the read by White's home side, set side to move, build the FEN, flag illegal positions (king count, pawns on back ranks, more than 16 a side, side not to move in check)
- [ ] `engine/read_eval.py`: per-square, occupied-square, perfect-board and home-side accuracy, cost and seconds per photo, confusion pairs
- [ ] Label the remaining usable green-mat photos (about 20) as a hold-out the prompt is never tuned on. Needs Isaac, from a blank board as in the pilot.
- [ ] Write the reader and position interfaces into `docs/PROTOCOL.md`

**Validation (gate — do not start Phase 2 until these pass):**
- `.venv/bin/pytest -q` → `0 failed`
- `.venv/bin/python -m engine.read_eval data/labels/green-mat --split pilot --model claude-fable-5-2` → `per-square >= 0.990` (costs about $1)
- `.venv/bin/python -m engine.read_eval data/labels/green-mat --split holdout --model claude-fable-5-2` → `per-square >= 0.985`, `home-side >= 0.95`
- `git grep -n "tracker\|emission\|synth" -- '*.py' '*.js' '*.toml'` → no matches

**Handoff (do before stopping):**
- [ ] Persist to `docs/PROTOCOL.md` and this spec's Notes: the final prompt variant, whether the grid was dropped, hold-out numbers, any photos excluded and why

**Resume with:** `/clear` → `/model opus` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `docs/PROTOCOL.md`, `server/app.py`, `server/storage.py`, `static/js/clock.js` — and nothing else.

## Phase 2 — Photo intake, settings, positions on the phones
A game can be created, clocked and photographed, and each photo comes back as a
FEN that can be corrected by tapping squares.
**Execution:** `Opus` — ordinary backend and page work. Agents: none — pieces share the wire contract.
**Context:** **CLEAR after this phase** — Phase 3 is engine math on a different surface; it needs the stored position format only.
- [ ] Game creation on the index/clock page; one phone, so the room code, QR pairing and camera role are removed
- [ ] Clock state (times, turn, press count, paused) is saved on the phone on every change and restored on reload. iOS can discard the page while the camera is open, and today a reload resets the clock.
- [ ] Per-game settings (before start, editable later on review): player names, rating per player (default 1350 on the chess.com scale), reader model (Fable 5.2 default; Sonnet 5.5, Opus 5.5; `local` appears in Phase 6). Stored in `meta.json`; last used values become the defaults.
- [ ] Camera button on the clock page: pauses the clock, opens the phone camera through a file input with capture (full-resolution photo), uploads, shows the returned board. The clock stays paused until a player resumes it; cancelling the camera leaves it paused too.
- [ ] `POST /games/{id}/photos`: store `photos/NNN.jpg`, stamp with the clock's press count and side to move at the moment of the pause, run the reader in a worker thread, write `positions.json`, return the result to the phone
- [ ] Tap-to-fix editor (`static/js/board.js`): change a square, rotate the board, confirm. A confirmed position stores the original read beside the corrections, which is the training label for Phase 5.
- [ ] Ply stamp is adjustable by ±1 on confirm (photo taken after the move but before the press)
- [ ] A photo with no board, or an illegal position, is reported on the phone and can be retaken; nothing is silently accepted
- [ ] Confirming a read must not hold up the game: the board can be confirmed later from the review page, and play resumes with one tap
- [ ] UI per the `kaczor-design` skill; rewrite `docs/PROTOCOL.md` §§ for photos, positions, settings; update `docs/SETUP.md`

**Validation (gate — do not start Phase 3 until these pass):**
- `.venv/bin/pytest -q` → `0 failed` (includes `tests/test_intake.py` with a stubbed reader: upload → stamped position → correction → confirmed FEN)
- `.venv/bin/python -m scripts.replay_photos data/labels/green-mat/2777.jpg --ply 24` → prints a FEN equal to the labelled one
- Manual, on the iPhone over the tunnel: create a game, press the clock 6 times, tap the camera button (clock pauses), take a photo, confirm the FEN shows `ply 6` and the right side to move, resume, and check both times are what they were at the pause. Repeat once after force-reloading the page mid-game. Record the result in Notes.

**Handoff (do before stopping):**
- [ ] Persist to `docs/PROTOCOL.md`: the `positions.json` schema exactly as written, the settings keys, and anything iOS did that the page had to work around

**Resume with:** `/clear` → `/model fable` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `docs/PROTOCOL.md` (positions and settings sections), `engine/position.py` — and nothing else.

## Phase 3 — Likely lines between positions
Given two confirmed positions and the number of plies between them, return the
most likely lines and, for every ply, how likely each candidate move is.
**Execution:** `Fable 5` — new search and probability design; the method is the deliverable. Agents: none — one coupled derivation.
**Context:** **CLEAR after this phase** — the derivation is spent once `engine/lines.py` and its README section exist; Phase 4 consumes `lines.json`.
- [ ] `engine/humanlike.py`: lc0 with a Maia weight file at one node, policy read from verbose move stats, cached per FEN and band; the two players may use different bands
- [ ] Rating → band: choose by which band best predicts each player's real moves in the chess-insights database, fitted on games before a cut-off date and checked on games after it; the per-game rating setting overrides
- [ ] Opening prior: move frequencies from the players' own games for positions they have reached before, blended with Maia by sample size
- [ ] `engine/lines.py`: best-first search from A for paths of exactly `n` plies ending in B, pruned by necessary conditions (material can only fall, pawns only advance, a lower bound on plies still needed), scored by the product of move probabilities; returns top lines with normalised probabilities and per-ply move marginals. Tries `n−1` and `n+1` when `n` yields nothing. Time-boxed; reports when it gave up.
- [ ] Castling rights and en passant in the resulting FENs come from the chosen path
- [ ] `engine/evalgraph.py`: Stockfish evaluation for every ply of a line, and Stockfish's top moves at a given ply (for the engine arrows)
- [ ] `engine/lines_eval.py`: cut real games from the chess-insights database at known plies and score the solver by gap length (4, 8, 12, 16, 24): a path found, true line is top-1, true line in top-3, share of plies where the true move is the top marginal. Evaluation games are after the cut-off; nothing is tuned on them.
- [ ] Photo hint threshold: from the accuracy-by-gap table, the number of plies since the last confirmed photo beyond which reconstruction is no longer dependable (true line outside the top 3 more often than not, or the search no longer finishes in its time box). One number per rating band, written to config. Fitted on games before the cut-off, reported on games after.
- [ ] Document the method and the measured table in `engine/README.md`

**Validation (gate — do not start Phase 4 until these pass):**
- `.venv/bin/pytest -q tests/test_lines.py` → `0 failed` (includes: every returned path is legal, has exactly the requested length, and ends in B)
- `.venv/bin/python -m engine.lines_eval --gaps 4,8 --games 200 --split after-cutoff` → `path-found >= 0.98`, `true-line-top3 >= 0.60` at gap 4
- `.venv/bin/python -m engine.lines_eval --gaps 12,16,24 --games 200 --split after-cutoff` → `path-found >= 0.90`, median time per gap `<= 30 s`; top-1/top-3 recorded in Notes, no threshold (these gaps are expected to be ambiguous)
- `.venv/bin/python -m engine.lines_eval --check-leak` → `0 evaluation games before cut-off`
- `.venv/bin/python -m engine.lines_eval --hint-threshold` → prints one ply count per band, each between 6 and 60

**Handoff (do before stopping):**
- [ ] Persist to `engine/README.md` and Notes: the `lines.json` schema, the chosen bands, the accuracy-by-gap table, what was tried and dropped

**Resume with:** `/clear` → `/model opus` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `engine/README.md` (lines section), `docs/PROTOCOL.md`, `static/js/board.js`, `server/review.py` — and nothing else.

## Phase 4 — Review: line, arrows, graph, export
The finished game is one page: photos with their FENs, the inferred moves
between them, and a PGN.
**Execution:** `Opus` — frontend and glue on interfaces that now exist. Agents: none.
**Context:** **CLEAR after this phase** — Phases 5–6 are a separate workstream (training data and models).
- [ ] Photo hint on the clock page: a slow, low-contrast glow on the camera button once the plies since the last photo pass the Phase 3 threshold. Only after at least one photo has been taken in the game; never while either clock is under one minute (and it goes away if one drops below); cleared by the next photo. No sound, no text, no vibration.
- [ ] After each confirmed photo, solve the gap from the previous confirmed position (or the start) in the background and push `lines.ready`
- [ ] Review page: step through plies on a board; photo positions marked as confirmed, plies between as inferred
- [ ] Arrows from the current ply: derived moves in one colour with thickness by marginal probability, Stockfish's top moves in a second colour; a legend; the pair chosen from the design system and checked for contrast in light and dark
- [ ] Accepting a derived arrow pins that move and re-solves the rest of the gap; pins are stored like square corrections and can be undone
- [ ] Evaluation graph over the whole game: solid through confirmed positions, visibly different between them; photo points marked; tapping jumps to the ply. Load the `dataviz` skill before drawing it.
- [ ] `engine/pgn.py`: `%clk` on every ply, a comment on inferred moves, FEN of each photo position in a comment; a separate FEN list for copying
- [ ] Lichess import of the PGN; games list shows photos and plies per game
- [ ] Update `README.md`, `docs/SETUP.md`, `CLAUDE.md`, `CLAUDE_HANDOFF.md` for the new app

**Validation (gate — do not start Phase 5 until these pass):**
- `.venv/bin/pytest -q` → `0 failed`
- `.venv/bin/python -m scripts.replay_game data/real/<first real game>` → writes `game.pgn` that `python -c "import chess.pgn,sys; g=chess.pgn.read_game(open(sys.argv[1])); assert not g.errors" game.pgn` accepts
- `.venv/bin/pytest -q tests/test_clock_sm.py` → `0 failed` (includes the hint rule: off before the first photo, on past the threshold, off under one minute, off after a photo)
- Manual: one real game with at least three photos, played and reviewed on the phone; the PGN imports into lichess without edits; note whether the glow was noticed and whether it distracted. Record what was wrong in Notes.

**Handoff (do before stopping):**
- [ ] Persist to `CLAUDE_HANDOFF.md`: field-test findings, open UI issues, where confirmed labels accumulate

**Resume with:** `/clear` → `/model opus` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `engine/reader.py`, `engine/read_eval.py`, `docs/PROTOCOL.md` (positions section) — and nothing else.

## Phase 5 — Labelled data and a grid finder
Everything the local reader needs before a model is trained: labels per set, and
squares cut reliably from hand-held photos.
**Execution:** `Opus` — data tooling and classical vision. Agents: none.
**Context:** carry into Phase 6 — it trains directly on this phase's crops and split files. A tier switch to Fable at the gate is still wanted; take it there.
- [ ] `static/label.html`: Fable drafts, Isaac confirms from the photo; confirmed game photos from Phase 2 flow into the same store automatically
- [ ] Label store `data/labels/<set>/`: photo, image-frame truth, home side, session date; splits are by session date, never by square
- [ ] Staged photos for rare pieces: queens, rooks and bishops on varied squares, several angles, both sets (needs Isaac; target 30 per set)
- [ ] `engine/gridfind.py`: four playing-area corners from a hand-held mid-game photo; must handle a mat that runs off the frame (pilot photo 2403) and the wooden board; returns a confidence that is low when it is wrong
- [ ] Corner truth for the labelled photos (four taps each in the label tool)
- [ ] Per-square crops from the unflattened photo, sized by square width, with the target square unambiguous (the pilot's crops leaked neighbouring pieces)

**Validation (gate — do not start Phase 6 until these pass):**
- `.venv/bin/pytest -q tests/test_gridfind.py` → `0 failed`
- `.venv/bin/python -m engine.gridfind --eval data/labels/green-mat` → `median corner error <= 0.10 squares`, `>= 0.90 of photos within 0.25 squares`, and every miss has confidence below the reported threshold
- `.venv/bin/python -m engine.gridfind --eval data/labels/wood-board` → same thresholds, or the set is marked "manual corners" in Notes with the measured numbers
- `.venv/bin/python -m engine.train_local --inventory` → prints photos, sessions and per-class counts per set; at least 60 labelled photos for the green mat

## Phase 6 — Local reader and the learning curve
Train a classifier that reads the board with no API key, measure how accuracy
grows with data, and offer it as a reader backend.
**Execution:** `Fable 5` — model and evaluation design. Agents: none.
**Context:** final phase.
- [ ] `engine/train_local.py`: per-square classifier on a pretrained backbone (frozen features plus a small head first; fine-tune only if the curve says so); torch as an optional `[local]` extra; runs on the Mac
- [ ] Board-level decoding: per-square probabilities combined under chess constraints (one king a side, piece counts, no pawns on back ranks) and, in a game, the previous confirmed position
- [ ] Learning curve: train on the earliest k sessions, test on later ones, for increasing k; report per-square, occupied-square and perfect-board accuracy, and the queen/rook and bishop/pawn confusions
- [ ] `engine/local_reader.py` behind the same interface as the Claude backend; `local` added to the settings toggle with its measured accuracy shown beside it
- [ ] Decide the default from the numbers and write the decision, and the photo count the curve projects for parity, into Notes

**Validation:**
- `.venv/bin/pytest -q` → `0 failed`
- `.venv/bin/python -m engine.train_local --curve data/labels/green-mat` → writes `data/models/green-mat/curve.json`; every test session is later than every training session
- `.venv/bin/python -m engine.read_eval data/labels/green-mat --split latest-sessions --model local` → prints the same table as for Claude; `local` becomes selectable only if `per-square >= 0.97`, and the default only if it is at least the Claude figure on the same split

## Final validation
- `.venv/bin/pytest -q` → `0 failed`
- One real game on one phone: clock run, at least three photos taken from the clock page, each confirmed with at most two corrected squares, lines solved for every gap, PGN imported into lichess without hand edits
- `.venv/bin/python -m engine.read_eval data/labels/green-mat --split latest-sessions --model local` → runs with `ANTHROPIC_API_KEY` unset

## Notes
- **Pilot, 2026-11-16** (10 green-mat photos, truth entered from a blank board): per-square accuracy, perfect boards, cost per photo — Fable 5.2 99.5 % / 8 / $0.10; Sonnet 5.5 98.9 % / 6 / $0.04; Opus 5.5 95.3 % / 3 / $0.06; Haiku 5.5 91.2 % on 8 usable / $0.003; Jev per-square with example crops 61.7 % / 0 / $0.017. The Jev crops leaked neighbouring pieces, so that figure is not a verdict on Jev; it is revisited only if Phase 6 wants a cheap second opinion.
- Fable's residual errors were queen read as rook and bishop read as pawn. In this set the queen is taller than the rook and has a dome top; pawn and bishop differ only in height.
- The reader was given a clean crop and a copy with a grid; on the photo whose grid was badly misplaced (2403) the Claude models still read every square, which is why Phase 1 tests dropping the grid.
- The September calibrator reported success on 36 of 41 hand-held photos while about ten outlines were actually on the board. Nothing in the new flow depends on it.
- The mat's printed coordinates are not reliable: the pieces are not always set up with White on the a1 side. Orientation comes from the reader's home-side call and can be rotated by hand.
- Photos include people and the repo is public: everything under `data/` stays untracked, including labels and trained weights.
- Engines: `lc0` and `stockfish` from Homebrew; Maia weights `maia-1100…1900.pb.gz` in `data/maia/` (v1.0 release of CSSLab/maia-chess). Maia bands are on the Lichess scale; the setting is on the chess.com scale and is converted. Maia-2 and Maia-3 exist and are a later comparison, not part of this spec.
- Long gaps are ambiguous by nature. With about three photos a game the line between them is a best guess, and the page must show it as one.
- Reader cost at three photos a game is about $0.30 on Fable.
- Decided 2026-11-16: one phone is clock and camera. A second-phone photo page is out of scope.
- The photo hint knows only the last confirmed position and the ply count, not the current board, so its threshold is a ply count. It is meant to be easy to miss; if it is never noticed in play, raise its contrast before adding anything louder.

## Amendments
