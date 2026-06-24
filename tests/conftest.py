"""Shared pytest config: gate live worker tests behind an opt-in.

Live tests attach to a real VGI worker (subprocess) and are skipped unless
``VGI_LINT_LIVE=1`` is set or ``--run-live`` is passed.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run @pytest.mark.live tests against real workers",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live") or os.environ.get("VGI_LINT_LIVE"):
        return
    skip = pytest.mark.skip(reason="live: pass --run-live or set VGI_LINT_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
