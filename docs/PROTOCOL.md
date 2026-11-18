# BoardCam protocol

Authoritative contract between the phone page, the review page and the server.
Every later phase reads this file rather than the spec.

> **2026-11-18 — photo-positions spec, Phase 4.** One phone is clock and
> camera. §§1–7 describe that flow (game creation, settings, the clock socket,
> photo intake, positions). §8 is the review: the moves between photos, pins,
> the evaluation graph and the PGN. §9 is the reader and position interface
> from Phase 1. The September tracker's frames, rectified views, corrections
> and verify endpoints are gone.

Transport: HTTP (JSON + multipart) for state and uploads, one WebSocket for
clock events and pushes. All paths are relative to the server root, reached
through the ngrok tunnel over HTTPS. Every `fetch` and the WebSocket upgrade
send `ngrok-skip-browser-warning: 1`; only the first navigation per phone per
browser session hits the interstitial.

---

## 1. One phone

The clock page (`/clock`) creates the game, runs the clock, and takes the
photos. There is no pairing: the room code, QR code and camera role of the
September design are gone. A game still has a 4-character `room` (alphabet
`ACDEFGHJKLMNPQRTUVWXY34679`), used only as the WebSocket path; nobody types it.

Pages: `/` (entry), `/clock` (phone; `/clock?new` forces the new-game form),
`/games` (list), `/review?game=<id>` (moves, arrows, graph, PGN, photos, settings).

---

## 2. HTTP endpoints

### `GET /api/settings`
The new-game form's starting values and the reader choices:
```json
{"defaults": {"initial_ms": 600000, "increment_ms": 5000,
              "white_name": "White", "black_name": "Black",
              "white_rating": 1350, "black_rating": 1350,
              "reader_model": "claude-fable-5-2"},
 "reader_models": ["claude-fable-5-2", "claude-sonnet-5-5", "claude-opus-5-5"]}
```
`defaults` are the last values used (`data/settings.json`), else the built-ins.

### `POST /games`
Body: any subset of the `defaults` keys (§4). `422` on an out-of-range value or
an unknown reader. Response: `game_id`, `room`, `created_at`, `initial_ms`,
`increment_ms` and the five setting keys. Creates `data/games/<game_id>/` with
`meta.json`, an empty `events.jsonl` and `photos/`, and stores the body as the
next game's defaults.

### `PATCH /api/games/{game_id}/settings`
Body: any subset of `white_name, black_name, white_rating, black_rating,
reader_model`. Response: all five after the change. Also updates the defaults.
A changed reader applies to photos taken afterwards; a changed name or rating
re-solves the game's lines (§8).

### `GET /api/games`
```json
{"games": [{"game_id": "...", "room": "K4WQ", "created_at": 1789...,
            "status": "running", "result": null, "photos": 3, "confirmed": 2,
            "plies": 41, "white_name": "White", "black_name": "Black"}]}
```
Newest first. `status` ∈ `ready | running | paused | finished`. `photos`
excludes replaced photos; `plies` is the highest press count in the event log.

### `GET /api/games/{game_id}`
`meta.json` fields plus `events` (parsed log) and `positions` (§5).

### `DELETE /api/games/{game_id}`
Removes the game directory.

### Photo endpoints
`POST /games/{id}/photos`, `GET /games/{id}/photos/{n}`,
`GET /api/games/{id}/positions`, `POST /api/games/{id}/photos/{n}/confirm`,
`POST /api/games/{id}/photos/{n}/read` — see §5. Review endpoints — §8.

---

## 3. WebSocket

`GET /ws/{room}` (`?role=clock` is accepted and is the only role; any other
role is refused). Every message is a JSON object with a `type`; unknown types
are ignored, so later phases stay additive. A second socket for the same room
replaces the first (close code 4000), which makes reconnects idempotent.
Heartbeat: the page sends `{"type":"ping"}` every 10 s, the server answers
`{"type":"pong","t":<ms>}`; two missed pongs and the page reconnects with
backoff capped at 5 s.

### Clock → server (persisted to `events.jsonl` with `server_t`)

