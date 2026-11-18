---
created: 2026-11-16                 # NEVER changes
modified: [2026-11-16, 2026-11-16, 2026-11-17, 2026-11-17, 2026-11-17, 2026-11-18, 2026-11-18]  # append-only
sessions: [2026-11-16, 2026-11-16, 2026-11-17, 2026-11-17, 2026-11-17, 2026-11-18, 2026-11-18]  # append-only; one entry per session that touches this spec
commits: [d0620c2, 2b1bb2e, 74704c3, e4c1b4d, fbff00e]               # append-only; short hashes, appended as phases land
back_refs: [planning/specs/2026-09-12-boardcam.md]
forward_refs: []
status: in-progress                 # draft | in-progress | done | abandoned
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
| 3 | Fable 5 → ran on Opus 5.5 | none | yes | New search and probability design |
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
- [x] Set the September spec to `abandoned` with an Amendments entry pointing here
- [x] Delete the files listed under Removed and their tests; `pytest` green on what remains
- [x] Move pilot data from `data/label-pilot/` to `data/labels/green-mat/` (photos + `truth.json`; stays gitignored)
- [x] `engine/reader.py`: Claude backend from the pilot prompt; structured output; model id is a parameter, default `claude-fable-5-2`; refusal and `max_tokens` handled as errors, not empty boards. Load the `claude-api` skill first.
- [x] Measure photo-only against photo-plus-grid on the 10 pilot photos and keep photo-only if it is within 0.5 points (it removes the grid dependency) — photo-only 99.7 % vs 99.5 %; **grid dropped** (see Notes)
- [x] `engine/position.py`: rotate the read by White's home side, set side to move, build the FEN, flag illegal positions (king count, pawns on back ranks, more than 16 a side, side not to move in check)
- [x] `engine/read_eval.py`: per-square, occupied-square, perfect-board and home-side accuracy, cost and seconds per photo, confusion pairs
- [x] Label the remaining usable green-mat photos (about 20) as a hold-out the prompt is never tuned on. Needs Isaac, from a blank board as in the pilot. — labelled 2026-11-17: 21 labelled, 1 excluded (2509, hand over board)
- [x] Hold-out gate first failed (per-square 0.903, home-side 0.905: two diagonal photos hit `max_tokens` at 16 000). Fixed by streaming with a 64 000 cap (Amendments 2026-11-17); passes at 0.996 / 1.000.
- [x] Write the reader and position interfaces into `docs/PROTOCOL.md` (§9)

**Validation (gate — do not start Phase 2 until these pass):**
- `.venv/bin/pytest -q` → `0 failed`
- `.venv/bin/python -m engine.read_eval data/labels/green-mat --split pilot --model claude-fable-5-2` → `per-square >= 0.990` (costs about $1)
- `.venv/bin/python -m engine.read_eval data/labels/green-mat --split holdout --model claude-fable-5-2` → `per-square >= 0.985`, `home-side >= 0.95`
- `git grep -n "tracker\|emission\|synth" -- '*.py' '*.js' '*.toml'` → no matches

**Handoff (do before stopping):**
- [x] Persist to `docs/PROTOCOL.md` and this spec's Notes: the final prompt variant, whether the grid was dropped, hold-out numbers, any photos excluded and why

**Resume with:** `/clear` → `/model opus` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `docs/PROTOCOL.md`, `server/app.py`, `server/storage.py`, `static/js/clock.js` — and nothing else.

