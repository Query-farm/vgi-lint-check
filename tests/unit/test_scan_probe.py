"""Tests for the live-scan probe rules (VGI911 responsiveness, VGI912 batch shape).

The probe reads the vgi extension's ``Batches`` / ``Batch Bytes`` ``extra_info``
out of ``get_profiling_information()``, so the fakes here return profiling trees
in the shape the extension actually emits.
"""

import threading

import pytest

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.connection import close_quietly
from vgi_lint_check.rules._util import QueryTimeout, map_isolated_queries
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.execution import (
    ScanBatchShape,
    ScanResponds,
    _fmt_bytes,
    _parse_bytes,
    _scan_shapes,
    _shape_from_extra,
)


def _profile(*scans):
    """A profiling tree whose leaves are VGI TABLE_SCAN nodes."""
    children = [
        {
            "operator_type": "TABLE_SCAN",
            "extra_info": {"Function": name, "Batches": batches, **extra},
            "children": [],
        }
        for name, batches, extra in scans
    ]
    return {"operator_type": "QUERY_ROOT", "children": children}


class FakeCursor:
    """Answers the probe's `SET enable_profiling` / scan / fetchall sequence."""

    def __init__(self, profile=None, *, error=None, hang=False):
        self.profile = profile if profile is not None else _profile()
        self.error = error
        self.hang = hang
        self.closed = False
        self.sql_seen: list[str] = []

    def execute(self, sql, params=None):
        self.sql_seen.append(sql)
        if sql.startswith("SET "):
            return self
        if self.hang:
            raise QueryTimeout("query exceeded 10s and was cancelled")
        if self.error is not None:
            raise self.error
        return self

    def fetchall(self):
        return [(1,)]

    def get_profiling_information(self):
        return self.profile

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.cursors_made = 0

    def cursor(self):
        self.cursors_made += 1
        return self._cursor


def _cat():
    return F.catalog(F.schema("main", comment="a schema comment", tables=[F.table("main", "t")]))


def _run(rule, cursor, **cfg_kw):
    cfg = Config(execute=True, execute_concurrency=1, **cfg_kw)
    ctx = RuleContext(_cat(), cfg, connection=FakeConn(cursor))
    ctx.severity = rule.default_severity
    return list(rule.check(ctx))


# --- parsing ----------------------------------------------------------------
def test_parse_batches_string():
    shape = _shape_from_extra(
        {
            "Function": "streamed",
            "Batches": "4 (rows: min 1000, avg 2500, max 3000)",
            "Batch Bytes": "78.1 KiB",
        }
    )
    assert (shape.function, shape.batches) == ("streamed", 4)
    assert (shape.rows_min, shape.rows_avg, shape.rows_max) == (1000, 2500, 3000)
    assert shape.bytes_total == 79975  # exact minimum for "78.1 KiB"
    assert shape.avg_bytes == 79975 // 4