| Type | Payload | Meaning |
|---|---|---|
| `clock.config` | `initial_ms, increment_ms` | time control changed before start |
| `clock.start` | `seq, side, white_ms, black_ms, t` | White's first press (`seq` 1) |
| `clock.press` | `seq, side, white_ms, black_ms, t` | a press; `side` is the player **who just moved**; `seq` is the press count = ply count |
| `clock.pause` | `t` | paused (the camera button pauses first) |
| `clock.resume` | `t` | resumed |
| `clock.flag` | `ply, side, white_ms, black_ms, t` | `side` ran out of time; result follows |
| `clock.stop` | `ply, result, white_ms, black_ms, t` | game ended (`1-0`, `0-1`, `1/2-1/2`, `*`) |
| `clock.reset` | `t` | legacy; the page no longer sends it |

Status: start/press/resume → `running`, pause → `paused`, flag/stop →
`finished`. A `clock.start|press|pause|resume|flag|stop` on a finished game is
refused with `game.finished` and not persisted (a stale page must not write a
second game into the first one's log). Photos are still accepted after the
game is finished: the final position is usually photographed after Stop.

### Server → clock

| Type | Payload | Meaning |
|---|---|---|
| `hello` | `game_id, room, status, result, seq, config{initial_ms, increment_ms, white_name, black_name, white_rating, black_rating, reader_model}, hint_plies, t` | on connect |
| `position.read` | `game_id, photo` (a §5 entry) | a read finished (also returned by the upload request) |
| `lines.ready` | `game_id, gaps` (the §8 gap summaries) | a gap was solved (sent after each gap and when the job ends) |
| `game.finished` | `game_id, result` | a play event was refused |
| `pong` | `t` | heartbeat |

`hello.seq` is the highest press count in the log. The page takes it as a floor
only when its own saved state was lost (§7). `hello.hint_plies` is the photo
hint threshold (§7) for the band of the two players' mean rating, from
`engine/solver.json` `hint_plies` (Phase 3); `null` if the table is missing.

---

## 4. Settings (`meta.json`)

| key | type | default | meaning |
|---|---|---|---|
| `initial_ms` | int, 1 000–10 800 000 | 600 000 | base time per side |
| `increment_ms` | int, 0–600 000 | 5 000 | added to the mover on every press |
| `white_name`, `black_name` | str ≤ 40 | `White`, `Black` | |
| `white_rating`, `black_rating` | int, 100–3500 | 1350 | **chess.com scale**; Phase 3 converts to a Maia band per player |
| `reader_model` | `claude-fable-5-2` \| `claude-sonnet-5-5` \| `claude-opus-5-5` | `claude-fable-5-2` | reader for new photos; `local` arrives in Phase 6 |

`meta.json` also holds `game_id, room, created_at, status, result` and, once
imported, `lichess_url`. The last values a game was created or edited with are
kept in `data/settings.json` and become the next form's defaults.

---

## 5. Photos and positions

### `POST /games/{game_id}/photos`
`multipart/form-data`:

| field | required | meaning |
|---|---|---|
| `file` | yes | the photo as taken (full resolution). JPEG is stored byte for byte, EXIF kept; any other format Pillow opens (PNG, …) is re-encoded upright as JPEG; anything else is `415` |
| `ply` | yes | the clock's press count when the camera button was tapped (≥ 0) |
| `turn` | no | `white`\|`black`, the side to move at the pause |
| `white_ms`, `black_ms` | no | remaining times at the pause |
| `retake_of` | no | `n` of an unconfirmed photo this one replaces (`404` if none, `409` if it is confirmed) |

The server stores `photos/NNN.jpg`, appends a `reading` entry, runs the reader
in a worker thread and **waits for it** before answering with the finished
entry (seconds; up to about three minutes for a diagonal photo). The read runs
as a shielded task: if the phone's request dies, the read still completes,
lands in `positions.json` and is pushed as `position.read`. A failed read is
not an HTTP error: the response is `200` with `status: "error"`.

### `positions.json`
```json
{"game_id": "20261117-201500-a1b2",
 "photos": [
  {"n": 1,                         // 1-based, never reused
   "file": "photos/001.jpg",
   "t": 1794900797.3,              // server time of the upload
   "ply": 6,                       // the clock's press count at the pause
   "turn": "white",                // side to move per the clock, or null
   "white_ms": 581000, "black_ms": 590500,
   "model": "claude-fable-5-2",    // reader used (the game's setting at upload)
   "status": "confirmed",          // reading | read | error | confirmed | replaced
   "retake_of": null,              // n this photo replaced
   "replaced_by": 2,               // only on a replaced photo
   "read": {                       // the reader's BoardRead (§9), or null
     "rows": ["R..K..NR", "..."],  // photo frame, 8×8
     "home": "top", "backend": "claude", "model": "claude-fable-5-2",
     "cost": 0.088, "secs": 13.8, "tokens_in": 5507, "tokens_out": 1100, "extra": {}},
   "error": null,                  // or {"reason", "detail", "retry"}
   "position": {                   // engine.position.build_position of the read at `ply`
     "fen": "…", "home": "top", "home_guessed": false, "flags": [], "legal": true},
   "confirmed": {                  // null until a player confirms
     "rows": ["Q..K..NR", "..."],  // corrected photo-frame rows
     "home": "top",                // corrected White home side ("Rotate")
     "ply": 6,                     // clock ply ±1
     "fen": "…",                   // the position of record for this photo
     "corrections": [{"r": 0, "c": 0, "read": "R", "now": "Q"}],
     "home_changed": false, "ply_changed": false,
     "t": 1794900801.2}}
 ]}
```

- Side to move is `ply` parity (even → White) for both the read's `position`
  and the confirmed FEN; `fullmove = ply // 2 + 1`. Castling rights are the
  §9 guess; en passant is never set. Phase 3 replaces both from the path.
- `read` is never overwritten by a confirmation. `read.rows/home` beside
  `confirmed.rows/home` is the training label Phase 5 collects; `corrections`
  are photo-frame cells, so a rotation alone is `home_changed` with no
  corrections.
- `error.reason`: the reader's (`refusal`, `max_tokens`, `no_board`,
  `bad_shape`, `bad_symbol`, `api_<status>`, `rate_limited`, `connection`) or
  `server_error` (a reader bug, caught so a photo never stays `reading`).
  `error.retry` is true for transport failures, which `…/read` can redo.
