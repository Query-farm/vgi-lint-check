"""Regression tests for semantic documentation fixture validation."""

import json
import sys

import pytest

from scripts import gen_semantic_docs


def test_committed_semantic_documentation_fixtures(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gen_semantic_docs.py", "--check"])

    assert gen_semantic_docs.main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("query", [{"measures": "invalid"}, None])
def test_conformance_requests_are_validated(tmp_path, monkeypatch, capsys, query):
    examples = tmp_path / "examples" / "semantic"
    examples.mkdir(parents=True)
    fixture = examples / "compiler-conformance.json"
    fixture.write_text(
        json.dumps({"version": 1, "cases": [{"name": "invalid", "request": query}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gen_semantic_docs, "__file__", str(tmp_path / "scripts" / "check.py"))
    monkeypatch.setattr(sys, "argv", ["gen_semantic_docs.py", "--check"])

    assert gen_semantic_docs.main() == 1
    output = capsys.readouterr().out
    assert f"{fixture}:query:" in output
    assert "unknown semantic tag" not in output
