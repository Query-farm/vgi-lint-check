"""Reproducible, offline end-to-end test over a committed real snapshot.

Drives the full build_catalog -> rule engine -> scoring -> reporting pipeline
against a recorded volcanos snapshot (tests/fixtures/snapshot_volcanos.json), so
the seam between the loader and the rules has CI coverage without a live worker.
"""

import json
from pathlib import Path

from vgi_lint_check import reporting, scoring
from vgi_lint_check.config import Config
from vgi_lint_check.loader import build_catalog
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.snapshot import Snapshot

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "snapshot_volcanos.json"


def _load():
    data = json.loads(FIXTURE.read_text())
    snap = Snapshot(
        schemas=data["schemas"],
        tables=data["tables"],
        columns=data["columns"],
        views=data["views"],
        functions=data["functions"],
        constraints=data["constraints"],
        settings=[],
    )
    cat = build_catalog(
        snap,
        data["alias"],
        "recorded://volcanos",
        catalog_name=data["catalog_name"],
        setting_rows=data["setting_rows"],
        pragma_rows=data["pragma_rows"],
    )
    return data, cat


def test_catalog_loads_from_recorded_snapshot():
    data, cat = _load()
    assert {s.name for s in cat.schemas} == {"earthquakes", "hans", "smithsonian", "vsc"}
    assert sum(1 for _ in cat.iter_tables()) == 27
    assert sum(1 for _ in cat.iter_views()) == 4
    assert sum(1 for _ in cat.iter_macros()) == 5
    # all 401 constraints loaded onto their tables
    assert sum(len(t.constraints) for t in cat.iter_tables()) == 401


def test_full_pipeline_offline():
    _, cat = _load()
    cfg = Config()
    findings = run(select_rules(cfg), RuleContext(cat, cfg))
    codes = {f.code for f in findings}
    # volcanos schemas lack llm/md tags (required) and examples aren't
    # catalog-qualified -> these fire
    assert "VGI116" in codes  # schema description_llm required
    assert "VGI505" in codes
    # its declared constraints are all valid -> no validity false positives
    # (VGI807/808 — missing-PK / suggested-FK nudges — may legitimately fire)
    assert not ({"VGI801", "VGI802"} & codes)

    quality = scoring.compute(cat, findings)
    assert 0 <= quality.score <= 100
    assert quality.coverage.families["columns"] is not None


def test_report_json_validates_against_schema():
    import jsonschema

    from vgi_lint_check.reporting.json_reporter import report_schema
    from vgi_lint_check.result import Report, VersionResult

    _, cat = _load()
    cfg = Config()
    findings = run(select_rules(cfg), RuleContext(cat, cfg))
    vr = VersionResult(cat, findings, scoring.compute(cat, findings), {"tables": 27})
    report = Report("recorded://volcanos", cat.database, "x", [vr], cfg.fail_on)
    doc = json.loads(reporting.render_json(report))
    jsonschema.validate(doc, report_schema())
