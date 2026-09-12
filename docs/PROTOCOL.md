# BoardCam protocol

Authoritative contract between the two phone pages and the server. Written in
Phase 1; every later phase reads this file rather than the spec.

Transport: HTTP (JSON + multipart) for state and uploads, one WebSocket per
client for events. All paths are relative to the server root, which is reached
through the ngrok tunnel over HTTPS.

---

## 1. Roles and pairing

Two clients per game:

| Role | Device | Page | Responsibility |
|---|---|---|---|
| `camera` | phone on the stand | `/camera` | creates the game, holds the video stream, uploads frames |
| `clock` | phone between the players | `/clock` | runs the chess clock, emits every press |

The **camera creates the game** (it is the device that is already set up and
aimed). `POST /games` returns a `game_id` and a 4-character `room` code. The
camera page displays the room code plus a QR code linking to
`/clock?room=<ROOM>`. The clock phone scans it or types the code.

Room codes are 4 characters from the alphabet `ACDEFGHJKLMNPQRTUVWXY34679`
(no `B/8`, `I/1`, `O/0`, `S/5`, `Z/2`). They are unique among games that are not
finished; a finished game's code may be reused.

### Reconnect semantics

A client joins with `GET /ws/{room}?role=clock|camera`. If a socket for that
role is already open in that room, **the new socket wins**: the server closes
the old one with code 4000 (`replaced`) and installs the new one. This makes
reconnect after a phone sleeps or a tunnel blip safe and idempotent.

On join, the server immediately sends a `hello` frame (§3.3) carrying the full
current game state, so a reconnecting client can restore itself without asking.

Heartbeat: each client sends `{"type":"ping"}` every 10 s; the server replies
`{"type":"pong","t":<server_ms>}`. A client that misses two consecutive pongs
should tear down its socket and reconnect with exponential backoff capped at 5 s.

---

## 2. HTTP endpoints

### `POST /games`
Body (JSON, all optional):
```json
{"initial_ms": 600000, "increment_ms": 5000, "white_name": "White", "black_name": "Black"}
```
Response `200`:
```json
{"game_id": "20260912-143355-a7f3", "room": "K4WQ", "created_at": 1789...,
 "initial_ms": 600000, "increment_ms": 5000}
```
Creates `data/games/<game_id>/` and writes `meta.json`.

### `POST /games/{game_id}/frames/{seq}/{k}`
`multipart/form-data`, single field `file`, a JPEG. `seq` is the capture
sequence number (§4), `k` the burst index `0..2`. Response `200`:
```json
{"ok": true, "seq": 3, "k": 0, "bytes": 148231, "path": "frames/0003_0.jpg"}
```
An upload for a `seq`/`k` that already exists **overwrites** it (frame 0 is
re-uploaded every 2 s before the game starts). Rejects non-JPEG bodies with
`415` and unknown `game_id` with `404`.

Side effect: the server fans out `frame.ok` (§3.2) to the clock socket.

### `GET /games`
```json
{"games": [{"game_id": "...", "room": "K4WQ", "created_at": 1789...,
            "status": "running", "result": null, "frames": 42, "plies": 41,
            "white_name": "White", "black_name": "Black"}]}
```
Newest first. `status` is one of `ready | running | paused | finished`.

### `GET /games/{game_id}`
Full detail: the `meta.json` fields, the parsed event list, the frame inventory
(`{"seq": 3, "k": [0,1,2]}`), and `analysis` (`null` until Phase 3 writes it).

### `GET /games/{game_id}/frames/{seq}/{k}`
Returns the stored JPEG (`image/jpeg`). Used by the review page in Phase 5.

### `DELETE /games/{game_id}`
Removes the game directory. Used by the games list.

### Static pages
`/` → `static/index.html`, `/clock` → `clock.html`, `/camera` → `camera.html`,
`/games` → `games.html`, `/review` → `review.html` (Phase 5). Assets are served
from `/static/...`.

### ngrok header
Every `fetch` and the WebSocket upgrade must send `ngrok-skip-browser-warning: 1`
so the tunnel does not return its interstitial HTML to a programmatic request.
Browsers still see the interstitial once per session on first navigation; that
is a one-time click-through per phone.

---

## 3. WebSocket messages

`GET /ws/{room}?role=clock|camera`. Every message is a JSON object with a
`type`. Unknown types are ignored, not errors — this keeps later phases additive.

### 3.1 Clock → server (persisted to `events.jsonl` and forwarded)

