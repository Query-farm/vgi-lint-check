from vgi_lint_check.connection import attach_statement, sql_str, validate_alias
from vgi_lint_check.diff import diff_snapshots
from vgi_lint_check.snapshot import Snapshot
from vgi_lint_check.versions import _parse_releases, resolve_versions

import pytest


def test_sql_str_escapes_quotes():
    assert sql_str("a'b") == "'a''b'"
    assert "''" in sql_str("uv run x.py --flag='y'")


def test_attach_statement_injection_safe():
    stmt = attach_statement("http://h/?a='b'", "volcanos", "v", "1.0.0")
    assert stmt.count("data_version_spec '1.0.0'") == 1
    assert "AS v " in stmt
    assert "'volcanos'" in stmt
    assert "''b''" in stmt  # the embedded quote was doubled, not broken out


def test_validate_alias():
    assert validate_alias("v_1") == "v_1"
    with pytest.raises(ValueError):
        validate_alias("1bad")
    with pytest.raises(ValueError):
        validate_alias("bad-alias")


def test_diff_scopes_settings_and_pragmas():
    before = Snapshot(
        settings=[{"name": "vgi_async_prefetch", "description": "ext setting"}],
        functions=[{"schema_name": "main", "function_name": "p0", "function_type": "pragma"}],
    )
    after = Snapshot(
        settings=[
            {"name": "vgi_async_prefetch", "description": "ext setting"},
            {"name": "worker_opt", "description": "worker setting"},
        ],
        functions=[
            {"schema_name": "main", "function_name": "p0", "function_type": "pragma"},
            {"schema_name": "main", "function_name": "worker_pragma", "function_type": "pragma"},
        ],
    )
    d = diff_snapshots(before, after, "v")
    assert [r["name"] for r in d.setting_rows] == ["worker_opt"]
    assert [r["function_name"] for r in d.pragma_rows] == ["worker_pragma"]


def test_diff_summary_counts_added_tables():
    before = Snapshot(tables=[])
    after = Snapshot(tables=[
        {"database_name": "v", "schema_name": "main", "table_name": "a"},
        {"database_name": "other", "schema_name": "main", "table_name": "z"},
    ])
    d = diff_snapshots(before, after, "v")
    assert d.summary["tables"] == 1  # 'other' catalog excluded


def test_parse_releases_json_string():
    raw = '[{"version":"2.0.0","summary":"x"},{"version":"1.0.0"}]'
    rels = _parse_releases(raw)
    assert [r.version for r in rels] == ["2.0.0", "1.0.0"]
    assert _parse_releases("[]") == []
    assert _parse_releases("not json") == []


class _FakeCon:
    def __init__(self, rows, cols):
        self._rows, self._cols = rows, cols

    def execute(self, sql, params=None):
        self.description = [(c,) for c in self._cols]
        self._buf = self._rows
        return self

    def fetchall(self):
        return self._buf


def test_resolve_versions_explicit_wins():
    assert resolve_versions(None, "loc", explicit=["1.0.0", "2.0.0"]) == ["1.0.0", "2.0.0"]


def test_resolve_versions_default_is_none():
    assert resolve_versions(None, "loc") == [None]


def test_resolve_versions_all_from_discovery():
    con = _FakeCon(
        rows=[("c", "", "", "[]", '[{"version":"2.0.0"},{"version":"1.0.0"}]', None)],
        cols=["catalog", "implementation_version", "data_version_spec", "attach_options", "releases", "source_url"],
    )
    assert resolve_versions(con, "loc", all_versions=True) == ["2.0.0", "1.0.0"]