## Phase 2 — Photo intake, settings, positions on the phones
A game can be created, clocked and photographed, and each photo comes back as a
FEN that can be corrected by tapping squares.
**Execution:** `Opus` — ordinary backend and page work. Agents: none — pieces share the wire contract.
**Context:** **CLEAR after this phase** — Phase 3 is engine math on a different surface; it needs the stored position format only.
- [x] Game creation on the index/clock page; one phone, so the room code, QR pairing and camera role are removed (room kept internally as the socket path)
- [x] Clock state (times, turn, press count, paused) is saved on the phone on every change and restored on reload. iOS can discard the page while the camera is open, and today a reload resets the clock.
- [x] Per-game settings (before start, editable later on review): player names, rating per player (default 1350 on the chess.com scale), reader model (Fable 5.2 default; Sonnet 5.5, Opus 5.5; `local` appears in Phase 6). Stored in `meta.json`; last used values become the defaults.
- [x] Camera button on the clock page: pauses the clock, opens the phone camera through a file input with capture (full-resolution photo), uploads, shows the returned board. The clock stays paused until a player resumes it; cancelling the camera leaves it paused too.
- [x] `POST /games/{id}/photos`: store `photos/NNN.jpg`, stamp with the clock's press count and side to move at the moment of the pause, run the reader in a worker thread, write `positions.json`, return the result to the phone
- [x] Tap-to-fix editor (`static/js/board.js`): change a square, rotate the board, confirm. A confirmed position stores the original read beside the corrections, which is the training label for Phase 5.
- [x] Ply stamp is adjustable by ±1 on confirm (photo taken after the move but before the press)
- [x] A photo with no board, or an illegal position, is reported on the phone and can be retaken; nothing is silently accepted
- [x] Confirming a read must not hold up the game: the board can be confirmed later from the review page, and play resumes with one tap
- [x] UI per the `kaczor-design` skill; rewrite `docs/PROTOCOL.md` §§ for photos, positions, settings; update `docs/SETUP.md`

**Validation (gate — do not start Phase 3 until these pass):**
- `.venv/bin/pytest -q` → `0 failed` (includes `tests/test_intake.py` with a stubbed reader: upload → stamped position → correction → confirmed FEN)
- `.venv/bin/python -m scripts.replay_photos data/labels/green-mat/2777.jpg --ply 24` → prints a FEN equal to the labelled one
- Manual, on the iPhone over the tunnel: create a game, press the clock 6 times, tap the camera button (clock pauses), take a photo, confirm the FEN shows `ply 6` and the right side to move, resume, and check both times are what they were at the pause. Repeat once after force-reloading the page mid-game. Record the result in Notes.

**Handoff (do before stopping):**
- [x] Persist to `docs/PROTOCOL.md`: the `positions.json` schema exactly as written, the settings keys, and anything iOS did that the page had to work around (§§4, 5, 7; the iOS list is from design and headless Chrome; add what the real iPhone test shows)

**Resume with:** `/clear` → `/model fable` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `docs/PROTOCOL.md` (positions and settings sections), `engine/position.py` — and nothing else.

## Phase 3 — Likely lines between positions
Given two confirmed positions and the number of plies between them, return the
most likely lines and, for every ply, how likely each candidate move is.
**Execution:** `Fable 5` — new search and probability design; the method is the deliverable. Agents: none — one coupled derivation.
**Context:** **CLEAR after this phase** — the derivation is spent once `engine/lines.py` and its README section exist; Phase 4 consumes `lines.json`.
- [x] `engine/humanlike.py`: lc0 with a Maia weight file at one node, policy read from verbose move stats, cached per FEN and band; the two players may use different bands
- [x] Rating → band: choose by which band best predicts each player's real moves in the chess-insights database, fitted on games before a cut-off date and checked on games after it; the per-game rating setting overrides
- [x] Opening prior: move frequencies from the players' own games for positions they have reached before, blended with Maia by sample size
- [x] `engine/lines.py`: best-first search from A for paths of exactly `n` plies ending in B, pruned by necessary conditions (material can only fall, pawns only advance, a lower bound on plies still needed), scored by the product of move probabilities; returns top lines with normalised probabilities and per-ply move marginals. Tries `n−1` and `n+1` when `n` yields nothing. Time-boxed; reports when it gave up.
- [x] Castling rights and en passant in the resulting FENs come from the chosen path
- [x] `engine/evalgraph.py`: Stockfish evaluation for every ply of a line, and Stockfish's top moves at a given ply (for the engine arrows)
- [x] `engine/lines_eval.py`: cut real games from the chess-insights database at known plies and score the solver by gap length (4, 8, 12, 16, 24): a path found, true line is top-1, true line in top-3, share of plies where the true move is the top marginal. Evaluation games are after the cut-off; nothing is tuned on them.
- [x] Photo hint threshold: from the accuracy-by-gap table, the number of plies since the last confirmed photo beyond which reconstruction is no longer dependable (true line outside the top 3 more often than not, or the search no longer finishes in its time box). One number per rating band, written to config. Fitted on games before the cut-off, reported on games after.
- [x] Document the method and the measured table in `engine/README.md`

