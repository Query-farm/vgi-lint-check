"""Small shared helpers for rules."""

from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def map_queries(
    con: Any, items: Iterable[Any], fn: Callable[[Any, Any], Any], concurrency: int
) -> list[Any]:
    """Run ``fn(item, cur)`` for each item, parallel across ``concurrency`` cursors.

    Each worker thread gets its own ``con.cursor()`` — a lightweight connection
    sharing the attached catalog — so the VGI worker pool serves the concurrent
    queries from distinct workers. With ``concurrency <= 1`` (or a single item)
    it runs sequentially on ``con``. Result order matches input order.
    """
    work = list(items)
    if concurrency <= 1 or len(work) <= 1:
        return [fn(it, con) for it in work]
    local = threading.local()

    def run(it: Any) -> Any:
        cur = getattr(local, "cur", None)
        if cur is None:
            cur = local.cur = con.cursor()
        return fn(it, cur)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        return list(ex.map(run, work))


def map_isolated_queries(
    con: Any,
    items: Iterable[Any],
    fn: Callable[[Any, Any], Any],
    concurrency: int,
    *,
    wedged: Callable[[Any], bool] | None = None,
) -> list[Any]:
    """Like :func:`map_queries`, but a *fresh, disposable* cursor per item.

    For probes that may wedge. ``con.interrupt()`` only takes effect between a
    scan's chunk emissions, so a worker that blocks inside its first batch cannot
    be cancelled: the cursor running it is unusable forever. Reusing a cursor
    across items — or running on ``con`` itself, as :func:`map_queries` does when
    ``concurrency <= 1`` — would spread that wedge to every later query in the run.

    One cursor per item confines it. A cursor is *abandoned* rather than closed
    when ``fn`` raises :class:`QueryTimeout`, or when ``wedged(result)`` says so —
    ``close()`` blocks on the stuck query, so a caller that swallows the timeout
    into a result object must report it back through ``wedged``. The leaked cursor
    is reaped at process exit. The pool thread is not held, because
    ``run_with_timeout`` runs the query on its own daemon thread.
    """
    work = list(items)
    if not work:
        return []

    def run(it: Any) -> Any:
        cur = con.cursor()
        stuck = True  # assume the worst until we know the cursor is safe to close
        try:
            result = fn(it, cur)
            stuck = bool(wedged(result)) if wedged else False
            return result
        except QueryTimeout:
            raise
        except Exception:
            stuck = False  # a raised engine error left the cursor usable
            raise
        finally:
            if not stuck:
                with contextlib.suppress(Exception):
                    cur.close()

    if concurrency <= 1 or len(work) <= 1:
        return [run(it) for it in work]
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        return list(ex.map(run, work))


def blank(s: Any) -> bool:
    """True when ``s`` is None, empty, or only whitespace."""
    return not (s and str(s).strip())