def test_non_vgi_scan_has_no_shape():
    """A RANGE scan carries no `Batches` key — the rule must skip, not crash."""
    assert _shape_from_extra({"Function": "RANGE"}) is None
    assert _shape_from_extra({"Batches": "not a batch string"}) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Values below are the exact minima, cross-checked against DuckDB's own
        # format_bytes() (the same StringUtil::BytesToHumanReadableString).
        ("390.6 KiB", 399975),
        ("78.1 KiB", 79975),
        ("1.0 MiB", 1024**2),
        ("1.5 MiB", 1572864),
        ("1.0 KiB", 1024),
        ("12 bytes", 12),
        ("0 bytes", 0),
        ("1 byte", 1),  # the formatter's singular case
        ("2.0 PiB", 2 * 1024**5),  # the largest unit it can emit
        # Not producible by the formatter -> no byte finding, rather than a guess.
        ("1.5 bytes", None),
        ("1.23 KiB", None),  # only ever one fractional digit
        ("5 KiB", None),  # the fraction is never omitted above 1024
        ("1.0 ZiB", None),  # unknown unit
        ("nonsense", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_bytes(text, expected):
    assert _parse_bytes(text) == expected


def test_parse_bytes_never_over_reports():
    """A `>` threshold must never fire on a scan that is actually under it.

    The formatter truncates, so each string names an interval of byte counts;
    _parse_bytes returns its lower bound.
    """
    # 390.6 KiB is produced by every count in [399975, 400076].
    low = _parse_bytes("390.6 KiB")
    assert low == 399975
    assert low <= 400000  # the real value that produced the sample string


@pytest.mark.parametrize(
    ("n", "expected"),
    [(512, "512.0 B"), (1024, "1.0 KiB"), (64 * 1024**2, "64.0 MiB"), (3 * 1024**3, "3.0 GiB")],
)
def test_fmt_bytes(n, expected):
    assert _fmt_bytes(n) == expected


def test_scan_shapes_walks_nested_tree_and_ignores_junk():
    prof = _profile(
        ("a", "1 (rows: min 5, avg 5, max 5)", {}),
        ("RANGE", "", {}),
        ("b", "2 (rows: min 1, avg 3, max 5)", {}),
    )
    assert [s.function for s in _scan_shapes(FakeCursor(prof))] == ["a", "b"]


def test_scan_shapes_tolerates_bad_profiling():
    class Bad(FakeCursor):
        def get_profiling_information(self):
            raise RuntimeError("profiling disabled")

    assert _scan_shapes(Bad()) == ()
    assert _scan_shapes(FakeCursor("{not json")) == ()


# --- VGI911 responsiveness ---------------------------------------------------
def test_hanging_scan_is_reported():
    findings = _run(ScanResponds(), FakeCursor(hang=True))
    assert [f.code for f in findings] == ["VGI911"]
    assert "did not return within" in findings[0].message


def test_healthy_scan_is_silent():
    prof = _profile(("t", "3 (rows: min 1000, avg 1000, max 1000)", {}))
    assert _run(ScanResponds(), FakeCursor(prof)) == []


def test_bind_error_is_left_to_vgi901():
    err = RuntimeError("Binder Error: no function matches")
    assert _run(ScanResponds(), FakeCursor(error=err)) == []


def test_mandatory_filter_policy_is_not_a_failure():
    err = RuntimeError("Invalid Error: a WHERE filter is required for this scan")
    assert _run(ScanResponds(), FakeCursor(error=err)) == []


def test_runtime_scan_error_is_reported():
    findings = _run(ScanResponds(), FakeCursor(error=RuntimeError("worker exploded")))
    assert [f.code for f in findings] == ["VGI911"]
    assert "worker exploded" in findings[0].message


def test_probe_uses_the_configured_limit():
    cur = FakeCursor()
    _run(ScanResponds(), cur, scan_limit=25)
    assert any("LIMIT 25" in s for s in cur.sql_seen)


# --- VGI912 batch shape ------------------------------------------------------
def test_single_oversized_batch_is_flagged():
    prof = _profile(("t", "1 (rows: min 200000, avg 200000, max 200000)", {}))
    findings = _run(ScanBatchShape(), FakeCursor(prof))
    assert [f.code for f in findings] == ["VGI912"]
    assert "one batch of 200000 rows" in findings[0].message


def test_single_small_batch_is_fine():
    """A 300-row table returning one batch is healthy, not a defect."""
    prof = _profile(("t", "1 (rows: min 300, avg 300, max 300)", {}))
    assert _run(ScanBatchShape(), FakeCursor(prof)) == []


def test_many_bounded_batches_are_fine():
    prof = _profile(("t", "200 (rows: min 1000, avg 1000, max 1000)", {}))
    assert _run(ScanBatchShape(), FakeCursor(prof)) == []


def test_high_average_batch_rows_flagged_even_when_multi_batch():
    prof = _profile(("t", "3 (rows: min 400000, avg 400000, max 400000)", {}))
    findings = _run(ScanBatchShape(), FakeCursor(prof))
    assert "mean batch is 400000 rows" in findings[0].message


def test_oversized_mean_batch_bytes_flagged():
    prof = _profile(
        ("t", "2 (rows: min 10, avg 10, max 10)", {"Batch Bytes": "600.0 MiB"}),
    )
    findings = _run(ScanBatchShape(), FakeCursor(prof))
    assert "mean batch is 300.0 MiB" in findings[0].message


def test_batch_thresholds_are_configurable():
    prof = _profile(("t", "1 (rows: min 5000, avg 5000, max 5000)", {}))
    assert _run(ScanBatchShape(), FakeCursor(prof)) == []
    findings = _run(ScanBatchShape(), FakeCursor(prof), single_batch_max_rows=1000)
    assert [f.code for f in findings] == ["VGI912"]


def test_probe_is_shared_between_the_two_rules():
    """Both rules read one memoized probe — the worker is scanned once."""
    prof = _profile(("t", "1 (rows: min 200000, avg 200000, max 200000)", {}))
    cur = FakeCursor(prof)
    conn = FakeConn(cur)
    cfg = Config(execute=True, execute_concurrency=1)
    ctx = RuleContext(_cat(), cfg, connection=conn)
    ctx.severity = ScanResponds.default_severity
    assert list(ScanResponds().check(ctx)) == []
    scans = [s for s in cur.sql_seen if s.startswith("SELECT")]
    assert list(ScanBatchShape().check(ctx))  # second rule still sees the shape
    assert [s for s in cur.sql_seen if s.startswith("SELECT")] == scans  # no re-scan


# --- cursor isolation (the load-bearing safety property) ---------------------
def test_isolated_queries_use_a_fresh_cursor_per_item_never_the_connection():
    made = []

    class Conn:
        def cursor(self):
            cur = FakeCursor()
            made.append(cur)
            return cur

        def execute(self, sql, params=None):  # pragma: no cover - must never be called
            raise AssertionError("probe ran on the parent connection")

    conn = Conn()
    out = map_isolated_queries(conn, [1, 2, 3], lambda it, cur: (it, cur), 1)
    assert [it for it, _ in out] == [1, 2, 3]
    assert len({id(c) for _, c in out}) == 3
    assert all(c.closed for c in made)


def test_wedged_cursor_is_abandoned_not_closed():
    """close() on a cursor stuck in an uncancellable scan would block forever."""
    cur = FakeCursor(hang=True)

    class Conn:
        def cursor(self):
            return cur

    def work(_it, c):
        raise QueryTimeout("wedged")

    with pytest.raises(QueryTimeout):
        map_isolated_queries(Conn(), [1], work, 1)
    assert not cur.closed


def test_swallowed_timeout_still_abandons_the_cursor():
    """The probe converts QueryTimeout into a result, so it must flag `wedged`."""
    cur = FakeCursor(hang=True)

    class Conn:
        def cursor(self):
            return cur

    # fn returns normally (timeout captured in the result), as _run_probe does.
    map_isolated_queries(
        Conn(), [1], lambda _it, _c: {"timed_out": True}, 1, wedged=lambda r: r["timed_out"]
    )
    assert not cur.closed, "close() would block on the wedged query"


def test_engine_error_leaves_the_cursor_reusable_and_closed():
    cur = FakeCursor()

    class Conn:
        def cursor(self):
            return cur

    def work(_it, _c):
        raise RuntimeError("bind error")

    with pytest.raises(RuntimeError):
        map_isolated_queries(Conn(), [1], work, 1)
    assert cur.closed


def test_hanging_probe_does_not_close_its_cursor_end_to_end():
    """VGI911's real path: timeout -> finding, and the cursor is never closed."""
    cur = FakeCursor(hang=True)
    findings = _run(ScanResponds(), cur)
    assert [f.code for f in findings] == ["VGI911"]
    assert not cur.closed


# --- bounded teardown --------------------------------------------------------
def test_close_quietly_closes_a_healthy_connection():
    class Conn:
        closed = False

        def close(self):
            self.closed = True

    con = Conn()
    assert close_quietly(con, timeout=5) is True
    assert con.closed


def test_close_quietly_abandons_a_wedged_connection():
    """close() blocks on an abandoned cursor's uncancellable scan — don't wait."""
    release = threading.Event()

    class WedgedConn:
        def close(self):
            release.wait(30)  # simulates close() blocking on the stuck query

    try:
        assert close_quietly(WedgedConn(), timeout=0.2) is False
    finally:
        release.set()


def test_close_quietly_swallows_close_errors():
    class Boom:
        def close(self):
            raise RuntimeError("already closed")

    assert close_quietly(Boom(), timeout=5) is True