**Validation (gate — do not start Phase 4 until these pass):**
- `.venv/bin/pytest -q tests/test_lines.py` → `0 failed` (includes: every returned path is legal, has exactly the requested length, and ends in B)
- `.venv/bin/python -m engine.lines_eval --gaps 4,8 --games 200 --split after-cutoff` → `path-found >= 0.98`, `true-line-top3 >= 0.60` at gap 4
- `.venv/bin/python -m engine.lines_eval --gaps 12,16,24 --games 200 --split after-cutoff` → `path-found >= 0.90` at gaps 12 and 16 and `>= 0.85` at gap 24 (amended 2026-11-18), median time per gap `<= 30 s`; top-1/top-3 recorded in Notes, no threshold (these gaps are expected to be ambiguous)
  - [x] 2026-11-17: gap 12 1.000 / 10.2 s, gap 16 0.970 / 17.1 s, gap 24 0.870 / 20.7 s. Gap 24 failed the original 0.90 and passes the amended 0.85 (see Amendments, 2026-11-18).
- `.venv/bin/python -m engine.lines_eval --check-leak` → `0 evaluation games before cut-off`
- `.venv/bin/python -m engine.lines_eval --hint-threshold` → prints one ply count per band, each between 6 and 60

**Handoff (do before stopping):**
- [x] Persist to `engine/README.md` and Notes: the `lines.json` schema, the chosen bands, the accuracy-by-gap table, what was tried and dropped

**Resume with:** `/clear` → `/model opus` → `/spec build planning/specs/2026-11-16-photo-positions.md`
Next phase reads: this spec, `engine/README.md` (lines section), `docs/PROTOCOL.md`, `static/js/board.js`, `static/js/photos.js`, `server/review.py`, `server/photos.py` — and nothing else.

## Phase 4 — Review: line, arrows, graph, export
The finished game is one page: photos with their FENs, the inferred moves
between them, and a PGN.
**Execution:** `Opus` — frontend and glue on interfaces that now exist. Agents: none.
**Context:** **CLEAR after this phase** — Phases 5–6 are a separate workstream (training data and models).
- [x] Photo hint on the clock page: a slow, low-contrast glow on the camera button once the plies since the last photo pass the Phase 3 threshold. Only after at least one photo has been taken in the game; never while either clock is under one minute (and it goes away if one drops below); cleared by the next photo. No sound, no text, no vibration.
- [x] After each confirmed photo, solve the gap from the previous confirmed position (or the start) in the background and push `lines.ready`
- [x] Review page: step through plies on a board; photo positions marked as confirmed, plies between as inferred
- [x] Arrows from the current ply: derived moves in one colour with thickness by marginal probability, Stockfish's top moves in a second colour; a legend; the pair chosen from the design system and checked for contrast in light and dark
- [x] Accepting a derived arrow pins that move and re-solves the rest of the gap; pins are stored like square corrections and can be undone
- [x] Evaluation graph over the whole game: solid through confirmed positions, visibly different between them; photo points marked; tapping jumps to the ply. Load the `dataviz` skill before drawing it.
- [x] `engine/pgn.py`: `%clk` on every ply, a comment on inferred moves, FEN of each photo position in a comment; a separate FEN list for copying
- [x] Lichess import of the PGN; games list shows photos and plies per game
- [x] Update `README.md`, `docs/SETUP.md`, `CLAUDE.md`, `CLAUDE_HANDOFF.md` for the new app

