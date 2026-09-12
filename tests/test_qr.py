"""The QR encoder is hand-written, so it is gated by decoding its own output.

Structural checks would only prove it matches my own assumptions. Rendering the
matrix and putting it through an independent decoder proves the phone can
actually scan it.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parent.parent
QR_JS = ROOT / "static" / "js" / "qr.js"
ALPHA = "ACDEFGHJKLMNPQRTUVWXY34679"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def encode(texts: list[str]) -> dict:
    """Run the browser encoder under node and return its matrices."""
    script = f"""
import {{ encodeQr }} from {json.dumps(str(QR_JS))};
const out = {{}};
for (const t of {json.dumps(texts)}) {{
  const qr = encodeQr(t);
  out[t] = qr ? {{ version: qr.version, size: qr.size,
                  rows: qr.modules.map(r => r.join('')) }} : null;
}}
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def render(rows: list[str], size: int, scale: int = 8, quiet: int = 4) -> np.ndarray:
    img = np.full(((size + 2 * quiet) * scale,) * 2, 255, np.uint8)
    for r in range(size):
        for c in range(size):
            if rows[r][c] == "1":
                y, x = (r + quiet) * scale, (c + quiet) * scale
                img[y:y + scale, x:x + scale] = 0
    return img


def test_room_urls_round_trip():
    """Every URL the camera page can show must decode back to itself."""
    random.seed(11)
    texts = [
        f"https://boardcam-ik.ngrok-free.app/clock?room="
        f"{''.join(random.choice(ALPHA) for _ in range(4))}"
        for _ in range(25)
    ]
    matrices = encode(texts)
    detector = cv2.QRCodeDetector()

    failures = []
    for text in texts:
        qr = matrices[text]
        assert qr is not None, f"encoder refused {text}"
        decoded, _, _ = detector.detectAndDecode(render(qr["rows"], qr["size"]))
        if decoded != text:
            # The detector is weaker than a phone camera, so retry once at a
            # different scale before calling it a real failure.
            decoded, _, _ = detector.detectAndDecode(render(qr["rows"], qr["size"], scale=12))
        if decoded != text:
            failures.append((text, decoded))

    assert not failures, f"{len(failures)}/{len(texts)} did not decode: {failures[:3]}"


def test_local_and_long_urls_round_trip():
    texts = [
        "http://127.0.0.1:8010/clock?room=KDQU",
        "https://a-much-longer-reserved-subdomain-name.ngrok-free.app/clock?room=WXYZ",
        "boardcam",
    ]
    matrices = encode(texts)
    detector = cv2.QRCodeDetector()
    for text in texts:
        qr = matrices[text]
        assert qr is not None
        decoded, _, _ = detector.detectAndDecode(render(qr["rows"], qr["size"]))
        assert decoded == text, f"{text!r} decoded as {decoded!r}"


def test_version_grows_with_length_and_overlong_is_refused():
    texts = ["A", "x" * 100, "x" * 400]
    matrices = encode(texts)
    assert matrices["A"]["version"] == 1
    assert matrices["A"]["size"] == 21
    assert matrices["x" * 100]["version"] > 1
    # Versions stop at 10; anything longer must return null so callers fall back.
    assert matrices["x" * 400] is None


def test_finder_patterns_are_present():
    """Cheap structural guard: three 7x7 finders in the usual corners."""
    qr = encode(["https://boardcam-ik.ngrok-free.app/clock?room=K4WQ"])[
        "https://boardcam-ik.ngrok-free.app/clock?room=K4WQ"
    ]
    rows, size = qr["rows"], qr["size"]
    for top, left in [(0, 0), (0, size - 7), (size - 7, 0)]:
        block = [rows[top + r][left:left + 7] for r in range(7)]
        assert block[0] == "1" * 7 and block[6] == "1" * 7
        assert all(b[0] == "1" and b[6] == "1" for b in block)
        assert block[3][2:5] == "111"