| Type | Payload | Meaning |
|---|---|---|
| `clock.config` | `initial_ms, increment_ms, white_name, black_name` | settings changed before start |
| `clock.start` | `t` | white's first press; **triggers capture `seq=1`** |
| `clock.press` | `seq, side, white_ms, black_ms, t` | a press; `side` is the player **who just moved** |
| `clock.pause` | `t` | clock paused; **no capture** |
| `clock.resume` | `t` | clock resumed; **no capture** |
| `clock.flag` | `side, t` | `side` ran out of time; triggers a final capture |
| `clock.stop` | `result, t` | game ended (`1-0`, `0-1`, `1/2-1/2`, `*`); triggers a final capture |
| `clock.reset` | `t` | clock reset to `ready`; the game is left finished, a new game must be created |

`t` is the client's `Date.now()` at the moment of the event. The server also
stamps `server_t` on every persisted event; the engine uses `server_t` for
frame association and the clock's own `white_ms`/`black_ms` for `%clk`.

`seq` is assigned by the **clock**, starting at 1 for `clock.start` and
incrementing by 1 on every subsequent press, flag or stop that captures. The
clock is the sole authority on `seq`; the server does not renumber.

### 3.2 Server → clock

| Type | Payload | Meaning |
|---|---|---|
| `hello` | see §3.3 | sent on connect |
| `peer` | `role, connected` | the camera connected or dropped |
| `frame.ok` | `seq, k, bytes` | a burst frame landed on disk |
| `calibration` | `ok, score, warning` | board detection result (Phase 3 fills this; Phase 1 never sends it) |
| `analysis.ready` | `pgn, lichess_url, flagged` | tracker finished (Phase 3/5) |
| `pong` | `t` | heartbeat reply |

### 3.3 Server → camera

| Type | Payload | Meaning |
|---|---|---|
| `hello` | `game_id, room, status, seq, config, peer_connected` | connect / reconnect state |
| `peer` | `role, connected` | the clock connected or dropped |
| `capture` | `seq, reason` | **grab a burst now**; `reason` ∈ `start | press | flag | stop` |
| `calibration` | `ok, score, corners, warning` | Phase 3 overlay data |
| `pong` | `t` | heartbeat reply |

The camera never sends game events. It may send `camera.status`
(`{streaming, w, h, error}`) which the server forwards to the clock as `peer`
metadata — used to light the "paired ●" indicator honestly.

---

## 4. Capture policy

- **Before the game starts**, the camera uploads the current frame as
  `seq=0, k=0` every 2 seconds, overwriting. This is the start-position frame
  and it is always the freshest view of an untouched board.
- **On `capture{seq}`**, the camera grabs three frames from the live `<video>`
  element at **+0 ms, +400 ms, +900 ms** after the command arrives, uploading
  each as `k=0,1,2`. The burst is **cut short** if another `capture` arrives
  first: remaining grabs are cancelled and the new sequence begins. The engine
  tolerates a short burst.
- Encoding: JPEG quality 0.85, long edge scaled to 1280 px, aspect preserved.
- `clock.pause` and `clock.resume` do **not** capture.
- `clock.stop` and `clock.flag` **do** capture, because the last move of a game
  is often played without pressing the clock.

### Sequence ↔ ply

In the normal case capture `seq = n` shows the position **after ply n**, and
`seq = 0` is the start position. The tracker never assumes it: a missed press
means one frame advanced two plies, a double press means one frame advanced
none. Both are candidates in the emission model (Notes §C of the spec).

---

## 5. Storage layout

```
data/games/<game_id>/
  meta.json        # game_id, room, created_at, config, status, result
  events.jsonl     # one JSON object per line, append-only, server_t stamped
  frames/
    0000_0.jpg     # start position (overwritten until clock.start)
    0001_0.jpg     # after ply 1, burst candidate 0
    0001_1.jpg
    0001_2.jpg
    0002_0.jpg
    ...
  calibration.json # Phase 2/3
  analysis.json    # Phase 3
  game.pgn         # Phase 3
  labels.json      # Phase 5 corrections
```

`game_id` is `YYYYMMDD-HHMMSS-xxxx` (local time, 4 random hex). Frame filenames
are zero-padded to 4 digits so they sort lexically. The **synthetic corpus uses
the identical layout** (`frames/`, `events.jsonl`) so `engine.evaluate` runs
unchanged on real and synthetic games.

