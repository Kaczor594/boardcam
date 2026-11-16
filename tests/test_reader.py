"""The Claude reader with a stubbed client: success, and every way it must fail loudly."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from engine import reader
from engine.reader import ReadError, read_position

START = ["rnbqkbnr", "pppppppp", "........", "........",
         "........", "........", "PPPPPPPP", "RNBQKBNR"]


class StubClient:
    def __init__(self, stop_reason="end_turn", rows=START, home="bottom", category=None):
        self.calls = []
        parsed = None if rows is None else reader._Board(rows=rows, white_home_side=home)
        self.response = SimpleNamespace(
            stop_reason=stop_reason, parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=6000, output_tokens=2000),
            stop_details=SimpleNamespace(category=category) if category else None)
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _jpeg(w=400, h=300, orientation=None) -> bytes:
    img = Image.new("RGB", (w, h), (30, 120, 40))
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def test_a_read_returns_rows_home_and_cost():
    client = StubClient()
    got = read_position(_jpeg(), client=client)
    assert got.rows == START and got.home == "bottom"
    assert got.model == "claude-fable-5-2" and got.backend == "claude"
    assert got.cost == pytest.approx((6000 * 8 + 2000 * 40) / 1e6)
    call = client.calls[0]
    assert call["model"] == "claude-fable-5-2"
    kinds = [b["type"] for b in call["messages"][0]["content"]]
    assert kinds == ["image", "text"], "photo-only sends one image and the prompt"
    assert "grid" not in call["messages"][0]["content"][1]["text"]


def test_the_grid_variant_sends_two_images():
    client = StubClient()
    read_position(_jpeg(), grid_image=_jpeg(), client=client)
    kinds = [b["type"] for b in client.calls[0]["messages"][0]["content"]]
    assert kinds == ["image", "image", "text"]


def test_model_and_effort_are_parameters():
    client = StubClient()
    got = read_position(_jpeg(), model="claude-sonnet-5-5", effort="low", client=client)
    assert got.model == "claude-sonnet-5-5"
    assert client.calls[0]["output_config"] == {"effort": "low"}


@pytest.mark.parametrize("stop, rows, reason", [
    ("refusal", None, "refusal"),
    ("max_tokens", None, "max_tokens"),
    ("end_turn", None, "no_board"),
    ("end_turn", START[:7], "bad_shape"),
    ("end_turn", START[:7] + ["RNBQKBNx"], "bad_symbol"),
])
def test_failures_raise_instead_of_returning_a_board(stop, rows, reason):
    with pytest.raises(ReadError) as exc:
        read_position(_jpeg(), client=StubClient(stop_reason=stop, rows=rows, category="cyber"))
    assert exc.value.reason == reason


def test_exif_orientation_is_applied_and_size_capped():
    # Orientation 6: stored landscape, shown portrait — as the iPhone writes it.
    client = StubClient()
    read_position(_jpeg(3000, 2000, orientation=6), client=client)
    data = client.calls[0]["messages"][0]["content"][0]["source"]["data"]
    sent = Image.open(io.BytesIO(base64.standard_b64decode(data)))
    assert sent.size[1] > sent.size[0], "the photo must be sent upright"
    assert max(sent.size) == reader.MAX_EDGE


def test_unknown_backend_is_refused():
    with pytest.raises(ValueError):
        read_position(_jpeg(), backend="local")


# ---------------------------------------------------------------- read_eval

def test_scoring_separates_reading_from_orientation():
    from engine.position import rotate_rows
    from engine.read_eval import score

    truth = {"a": {"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR", "done": True},
             "b": {"rows": START, "home": "bottom", "done": True}}
    reads = {"a": {"rows": rotate_rows(START, 1), "home": "bottom"},   # read right, called wrong
             "b": {"rows": START[:7] + ["RNBQKBN."], "home": "bottom"}}  # one square missed
    res = score(truth, reads)
    assert res["per-square"] == (64 + 63) / 128
    assert res["perfect-boards"] == 1
    assert res["home-side"] == 0.5 and res["fen-correct"] == 0.0
    assert res["confusions"] == {"R->.": 1}


def test_a_failed_read_counts_every_square_wrong():
    from engine.read_eval import score

    res = score({"a": {"rows": START, "home": "bottom", "done": True}},
                {"a": {"error": "refusal"}})
    assert res["per-square"] == 0.0 and res["errors"] == {"a": "refusal"}
