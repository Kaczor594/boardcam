"""Keep the tests that spend money out of a plain ``pytest`` run.

A ``live`` test calls the real Anthropic API. Marking it is not enough — pytest
runs marked tests by default — so a plain run would bill on every invocation.
Select them deliberately with ``pytest -m live``.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    if "live" in (config.getoption("-m") or ""):
        return
    skip_live = pytest.mark.skip(reason="live: real API calls — run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