- A read whose board is impossible is `status: "read"` with
  `position.legal: false` and its `flags`: shown, never silently accepted.

### `POST /api/games/{game_id}/photos/{n}/confirm`
Body `{"rows": [8 strings of 8], "home": "top|bottom|left|right", "ply": int}`.
`ply` must be within 1 of the photo's clock `ply` (`400` otherwise). Response:
the updated entry. `422` with `{"detail": {"message", "flags", "fen"}}` if the
board is impossible (nothing is stored); `409` on a replaced photo; `400` on a
malformed board. A confirmed photo can be confirmed again (an edit). A photo
whose read failed can be confirmed too: the board is entered by hand and every
piece counts as a correction.

### `POST /api/games/{game_id}/photos/{n}/read`
Re-runs the reader on a photo in `error` (`409` otherwise). Same response as
the upload.

### `GET /games/{game_id}/photos/{n}`, `GET /api/games/{game_id}/positions`
The stored JPEG; the whole `positions.json` (`{"game_id", "photos": []}` before
the first photo).

---

## 6. Storage layout

```
data/
  settings.json      # last-used settings (§4)
  games/<game_id>/
    meta.json        # §4
    events.jsonl     # clock events, one JSON object per line, server_t stamped
    photos/001.jpg   # as uploaded
    positions.json   # §5
    lines.json       # §8: one solved gap per pair of consecutive anchors (engine/README.md)
    pins.json        # §8: moves the player accepted
    evals.json       # §8: Stockfish per FEN (evals) and top moves per FEN (top)
    game.pgn         # §8: written after every solve and result change
```

`game_id` is `YYYYMMDD-HHMMSS-xxxx` (local time, 4 random hex), and is
pattern-checked on every request so it cannot escape the data directory.

---

## 7. The clock page

**Time.** The page owns the clock (`static/js/clocksm.js`). Times come from
`performance.now()` deltas; the display re-renders on `requestAnimationFrame`.
The increment goes to the player who just pressed; only the side to move can
press; a press while paused is ignored. The press count is the ply count.

**Persistence.** On every press, pause, resume, stop, flag and new game the
page saves `ClockSM.snapshot(performance.now(), Date.now())` to
`localStorage["boardcam.clock.<game_id>"]` (with `seqBase` and `flagSent`),
and the game's `{game_id, room}` to `localStorage["boardcam.game"]`. On load,
`ClockSM.restore` rebuilds it: a paused clock returns with the paused times; a
running clock charges the wall time that passed to the side to move (a clock
that ran out meanwhile flags on the next frame). Wall time is used because
`performance.now()` restarts with the page. If local storage is unavailable
the clock still runs but a reload starts it over; `hello.seq` then keeps the
ply numbering continuous.

