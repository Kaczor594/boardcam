# BoardCam — project rules

Clock-triggered over-the-board chess recorder: two phones, one FastAPI server on
the Mac behind ngrok, a tracking engine that turns per-move photos into PGN.
Spec: `planning/specs/2026-09-12-boardcam.md` (read it first; `/spec build` it).

- Python 3.13 venv at `.venv/` (`/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv`); never use the system 3.14.
- Never stage `data/` (real game photos), `.env`, `.venv/`.
- Frontend is vanilla HTML/JS served from `static/` — no bundler. UI uses the
  `kaczor-design` skill (personal project).
- Engine work is gated by `engine.evaluate` on `data/synth/*`; regenerate the
  corpora with the seeds in the spec before comparing numbers.
- Vision-LLM calls use `ANTHROPIC_API_KEY` from the environment (personal key);
  load the `claude-api` skill before touching `engine/vision_llm.py`.
- Commit per phase; append the short hash to the spec's `commits`.
