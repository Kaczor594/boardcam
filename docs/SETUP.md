# BoardCam setup

One-time setup on the Mac, then a one-minute routine before each game. One phone
runs the clock and takes the photos.

## 1. One-time: the Mac

```bash
cd ~/claude-projects/boardcam
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## 2. One-time: the ngrok static domain

The phone reaches the Mac through an HTTPS tunnel, so the clock works away from
home Wi-Fi and the camera input is allowed (mobile browsers restrict it on plain
HTTP). ngrok's free tier includes **one reserved
static domain**, which means the phones get the same URL every game instead of
a fresh random one.

1. `brew install ngrok`
2. Sign in at <https://dashboard.ngrok.com>, copy the authtoken, run
   `ngrok config add-authtoken <token>`.
3. Go to **Domains** and reserve the free static domain, e.g.
   `your-name.ngrok-free.app`.
4. Add it to `~/.zprofile`:
   ```bash
   export BOARDCAM_DOMAIN=your-name.ngrok-free.app
   ```

### The interstitial

Free ngrok shows a warning page the first time a browser visits the domain.
Click through it **once per phone, per browser session** before the game
starts. The app's own requests set `ngrok-skip-browser-warning: 1` so uploads
and the WebSocket are never intercepted; only the initial navigation is.

## 3. One-time: the reader's API key

Photos are read by Claude (Fable 5.2 by default). The server takes the key from
`ANTHROPIC_API_KEY`; keep it in `~/.zprofile` and start the server from a new
terminal after changing it (a shell started before the change still has the old
key, which shows up as `api_401` on the phone). `scripts/serve.sh` refuses to
start without it.

## 3a. One-time: the engines

The moves between photos come from Maia (human-like move probabilities) through
lc0, and the evaluation graph from Stockfish.

```bash
brew install lc0 stockfish
```

Put the Maia-1 weight files `maia-1100.pb.gz` … `maia-1900.pb.gz` (CSSLab
maia-chess v1.0 release) in `data/maia/`. Optional: an opening prior from a
player's own online games, via the chess-insights database
(`~/claude-projects/chess-insights/data/chess_insights.db`, or
`CHESS_INSIGHTS_DB`) and `data/players.json`
(`{"Isaac": ["kaczor594", "simsimlamachine"]}`: the name typed on the clock
→ that player's accounts). Without it, Maia alone is used.

## 4. Before each game

```bash
cd ~/claude-projects/boardcam && scripts/serve.sh
```

It prints the URL. Then, on the phone:

1. Open `/clock`. With no game in progress it shows **New game**: time control,
   both players' names and ratings (chess.com scale, default 1350), and the
   board reader. The last game's values are filled in.
2. Tap **Set up the clock**, put the phone beside the board, and White presses
   first, as on any clock.
3. **To photograph the board**, tap **Photo**. The clock pauses at once, so the
   time spent framing the shot is nobody's. Take the photo from wherever you
   sit, the whole board in frame. Cancelling the camera leaves the clock paused.
4. The photo uploads and the board comes back as a diagram, usually in under a
   minute (a photo taken across a corner can take three). You do not have to
   wait: **Resume play** is always one tap, and the board waits for you.
5. To fix a read, tap a square and choose the piece. **Flip view** only turns
   the diagram over; if the reader put White on the wrong side, change
   **White's side in the photo** (rarely needed); **−/+** if the photo was taken after a move but before its press.
   **Confirm board** stores it. An impossible position (two white kings, a pawn
   on the back rank, …) is refused with the reason; fix it or **Retake**.
6. At the end, **Stop** and record the result, then take a photo of the final
   position. Boards skipped during play are confirmed later on the review page
   (`/games` → the game).

Three or four photos a game is the intended rate: one in the opening, one or
two in the middlegame, and the final position. When a terracotta frame
starts pulsing around the clock screen, the moves since the last photo are becoming too many
to reconstruct reliably (11 plies at the default 1350 ratings); a photo then
clears it. It never shows before the first photo or while either clock is
under a minute.

## 4a. After the game: review

Open `/games` and the game. Every confirmed photo is an anchor; the moves
between anchors are worked out in the background (seconds per gap) and the
page fills in as each gap is solved.

- Step through the game with ‹ › (or the arrow keys). Dark moves in the list
  are photographed, accepted or forced; grey ones are inferred.
- The green arrows are the likely next moves, thicker for more likely; the
  hollow orange ones are Stockfish's best moves. Under **Next move**, **Accept**
  the move that was actually played: the rest of that gap is worked out again
  with it fixed. **Undo** takes it back.
- A gap that cannot be solved is reported above the board: a wrong board or
  ply stamp on the next photo (fix it under the photos), or an accepted move
  that does not fit.
- **Download PGN**, **Copy PGN**, **Copy FENs** (the photo positions), and
  **Import to lichess**. Inferred moves carry `inferred NN%` comments.

To solve a recorded game from the command line:
`.venv/bin/python -m scripts.replay_game data/games/<id>`.

### If the page reloads

iOS can discard the page while the camera is open, and a reload used to reset
the clock. The clock now saves its state on the phone on every press, pause and
resume, and comes back exactly where it was: a paused clock with the paused
times, a running one with the time that passed charged to the side to move.
A photo whose upload was cut off by the reload is lost; take it again. A read
that was already running on the server finishes and appears on the phone.

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| ngrok warning page instead of the app | Interstitial not clicked through | Tap "Visit site" once |
| Photo shows `api_401` | The server started with an old API key | Restart `scripts/serve.sh` from a new terminal |
| Photo shows `rate_limited` or `connection` | The reader is busy or unreachable | **Read again** on the photo; no new photo needed |
| Photo shows `no_board` or `max_tokens` | The board was not in frame, or the shot is hard to read | **Retake** from one side of the board |
| "server" dot red | Tunnel or server down | The clock keeps running; photos wait until it is back |
| Clock back at 10:00 after a reload | Private browsing (no local storage) | Use a normal Safari tab |
| Review says "No legal sequence … reaches photo n" | That photo's board or ply is wrong, or an accepted move does not fit | Fix the board (± ply) under Photos, or undo the accepted move |
| Review says a search "ran out of time" | A long gap (20+ plies) | Accept a move or two in that gap; take photos more often next time |
| Review stays at "Working out the moves…" | lc0 or Stockfish missing | `brew install lc0 stockfish`; check `data/maia/` |
