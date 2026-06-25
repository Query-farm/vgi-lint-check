"""Tests for the LLM-review mode (with a fake backend — no real model calls)."""

import json

from tests import fixtures as F
from vgi_lint_check import review as rv


def _catalog():
    t = F.table(
        "main",
        "animals",
        comment="Animal facts",
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "## y"},
        columns=[F.col("main", "animals", "name", "the name")],
        examples=[F.example(0, "all", "SELECT * FROM v.main.animals")],
    )
    fn = F.func("main", "loud", "scalar", description="d", parameters=["x"])
    s = F.schema(
        "main",
        comment="Zoo",
        tags={"vgi.doc_llm": "z" * 50, "vgi.doc_md": "## z"},
        tables=[t],
        functions=[fn],
    )
    return F.catalog(s)


def test_build_items_includes_grounding():
    items = rv.build_items(_catalog())
    by = {it["object"]: it for it in items}
    assert any(it["kind"] == "catalog" for it in items)
    tbl = by["v.main.animals"]
    assert tbl["columns"][0]["name"] == "name" and tbl["columns"][0]["type"] == "VARCHAR"
    assert tbl["examples"] == ["SELECT * FROM v.main.animals"]
    fn = by["v.main.loud"]
    assert fn["parameters"] == ["x"]


def test_content_hash_changes_with_content():
    a = {"object": "x", "doc_llm": "one"}
    b = {"object": "x", "doc_llm": "two"}
    assert rv.content_hash(a) != rv.content_hash(b)
    assert rv.content_hash(a) == rv.content_hash(dict(a))


def test_parse_reviews_extracts_json_amid_prose():
    items = [{"object": "v.main.animals", "kind": "table"}]
    raw = (
        'Here is my review:\n[{"object":"v.main.animals","scores":'
        '{"accuracy":4,"clarity":3,"completeness":2,"audience_fit":5},'
        '"suggestions":["add units"],"summary":"decent"}]\nDone.'
    )
    reviews = rv.parse_reviews(raw, items)
    assert len(reviews) == 1
    r = reviews[0]
    assert r.scores == {"accuracy": 4, "clarity": 3, "completeness": 2, "audience_fit": 5}
    assert r.overall == 3.5 and r.suggestions == ["add units"]


class FakeBackend:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        # echo a verdict for every object id mentioned in the prompt
        import re

        ids = re.findall(r'"object": "([^"]+)"', prompt)
        return json.dumps(
            [
                {
                    "object": i,
                    "scores": {k: 4 for k in rv.SCORE_KEYS},
                    "suggestions": ["s"],
                    "summary": "ok",
                }
                for i in ids
            ]
        )


def test_review_catalog_and_cache(tmp_path):
    cat = _catalog()
    backend = FakeBackend()
    cache = rv.ReviewCache(tmp_path / "c.json").load()
    report = rv.review_catalog(cat, backend, cache=cache, batch_size=2)
    assert report.judged == len(rv.build_items(cat)) and report.cached == 0
    assert report.score == 4.0
    assert backend.calls >= 1  # batched
    # a second run reuses the cache -> nothing re-judged
    backend2 = FakeBackend()
    cache2 = rv.ReviewCache(tmp_path / "c.json").load()
    report2 = rv.review_catalog(cat, backend2, cache=cache2, batch_size=2)
    assert report2.cached == len(rv.build_items(cat)) and report2.judged == 0
    assert backend2.calls == 0


def test_render_terminal_and_json():
    cat = _catalog()
    report = rv.review_catalog(cat, FakeBackend(), cache=None)
    txt = rv.render_terminal(report)
    assert "doc-quality score" in txt and "v.main.animals" in txt
    doc = json.loads(rv.render_json(report))
    assert doc["tool"] == "vgi-lint review" and doc["reviews"]
