"""Tests for the Jev integration (VGI190 / VGI191 / VGI920 Tier 3) — all offline."""

import json

from tests import fixtures as F
from tests.unit import test_simulate as T
from vgi_lint_check import jev
from vgi_lint_check import simulate as sim
from vgi_lint_check.config import Config
from vgi_lint_check.model import AgentTask, ExampleQuery
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def _registry(*entries):
    return json.dumps([{"name": n, "description": d} for n, d in entries])


def _catalog():
    gc = F.func(
        "seq",
        "gc_content",
        comment="GC content of a DNA sequence as a fraction in [0, 1]",
        tags={"vgi.category": "transforms"},
        examples=[
            ExampleQuery(0, "GC fraction of a short read", "SELECT seq.gc_content('ATGC')"),
            ExampleQuery(1, "Daily buckets", "SELECT * FROM hourly()"),
            ExampleQuery(2, None, "SELECT 1"),  # no description: not asked
        ],
    )
    s = F.schema(
        "seq",
        tags={
            "vgi.categories": _registry(
                ("composition", "Base composition"), ("transforms", "Sequence transforms")
            )
        },
        functions=[gc],
    )
    return F.catalog(s)


class _FakeJev:
    """Answers by key substring; records what it was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def ask(self, questions):
        out = {}
        for q in questions:
            self.asked.append(q)
            for needle, ans in self.answers.items():
                if needle in q.key:
                    out[q.key] = ans
        return out

    def noul(self, text, instructions):
        self.asked.append((text, instructions))
        return self.answers["noul"]


def _findings(cat, report, *, enabled=True):
    cfg = Config(jev=enabled)
    ctx = RuleContext(cat, cfg, jev_report=report)
    return [f for f in run(select_rules(cfg), ctx) if f.code in ("VGI190", "VGI191")]


# --- building questions -------------------------------------------------------------
def test_catalog_items_collects_described_examples_with_context_and_ids():
    examples, categories = jev.catalog_items(_catalog())
    assert [e.description for e in examples] == ["GC fraction of a short read", "Daily buckets"]
    assert examples[0].context.startswith("gc_content: GC content")
    assert examples[0].object_id is not None
    [cat] = categories
    assert cat.declared == "transforms"
    assert [n for n, _ in cat.registry] == ["composition", "transforms"]


def test_category_needs_a_registry_of_at_least_two():
    f = F.func("main", "f", tags={"vgi.category": "only"})
    s = F.schema("main", tags={"vgi.categories": _registry(("only", "The one"))}, functions=[f])
    assert jev.catalog_items(F.catalog(s))[1] == []


def test_dump_items_reads_tagged_examples_and_dedupes():
    ex = {"description": "Top five", "sql": "SELECT 1"}
    dump = {
        "catalog": {
            "example_queries": [ex],
            "schemas": [
                {
                    "name": "main",
                    "tags": {"vgi.example_queries": [ex]},
                    "functions": [
                        {
                            "name": "f",
                            "comment": "Does f",
                            "tags": {"vgi.example_queries": [ex, {"description": "", "sql": "x"}]},
                        }
                    ],
                }
            ],
        }
    }
    examples, _ = jev.dump_items(dump)
    assert len(examples) == 1
    assert examples[0].where == "schema main example 1"


def test_example_question_is_a_calibrated_choice():
    [q] = jev.example_questions([jev.ExampleItem("k", "w", "Top five", "SELECT 1", "f: does f")])
    assert q.kind == "choice"
    assert dict(q.criteria).keys() == {"accurate", "inaccurate"}
    assert q.text.startswith("Object: f: does f")


def test_cache_key_changes_with_wording_and_model():
    q = jev.Question("k", "noul", "t", "i")
    assert q.cache_key("m1") != q.cache_key("m2")
    assert q.cache_key("m1") != jev.Question("k", "noul", "t", "j").cache_key("m1")


def test_cached_answers_need_no_connection(tmp_path):
    q = jev.Question("k", "noul", "t", "i")
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({q.cache_key("jev-latest"): {"noul": 0.9}}))
    client = jev.JevClient(cache_path=cache)
    assert client.ask([q])["k"].noul == 0.9  # would raise if it tried to attach


# --- rules --------------------------------------------------------------------------
def _report(answers):
    examples, categories = jev.catalog_items(_catalog())
    return jev.run_questions(_FakeJev(answers), examples, categories)


def test_rules_are_off_without_jev():
    report = _report({"example:seq.gc_content:1": jev.Answer(choice="inaccurate", confidence=1)})
    assert _findings(_catalog(), report, enabled=False) == []


def test_vgi190_flags_only_confident_inaccurate():
    report = _report(
        {
            "example:seq.gc_content:0": jev.Answer(choice="inaccurate", confidence=0.6),
            "example:seq.gc_content:1": jev.Answer(choice="inaccurate", confidence=0.95),
        }
    )
    [f] = [f for f in _findings(_catalog(), report) if f.code == "VGI190"]
    assert "Daily buckets" in f.message and "0.95" in f.message


def test_vgi191_flags_only_a_confident_other_category():
    confident = _report({"category:": jev.Answer(choice="composition", confidence=0.99)})
    [f] = [f for f in _findings(_catalog(), confident) if f.code == "VGI191"]
    assert "'transforms'" in f.message and "'composition'" in f.message
    unsure = _report({"category:": jev.Answer(choice="composition", confidence=0.9)})
    assert [f for f in _findings(_catalog(), unsure) if f.code == "VGI191"] == []


def test_a_failed_jev_pass_is_reported_not_raised():
    class _Broken:
        def ask(self, questions):
            raise RuntimeError("no secret")

    examples, categories = jev.catalog_items(_catalog())
    report = jev.run_questions(_Broken(), examples, categories)
    assert report.errors and "no secret" in report.errors[0]
    assert _findings(_catalog(), report) == []


# --- VGI920 Tier 3 through Jev --------------------------------------------------------
def _tier3(p):
    cat = T._catalog_with_tasks([AgentTask(name="j", prompt="do", success_criteria="is 1")])
    backend = T._Backend(
        [json.dumps({"action": "final", "answer_sql": "SELECT 1", "answer_summary": "it is 1"})]
    )
    fake = _FakeJev({"noul": p})
    limits = sim.SimLimits(jev=fake)
    v = sim.simulate_tasks(cat, T._Con(), backend, limits=limits).verdicts[0]
    return v, fake


def test_tier3_jev_pass_fail_partial():
    v, fake = _tier3(0.9)
    assert (v.outcome, v.grader) == ("pass", "jev")
    assert "is 1" in fake.asked[0][1]  # the success criteria reach Jev, not the actor
    assert _tier3(0.1)[0].outcome == "fail"
    assert _tier3(0.5)[0].outcome == "partial"
