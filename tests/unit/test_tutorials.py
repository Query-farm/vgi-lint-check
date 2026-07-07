"""Tests for the tutorials feature: loader (offline) + VGI13xx static rules."""

from __future__ import annotations

from pathlib import Path

from vgi_lint_check.config import Config
from vgi_lint_check.rules.tutorials import (
    TutorialContext,
    run_tutorial_rules,
    tutorial_rules,
)
from vgi_lint_check.tutorials.loader import load_tutorial

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "tutorials"

_GOOD = """\
---
title: Count business days between two dates in DuckDB SQL
slug: sample
worker: cal
description: >-
  A hands-on calendar tutorial that counts business days between two dates and
  sets order-due dates, entirely in DuckDB with no external calendar service.
keywords: [business days, duckdb, calendar]
difficulty: beginner
est_minutes: 6
tier: quickstart
dataset: {name: "Inline rows", provenance: "synthetic"}
datePublished: 2026-07-06
dateModified: 2026-07-06
---

## The problem

Every ops team needs to count business days between two dates without tripping
over weekends and public holidays, and nobody wants to maintain a calendar table
for it. The cal worker answers it directly.

```sql {role=step expect=scalar}
SELECT cal.main.business_days_between(DATE '2026-12-21', DATE '2026-12-31') AS days;
```
```result
days
8
```

## Next steps

- Read the [reference](https://github.com/Query-farm/vgi-calendar).
- Try the [trading-calendar recipe](trading-calendar-gotchas.html).
"""


def _codes(text: str, tmp_path: Path, name: str = "sample.vgi.md") -> set[str]:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    cfg = Config()
    cfg.execute = False
    ctx = TutorialContext(docs=[load_tutorial(p)], config=cfg)
    return {f.code for f in run_tutorial_rules(tutorial_rules(), ctx)}


# --- loader is defensive ---------------------------------------------------
def test_missing_frontmatter_is_finding_not_crash(tmp_path):
    doc = load_tutorial_str("no front matter here", tmp_path)
    assert doc.parse_error is not None


def load_tutorial_str(text: str, tmp_path: Path):
    p = tmp_path / "x.vgi.md"
    p.write_text(text, encoding="utf-8")
    return load_tutorial(p)


def test_yaml_date_is_accepted(tmp_path):
    # YAML parses unquoted ISO dates to date objects; the loader must coerce them.
    doc = load_tutorial_str(_GOOD, tmp_path)
    assert doc.front_matter is not None
    assert doc.front_matter.date_published == "2026-07-06"
    assert doc.fm_errors == ()


# --- a clean tutorial passes ----------------------------------------------
def test_good_tutorial_has_no_errors(tmp_path):
    codes = _codes(_GOOD, tmp_path)
    # No structural/front-matter/SEO errors should fire on the compliant sample.
    for bad in ("VGI1300", "VGI1301", "VGI1302", "VGI1303", "VGI1310", "VGI1313", "VGI1324"):
        assert bad not in codes, f"{bad} unexpectedly fired: {codes}"


# --- individual rules fire on crafted violations ---------------------------
def test_missing_required_key_flagged(tmp_path):
    text = _GOOD.replace("difficulty: beginner\n", "")
    assert "VGI1301" in _codes(text, tmp_path)


def test_search_path_flagged(tmp_path):
    text = _GOOD.replace(
        "SELECT cal.main.business_days_between",
        "SET search_path = 'cal.main';\nSELECT cal.main.business_days_between",
    )
    assert "VGI1313" in _codes(text, tmp_path)


def test_bad_tier_flagged(tmp_path):
    assert "VGI1302" in _codes(_GOOD.replace("tier: quickstart", "tier: advanced-recipe"), tmp_path)


def test_slug_must_match_filename(tmp_path):
    # slug 'sample' but file named other.vgi.md -> VGI1302
    assert "VGI1302" in _codes(_GOOD, tmp_path, name="other.vgi.md")


def test_placeholder_data_flagged(tmp_path):
    text = _GOOD.replace("The cal worker answers it directly.", "Use foo and bar as inputs.")
    assert "VGI1322" in _codes(text, tmp_path)


def test_too_few_links_flagged(tmp_path):
    text = _GOOD.replace("- Try the [trading-calendar recipe](trading-calendar-gotchas.html).", "")
    assert "VGI1324" in _codes(text, tmp_path)


# --- runner + execution rules (offline, fake cursor) -----------------------
class _Result:
    def __init__(self, cols, rows):
        self.description = [(c,) for c in cols] if cols else None
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Cur:
    def __init__(self, mapping):
        self.mapping = mapping

    def execute(self, sql):
        cols, rows = self.mapping.get(sql.strip(), ([], []))
        return _Result(cols, rows)


class _Con:
    def __init__(self, mapping):
        self.mapping = mapping

    def cursor(self):
        return _Cur(self.mapping)


def test_runner_matches_pinned_result(tmp_path):
    from vgi_lint_check.tutorials.runner import run_tutorial

    doc = load_tutorial_str(_GOOD, tmp_path)
    sql = doc.steps[0].sql.strip()
    con = _Con({sql: (["days"], [(8,)])})
    results = run_tutorial(con, doc, timeout=0)
    assert len(results) == 1
    assert results[0].ok and results[0].matched is True


def test_runner_detects_result_mismatch(tmp_path):
    from vgi_lint_check.tutorials.runner import run_tutorial

    doc = load_tutorial_str(_GOOD, tmp_path)
    sql = doc.steps[0].sql.strip()
    con = _Con({sql: (["days"], [(99,)])})  # wrong number
    results = run_tutorial(con, doc, timeout=0)
    assert results[0].matched is False


