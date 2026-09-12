#!/usr/bin/env bash
# Start the BoardCam server and the ngrok tunnel the phones connect through.
#
#   scripts/serve.sh              # tunnel on $BOARDCAM_DOMAIN
#   scripts/serve.sh --local      # no tunnel, http://<LAN ip>:8010 only
#
# HTTPS is not optional: mobile browsers refuse getUserMedia on plain HTTP
# from anything but localhost, so the camera page needs the tunnel (or a LAN
# address you have already trusted a certificate for).

set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${BOARDCAM_PORT:-8010}"
LOCAL_ONLY=0
[[ "${1:-}" == "--local" ]] && LOCAL_ONLY=1

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "No venv. Run:" >&2
  echo "  /opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv" >&2
  echo "  .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

mkdir -p data/games

if [[ $LOCAL_ONLY -eq 0 ]]; then
  if ! command -v ngrok >/dev/null 2>&1; then
    echo "ngrok is not installed. brew install ngrok — see docs/SETUP.md" >&2
    exit 1
  fi
  if [[ -z "${BOARDCAM_DOMAIN:-}" ]]; then
    echo "BOARDCAM_DOMAIN is not set." >&2
    echo "Reserve a free static domain once at https://dashboard.ngrok.com/domains," >&2
    echo "then add to ~/.zprofile:  export BOARDCAM_DOMAIN=your-name.ngrok-free.app" >&2
    exit 1
  fi
fi

cleanup() {
  local status=$?
  [[ -n "${NGROK_PID:-}" ]] && kill "$NGROK_PID" 2>/dev/null || true
  [[ -n "${UVICORN_PID:-}" ]] && kill "$UVICORN_PID" 2>/dev/null || true
  exit $status
}
trap cleanup EXIT INT TERM

.venv/bin/uvicorn server.app:app --host 0.0.0.0 --port "$PORT" --log-level info &
UVICORN_PID=$!

# Wait for the server rather than guessing with a sleep.
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Server did not come up on port $PORT." >&2
  exit 1
fi

if [[ $LOCAL_ONLY -eq 1 ]]; then
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)
  echo
  echo "  BoardCam (local only, no HTTPS — the camera page will not work on a phone)"
  echo "  http://$LAN_IP:$PORT"
  echo
  wait "$UVICORN_PID"
  exit 0
fi

ngrok http --domain="$BOARDCAM_DOMAIN" "$PORT" --log stdout --log-level warn >/dev/null &
NGROK_PID=$!

# ngrok's local API tells us when the tunnel is actually up.
URL=""
for _ in $(seq 1 50); do
  URL=$(curl -fsS http://127.0.0.1:4040/api/tunnels 2>/dev/null \
        | .venv/bin/python -c 'import json,sys; ts=json.load(sys.stdin)["tunnels"]; print(next((t["public_url"] for t in ts if t["public_url"].startswith("https")), ""))' 2>/dev/null || echo "")
  [[ -n "$URL" ]] && break
  sleep 0.2
done
[[ -z "$URL" ]] && URL="https://$BOARDCAM_DOMAIN"

cat <<EOF

  BoardCam is up.

    Camera phone (open first):  $URL/camera
    Clock phone  (join second): $URL/clock
    Games on the Mac:           $URL/games

  Both phones click through the ngrok warning page once per browser session.
  Keep the camera page in the foreground for the whole game.

EOF

wait "$UVICORN_PID"
