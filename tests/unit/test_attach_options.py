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
from vgi_lint_check.model import AttachOption, ObjectId, ObjectKind
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.execution import AdvertisedCatalogsAttachable

from .. import fixtures as F


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


# --- VGI905 vs. catalogs gated on a required attach option ----------------
def _opt(name, default=None):
    return AttachOption(
        id=ObjectId("v", ObjectKind.ATTACH_OPTION, name=name),
        name=name,
        type="VARCHAR",
        default=default,
    )


class _AttachCon:
    """Records ATTACH statements; refuses any catalog named in ``refuse``."""

    def __init__(self, refuse=()):
        self.attaches: list[str] = []
        self._refuse = tuple(refuse)

    def execute(self, sql: str):
        if sql.startswith("ATTACH"):
            self.attaches.append(sql)
            if any(f"ATTACH '{name}'" in sql for name in self._refuse):
                raise RuntimeError("MissingAttachOptionsError")
        return self


def _vgi905(cat, con, cfg=None):
    rule = AdvertisedCatalogsAttachable()
    ctx = RuleContext(cat, cfg or Config(execute=True), connection=con)
    return list(rule.check(ctx)), con


def test_vgi905_skips_a_sibling_gated_on_an_unsupplied_required_option():
    # 'gated' declares api_key with no default → required. We have no value for
    # it, so the attach would fail for a documented reason, not a worker defect.
    cat = F.catalog(
        advertised_catalogs=["v", "gated"],
        advertised_attach_options={"gated": [_opt("api_key")]},
    )
    findings, con = _vgi905(cat, _AttachCon(refuse=["gated"]))
    assert findings == []
    assert con.attaches == []  # never probed at all


def test_vgi905_supplies_a_configured_value_for_the_required_option():
    cat = F.catalog(
        advertised_catalogs=["v", "gated"],
        advertised_attach_options={"gated": [_opt("api_key")]},
    )
    cfg = Config(execute=True)
    cfg.attach_options = {"API_KEY": "sekret"}  # matched case-insensitively
    findings, con = _vgi905(cat, _AttachCon(), cfg)
    assert findings == []
    assert ", api_key 'sekret'" in con.attaches[0]


def test_vgi905_still_fires_when_a_satisfiable_sibling_refuses():
    # Every option has a default, so nothing is unmet — a refusal here is real.
    cat = F.catalog(
        advertised_catalogs=["v", "broken"],
        advertised_attach_options={"broken": [_opt("mode", default="fast")]},
    )
    findings, _ = _vgi905(cat, _AttachCon(refuse=["broken"]))
    assert [f.code for f in findings] == ["VGI905"]
    assert "cannot be attached" in findings[0].message


def test_vgi905_probes_siblings_with_no_declared_options():
    cat = F.catalog(advertised_catalogs=["v", "plain"])
    findings, con = _vgi905(cat, _AttachCon(refuse=["plain"]))
    assert [f.code for f in findings] == ["VGI905"]
    assert con.attaches and "ATTACH 'plain'" in con.attaches[0]


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
