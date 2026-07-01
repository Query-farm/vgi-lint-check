"""Tests for user-supplied ATTACH options + pre-attach setup SQL.

Covers workers that require options/credentials to attach (e.g. a mail worker
that resolves credentials from a DuckDB SECRET): the linter can pass extra
ATTACH options and run setup SQL so the catalog metadata is introspectable.
"""

from __future__ import annotations

import pytest

from vgi_lint_check.cli import _apply_cli_overrides
from vgi_lint_check.config import Config, from_table
from vgi_lint_check.connection import (
    WorkerConnectionError,
    apply_setup_sql,
    attach_statement,
    render_attach_options,
)


def test_render_attach_options_quotes_strings_and_bares_literals():
    rendered = render_attach_options(
        {"provider": "imap", "secret": "lint", "port": "993", "use_ssl": "false"}
    )
    assert ", provider 'imap'" in rendered
    assert ", secret 'lint'" in rendered
    assert ", port 993" in rendered  # int literal → bare
    assert ", use_ssl false" in rendered  # bool literal → bare (lower-cased)


def test_render_attach_options_escapes_quotes():
    assert render_attach_options({"password": "a'b"}) == ", password 'a''b'"


def test_render_attach_options_empty():
    assert render_attach_options(None) == ""
    assert render_attach_options({}) == ""


def test_render_attach_options_rejects_bad_key():
    with pytest.raises(ValueError, match="invalid ATTACH option key"):
        render_attach_options({"bad key": "x"})
    with pytest.raises(ValueError, match="invalid ATTACH option key"):
        render_attach_options({"x); DROP": "1"})


def test_attach_statement_includes_options():
    stmt = attach_statement(
        "uv run w.py", "mail", "w", None, {"provider": "imap", "secret": "lint"}
    )
    assert stmt.startswith("ATTACH 'mail' AS w (TYPE vgi, LOCATION 'uv run w.py'")
    assert ", provider 'imap'" in stmt and ", secret 'lint'" in stmt
    assert stmt.endswith(")")


def test_attach_statement_no_options_unchanged():
    assert (
        attach_statement("loc", "mail", "w", None)
        == "ATTACH 'mail' AS w (TYPE vgi, LOCATION 'loc')"
    )


class _FakeCon:
    def __init__(self, fail_on: str | None = None):
        self.executed: list[str] = []
        self._fail_on = fail_on

    def execute(self, sql: str):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("boom")
        self.executed.append(sql)


def test_apply_setup_sql_runs_each():
    con = _FakeCon()
    apply_setup_sql(con, ["CREATE SECRET a (TYPE imap)", "CREATE SECRET b (TYPE gmail)"])
    assert con.executed == ["CREATE SECRET a (TYPE imap)", "CREATE SECRET b (TYPE gmail)"]


def test_apply_setup_sql_empty_is_noop():
    con = _FakeCon()
    apply_setup_sql(con, None)
    apply_setup_sql(con, ())
    assert con.executed == []


def test_apply_setup_sql_wraps_failure():
    con = _FakeCon(fail_on="BAD")
    with pytest.raises(WorkerConnectionError, match="setup SQL failed"):
        apply_setup_sql(con, ["BAD SQL"])


def test_from_table_parses_attach_options_and_setup_sql():
    cfg = from_table(
        {
            "attach_options": {"provider": "imap", "secret": "lint"},
            "setup_sql": ["CREATE SECRET lint (TYPE imap, HOST 'h', USERNAME 'u', PASSWORD 'p')"],
        }
    )
    assert cfg.attach_options == {"provider": "imap", "secret": "lint"}
    assert cfg.setup_sql == ["CREATE SECRET lint (TYPE imap, HOST 'h', USERNAME 'u', PASSWORD 'p')"]


def test_from_table_setup_sql_accepts_scalar():
    cfg = from_table({"setup_sql": "CREATE SECRET s (TYPE imap)"})
    assert cfg.setup_sql == ["CREATE SECRET s (TYPE imap)"]


def test_cli_overrides_merge_attach_options():
    cfg = Config()
    _apply_cli_overrides(
        cfg,
        select=None,
        extend_select=None,
        ignore=None,
        extend_ignore=None,
        categories=None,
        severities=(),
        execute=None,
        execute_mode=None,
        execute_limit=None,
        execute_concurrency=None,
        check_links=None,
        attach_options=("provider=imap", "secret=lint"),
        setup_sql=("CREATE SECRET lint (TYPE imap)",),
    )
    assert cfg.attach_options == {"provider": "imap", "secret": "lint"}
    assert cfg.setup_sql == ["CREATE SECRET lint (TYPE imap)"]


def test_cli_overrides_attach_option_requires_kv():
    import click

    with pytest.raises(click.UsageError, match="KEY=VALUE"):
        _apply_cli_overrides(
            cfg=Config(),
            select=None,
            extend_select=None,
            ignore=None,
            extend_ignore=None,
            categories=None,
            severities=(),
            execute=None,
            execute_mode=None,
            execute_limit=None,
            execute_concurrency=None,
            check_links=None,
            attach_options=("noequals",),
        )