**Camera button.** One tap: pause (inside the tap, before the camera opens),
stamp `{ply, turn, white_ms, black_ms}`, then `input[type=file][accept=image/*]
[capture=environment].click()` — the click must stay inside the user gesture on
iOS. A full-resolution photo comes back through `change`; cancelling fires
nothing and the clock simply stays paused. The upload shows a sheet with the
photo, an elapsed timer while reading, then the editor. **Resume play** is on
the sheet at all times and resumes the clock in one tap; an unconfirmed board
is confirmed later from the strip ("n to confirm") while paused, or from the
review page.

**What iOS made the page do.**
- Safari may discard the page while the camera is open; that is why the clock
  persists itself and pauses *before* opening the camera. The photo of a
  discarded page is lost (the `change` event never arrives) and is retaken.
- An upload request that dies is followed by a look at `positions` before it is
  reported lost, since the server may have the photo and be reading it.
- `visibilitychange` → visible refetches positions, so a read that finished
  while the page was hidden shows up.
- Chess glyphs: the solid symbol for both colours, coloured by CSS, with
  U+FE0E so the pawn is not drawn as an emoji; fonts disagree on which code
  points are hollow.
- Native `alert()`/`confirm()` are never used; dialogs go through `modal()`.
- The page repaints from one rAF loop; a backgrounded tab freezes until it is
  foregrounded (harmless: the clock phone is in front all game).

**Photo hint.** The camera button glows (a slow 3.2 s pulse of a pale ring on
the dark control bar; a static ring under reduced motion) when
`ClockSM`'s `photoHint()` says so: at least one photo taken in this game, the
clock running or paused, both clocks at one minute or more, and
`ply − (highest ply of any non-replaced photo) ≥ hello.hint_plies`. The next
photo clears it. No sound, no text, no vibration. The default 1350/1350 game
glows after 11 plies.

---

## 8. Review: the moves between the photos

The review page (`/review?game=<id>`) works on a Mac or a phone, during or
after the game. It shows the game as one sequence of plies along the most
likely line between the photos, the likely next moves and Stockfish's as
arrows, an evaluation graph, and the PGN.

**Anchors and gaps.** Anchors are the initial position (ply 0) and every
confirmed photo (`confirmed.ply`, `confirmed.fen`; the latest photo wins when
two share a ply), in ply order. Each consecutive pair is a gap, solved by
`engine.lines.solve` under the game's two players (`engine.humanlike`,
ratings and names from `meta.json`); method and the stored per-gap result:
`engine/README.md` → Lines. A gap starts from the previous gap's top-line
`fen_end` (castling and e.p. from the path; its ply from that FEN), or from the
previous photo's FEN when that gap was not solved. Each gap carries a `key`
over everything its answer depends on (start FEN, end placement, `n`, bands,
prior flags, pins inside it, solver constants); a solve reuses every gap whose
key is unchanged, so one more photo solves one more gap.

**Solving** runs in the background, one job per game; a request while it runs
makes the job go round once more. Triggers: a confirmed photo, a pin added or
removed, a changed name or rating, and `POST …/solve`. lc0 and Stockfish are
shared by all games (one job at a time). After each newly solved gap the
server writes `lines.json` and pushes `lines.ready`; at the end it evaluates new
positions (`evals.json`) and rewrites `game.pgn`.

### `GET /api/games/{game_id}/review`
```json
{"game_id": "…", "room": "K4WQ", "status": "finished", "created_at": 1794…,
 "white_name": "Isaac", "black_name": "Guest", "white_rating": 1350, "black_rating": 1350,
 "reader_model": "claude-fable-5-2", "result": "0-1", "lichess_url": null,
 "plies": 52,                    // highest clock press
 "last_photo_ply": 44,
 "solving": false,               // a solve job is running
 "stale": false,                 // lines.json does not match the confirmed photos yet
 "positions": {…},               // §5
 "gaps": [{"from": {"photo": null, "ply": 0, "fen": "…"}, "to": {"photo": 1, "ply": 11, "fen": "…"},
           "status": "found", "complete": true, "n_requested": 11, "n_used": 11,
           "lines": 5, "top_prob": 0.31, "secs": 3.1, "bands": {"white": 1600, "black": 1600},
           "prior": {"white": false, "black": false}, "pins": 0}],
 "timeline": [ … ],              // below
 "pins": [{"ply": 3, "uci": "g1f3", "san": "Nf3", "t": 1794…}],
 "fens": [{"photo": 1, "ply": 11, "fen": "…"}],
 "pgn": "[Event \"BoardCam\"] …"}
```
`gaps[].status`: `found`, `none` (no legal path of `n − 1…n + 1` plies: a board,
its ply stamp, or an accepted move is wrong), `timeout`, or `order` (the photo's
ply is before the end of the line leading to it).

