"""Opt-in real-model acceptance test for semantic agent interaction."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from tests.semantic_example import load_example
from vgi_lint_check.review import make_backend
from vgi_lint_check.simulate import SimLimits, render_json, simulate_tasks
from vgi_lint_check.tags import merge_agent_task_sidecar

pytestmark = pytest.mark.ai


def test_agent_uses_the_semantic_model(tmp_path: Path) -> None:
    """A real model must discover, invoke, and answer through the semantic tool."""
    backend_name = os.environ.get("VGI_LINT_AI_BACKEND", "claude")
    if backend_name == "claude":
        if shutil.which("claude") is None:
            pytest.skip("the authenticated Claude Code CLI is required")
    elif backend_name == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY is required for the API backend")
        if importlib.util.find_spec("anthropic") is None:
            pytest.skip("install the anthropic package to run the API backend")
    else:
        pytest.fail("VGI_LINT_AI_BACKEND must be 'claude' or 'api'")
    fixture, catalogs, connection = load_example()
    try:
        sidecar = tmp_path / "semantic-agent-tests.yaml"
        sidecar.write_text(yaml.safe_dump(fixture["agent_graders"]), encoding="utf-8")
        public_tasks = [task for catalog in catalogs.values() for task in catalog.agent_test_tasks]
        tasks = merge_agent_task_sidecar(public_tasks, sidecar)
        backend = make_backend(backend_name, os.environ.get("VGI_LINT_AI_MODEL"))
        report = simulate_tasks(
            list(catalogs.values()),
            connection,
            backend,
            backend_name=backend_name,
            limits=SimLimits(max_steps=10, max_queries=4, attempts=2, concurrency=1),
            tasks=tasks,
        )
        assert report.pass_rate == 1.0, json.loads(render_json(report))
        assert report.verdicts[0].grader == "reference"
    finally:
        connection.close()
