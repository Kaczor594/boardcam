# BoardCam

Clock-triggered over-the-board chess recorder. Two phones and a laptop turn a
casual rapid game into a PGN with clock times, without either player writing
anything down.

One phone runs the chess clock. A second phone on a stand beside the board
photographs the position every time the clock is pressed. The server on the Mac
stores the frames, and a tracking engine turns the sequence of photographs into
a move list.

The engine does not recognise pieces. It knows the start position and sees one
photograph per ply, so it *tracks* the position instead: each frame is compared
with the previous one, the pattern of change is scored against every legal move,
and a beam search over the whole game picks the most likely sequence. Later
frames resolve earlier ambiguities.

## Status

Phase 1 of 6 complete: server, clock page, camera page, pairing and tunnel.
The physical workflow runs end to end and stores frames; there is no engine yet.
See `planning/specs/2026-09-12-boardcam.md` for the remaining phases.

## Quick start

```bash
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export BOARDCAM_DOMAIN=your-name.ngrok-free.app   # reserved once, see docs/SETUP.md
scripts/serve.sh
```

Open `/camera` on the phone that will sit on the stand, then scan its QR code
with the phone that will run the clock. Full instructions, including where to
put the camera, are in [`docs/SETUP.md`](docs/SETUP.md).

## Layout

| Path | What |
|---|---|
| `server/` | FastAPI app, WebSocket rooms, game storage |
| `static/` | The two phone pages and the games list. Vanilla ES modules, no build step |
| `engine/` | Tracking engine (phases 2–4) |
| `synth/` | Synthetic renderer the engine is gated on (phase 2) |
| `docs/PROTOCOL.md` | The wire contract. Authoritative |
| `data/` | Games, frames, corpora. Never committed |

## Tests

```bash
.venv/bin/pytest -q                      # server and, later, engine
node --test static/js/clock.test.mjs     # clock state machine
```

## Why a tunnel

Mobile browsers refuse camera access to any page that is not on HTTPS or
localhost, so the phones cannot simply hit the Mac's LAN address. ngrok's free
reserved domain gives a stable HTTPS URL with no certificate work.

## Phase 1 notes

`docs/PROTOCOL.md` §7 records the decisions and browser constraints found while
building the phone pages. The two that matter most to anyone touching this code:

- The clock owns `seq`, and carries an offset so a page reload mid-game
  continues the frame numbering instead of overwriting earlier frames.
- Both pages must stay in the foreground. A backgrounded tab stops
  `requestAnimationFrame` (the clock freezes) and clamps `setTimeout` to ~1 s
  (the capture burst collapses).

The QR encoder in `static/js/qr.js` is hand-written with no dependency, and is
tested by decoding its own rendered output rather than by structural assertions.
