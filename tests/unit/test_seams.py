"""Unit coverage for I/O seams and the parsing heuristics flagged in review."""

import pytest

from vgi_lint_check.connection import (
    _explain_attach_failure,
    attach_statement,
    derive_alias,
    sql_str,
)
from vgi_lint_check.rules._util import QueryTimeout, is_filter_policy_error, run_with_timeout
from vgi_lint_check.rules.constraints import _check_expression
from vgi_lint_check.rules.examples import _references_catalog, _references_identifier
from vgi_lint_check.rules.functions import _is_unnamed


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("un-filtered scans are rejected by the VGI optimizer extension", True),
        ("All tables REQUIRE WHERE filters on bbox.xmin", True),
        ("a WHERE clause is required for this table", True),
        ("rejected to prevent full-bucket reads", True),
        ('Binder Error: referenced column "foo" not found', False),
        ("Parser Error: syntax error at or near", False),
        ("Catalog Error: Table with name x does not exist", False),
    ],
)
def test_is_filter_policy_error(msg, expected):
    assert is_filter_policy_error(Exception(msg)) is expected


def test_run_with_timeout_returns_value():
    assert run_with_timeout(object(), lambda: 42, 5.0) == 42


def test_run_with_timeout_propagates_error():
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        run_with_timeout(object(), boom, 5.0)


def test_run_with_timeout_cancels_slow_query():
    import time

    interrupted = []

    class SlowCon:
        def execute(self, *a):
            time.sleep(0.4)

        def interrupt(self):
            interrupted.append(True)

    con = SlowCon()
    with pytest.raises(QueryTimeout):
        run_with_timeout(con, lambda: con.execute("SELECT 1"), 0.05)
    assert interrupted  # con.interrupt() was called to cancel


def test_run_with_timeout_disabled_when_zero():
    assert run_with_timeout(object(), lambda: "ok", 0) == "ok"


# --- connection -----------------------------------------------------------
def test_sql_str_and_attach_statement_quoting():
    assert sql_str("a'b") == "'a''b'"
    stmt = attach_statement("uv run w.py --x='y'", "volcanos", "v", "1.0.0")
    assert "AS v " in stmt
    assert "''y''" in stmt  # embedded quote doubled, not broken out
    assert stmt.endswith("data_version_spec '1.0.0')")


def test_attach_statement_no_data_version():
    stmt = attach_statement("http://h", "c", "v", None)
    assert "data_version_spec" not in stmt


def test_derive_alias_sanitizes():
    assert derive_alias("my-worker.v2") == "my_worker_v2"
    assert derive_alias("1bad")[0].isalpha()


@pytest.mark.parametrize(
    "msg,needle",
    [
        ("VGI HTTP authentication required (401)", "authentication"),
        ("Unsupported data_version_spec '9.9.9'", "data version"),
        ("Connection refused", "reach"),
    ],
)
def test_explain_attach_failure_maps_messages(msg, needle):
    out = _explain_attach_failure("http://h", "9.9.9", Exception(msg))
    assert needle in out.lower()


# --- CHECK-expression stripping (the review's must-fix #1) -----------------
def test_check_expression_strips_wrapper():
    assert _check_expression("CHECK((sig >= 0))") == "(sig >= 0)"
    assert _check_expression("CHECK(a > 0)") == "a > 0"


def test_check_expression_balances_parens():
    assert _check_expression("CHECK((a) AND (b))") == "(a) AND (b)"


def test_check_expression_leaves_checksum_alone():
    # must NOT be mis-stripped just because it starts with "CHECK"
    assert _check_expression("CHECKSUM(x) > 0") == "CHECKSUM(x) > 0"


def test_check_expression_passthrough_for_unwrapped():
    assert _check_expression("sig >= 0") == "sig >= 0"


# --- VGI505 qualification heuristic ---------------------------------------
def test_references_catalog_word_boundary():
    assert _references_catalog("SELECT * FROM volcanos.main.t", "volcanos")
    # qualifier as a substring of another identifier must NOT count
    assert not _references_catalog("SELECT * FROM lev.x", "v")
    # bare table name is not catalog-qualified
    assert not _references_catalog("SELECT * FROM eruptions", "volcanos")


def test_references_catalog_ignores_string_literals():
    # 'volcanos.' only appears inside a string literal -> not a real reference
    assert not _references_catalog("SELECT 'volcanos.x' FROM eruptions", "volcanos")


# --- VGI504 example-references-object matcher ------------------------------
def test_references_identifier_whole_token():
    assert _references_identifier("SELECT * FROM v.main.felt", "felt")
    assert _references_identifier("SELECT v.main.felt(1)", "felt")  # call form
    # substring of a larger identifier is NOT a use
    assert not _references_identifier("SELECT * FROM v.main.unfelt", "felt")
    assert not _references_identifier("SELECT felt_at FROM t", "felt")


def test_references_identifier_ignores_literals_and_comments():
    assert not _references_identifier("SELECT 'felt' FROM t  -- felt", "felt")


# --- VGI305 unnamed-arg detection -----------------------------------------
@pytest.mark.parametrize(
    "name,unnamed",
    [
        ("col0", True),
        ("col12", True),
        ("0", True),
        ("", True),
        ("intensity", False),
        ("vei", False),
        ("column_name", False),
    ],
)
def test_is_unnamed(name, unnamed):
    assert _is_unnamed(name) is unnamed