`events.jsonl` lines are the clock messages verbatim plus `server_t`, e.g.
```json
{"type":"clock.press","seq":4,"side":"black","white_ms":574210,"black_ms":588930,"t":1789...,"server_t":1789...}
```

---

## 6. Client state expectations

**Clock page.** Owns the clock. Times come from `performance.now()` deltas, not
from counting ticks; the display re-renders on `requestAnimationFrame`. The
increment is added to the player who just pressed. Pressing is only legal for
the side to move. A press while `paused` is ignored.

**Camera page.** Owns the stream. Re-requests the wake lock on
`visibilitychange`. If `getUserMedia` fails or the stream ends, it shows a
"Restart camera" button — a user gesture is required again on iOS. It keeps
uploading frame 0 until the first `capture` arrives.

Neither page is authoritative about the other; both reconcile from `hello`.

---

## 7. Phase 1 implementation notes

Written when Phase 1 landed. These are the decisions the spec left open, and
the behaviours later phases can rely on.

### Capture sequence numbering

`seq` is assigned by the clock and is **not** the state machine's press count.
The clock keeps a `seqBase` offset that it raises to the server's count on
every `hello`, so a clock page that reloads or reconnects mid-game continues
the numbering rather than restarting at 1 and overwriting frames 1..n. A
`clock.reset` clears the offset.

`clock.stop` and `clock.flag` also carry a `seq` — they capture a frame, so
they must. Their seq continues from the last press. The server falls back to
`max(seq in events) + 1` if a capturing event arrives without one, because
losing the final frame of a game is worse than guessing its number; the tracker
tolerates a frame that advanced no plies.

A reloaded clock page does **not** restore the players' remaining times. Only
the frame numbering survives a reload. Full mid-game clock resume is out of
scope for V1.

### Events the clock actually sends

`clock.start` carries the full press payload (`seq`, `side`, `white_ms`,
`black_ms`, `t`), not just `t`, because the engine needs the clock times on
every capturing event to build `%clk` tags. `clock.config` sends
`white_name`/`black_name` as `"White"`/`"Black"`; the pages have no name fields
yet.

### Roles and trust

Only the `clock` socket may send `clock.*` events; the same message from the
camera is ignored and never persisted. Unknown message types are ignored rather
than rejected, so later phases can add events without breaking a phone page
cached in Safari.

### Frames

Uploads are `multipart/form-data`, field name `file`, JPEG quality 0.85, long
edge 1280 px, aspect preserved (a 1920x1080 stream stores as 1280x720). The
server checks the JPEG SOI marker and returns 415 otherwise, which is what
keeps an ngrok interstitial HTML page off disk. Frame files are zero-padded to
four digits (`0003_1.jpg`) so they sort lexically.

The camera retries a failed upload once after 800 ms, then counts it as lost
and shows the error. It never blocks the next grab. A grab that yields no frame
is also counted and surfaced, rather than being dropped silently.

### Browser constraints discovered while building

- The clock repaints from one `requestAnimationFrame` loop, and control-button
  enablement is set there too. A backgrounded tab stops rAF, so the page
  freezes until it is foregrounded again. Harmless on the clock phone, which is
  in the foreground all game, but it means the page cannot be driven headlessly
  in a background tab.
- Background tabs also clamp `setTimeout` to ~1 s, which collapses the
  +0/+400/+900 ms burst. Another reason the camera page must stay in front.
- `getUserMedia` needs both HTTPS (or localhost) and a user gesture. iOS Safari
  additionally needs `playsinline`, `muted` and `autoplay` on the `<video>`.
- Native `alert()`/`confirm()` are never used: they block the event loop and
  can kill the camera stream on iOS. All dialogs go through `modal()`.

### QR code

`static/js/qr.js` is a hand-written byte-mode encoder, EC level M, versions
1–10, with no external dependency (the venue network may be captive). A
typical ngrok join URL lands at version 4 (33x33). `renderQr()` returns `null`
when the text does not fit, and the camera page then hides the QR and shows
the room code and URL alone. It is gated by `tests/test_qr.py`, which renders
the matrices and decodes them with OpenCV rather than checking them against my
own assumptions.

### ngrok

`BOARDCAM_DOMAIN` holds the reserved static domain. `scripts/serve.sh`
validates it and the ngrok binary *before* starting uvicorn, so a missing
variable does not leave a stray server bound to the port. Every `fetch` and the
WebSocket upgrade send `ngrok-skip-browser-warning: 1`; only the first
navigation per phone per session hits the interstitial.