def _normalize(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def base_type(sql_type: str | None) -> str:
    """Leading type identifier, lowercased (``DECIMAL(18,4)``/``BIGINT[]`` -> ``decimal``/``bigint``)."""  # noqa: E501
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", sql_type or "")
    return m.group(1).lower() if m else ""


def is_trivial_echo(comment: str | None, name: str | None) -> bool:
    """True when a comment merely restates the object's name."""
    if blank(comment) or blank(name):
        return False
    return _normalize(comment) == _normalize(name)


# A worker may reject an unfiltered scan at bind time (mandatory WHERE filters,
# e.g. bbox predicates) to avoid full-bucket reads. That is a deliberate policy,
# not a broken object — so generated bare-scan probes (view/CHECK execution)
# must treat it as a pass, not a failure.
_FILTER_POLICY = re.compile(
    r"un-?filtered"
    r"|requires?\s+(a\s+)?(where|filter|predicate)"
    r"|(where|filter|predicate)s?\s+(clause\s+)?(are\s+|is\s+)?required"
    r"|full[-\s]?(bucket|table)\s+(read|scan)"
    r"|must\s+(have|include|specify).{0,40}(filter|where|predicate|bbox)",
    re.IGNORECASE,
)


def is_filter_policy_error(error: object) -> bool:
    """True when an exception/message indicates a mandatory-filter rejection.

    Used to keep generated bare-scan probes (e.g. ``EXPLAIN SELECT * FROM view``)
    from false-failing on workers that require WHERE predicates by policy.
    """
    return bool(_FILTER_POLICY.search(str(error)))


# A backend-dependent worker scanned with no credentials refuses at bind time —
# "attach an 'azure_graph' secret (TYPE azure_graph)", "no AWS credentials", and
# so on. That is the *correct* behaviour, and specifically the good version of
# it: a prompt, explicit refusal naming what is missing.
#
# The scan-responsiveness rule (VGI911) exists because a worker blocked inside
# its first batch cannot be interrupted and wedges its client forever. A fast
# credential refusal is the opposite of that failure, so scoring it as an error
# says something untrue about the worker — and permanently caps every
# credentialed connector below the level its metadata has actually earned.
_CREDENTIAL_REQUIRED = re.compile(
    r"attach\s+an?\s+'?[\w.-]+'?\s+secret"
    r"|\b(secret|credential|api[\s_-]?key|token|auth(entication|orization)?)\b"
    r"[^.]{0,60}\b(required|missing|not\s+(set|found|configured|provided)|must\s+be)"
    r"|\b(no|missing)\b[^.]{0,30}\b(credentials?|secret|api[\s_-]?key)\b"
    r"|CREATE\s+SECRET"
    r"|\b(unauthenti|unauthori)\w*\b"
    r"|\b(401|403)\b[^.]{0,30}\b(unauthori|forbidden)",
    re.IGNORECASE,
)


def is_credential_error(error: object) -> bool:
    """True when a failure is a worker refusing because no credential was supplied.

    Deliberately narrow: it matches an explicit statement that a secret/credential
    is missing, not any authentication-adjacent word. A worker that fails *because
    its credential is wrong* still surfaces as a real failure.
    """
    return bool(_CREDENTIAL_REQUIRED.search(str(error)))


# DuckDB's structural (bind-time) error classes — the SQL is wrong against the
# catalog (unknown table/column/function, type/arg mismatch, syntax), as opposed
# to a runtime/data failure. These are real authoring bugs, not "needs data".
_BIND_ERROR = re.compile(
    r"\b(binder|parser|catalog|binding)\s*(error|exception)\b"
    r"|syntax error|does not exist|not found in|no function matches"
    r"|referenced (column|table)",
    re.IGNORECASE,
)


def is_bind_error(error: object) -> bool:
    """True when an exception indicates a bind/parse/catalog (structural) failure."""
    return bool(_BIND_ERROR.search(str(error)))


# Strip string literals and -- / /* */ comments so a keyword inside one doesn't
# fool the leading-verb check.
_SQL_LITERAL_OR_COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)
# Statement verbs allowed in the agent simulation. The worker catalog is
# read-only and the local DuckDB is disposable, so this is "no side effects
# outside the session" — analysts may build session-local state (SET, TEMP
# objects), but not escape (ATTACH/INSTALL/COPY ... TO) or mutate persistently.
_SAFE_LEADING = frozenset(
    {
        "select",
        "with",
        "explain",
        "describe",
        "desc",
        "show",
        "summarize",
        "values",
        "table",
        "from",  # DuckDB FROM-first syntax
        "pragma",
        "set",
        "reset",
        "use",
        "drop",  # only affects local temp / read-only worker (no-op there)
    }
)
_TEMP = re.compile(r"\btemp(orary)?\b", re.IGNORECASE)


def safe_session_sql(sql: str) -> bool:
    """True when ``sql`` is a single statement safe to run in the simulation.

    Allows read/session-local statements (SELECT/WITH/EXPLAIN, SET/PRAGMA, and
    ``CREATE ... TEMP`` objects); rejects multi-statement input and anything that
    escapes the disposable session — ATTACH/DETACH, INSTALL/LOAD, COPY ... TO,
    EXPORT/IMPORT, or persistent writes (INSERT/UPDATE/DELETE/ALTER/non-TEMP
    CREATE). Comments/string literals are stripped before inspection.
    """
    stripped = _SQL_LITERAL_OR_COMMENT.sub(" ", sql or "")
    # A single statement only (a trailing ';' is fine).
    if len([s for s in stripped.split(";") if s.strip()]) > 1:
        return False
    m = re.match(r"\s*([A-Za-z_]+)", stripped)
    if not m:
        return False
    verb = m.group(1).lower()
    if verb == "create":
        return bool(_TEMP.search(stripped))  # only CREATE ... TEMP ...
    return verb in _SAFE_LEADING


class QueryTimeout(Exception):
    """A worker query exceeded its execution-rule time budget and was cancelled."""


def run_with_timeout(con: Any, fn: Callable[..., Any], timeout: float) -> Any:
    """Run ``fn`` (which executes SQL on ``con``) under a wall-clock timeout.

    On timeout, the in-flight query is cancelled via ``con.interrupt()`` so an
    example query can never run forever, and :class:`QueryTimeout` is raised.
    A non-positive timeout disables the guard.
    """
    if not timeout or timeout <= 0:
        return fn()
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - relayed to the caller's thread
            box["error"] = e

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        with contextlib.suppress(Exception):
            con.interrupt()
        thread.join(5)
        raise QueryTimeout(f"query exceeded {timeout:g}s and was cancelled")
    if "error" in box:
        raise box["error"]
    return box.get("value")
