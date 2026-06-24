"""Live integration tests against real VGI workers (opt-in: --run-live).

These exercise the full connect -> snapshot -> diff -> load -> lint pipeline,
the snapshot column-shape drift alarm, and the multi-version path.
"""

from __future__ import annotations

import os
import shutil

import pytest

from vgi_lint_check import lint_worker
from vgi_lint_check.config import Config

pytestmark = pytest.mark.live

VOLCANOS = os.path.expanduser("~/Development/vgi-volcanos")
VGI_PYTHON = os.path.expanduser("~/Development/vgi-python")

VOLCANOS_LOC = (
    f"uv run --project {VOLCANOS} {VOLCANOS}/volcano_worker.py"
)
VERSIONED_LOC = (
    f"uv run --project {VGI_PYTHON} --with pytz "
    f"python -m vgi._test_fixtures.versioned_tables"
)


def _need(path):
    if not os.path.isdir(path) or shutil.which("uv") is None:
        pytest.skip(f"requires {path} and uv")


def test_lint_volcanos_end_to_end():
    _need(VOLCANOS)
    report = lint_worker(VOLCANOS_LOC, config=Config(), spatial=True)
    assert report.alias == "volcanos"
    r = report.results[0]
    assert r.catalog.schemas, "expected schemas"
    # volcanos uses plain comments + example_queries, no llm/md tags -> VGI112 fires
    assert any(f.code == "VGI112" for f in r.findings)
    assert 0 <= r.score <= 100


def test_snapshot_columns_superset_drift_alarm():
    """The real duckdb_* outputs must contain the columns the loader reads."""
    _need(VOLCANOS)
    from vgi_lint_check.connection import attached, connect_loaded, derive_alias
    from vgi_lint_check.snapshot import take_snapshot
    from vgi_lint_check.versions import discover_catalogs

    con, _ = connect_loaded(spatial=True)
    try:
        name = discover_catalogs(con, VOLCANOS_LOC)[0].catalog
        with attached(con, VOLCANOS_LOC, name, derive_alias(name)):
            snap = take_snapshot(con)
    finally:
        con.close()
    table_cols = set(snap.tables[0]) if snap.tables else set()
    assert {"database_name", "schema_name", "table_name", "comment", "tags"} <= table_cols
    fn_cols = set(snap.functions[0]) if snap.functions else set()
    assert {"function_type", "description", "parameters"} <= fn_cols


def test_versioned_worker_all_versions():
    _need(VGI_PYTHON)
    report = lint_worker(VERSIONED_LOC, all_versions=True, config=Config())
    versions = [r.data_version for r in report.results]
    assert set(versions) >= {"1.0.0", "1.1.0", "2.0.0", "3.0.0"}
    assert report.comparison is not None
    # metadata differs across versions: 1.1.0 adds the 'color' column to animals,
    # so the set of column objects is not identical between adjacent versions.
    assert any(row.added_objects or row.removed_objects for row in report.comparison.rows)


def test_unqualified_examples_flagged_and_fail_execution():
    _need(VOLCANOS)
    cfg = Config(execute=True, execute_mode="explain")
    report = lint_worker(VOLCANOS_LOC, config=cfg, spatial=True)
    findings = report.results[0].findings
    # volcanos example queries use bare table names (not catalog-qualified), so
    # the static qualification rule flags them...
    assert any(f.code == "VGI505" for f in findings)
    # ...and executing them as-written fails to bind for the same reason.
    assert any(f.code == "VGI901" for f in findings)
