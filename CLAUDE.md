# BoardCam — project rules

Over-the-board chess recorder: one phone runs the clock and takes a few hand-held
photos per game; a FastAPI server on the Mac behind ngrok reads each photo into
a position and infers the moves between them.
Spec: `planning/specs/2026-11-16-photo-positions.md` (read it first; `/spec build` it).
The September stand-camera spec is abandoned and kept for history only.

- Python 3.13 venv at `.venv/` (`/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv`); never use the system 3.14.
- Never stage `data/` (real game photos), `.env`, `.venv/`.
- Frontend is vanilla HTML/JS served from `static/` — no bundler. UI uses the
  `kaczor-design` skill (personal project).
- Reader work is gated by `engine.read_eval` on `data/labels/<set>/`; paid reads
  are cached under `reads/`, so re-scoring is free. Never tune a prompt on the
  `holdout` split.
- Claude reader calls use `ANTHROPIC_API_KEY` from the environment (personal key);
  load the `claude-api` skill before touching `engine/reader.py`. Unit tests stub
  the client; anything that hits the real API is marked `live`.
- Moves between photos: `server/gaps.py` drives `engine.lines` (lc0 + Maia) and
  Stockfish; tests stub both (`tests/conftest.py`). Scripts that touch the
  engines must call `server.gaps.shutdown()` before exiting.
- Session context and the open field-test issues live in `CLAUDE_HANDOFF.md`.
- Commit per phase; append the short hash to the spec's `commits`.