def test_execution_rules_flag_failure_and_mismatch(tmp_path):
    from vgi_lint_check.rules.tutorials import TutorialContext, run_tutorial_rules, tutorial_rules
    from vgi_lint_check.tutorials.model import StepResult

    doc = load_tutorial_str(_GOOD, tmp_path)
    cfg = Config()
    cfg.execute = True  # enable connection rules
    results = {
        doc.slug: [StepResult(0, False, "BinderException: no such function", [], [], None, 0.1)]
    }
    ctx = TutorialContext(
        docs=[doc], config=cfg, catalogs={"cal": _FakeCatalog()}, connection=True, results=results
    )
    codes = {f.code for f in run_tutorial_rules(tutorial_rules(), ctx)}
    assert "VGI1341" in codes  # the failed step is reported


class _FakeCatalog:
    def iter_all_functions(self):
        return []

    def iter_table_like(self):
        return []


# --- P4: LLM suggester (fake backend) --------------------------------------
class _FakeBackend:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return self.reply


def test_suggest_tutorials_parses_proposals():
    from tests import fixtures as F
    from vgi_lint_check.tutorials.suggest import suggest_tutorials

    cat = F.catalog(
        F.schema("main", functions=[F.func("main", "convert", description="convert x")])
    )
    backend = _FakeBackend(
        '[{"slug":"convert-units-fast","title":"Convert physical units in DuckDB SQL",'
        '"job":"convert units","tier":"quickstart","functions":["v.main.convert"]}]'
    )
    proposals = suggest_tutorials(cat, backend, cap=5)
    assert backend.calls >= 1
    assert proposals[0]["slug"] == "convert-units-fast"


def test_suggest_adds_composition_with_fleet():
    from tests import fixtures as F
    from vgi_lint_check.tutorials.suggest import suggest_tutorials

    cat = F.catalog(F.schema("main", functions=[F.func("main", "convert")]))

    class _Queue:
        def __init__(self, replies):
            self.replies = list(replies)

        def complete(self, prompt):
            return self.replies.pop(0) if self.replies else "[]"

    backend = _Queue(
        [
            '[{"slug":"a","title":"t","keyword":"k","job":"j","tier":"recipe",'
            '"functions":["v.main.convert"],"with":[]}]',
            '[{"slug":"combo","title":"t2","keyword":"k2","job":"j2","tier":"composition",'
            '"functions":["v.main.convert"],"with":["geocode"]}]',
        ]
    )
    props = suggest_tutorials(cat, backend, fleet={"geocode": "reverse geocoding"})
    assert "combo" in {p["slug"] for p in props}
    assert any(p.get("with") == ["geocode"] for p in props)


def test_draft_tutorial_returns_backend_output():
    from tests import fixtures as F
    from vgi_lint_check.tutorials.suggest import draft_tutorial

    cat = F.catalog(F.schema("main", functions=[F.func("main", "convert")]))
    backend = _FakeBackend("---\ntitle: x\n---\n## The problem\n")
    out = draft_tutorial(cat, backend, worker="units", slug="s", tier="quickstart", job="convert")
    assert out.startswith("---")


# --- P5: narrative judge (fake backend) + corpus dedup ---------------------
def test_narrative_judge_flags_low_score(tmp_path):
    doc = load_tutorial_str(_GOOD, tmp_path)
    cfg = Config()
    cfg.doc_review = True  # ungate the requires_review rule
    backend = _FakeBackend('{"accuracy":2,"clarity":2,"aha":1,"voice":2,"issue":"thin"}')
    ctx = TutorialContext(docs=[doc], config=cfg, backend=backend)
    assert "VGI1370" in {f.code for f in run_tutorial_rules(tutorial_rules(), ctx)}


def test_narrative_judge_passes_high_score(tmp_path):
    doc = load_tutorial_str(_GOOD, tmp_path)
    cfg = Config()
    cfg.doc_review = True
    backend = _FakeBackend('{"accuracy":5,"clarity":5,"aha":4,"voice":5,"issue":""}')
    ctx = TutorialContext(docs=[doc], config=cfg, backend=backend)
    assert "VGI1370" not in {f.code for f in run_tutorial_rules(tutorial_rules(), ctx)}


def test_corpus_anti_sameness_flags_near_duplicates(tmp_path):
    # Two near-identical tutorials from different dirs, linted together (corpus mode).
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.vgi.md").write_text(_GOOD.replace("slug: sample", "slug: one"), encoding="utf-8")
    (b / "two.vgi.md").write_text(_GOOD.replace("slug: sample", "slug: two"), encoding="utf-8")
    docs = [load_tutorial(a / "one.vgi.md"), load_tutorial(b / "two.vgi.md")]
    cfg = Config()
    cfg.execute = False
    ctx = TutorialContext(docs=docs, config=cfg)
    assert "VGI1326" in {f.code for f in run_tutorial_rules(tutorial_rules(), ctx)}


# --- the shipped calendar suite passes at fail-on error --------------------
def test_example_calendar_suite_clean_at_error():
    from vgi_lint_check.tutorials.hub import find_hub
    from vgi_lint_check.tutorials.loader import load_dir

    suite = EXAMPLES / "calendar"
    cfg = Config()
    cfg.execute = False
    ctx = TutorialContext(docs=load_dir(suite), config=cfg, hub=find_hub(suite))
    findings = run_tutorial_rules(tutorial_rules(), ctx)
    from vgi_lint_check.findings import Severity

    errors = [f for f in findings if f.severity >= Severity.ERROR]
    assert errors == [], [f"{f.code} {f.message}" for f in errors]
