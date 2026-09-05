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
    parser.addoption(
        "--run-ai",
        action="store_true",
        default=False,
        help="run @pytest.mark.ai tests against the configured Anthropic API",
    )


def pytest_collection_modifyitems(config, items):
    run_live = config.getoption("--run-live") or os.environ.get("VGI_LINT_LIVE")
    run_ai = config.getoption("--run-ai") or os.environ.get("VGI_LINT_AI")
    skip_live = pytest.mark.skip(reason="live: pass --run-live or set VGI_LINT_LIVE=1")
    skip_ai = pytest.mark.skip(reason="ai: pass --run-ai or set VGI_LINT_AI=1")
    for item in items:
        if "live" in item.keywords and not run_live:
            item.add_marker(skip_live)
        if "ai" in item.keywords and not run_ai:
            item.add_marker(skip_ai)