**`timeline`** — one entry per known position, in ply order (`server.gaps.timeline`):

| field | meaning |
|---|---|
| `ply`, `fen` | position after `ply` plies (FEN with castling/e.p. from the path) |
| `uci`, `san` | the move that led here (`null` at the start and after an unsolved gap) |
| `kind` | `start`, `photo`, `inferred`, `pinned` |
| `photo` | photo number when this position is a confirmed photo |
| `p` | marginal probability of that move among the lines found (`null` for anchors after an unsolved gap) |
| `certain` | a photo position, or reached from a certain one by accepted or forced moves (forced: the search was exhaustive and every line found plays it) |
| `alts` | the marginals of the move *into* this position: `[{"uci", "san", "p"}]`, `san` may be `null` |
| `break` | true for a photo position after an unsolved gap |
| `eval` | `{"cp", "mate"}` from White's side (Stockfish depth 12), or `null` until evaluated |

The arrows at ply `i` are the `alts` of entry `i + 1` (the move out of the
current position), unless that entry is a `break`.

### `POST /api/games/{game_id}/solve`
Starts (or re-runs) the background solve; answers `{"ok": true, "solving": true}`
at once. The page calls it when the payload is `stale` and nothing is solving.

### `GET /api/games/{game_id}/engine/{ply}`
`{"ply", "fen", "moves": [{"uci", "san", "cp", "mate"}]}` — Stockfish's 3 best
moves at the timeline position of `ply` (depth 12, cached per FEN in
`evals.json`). `404` when no position is known at that ply.

