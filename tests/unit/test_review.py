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
    # No per-argument metadata (older extension) -> bare parameter names.
    assert fn["parameters"] == ["x"]


def test_build_items_exposes_argument_descriptions():
    # When vgi_function_arguments() metadata is present, the reviewer sees each
    # argument's description (not just its name/type) so it can judge documentation.
    fn = F.func(
        "main",
        "candles",
        "table_function",
        description="OHLC bars",
        arguments=[
            F.arg("symbols", "VARCHAR[]", "Symbols to fetch, e.g. ['QQQ']."),
            F.arg("period", "VARCHAR", "Candle width, e.g. '5m'."),
        ],
    )
    cat = F.catalog(F.schema("main", functions=[fn]))
    item = next(it for it in rv.build_items(cat) if it["object"] == "v.main.candles")
    assert item["parameters"] == [
        {"name": "symbols", "type": "VARCHAR[]", "description": "Symbols to fetch, e.g. ['QQQ']."},
        {"name": "period", "type": "VARCHAR", "description": "Candle width, e.g. '5m'."},
    ]


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


def test_parse_reviews_tolerates_brackets_in_strings():
    # A suggestion containing [ ] must not break the array bracket-matcher.
    items = [{"object": "v.main.t", "kind": "table"}]
    raw = (
        '[{"object":"v.main.t","scores":{"accuracy":2,"clarity":2,"completeness":2,'
        '"audience_fit":2},"suggestions":["use cal.main.f(x) [see docs]"],'
        '"summary":"mentions a[0] index"}]'
    )
    reviews = rv.parse_reviews(raw, items)
    assert len(reviews) == 1 and reviews[0].suggestions == ["use cal.main.f(x) [see docs]"]


def test_extract_json_array_salvages_truncated_output():
    # A response cut off mid-array still yields the complete leading entries.
    truncated = (
        '[{"object":"a","scores":{"accuracy":3}},'
        '{"object":"b","scores":{"accuracy":4}},{"object":"c","sc'
    )
    data = rv._extract_json_array(truncated)
    assert [d["object"] for d in data] == ["a", "b"]


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


def test_review_catalog_parallel_preserves_order():
    cat = _catalog()
    backend = FakeBackend()
    # batch_size 1 + concurrency 4 -> many single-object batches run in parallel
    report = rv.review_catalog(cat, backend, cache=None, batch_size=1, concurrency=4)
    n = len(rv.build_items(cat))
    assert report.judged == n and backend.calls == n
    # results are reassembled in the original object order despite parallelism
    assert [r.object for r in report.reviews] == [it["object"] for it in rv.build_items(cat)]


class _FlakyBackend:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        if self.calls == 1:  # first batch fails
            raise RuntimeError("boom")
        import re

        ids = re.findall(r'"object": "([^"]+)"', prompt)
        return json.dumps(
            [
                {
                    "object": i,
                    "scores": {k: 3 for k in rv.SCORE_KEYS},
                    "suggestions": [],
                    "summary": "",
                }
                for i in ids
            ]
        )


def test_review_catalog_survives_a_failed_batch():
    cat = _catalog()
    # concurrency 1 so exactly the first single-object batch is the one that raises
    report = rv.review_catalog(cat, _FlakyBackend(), cache=None, batch_size=1, concurrency=1)
    n = len(rv.build_items(cat))
    assert report.judged == n - 1  # one object unreviewed, the rest still judged


def test_render_terminal_and_json():
    cat = _catalog()
    report = rv.review_catalog(cat, FakeBackend(), cache=None)
    txt = rv.render_terminal(report)
    assert "doc-quality score" in txt and "v.main.animals" in txt
    doc = json.loads(rv.render_json(report))
    assert doc["tool"] == "vgi-lint review" and doc["reviews"]