**Validation (gate — do not start Phase 5 until these pass):**
- `.venv/bin/pytest -q` → `0 failed`
- `.venv/bin/python -m scripts.replay_game data/real/<first real game>` → writes `game.pgn` that `python -c "import chess.pgn,sys; g=chess.pgn.read_game(open(sys.argv[1])); assert not g.errors" game.pgn` accepts
- `.venv/bin/pytest -q tests/test_clock_sm.py` → `0 failed` (includes the hint rule: off before the first photo, on past the threshold, off under one minute, off after a photo)
- Manual: one real game with at least three photos, played and reviewed on the phone; the PGN imports into lichess without edits; note whether the glow was noticed and whether it distracted. Record what was wrong in Notes.
  - [x] 2026-11-18: `pytest -q` 85 passed; `test_clock_sm.py` 1 passed (node suite 28, 5 hint tests).
  - [x] 2026-11-18: `scripts.replay_game` run on a **demo** game (built with `--demo` from a chess.com game, photos at 11/22/33/44) → `game.pgn` parses with no errors. The gate names the first *real* game, which does not exist yet.
  - [F] Manual real game and the replay on it — needs Isaac to play a game. Blocks Phase 5.

**Handoff (do before stopping):**
- [x] Persist to `CLAUDE_HANDOFF.md`: where confirmed labels accumulate, gotchas, current state (rewritten for the photo design)
- [F] Field-test findings and open UI issues — wait for the manual game

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
- [ ] Diagonal photos: about 5 new green-mat photos taken across a corner (new split `diagonal`, never the hold-out). Test a prompt that lets the reader take any board edge as the top when no row is clearly farthest; keep it only if it does not lose accuracy on pilot + diagonal and cuts output tokens and time (added 2026-11-17)
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
- **Phase 1 build, 2026-11-16.** Originals are `~/Downloads/chess training full/IMG_*.HEIC` (42 photos, 5712x4284, EXIF orientation 6; the `.mov` live-photo halves are ignored). Converted with `sips` to full-resolution JPEG. 32 are on the green mat (10 pilot + 22 hold-out: 2378 2481 2483 2508 2509 2516 2518 2555 2599 2771 2776 2778 2779 2796 2798 2799 2806 2807 2808 2846 3044 3045); 10 are on wooden boards (1788 2325–2328 2404–2406 2708 3013) and went to `data/labels/wood-board/` (split `unlabelled`) for Phase 5. Session = capture date from the HEIC metadata; the sets span 2025-10-18 to 2026-11-15.
- The pilot's truth is in the **upright** frame (OpenCV applied the EXIF rotation in `prep.py`); the reader applies `ImageOps.exif_transpose` so photo-only reads are in the same frame.
- Photo-only sends the whole photo at 2576 px long edge (the pilot's crops were 1568). Fable 5.2 accepts 2576.
- The pilot's per-model reads were seeded into the cache as `reads/<model>/crop-grid/`; re-scoring them with the new `read_eval` reproduces the pilot exactly (Fable 99.5 % / 8 of 10 / home 100 %; Sonnet 98.9 / 6; Opus 95.3 / 3), which checks the scorer.
- No server-side refusal fallback on the reader, on purpose: a read silently served by another model would be scored under the wrong name. Refusal is a `ReadError`.
- **Photo-only vs crop-grid, Fable 5.2, pilot split (2026-11-16):** photo-only per-square 0.997, occupied 0.991, perfect 9/10, home-side 1.000, fen-correct 0.9, $0.110 and 23 s per photo; crop-grid (pilot reads) 0.995 / 0.987 / 8/10 / 1.000 / 0.8, $0.101, 17 s. Photo-only is kept: no grid finder in the read path, and Phase 2 can read a photo as uploaded. Residual errors are bishop read as pawn (one white, one black); the queen-vs-rook miss did not recur at 2576 px. One run on 10 photos (640 squares, so 0.2 points is about one square): read it as "not worse", not as an improvement. The first attempt failed with 401 because this Claude Code session had inherited the key from before it was replaced in `~/.zprofile`; a fresh login shell's key works.
- **Hold-out, Fable 5.2, photo-only (2026-11-17):** 21 photos (2509 excluded: hand over board). Per-square 0.903, occupied 0.896, perfect 17/21, home-side 0.905, fen-correct 0.810, $0.163 and 49 s per photo. **Gate failed.** 2481 and 2799 (both taken diagonally) ended at `max_tokens` after 16 000 output tokens without an answer; scored as all 64 squares wrong, they account for the whole shortfall. On the other 19: 2 wrong squares (bishop read as pawn, queen read as rook), per-square about 0.998, home side 19/19. Likely cause: the prompt defines rows by "farthest from the camera", which has no answer on a diagonal photo, so the model keeps reasoning. A prompt change found here would be tuned on the hold-out, so it needs new diagonal photos to check it; a larger output budget is not a prompt change but costs more per read.
- **Hold-out after the 64 000-token cap (2026-11-17), gate passed:** only 2481 and 2799 were re-read (the other 19 cached reads are unchanged). Per-square 0.996, occupied 0.992, perfect 18/21, home-side 1.000, fen-correct 0.857, $0.206 and 60 s per photo. 2481 took 18 821 output tokens, $0.80, 202 s, and has 3 wrong squares (a black knight missed, a white knight read as black, a knight where there is none); 2799 took 9 665 tokens, $0.43, 116 s, and is perfect, so the same photo can finish well inside 16 000 on another run. Errors overall: bishop→pawn, queen→rook and the three knight squares on 2481.
- **Phase 2 build, 2026-11-17.** Gates: `pytest -q` 66 passed (13 in `test_intake.py`; node suite 23, incl. snapshot/restore and a JS↔Python `square_of` cross-check). `scripts.replay_photos data/labels/green-mat/2777.jpg --ply 24` → `r1b1k1nr/pppp1ppp/2q5/4P3/2BQ4/8/PPP2PPP/RN2K2R w KQkq - 0 13`, equal to the FEN built from the label, read through the label set's cache ($0). Headless Chrome smoke (390×844, stub reader on a local server): new game → 6 presses → camera button paused at `10:14 / 10:14`, photo stamped `ply 6 · White to move`, one square fixed and confirmed; reload while paused → `10:14 / 10:14`, Resume, ply 6; reload while running → White's clock had kept running (10:10). No console errors besides favicon 404s. Manual iPhone gate over the tunnel, 2026-11-17 (Isaac): **passed** for the clock half: after 6 presses the sheet showed `ply 6 · White to move`; the Photo button paused the clock and Resume brought back the paused times; a force-reload mid-game restored times, ply and run state. Nothing iOS-specific was reported. Not checked on the phone: a real read of a real board and its confirm (no game set up); that path is covered by `test_intake.py` and the headless smoke, and gets its first real-board test in the Phase 4 manual game.
- Upload waits for the read (the spec's "return the result to the phone") but the read is a shielded server task, so a dropped request does not lose it; it is also pushed as `position.read`. Photos are accepted after Stop, because the final position is usually photographed then.
- Glyphs: fonts disagree on which chess code points are hollow (headless Chrome drew Black's king hollow), so the board uses the solid glyph for both colours and colours White by CSS.
- The photo hint knows only the last confirmed position and the ply count, not the current board, so its threshold is a ply count. It is meant to be easy to miss; if it is never noticed in play, raise its contrast before adding anything louder.

- **Phase 3 build, 2026-11-17 (Opus 5.5).** Method, `lines.json` schema, full tables and the tried-and-dropped list are in `engine/README.md` (Lines section). Bands: chess.com rating + 250 → nearest Maia-1 band (1350 → 1600); opening prior from the player's own games blended at k = 5 (−0.99 vs −1.58 nats/move without). Fit split before-cutoff = 142 games 2024-12-24…2025-06-23; evaluation split = 768 games 2025-07-18…2026-11-16. After-cutoff, 200 cases per gap (found / top-1 / top-3 / median s): 4 → 1.000 / 0.915 / 0.985 / 0.0; 8 → 1.000 / 0.590 / 0.745 / 0.1; 12 → 1.000 / 0.350 / 0.505 / 10.2; 16 → 0.970 / 0.210 / 0.295 / 17.1; 24 → 0.870 / 0.020 / 0.030 / 20.7. Hint threshold (`hint_plies` in `engine/solver.json`): 1100 18, 1200 17, 1300 15, 1400 14, 1500 12, 1600 11, 1700 9, 1800 8, 1900 6 plies; only 1300–1600 seen in the fit. `pytest -q` 80 passed; `--check-leak` → 0 evaluation games before cut-off.

- **Phase 4 build, 2026-11-18 (Opus 5.5).** New `server/gaps.py` drives the solver per game: anchors (start + confirmed photos, latest photo per ply) → gaps, each cached in `lines.json` under a key of its inputs (start FEN, end placement, n, bands, prior flags, pins, solver constants), so one more photo solves one more gap; the next gap starts from the previous top line's `fen_end`. Pins force the move through a policy wrapper (`board.ply() == ply − 1` → only that move), no change to `engine/lines.py`. Solving is a background job per game (confirm, pin, rating/name change, `POST …/solve`), one at a time across games (shared lc0 + Stockfish), `lines.ready` pushed per gap. Timeline `certain` = photo, or reached from a certain position by accepted or forced moves (exhaustive search, all lines agree). Graph: solid between certain positions, dashed between inferred, dots at photos, clamp ±6. PGN: `inferred NN%` comments (">99%" instead of 100), `photo n: FEN` comments, `[%clk]` every ply, WhiteElo/BlackElo from the settings; an unsolved gap ends the move list with the later FENs in a comment. Demo game (chess.com game 2026-11-17, photos every 11 plies, 1350/1350): 4 gaps in 16.7 s, top-line probabilities 0.31/0.29/0.93/0.72, 29/44 plies equal to the true game. Pinning 2…e5 there (impossible against photo 1) correctly turns the gap to `none`, and the page then blames the accepted move, not the photo.
- **Arrow colours (2026-11-18)**, validated with the dataviz palette script: likely moves moss-50 `#3E5A32` solid, Stockfish terra-40 `#B96F3E` hollow, 2-unit paper ring on both. Pine vs lake failed the normal-vision floor (ΔE 10.2); pine vs signal orange failed CVD (protan 6.9). The chosen pair: CVD 12.6, normal 22.0, contrast ≥ 3 on the light square, WARN on the dark square (relieved by the ring, solid/hollow, legend, tables); chroma floor fails as for every film-palette hue. Dark-page pair moss-40/terra-30 passes CVD and contrast; the app has no dark theme.

## Amendments
- **2026-11-18 (Phase 4, files and scope).** The `Resume with` list was too thin: building the phase also needed `server/app.py`, `server/storage.py`, `static/review.html`, `static/js/review.js`, `static/js/clock.js`, `static/js/clocksm.js`, `static/clock.html`, `engine/pgn.py`, `engine/evalgraph.py`, `engine/lines.py` (Result/solve), `engine/humanlike.py` (Mover), and the old tests. New beyond Files: `server/gaps.py`, `scripts/replay_game.py` (with `--demo`, used because no real game exists yet), `pins.json`/`evals.json` per game. Removed: `server/analysis.py`, frame helpers in `storage.py`, the legacy `/analysis`, `/frames`, `/rect`, `/corrections`, `/verify` endpoints and `labels.json` (PROTOCOL §8 rewritten). `engine/pgn.py` `build_pgn` now takes `(meta, events, timeline)`; `pgn_from_sans` dropped (unused). The gate's manual game is open, so the phase is not closed: the CLEAR boundary is honoured, and the next session starts with the field test, not Phase 5.
- **2026-11-17 (Phase 2, scope and files).** The `Resume with` list was too thin: building the phase also needed `static/js/clocksm.js`, `static/clock.html`, `server/models.py`, `server/ws.py`, `engine/reader.py`, `engine/position.py`, `engine/read_eval.py` (cache reuse), the existing tests, and the review/games pages. Phase 4's list should name `static/js/photos.js`, `static/js/board.js` and `server/photos.py`. Changes beyond Files: `clocksm.js` gained `snapshot`/`restore` (listed as unchanged; the reload requirement is a state-machine concern and is tested there); new `server/photos.py` (intake logic, `READER` stub point) and `static/js/photos.js` (photos + settings section on the review page, which Phase 4 extends rather than replaces); `static/js/qr.js` and `tests/test_qr.py` removed with pairing; stop/flag no longer push `analysis.error` (nothing reads moves until Phase 3) and send `ply` instead of a capture `seq`; `scripts/serve.sh` refuses to start without `ANTHROPIC_API_KEY`. `scripts.replay_photos` accepts the gate's `data/labels/green-mat/2777.jpg` for the real `photos/2777.jpg`.
- **2026-11-16 (Phase 1, scoring frame).** `read_eval` scores in board coordinates, not the photo frame: a diagonal photo (2378, 2481, 2483, 2799, 3045) has no well-defined "row farthest from the camera", so a photo-frame label would be arbitrary. `per-square`, `occupied-square` and `perfect-board` take the read at the best-fitting of the four orientations; `home-side` scores the reader's own call; `fen-correct` (new) is the end-to-end share. Hold-out truth is a FEN placement, entered from a blank board on a page written by the new `scripts/label_page.py` (not in Files; the Phase 5 `static/label.html` still replaces it) and merged with `--merge`. The gate thresholds are unchanged.
- **2026-11-16 (Phase 1, removals).** `tests/test_calibrate.py` was removed with the others: it builds its frames with `synth`. `engine/calibrate.py` and `engine/rectify.py` stay (as planned) but are now covered only by `test_field_fixes.py::test_frame_checks…`. Server: `analysis.py` cut to the per-game registry plus a `finish` that reports "no reader yet"; `/camera`, the calibration push, per-press reading, manual corners and orientation matching are gone from `app.py`; corrections return 409 until the Phase 3 solver. `test_review.py` keeps payload, result, verify, lichess and finished-game tests on a fixture that writes `analysis.json` directly. `CLAUDE.md` now points at this spec (the stale `engine.evaluate`/`vision_llm` rules would have misled Phase 2).
- **2026-11-17 (Phase 1, output cap).** The hold-out gate failed on two diagonal photos that hit `max_tokens` at 16 000. Isaac chose both fixes: now, the reader streams (`messages.stream` + `get_final_message`) with `MAX_TOKENS` 64 000, which is not a prompt change and so is fair on the hold-out; later, a prompt for diagonal photos, tested only on new diagonal photos (task added to Phase 5). Only the two failed reads were redone. Phase 2 should expect a diagonal photo to take up to a few minutes and about $0.80, and show the phone that the read is still running.
- **2026-11-17 (Phase 3, tier, files, gate).** Ran on Opus 5.5, not Fable 5: Fable was dropped from Isaac's plan (downgrade logged; no rework yet attributable to it). New files beyond Files: `engine/humanlike.json` (fitted offset, k, cut-off), `engine/solver.json` (search constants, tune log, `hint_plies`), untracked `data/players.json` (player → online accounts for the opening prior), cache `data/lines_eval/`. Building it also read the chess-insights DB schema (`~/claude-projects/chess-insights/data/chess_insights.db`, env `CHESS_INSIGHTS_DB`) and the old `engine/README.md`. Definitions: path-found counts only lines of exactly `n` plies; times are measured with 3 solves in parallel, each with 3 lc0 processes. The first gated run had gap 24 at 0.735, with 47 of 53 misses mislabelled `none`; two fixes chosen on the before-cutoff split only (beam reserves half its width for positions nearest B; time planning with a safety margin and finishing past the deadline instead of returning nothing; a pruned beam is never `complete`) raised it to 0.870 — still under 0.90. The after-cutoff split has now been looked at twice; further changes must be chosen on before-cutoff. Phase 4's `Resume with` list should also name `static/js/photos.js` and `server/photos.py`.
- **2026-11-18 (Phase 3, gap-24 gate accepted).** Isaac chose to accept gap-24 path-found 0.870 rather than tune further: the threshold is now `>= 0.85` at gap 24 (`>= 0.90` stays for 12 and 16). Reasons: the photo hint fires at 11 plies for the default 1350 setting (6–18 across bands), so 24-ply gaps should be rare; and when a 24-ply path is found the true line is in the top 3 only 3 % of the time, so a higher found rate would not give a usable line. Alternatives considered and not taken: a 40 s time box, timing solves one at a time, and a bidirectional (meet-in-the-middle) search. The last is the real fix if long gaps turn out to be common in play; it needs a reverse move generator (un-captures, un-promotion, un-castling, en passant), a reversed pruning bound, and a join of two pruned frontiers, about as much code as `engine/lines.py`. Phase 4's manual game should note how far apart the photos actually were.
