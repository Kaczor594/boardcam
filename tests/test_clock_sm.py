"""Run the clock state-machine tests under pytest too.

The state machine is JavaScript (it runs on the clock phone), so its real
tests live in `static/js/clock.test.mjs` and run under `node --test`. This
shim exists so `pytest -q` is a complete gate for the whole project rather
than only its Python half.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "static" / "js" / "clock.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_clock_state_machine():
    assert SUITE.exists(), f"missing {SUITE.relative_to(ROOT)}"
    proc = subprocess.run(
        ["node", "--test", str(SUITE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
