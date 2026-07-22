"""Unit coverage for I/O seams and the parsing heuristics flagged in review."""

import itertools
import threading

import pytest

from vgi_lint_check.config import Config
from vgi_lint_check.connection import (
    _explain_attach_failure,
    attach_statement,
    connect_loaded,
    derive_alias,
    is_subprocess_location,
    sql_str,
)
from vgi_lint_check.core import _maybe_warn_relaunch, _RelaunchMeter
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


# --- subprocess-pool keepalive + relaunch warning -------------------------
@pytest.mark.parametrize(
    "location,expected",
    [
        ("uv run worker.py", True),
        ("/opt/bin/worker", True),
        ("http://host:8080/", False),
        ("https://host/", False),
        ("unix:///run/w.sock", False),
        ("launch:uv run worker.py", False),
        ("  HTTP://Host/  ", False),  # trimmed + case-insensitive
    ],
)
def test_is_subprocess_location(location, expected):
    assert is_subprocess_location(location) is expected


class _RecordingCon:
    """Fake haybarn connection that records executed SQL and can fail chosen statements."""

    def __init__(self, fail_on=()):
        self.executed = []
        self._fail_on = tuple(fail_on)
        self.closed = False

    def execute(self, sql, *_a):
        self.executed.append(sql)
        if any(needle in sql for needle in self._fail_on):
            raise RuntimeError(f"boom: {sql}")
        return self

    def fetchone(self):
        return ("1.2.3",)

    def close(self):
        self.closed = True


def _patch_haybarn(monkeypatch, con):
    import sys
    import types

    fake = types.ModuleType("haybarn")
    fake.connect = lambda: con
    monkeypatch.setitem(sys.modules, "haybarn", fake)


def test_connect_loaded_raises_pool_idle_timeout(monkeypatch):
    con = _RecordingCon()
    _patch_haybarn(monkeypatch, con)
    out, _version = connect_loaded(install=False, spatial=False, worker_idle_timeout=300)
    assert out is con
    assert "SET vgi_worker_pool_idle_limit_seconds = 300" in con.executed
    # ...and it is issued after LOAD vgi (the extension snapshots it at ATTACH time).
    assert con.executed.index("LOAD vgi") < con.executed.index(
        "SET vgi_worker_pool_idle_limit_seconds = 300"
    )


def test_connect_loaded_swallows_unknown_pool_setting(monkeypatch):
    # An older extension without the setting must not fail the connection.
    con = _RecordingCon(fail_on=("vgi_worker_pool_idle_limit_seconds",))
    _patch_haybarn(monkeypatch, con)
    out, _version = connect_loaded(install=False, spatial=False, worker_idle_timeout=300)
    assert out is con and not con.closed


def test_connect_loaded_skips_keepalive_when_zero(monkeypatch):
    con = _RecordingCon()
    _patch_haybarn(monkeypatch, con)
    connect_loaded(install=False, spatial=False, worker_idle_timeout=0)
    assert not any("vgi_worker_pool_idle_limit_seconds" in s for s in con.executed)


def test_relaunch_meter_accumulates():
    meter = _RelaunchMeter()
    with meter.spawn():
        pass
    with meter.spawn():
        pass
    assert meter.count == 2
    assert meter.seconds >= 0.0


def test_maybe_warn_relaunch_fires_for_slow_subprocess(capsys):
    meter = _RelaunchMeter(count=3, seconds=9.0)
    _maybe_warn_relaunch("uv run worker.py", meter, Config(relaunch_warn_seconds=6.0))
    err = capsys.readouterr().err
    assert "launching the subprocess worker" in err
    assert "launch:" in err  # steers toward a persistent transport


@pytest.mark.parametrize(
    "location,meter,cfg",
    [
        # persistent transport: nothing to relaunch
        ("http://host/", _RelaunchMeter(count=3, seconds=9.0), Config()),
        # under threshold
        ("uv run w.py", _RelaunchMeter(count=3, seconds=1.0), Config(relaunch_warn_seconds=6.0)),
        # only one launch
        ("uv run w.py", _RelaunchMeter(count=1, seconds=9.0), Config(relaunch_warn_seconds=6.0)),
        # disabled
        ("uv run w.py", _RelaunchMeter(count=3, seconds=9.0), Config(relaunch_warn_seconds=0)),
    ],
)
def test_maybe_warn_relaunch_stays_silent(location, meter, cfg, capsys):
    _maybe_warn_relaunch(location, meter, cfg)
    assert capsys.readouterr().err == ""


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


def test_map_queries_sequential_and_parallel():
    from vgi_lint_check.rules._util import map_queries

    # Cursors carry a serial rather than being compared by id(): a short-lived
    # cursor can be collected and its address reused, so id() is not a stable
    # identity to assert on.
    class FakeCon:
        _next = itertools.count()

        def __init__(self):
            self.serial = next(FakeCon._next)

        def cursor(self):
            return FakeCon()

    con = FakeCon()
    items = list(range(10))
    # order preserved + correct results, both sequential and parallel
    assert map_queries(con, items, lambda i, cur: i * i, 1) == [i * i for i in items]
    assert map_queries(con, items, lambda i, cur: i * i, 4) == [i * i for i in items]

    # Each parallel worker gets its own cursor, and never the main connection.
    # A barrier forces all four tasks to be in flight at once — without it the
    # pool can hand every (trivially fast) task to one thread, which would make
    # "more than one cursor was created" a race rather than an assertion.
    gate = threading.Barrier(4, timeout=5)

    def probe(_i, cur):
        gate.wait()
        return cur.serial

    seen = map_queries(con, list(range(4)), probe, 4)
    assert len(set(seen)) == 4
    assert con.serial not in seen