### `POST /api/games/{game_id}/pins`, `DELETE /api/games/{game_id}/pins/{ply}`
Body `{"ply": 12, "uci": "g1f3"}`: the move played *as* ply 12 (1-based, the
same count as the clock's presses). The move must be legal on the timeline's
board at ply 11 (`400`), and `ply` must lie inside the photos (`409`).
Response `{"pins": [...]}`; the gap is solved again with that ply forced (the
solver's policy returns only the pinned move there). Delete undoes one pin
(`404` if not pinned). Stored in `pins.json` as `{"pins": [{"ply", "uci", "san", "t"}]}`.

### `POST /api/games/{game_id}/result`
`{"result": "1-0"}` (`1-0`, `0-1`, `1/2-1/2`, `*`; else `400`). Rewrites
`meta.json` and `game.pgn`; response `{"ok": true, "result", "pgn"}`.

### `GET /games/{game_id}/pgn`
The PGN as a download (written from `lines.json` if needed; no solving).
`engine/pgn.py`: headers Event, Date, White, Black, Result, TimeControl,
WhiteElo/BlackElo (the settings), Annotator; every move has `[%clk h:mm:ss]`
from the clock press with the same ply number; an inferred move adds
`inferred NN%` (its marginal; `>99%` rather than 100 %, since it is a share of
the lines found, not certainty); a photo position adds `photo n: <FEN>`. An
unsolved gap ends the move list (lichess imports only legal games) with a
comment listing the later photo FENs. Plies after the last photo are not in
the PGN.

### `POST /api/games/{game_id}/lichess`
Imports the current PGN and returns `{"url": "https://lichess.org/…"}`
(`{"url": null}` if lichess refused or the network is down). The URL is cached
in `meta.json`, so a second press returns the same game. A PGN changed after
the import is not re-imported.

### `DELETE /api/games/{game_id}`
Deletes the game directory (§2). The review page asks first through `modal()`.

### Arrows and graph (page)
Likely moves are solid moss-50 `#3E5A32` arrows, width 8 + 26·p (of a 100-unit
square); Stockfish's are hollow terra-40 `#B96F3E` outlines; every arrow has a
2-unit paper ring. Validated with the dataviz palette script against both board
squares: lightness, CVD (ΔE 12.6 protan) and normal vision (ΔE 22.0) pass;
contrast passes on the light square and warns on the dark one, which the ring,
the hollow/solid encoding, the legend and the move tables relieve; the chroma
floor fails as it does for every film-palette hue. A dark page would use
moss-40 `#5F784A` / terra-30 `#D29972` (passes CVD and contrast on stone-90);
the app has no dark theme yet. The graph is one series in ink: solid segments
between certain positions, dashed between inferred ones, a dot at each photo,
nothing across an unsolved gap, clamped at ±6 pawns (mate at the clamp); hover
shows ply, move and eval, a tap jumps to the ply.

---

## 9. Reading a position from a photo (photo-positions spec, Phase 1)

Two engine modules. The server calls them through `server/photos.py` (§5).

### `engine.reader.read_position(image, backend="claude", model="claude-fable-5-2", *, grid_image=None, max_edge=2576, effort=None, client=None) -> BoardRead`

- `image`: a path, `bytes` or PIL image. EXIF orientation is applied (iPhone
  photos are stored sideways with orientation 6), then the long edge is capped
  at `max_edge` (2576 px, the current Claude models' maximum) and the photo is
  sent as JPEG q90.
- Default variant is **photo-only**: the whole photo, no grid, no crop, so no
  grid finder is needed. `grid_image` selects the pilot's variant (a crop of the
  mat plus the same crop with a labelled grid); it needs four corners and exists
  for comparison only. Photo-only ships: on the pilot it read 99.7 % of squares
  against 99.5 % for crop-grid (Fable 5.2, spec Notes 2026-11-16). On the
  21-photo hold-out (2026-11-17; 2509 excluded, a hand over the board) it read
  99.6 % of squares, 18 of 21 boards perfect, White's side right on all 21, at
  about $0.21 and 60 s a photo.
- The request is streamed with `max_tokens` 64 000 (`reader.MAX_TOKENS`). At
  16 000, two diagonal hold-out photos ran out before answering; with 64 000
  they took 19 k and 10 k output tokens ($0.80 and $0.43, up to 200 s). A
  diagonal photo is the slow, expensive case.
- `BoardRead`: `rows` (8 strings of 8, `KQRBNPkqrbnp.`, **photo frame**: row 0
  is the row of squares farthest from the camera, column 0 the leftmost),
  `home` (side of the photo where White's home rank is: `top|bottom|left|right|unknown`),
  `backend`, `model`, `cost` (dollars, from `reader.PRICES`), `secs`,
  `tokens_in`, `tokens_out`. `to_json()` gives the dict that is cached and stored.
- Failure is `ReadError(reason, detail)`, never an empty board. Reasons the
  model produced (cached and scored as a wrong board): `refusal`, `max_tokens`,
  `no_board`, `bad_shape`, `bad_symbol`. Transport reasons (not cached, retry):
  `api_<status>`, `rate_limited`, `connection`.
- No server-side model fallback: a read is always by the model named.
- `backend="local"` arrives in Phase 6 with the same signature.

### `engine.position.build_position(rows, home, white_to_move=True, fullmove=1) -> Position`

- Rotates the photo-frame read into board coordinates by White's home side
  (`square_of(r, c, home)`); `home="unknown"` is replaced by `guess_home(rows)`
  (White's men low, Black's high, no pawns on back ranks) and marked
  `home_guessed`.
- `Position`: `fen`, `home` (used), `home_guessed`, `flags`, `legal`
  (= no flags). Flags: `no_white_king`, `no_black_king`, `too_many_kings`,
  `too_many_white_pawns`, `too_many_black_pawns`, `pawns_on_back_rank`,
  `too_many_white_pieces`, `too_many_black_pieces`,
  `side_not_to_move_in_check`, `too_many_checkers`, `impossible_check`.
- Castling rights are a guess (king and rook on their home squares); the line
  solver sets the real ones from the path. En passant is never set. Halfmove 0.
- Side to move comes from the clock (press count parity at the photo);
  `fullmove_of(ply)` gives the move number after `ply` half-moves.
- `rotate_rows(rows, k)` turns a photo-frame grid clockwise `k` quarter turns —
  what the phone's "rotate" control does before confirming.

### Label sets (`data/labels/<set>/`, never committed)

`photos/<name>.jpg` (full resolution, EXIF orientation kept), `truth.json`
`{name: {fen | rows+home, split, session (capture date), done, excluded}}`,
`reads/<model>/<variant>/<name>.json` (cached `BoardRead`s or model errors),
`pilot/` (the 2026-11-16 pilot's scripts and crops). Hold-out truth is a FEN
placement (board frame), entered on `label-holdout.html` from
`python -m scripts.label_page <set> --split holdout` and folded back with
`--merge`. `engine.read_eval` scores in board coordinates (see its docstring):
`per-square`, `occupied-square`, `perfect-board` at the best-fitting
orientation, `home-side`, `fen-correct`, cost and seconds per photo, confusions.
