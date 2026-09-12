# BoardCam setup

One-time setup on the Mac, then a two-minute routine before each game.

## 1. One-time: the Mac

```bash
cd ~/claude-projects/boardcam
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## 2. One-time: the ngrok static domain

Phones will not give a web page camera access over plain HTTP, so the server
has to be reachable over HTTPS. ngrok's free tier includes **one reserved
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

If the click-through becomes annoying, the documented alternative is a
Cloudflare Tunnel, which has no interstitial but needs a domain you own.

## 3. Before each game

```bash
cd ~/claude-projects/boardcam && scripts/serve.sh
```

It prints the two URLs. Then:

1. **Camera phone first.** Open `/camera`, tap "Start camera". It creates the
   game and shows a 4-character room code and a QR code.
2. **Clock phone second.** Scan the QR, or open `/clock` and type the code.
   The camera page's "clock" dot turns green.
3. Check the preview: the board should **fill most of the frame**, with a little
   room above its far edge and as little floor as possible (§4).
4. Set the pieces up, then tap **"Board is set"** on the camera phone. This
   freezes the start-position photograph.

   It matters more than it looks. Until it is tapped the start frame is
   replaced every two seconds, and the clock's first press comes *after* White
   has moved — so without the tap the engine's "initial position" already has a
   move in it, and every ply after that is read against the wrong board.
5. Play. White presses first, after white's move.
6. Press **Stop** and pick the result. The last position is photographed even
   if the final move was never clocked.

The Mac must stay awake and on the same internet connection for the whole
game. `caffeinate -i scripts/serve.sh` keeps it from sleeping.

## 4. Phone placement

This is the part that decides whether the engine works.

- **Put the camera phone on the side of the board**, perpendicular to the two
  players, not behind either of them. The background above the far edge is then
  a static wall or table rather than a moving opponent.
- **As high as the stand allows.** Elevation is the single most valuable
  variable: at a shallow angle a piece hides up to three squares behind it.
  The engine is built and tested at roughly 30° above the board plane
  (about 2 ft up, 3–4 ft to the side), so 30° works — but every extra 10°
  makes it easier.
- **Fill the frame with the board.** A little room above the far edge, and as
  little of the surroundings as the stand allows. This is what decides whether
  automatic calibration works at all: the detector looks for the most
  convincing set of nine evenly spaced parallel lines in the picture, and a
  **hardwood floor or a planked table is a better grid than a chessboard** —
  its lines are longer, straighter and higher contrast than a square edge. On
  the first real game the board filled about 15 % of the frame and every
  candidate the detector produced was on the floorboards. Cropped to the board,
  the same photograph nearly passed.
- Landscape beats portrait for this: it puts the board's long axis along the
  frame's long axis and leaves less room for floor.
- Hands at rest, the players and captured pieces are only a problem if they sit
  *on* the board area.
- **Put the clock phone and the captured pieces outside the board rectangle.**
- **Do not move the stand mid-game.** A small bump is tolerated; a
  repositioning is not.
- Even, diffuse light. A single hard lamp across the board throws long shadows
  that look like pieces.

## 5. During the game

- Keep the camera page **in the foreground**. iOS Safari pauses a camera stream
  on a backgrounded tab. The page holds a screen wake lock and re-acquires it
  when it comes back to the front, but it cannot fight a locked phone.
- The camera page's "frame" counter should tick on every clock press. If it
  stops, the camera phone lost the socket — it reconnects on its own within a
  few seconds; nothing needs restarting.
- Pausing the clock does not photograph anything. Adjust a leaning piece during
  a pause and the engine will never see it.

## 6. If calibration fails

The camera page opens a corner panel when it cannot find the board. Drag each
handle to the **corner of the chequered area** it names, not to the middle of a
square: `a1` is White's near-left corner, then `h1`, `h8`, `a8` going round. The
small red dot inside each ring is the point that counts. Getting `a1` right is
what tells the engine which way round the board is.

Corners placed by hand are final — the pre-start re-detection will not overwrite
them, and the panel will not reappear.

## 7. After the game

The clock phone shows the PGN with a copy button, a lichess import link and a
link to the review page. Everything is also on the Mac under
`data/games/<game_id>/`, and `/review?game=<id>` is where a wrong ply gets
fixed.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Camera page shows an ngrok warning instead of the app | Interstitial not clicked through | Tap "Visit site" once |
| "Start camera" does nothing | `getUserMedia` needs a user gesture and HTTPS | Confirm the URL is `https://`, tap the button directly |
| Stream freezes mid-game | Page was backgrounded | Bring it to the front; tap "Restart camera" if the dot is red |
| Clock says "not paired" | Camera socket dropped | Wait ~5 s for reconnect; reload the camera page if it persists |
| `BOARDCAM_DOMAIN is not set` | Env var missing | See §2, then open a new terminal |
| Frame counter stuck at 0 | Uploads failing | Check the camera page's error line; usually the tunnel died |
